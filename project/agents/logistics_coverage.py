"""从已授权工具证据计算逾期覆盖；不访问数据库，不猜测未展示记录。"""
from collections import Counter
import re


def overdue_coverage(evidence, scope):
    totals, groups, group_totals, tasks, refs, reasons = set(), {}, set(), {}, [], []
    for ref, item in evidence.items():
        record = item.get('record', {})
        tool = record.get('tool')
        if item.get('source_type') != 'tool_result' or tool not in (
                'get_snapshot_summary', 'get_risk_distribution', 'get_pending_tasks'):
            continue
        if tool == 'get_risk_distribution' and (record.get('data') or {}).get('dimension') != 'courier_id':
            continue
        refs.append(ref)
        keys = ('run_id', 'snapshot_id', 'fingerprint', 'business_as_of', 'metric_version')
        if any(record.get(k) != scope.get(k) for k in keys) or any(
                record.get(k) != scope[k] for k in ('city', 'region_ids') if k in scope):
            reasons.append('证据范围、时点或口径不一致')
            continue
        if record.get('status') not in ('ok', 'empty'):
            reasons.append('相关工具不可用或风险指标不完整')
            continue
        data = record.get('data') or {}
        metric = data.get('metrics', {}).get('overdue', {})
        total = metric.get('value') if tool == 'get_snapshot_summary' else record.get('denominator', {}).get('overdue')
        if type(total) is not int or total < 0 or (tool == 'get_snapshot_summary' and metric.get('status') not in ('ok', 'empty')):
            reasons.append('全局逾期分母不可计算')
            continue
        totals.add(total)
        if tool == 'get_risk_distribution' and type(record.get('total_rows')) is int:
            group_totals.add(record['total_rows'])
        for row in record.get('rows', []):
            if tool == 'get_risk_distribution':
                m = row.get('metrics', {}).get('overdue', {})
                courier, count = row.get('courier_id'), m.get('value')
                if not courier or type(count) is not int or count < 0 or m.get('status') not in ('ok', 'empty') or m.get('denominator') != total:
                    reasons.append('人员聚合不可计算或分母不一致')
                elif courier in groups and groups[courier] != count:
                    reasons.append('同一人员聚合计数矛盾')
                else:
                    groups[courier] = count
            elif tool == 'get_pending_tasks' and row.get('risk_state') == 'overdue':
                identity, courier = row.get('evidence_id'), row.get('courier_id')
                if not identity or not courier:
                    reasons.append('明细缺少可去重任务标识或人员归属')
                elif identity in tasks and tasks[identity] != courier:
                    reasons.append('同一任务人员归属矛盾')
                else:
                    tasks[identity] = courier
    if len(totals) > 1:
        reasons.append('全局逾期计数矛盾')
    if not totals:
        reasons.append('缺少可计算的同口径全局逾期分母')
    total = next(iter(totals)) if len(totals) == 1 else None
    detail_counts = Counter(tasks.values())
    if any(detail_counts[c] > count for c, count in groups.items()):
        reasons.append('逾期明细超过人员聚合计数')
    counts = dict(detail_counts)
    counts.update(groups)  # 聚合与明细描述同一批任务，不能相加。
    covered = sum(counts.values())
    if total is not None and covered > total:
        reasons.append('已覆盖逾期超过全局计数')
    complete = total is not None and not reasons and covered == total
    candidates = []
    for courier, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3]:
        exact = courier in groups or complete
        verdict = ('unknown' if reasons or total is None else 'no_overdue' if total == 0
                   else 'over_half' if count * 2 > total else 'not_over_half' if exact else 'unknown')
        candidates.append({'courier_id': courier, 'overdue_count': count,
                           'count_is_exact': exact and not reasons,
                           'share': count / total if total and not reasons else None,
                           'verdict': verdict})
    leaders = []
    if counts and not reasons and total:
        maximum = max(counts.values())
        # 完整归属或过半可确定最高人员；逾期降序页必须越过并列边界。
        ordered = any(item.get('record', {}).get('tool') == 'get_risk_distribution' and
            (item['record'].get('data') or {}).get('dimension') == 'courier_id' and
            (item['record'].get('data') or {}).get('sort') == 'overdue_desc' and
            item['record'].get('page', 1) == 1 and item['record'].get('rows') and
            (not item['record'].get('truncated') or
             item['record']['rows'][-1].get('metrics', {}).get('overdue', {}).get('value', maximum) < maximum)
            for item in evidence.values())
        if maximum > 0 and (complete or maximum * 2 > total or ordered):
            leaders = sorted(c for c, n in counts.items() if n == maximum and (c in groups or complete))
    return {'global_overdue': total, 'covered_overdue': covered,
            'focus_courier_ids': leaders, 'focus_evidence_ids': refs if leaders else [],
            'person_list_coverage': ('unknown' if len(group_totals) != 1 else
                                     'complete' if len(groups) == next(iter(group_totals)) else 'partial'),
            'ownership_coverage': 'unknown' if reasons or total is None else 'complete' if complete else 'partial',
            'couriers': candidates, 'evidence_ids': refs,
            'missing_evidence': list(dict.fromkeys(reasons)) or ([] if complete else ['完整逾期归属尚未覆盖']),
            'limitation': ('当前快照同口径逾期已完整归属；不代表生产全量风险。' if complete else
                           '逾期归属未完整覆盖；仅明细支持的人员计数为下界，不代表生产全量风险。')}


def goal_evidence_status(question, evidence, scope):
    """仅识别当前三类开发目标；不凭模型complete或观察空列表决定完成。"""
    text = re.sub(r'(?:不查|不调查|无需调查|不需要调查)[^，。；]*', '', question)
    concentration = any(word in text for word in ('集中', '过半', '一半'))
    workload = any(word in text for word in ('样本负荷', '人员负荷'))
    window = any(word in text for word in ('共同窗口', '共同时间窗口', '相同窗口'))
    coverage_goal = any(word in text for word in ('首页', '覆盖', '逾期规模', '逾期分布'))
    supported = (concentration or window or coverage_goal) and not any(
        word in text for word in ('原因', '根因', '运力', '派单', '建议', '坐标', '临期', '区域集中', 'AOI集中', '全部人员负荷'))
    coverage = overdue_coverage(evidence, scope)
    records = []
    for item in evidence.values():
        record = item.get('record', {})
        if item.get('source_type') == 'tool_result' and record.get('status') in ('ok', 'empty') and all(
                record.get(k) == scope.get(k) for k in ('run_id', 'snapshot_id', 'fingerprint', 'business_as_of', 'metric_version')):
            records.append(record)
    missing = []
    if coverage['global_overdue'] is None or coverage['ownership_coverage'] == 'unknown':
        missing.append('缺少可计算的全局逾期汇总')
    top = coverage['couriers'][0] if coverage['couriers'] else None
    if concentration and not (coverage['global_overdue'] == 0 or (top and
            (top['verdict'] == 'over_half' or (top['verdict'] == 'not_over_half' and coverage['ownership_coverage'] == 'complete')))):
        missing.append('缺少足以判断单人逾期集中的人员分布')
    missing_workloads = []
    if workload:
        rows = [row for r in records if r.get('tool') == 'get_courier_workload' for row in r.get('rows', [])]
        for courier in coverage['focus_courier_ids']:
            if not any(row.get('courier_id') == courier and
                all(type(row.get(k)) is int for k in ('visible_tasks', 'completed_tasks')) and
                all(type(row.get('metrics', {}).get(k, {}).get('value')) is int
                    for k in ('backlog', 'overdue', 'due_soon', 'recent_accepted', 'recent_completed')) for row in rows):
                missing_workloads.append(courier)
        if not coverage['focus_courier_ids'] or missing_workloads:
            missing.append('缺少重点人员完整样本负荷，逾期明细不能替代负荷查询')
    if coverage_goal and coverage['ownership_coverage'] != 'complete':
        missing.append('逾期归属尚未完整覆盖，需明细或人员聚合')
    if window:
        windows, tasks, conflicting_windows = {}, {}, False
        for r in records:
            if r.get('tool') != 'get_pending_tasks':
                continue
            for row in r.get('rows', []):
                if row.get('risk_state') != 'overdue' or not row.get('evidence_id'):
                    continue
                key = (row.get('time_window_start_reference'), row.get('time_window_end_reference'))
                if not all(key) or not row.get('courier_id'):
                    continue
                observed = (key, row['courier_id'])
                if row['evidence_id'] in tasks and tasks[row['evidence_id']] != observed:
                    conflicting_windows = True
                tasks[row['evidence_id']] = observed
        for key, courier in tasks.values():
            windows.setdefault(key, set()).add(courier)
        shared = any(len(couriers) >= 2 for couriers in windows.values())
        wants_all = bool(re.search(r'(所有|全部|全区域).*共同', text))
        complete_windows = coverage['ownership_coverage'] == 'complete' and len(tasks) == coverage['global_overdue']
        if conflicting_windows or ((not shared or wants_all) and not complete_windows):
            missing.append('缺少不同人员的共同窗口证据，或足以否定共同窗口的完整逾期明细')
    return {'supported': supported, 'ready': supported and not missing, 'missing': missing if supported else [],
            'workload_required': workload and supported, 'missing_workload_ids': missing_workloads,
            'window_required': window and supported, 'coverage_required': coverage_goal and supported,
            'coverage': coverage}


def goal_followup(question, evidence, scope, trace):
    """只提出一次可补齐缺口的逾期分页；查询执行及预算由宿主负责。"""
    goal = goal_evidence_status(question, evidence, scope)
    if not goal['supported'] or goal['ready'] or goal['coverage']['ownership_coverage'] != 'partial':
        return None
    missing = goal['coverage']['global_overdue'] - goal['coverage']['covered_overdue']
    for previous in reversed(trace):
        args = previous.get('parameters', {})
        item = evidence.get(previous.get('evidence_id'), {})
        record = item.get('record', {})
        if (previous.get('tool') != 'get_pending_tasks' or args.get('risk_state') != 'overdue'
                or record.get('status') != 'ok' or not record.get('truncated')
                or any(record.get(k) != scope.get(k) for k in
                       ('run_id', 'snapshot_id', 'fingerprint', 'business_as_of', 'metric_version'))):
            continue
        page, size = args.get('page'), args.get('page_size')
        if type(page) is not int or type(size) is not int or not 0 < missing <= size:
            continue
        if record.get('page') != page or record.get('page_size') != size:
            continue
        same_query = [t for t in trace if t.get('tool') == previous['tool'] and
                      {k: v for k, v in t.get('parameters', {}).items() if k != 'page'} ==
                      {k: v for k, v in args.items() if k != 'page'}]
        if set(t['parameters']['page'] for t in same_query) != set(range(1, page+1)):
            continue
        if any(t.get('status') != 'ok' for t in same_query):
            continue
        return {'tool': 'get_pending_tasks', 'parameters': {**args, 'page': page+1},
                'reason': f'当前同口径逾期归属尚缺{missing}条，补取下一页',
                'evidence_ids': goal['coverage']['evidence_ids']}
    return None
