"""Deterministic transfer/expedite candidates, always re-simulated against source data."""
import copy
from datetime import timedelta
from decimal import Decimal

from psycopg import sql

from .shortage import calculate, fingerprint, plain, timestamp

PLANNING_TABLES = ('transfer_routes', 'expedite_options', 'approval_policies')


def planning_snapshot(db, monitor, config):
    snapshot = monitor.snapshot(db, config)
    snapshot['localPolicies'] = monitor.data.all('SELECT action,max_quantity,max_cost,version FROM business_policies WHERE workspace_id=? ORDER BY action',(config['workspace_id'],)) if hasattr(monitor,'permissions') else []
    for table in PLANNING_TABLES:
        snapshot['tables'][table] = db.execute(sql.SQL(
            'SELECT * FROM supply_demo.{} WHERE scenario_id=%s AND ingested_at<=%s ORDER BY id'
        ).format(sql.Identifier(table)), (config['scenario_id'], snapshot['asOf'])).fetchall()
    return snapshot


def item_for(result, event):
    return next((r for r in result['results'] if r['factory'] == event['factory'] and
                 r['material'] == event['material'] and r['revision'] == event['revision']), None)


def simulate(snapshot, event, actions, days):
    """Transfers add a synthetic supply; expedites MOVE existing installments."""
    trial = copy.deepcopy(snapshot)
    tables = trial['tables']
    for index, action in enumerate(actions):
        q = Decimal(action['quantity'])
        at = timestamp(action['availableAt'])
        if action['kind'] == 'expedite':
            commitments = [c for c in tables['supplier_commitments'] if c['purchase_line_id'] == action['purchaseLineId']]
            latest = {}
            for c in commitments:
                if c['installment'] not in latest or c['version'] > latest[c['installment']]['version']:
                    latest[c['installment']] = c
            # The plan generator only offers unreceived/unshipped installments.
            remaining = q
            for c in sorted(latest.values(), key=lambda c: (c['available_at'], c['installment'])):
                moved = min(c['quantity'], remaining)
                if not moved:
                    continue
                original = c['quantity']
                tables['supplier_commitments'].append({**c, 'id': f"SIM-REM-{index}-{c['id']}",
                    'version': c['version'] + 1, 'quantity': original - moved})
                tables['supplier_commitments'].append({**c, 'id': f"SIM-EXP-{index}-{c['id']}",
                    'installment': f"SIM-{index}-{c['installment']}", 'version': 1, 'quantity': moved, 'available_at': at})
                remaining -= moved
            if remaining:
                return None
        else:
            identifier = f'SIM-TRANSFER-{index}'
            tables['purchase_order_lines'].append({'id': identifier, 'factory_id': event['factory'],
                'material_id': event['material'], 'revision_id': event['revision'], 'unit': action['unit'],
                'quantity': q, 'delivered_quantity': Decimal(0), 'cancelled_quantity': Decimal(0),
                'ingested_at': snapshot['asOf'], 'source_record_id': identifier})
            tables['supplier_commitments'].append({'id': identifier, 'purchase_line_id': identifier,
                'installment': '01', 'version': 1, 'quantity': q, 'available_at': at,
                'known_at': snapshot['asOf'], 'changed_at': snapshot['asOf'], 'ingested_at': snapshot['asOf']})
    return item_for(calculate(trial, days, event['factory'], event['material']), event)


def build_candidates(snapshot, event, days, factories):
    baseline = calculate(snapshot, days, event['factory'], event['material'])
    target = item_for(baseline, event)
    if not target or not target['computable'] or Decimal(target['shortage']) <= 0:
        return {'baseline': baseline, 'target': target, 'candidates': [],
                'missing': (target['missing'] if target else baseline['missing']) or ['当前窗口没有可处置的缺口。'],
                'snapshotHash': fingerprint(snapshot)}
    tables, now = snapshot['tables'], snapshot['asOf']
    need, gap = timestamp(target['firstShortageAt']), Decimal(target['shortage'])
    candidates, transfers, expedites = [], [], []

    def candidate(kind, actions, reasons, source_ids, valid_until, donor=None):
        result = simulate(snapshot, event, actions, days) if actions and not reasons else None
        remaining = Decimal(result['shortage']) if result and result['computable'] else gap
        covered = max(Decimal(0), gap - remaining)
        if result and not result['computable']:
            reasons.extend(result['missing'])
        if actions and covered == 0 and not reasons:
            reasons.append('到线时点或数量无法减少当前窗口缺口。')
        value = plain({'kind': kind, 'actions': actions, 'quantity': sum((Decimal(a['quantity']) for a in actions), Decimal(0)),
            'covered': covered, 'remainingShortage': remaining, 'complete': remaining == 0,
            'feasible': bool(actions) and not reasons and covered > 0, 'reasons': reasons,
            'totalCost': sum((Decimal(a['cost']) for a in actions), Decimal(0)), 'currency': 'CNY',
            'availableAt': max((timestamp(a['availableAt']) for a in actions), default=None),
            'validUntil': valid_until, 'sourceEvidence': source_ids, 'donor': donor,
            'snapshotHash': fingerprint(snapshot), 'ruleVersion': 'supply-planning-v1',
            'assumptions': ['到线承诺含收货、检验和配送。', '试算不预占库存、不创建业务单据。',
                            '加急报价按整份报价计费，不擅自按数量折价。']})
        candidates.append(value)
        return value

    for route in tables['transfer_routes']:
        if route['to_factory'] != event['factory']:
            continue
        reasons = []
        source = route['from_factory']
        at = now + timedelta(hours=route['transport_hours'] + route['handling_hours'])
        if source not in factories:
            # Do not expose donor quantities without permission.
            candidate('transfer', [], ['无来源工厂查询权限。'], [], route['cutoff_at'])
            continue
        donor_event = {**event, 'factory': source}
        donor = item_for(calculate(snapshot, 14, source, event['material']), donor_event)
        surplus = Decimal(0)
        if not donor or not donor['computable']:
            reasons.append('来源工厂数据不足，不能计算可调拨量。')
        else:
            surplus = max(Decimal(0), min([Decimal(donor['opening'])] + [Decimal(p['balance']) for p in donor['timeline']]))
        q = min(gap, surplus, route['capacity'])
        if q <= 0:
            reasons.append('来源工厂覆盖完整两周需求后没有可调拨库存。')
        if now > route['cutoff_at']:
            reasons.append('已超过线路发运截止时间。')
        if at > need:
            reasons.append('预计到线晚于首次缺料时间。')
        if route['currency'] != 'CNY':
            reasons.append('尚不支持该报价币种。')
        action = plain({'kind': 'transfer', 'routeId': route['id'], 'fromFactory': source,
            'factory': event['factory'], 'material': event['material'], 'revision': event['revision'], 'unit': target['unit'],
            'quantity': q, 'availableAt': at, 'cost': q * route['cost_per_unit']})
        value = candidate('transfer', [action] if q > 0 else [], reasons,
            [f"transfer_routes:{route['id']}"] + (donor['evidenceIds'] if donor else []),
            min(route['cutoff_at'], need), plain({'factory': source, 'horizonDays': 14, 'surplus': surplus,
                'remainingAfterTransfer': surplus - q}))
        if value['feasible']:
            transfers.append(value)

    purchases = {p['id']: p for p in tables['purchase_order_lines']}
    for option in tables['expedite_options']:
        p = purchases.get(option['purchase_line_id'])
        if not p or (p['factory_id'], p['material_id'], p['revision_id']) != (event['factory'], event['material'], event['revision']):
            continue
        reasons = []
        if p['unit'] != target['unit']:
            reasons.append('采购计量单位与物料基准单位不同，尚不能试算加急。')
        if p['delivered_quantity'] or p['cancelled_quantity'] or any(s['purchase_line_id'] == p['id'] for s in tables['shipment_lines']):
            reasons.append('该采购行已发运、收货或取消，需先明确剩余批次后再加急。')
        if option['available_at'] > need:
            reasons.append('加急到线晚于首次缺料时间。')
        if option['valid_until'] < now or option['available_at'] < max(now, option['ready_at']):
            reasons.append('加急报价已失效或时间条件不成立。')
        if option['currency'] != 'CNY':
            reasons.append('尚不支持该报价币种。')
        q = min(gap, option['available_quantity'], p['quantity'] - p['delivered_quantity'] - p['cancelled_quantity'])
        if q <= 0:
            reasons.append('报价可用数量或采购未交数量不足。')
        action = plain({'kind': 'expedite', 'optionId': option['id'], 'purchaseLineId': p['id'],
            'factory': event['factory'], 'material': event['material'], 'revision': event['revision'], 'unit': target['unit'],
            'quantity': q, 'availableAt': option['available_at'], 'cost': option['total_cost']})
        value = candidate('expedite', [action] if q > 0 else [], reasons,
            [f"expedite_options:{option['id']}", f"purchase_order_lines:{p['id']}"], min(option['valid_until'], need))
        if value['feasible']:
            expedites.append(value)

    # One transfer + one expedite, bounded two-action combination; no optimizer.
    for transfer in transfers:
        for expedite in expedites:
            if transfer['complete'] or expedite['complete']:
                continue
            actions = copy.deepcopy(transfer['actions'] + expedite['actions'])
            actions[1]['quantity'] = str(min(Decimal(actions[1]['quantity']), gap - Decimal(actions[0]['quantity'])))
            candidate('combined', actions, [], transfer['sourceEvidence'] + expedite['sourceEvidence'],
                      min(timestamp(transfer['validUntil']), timestamp(expedite['validUntil'])), transfer['donor'])
    refs = {ref for p in candidates for ref in p['sourceEvidence']}
    evidence = {e['id']: e for e in baseline['evidence']}
    for table, rows in tables.items():
        for row in rows:
            key = f"{table}:{row['id']}"
            if key in refs:
                evidence[key] = plain({'id': key, 'table': table, 'record': row,
                                       'knownAt': row.get('ingested_at', now), 'sourceRecordId': row.get('source_record_id',key)})
    return {'baseline': baseline, 'target': target, 'candidates': candidates, 'missing': [],
            'evidence': list(evidence.values()), 'snapshotHash': fingerprint(snapshot)}
