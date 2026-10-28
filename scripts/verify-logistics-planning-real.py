"""T13 已导入源的只读开发抽样；答案仅供评测器，Agent使用持久化授权快照。"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import runpy
from unittest.mock import patch

from dotenv import dotenv_values
from fastapi.testclient import TestClient

from backend.auth import now, password_hash
from backend.logistics_access import WORKSPACE
from backend.logistics_repository import source_connection
from backend.logistics_runs import RunService
from backend.main import create_app
from scripts.reference_logistics_metrics import reference

ROOT = Path(__file__).resolve().parents[1]
SHARED = runpy.run_path(str(ROOT/'scripts/verify-logistics-planning.py'))


def save(path, value):
    with path.open('x', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=False, indent=2)


def coordinates(row):
    try:
        lng, lat = float(row['lng']), float(row['lat'])
        return math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180 and -90 <= lat <= 90
    except (ValueError, TypeError):
        return False


def classify(rows, point):
    """独立开发筛选：只使用T已接单、尚无可见完成事件及已知窗口/静态坐标。"""
    rows = [r for r in rows if '2000-'+r['accept_time'] <= point]
    oracle = reference(rows, point)
    risk = [t for t in oracle['tasks'] if t['risk_state'] == 'overdue']
    counts = Counter(t['courier_id'] for t in risk)
    top, amount = counts.most_common(1)[0] if counts else (None, 0)
    windows = defaultdict(set)
    by_order = {r['order_id']: r for r in rows}
    for t in risk:
        raw = by_order[t['order_id']]
        windows['2000-'+raw['time_window_end']].add(t['courier_id'])
    common = next((end for end in sorted(windows) if len(windows[end]) >= 2), None)
    share = amount/len(risk) if risk else 0
    pending_ids = {t['order_id'] for t in oracle['tasks']}
    missing = sum(r['order_id'] in pending_ids and not coordinates(r) for r in rows)
    # 开发阈值先固定；无匹配就记录未找到，不改源记录制造类别。
    matched = {'courier': amount >= 2 and share >= .6,
               'window': bool(common) and share < .5,
               'spatial_missing': missing > 0,
               'truncated': len(risk) > 2}
    return matched, oracle, {'top_courier': top, 'top_overdue_count': amount,
        'top_overdue_share': share, 'common_window_end': common,
        'pending_missing_coordinates': missing,
        'visible_coordinates_available': sum(coordinates(r) for r in rows)}


def app_for(output, env):
    return create_app({**env, 'AUTH_DB_PATH': str(output/'auth.sqlite'),
        'DATA_DB_PATH': str(output/'business.sqlite'), 'NODE_ENV': 'test', 'SUPPLY_MONITOR_AUTOSTART': '0'})


def enter_client(app):
    # 仅避免评测基线被后台抢先执行；动态运行会启动正式执行器和心跳。
    with patch.object(RunService, 'start'):
        return TestClient(app).__enter__()


def prepare(output, env, region, date):
    day = datetime.strptime(date, '%Y-%m-%d')
    if day.year != 2000 or day.month != 7 or not 1 <= day.day <= 14:
        raise ValueError('只允许2000-07-01至07-14开发日期；不使用八月冻结评价区间。')
    selected = {}; checked = []
    with source_connection(env) as db:
        batch = db.execute('''SELECT batch_id,source_sha256,content_sha256 FROM lade.import_batches
            WHERE verified_at IS NOT NULL ORDER BY imported_at DESC LIMIT 1''').fetchone()
        assert batch, '缺少已核验导入批次'
        for hour in (10, 12, 14, 16, 18):
            point = f'{date} {hour:02}:00:00'
            raw = db.execute('''SELECT batch_id,source_row,order_id,city,region_id,courier_id,aoi_id,
                accept_time,pickup_time,time_window_start,time_window_end,lng,lat
                FROM lade.pickup_tasks WHERE batch_id=%s AND city='Shanghai' AND region_id=%s
                AND ('2000-'||accept_time)::timestamp<=%s::timestamp''',
                (batch['batch_id'], region, point)).fetchall()
            matched, oracle, criteria = classify(raw, point)
            checked.append({'business_as_of': point, 'visible_tasks': oracle['visible_tasks'],
                'backlog': oracle['backlog'], 'overdue': oracle['overdue'], 'due_soon': oracle['due_soon'],
                'criteria': criteria, 'matched': matched})
            for case, found in matched.items():
                if not found or case in selected:
                    continue
                folder = output/case; folder.mkdir()
                app = app_for(folder, env); client = enter_client(app)
                try:
                    for mode in ('fixed', 'dynamic'):
                        account = f't13-real-{mode}'
                        app.state.auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'email','T13',?,?)",
                            (account, password_hash('temporary-evaluation-password'), now()))
                        app.state.access.sync()
                        app.state.data.db.execute('INSERT INTO logistics_grants VALUES(?,?,?,?,?)',
                            (WORKSPACE, account, 'investigator', 'Shanghai', json.dumps([region])))
                    app.state.data.db.execute('INSERT INTO database_source_connections VALUES(?,?,?)',
                        (WORKSPACE, batch['batch_id'], 't13-real-evaluation'))
                    assert client.post('/api/auth/login', json={'account': 't13-real-fixed',
                        'password': 'temporary-evaluation-password'}).status_code == 200
                    response = client.post('/api/logistics/snapshots', json={'batch_id': batch['batch_id'],
                        'city': 'Shanghai', 'region_ids': [region], 'business_as_of': point})
                    assert response.status_code == 200, response.text
                    snapshot = response.json()
                    assert snapshot['coverage']['visible_tasks'] == oracle['visible_tasks']
                    source_hash = sha256(json.dumps({k: batch[k] for k in ('source_sha256', 'content_sha256')},
                        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                    assert snapshot['data_version'] == source_hash, '抽样与快照源版本不同'
                    selected[case] = {'case': case, 'snapshot': snapshot, 'oracle': oracle, 'criteria': criteria,
                        'source_version': dict(batch), 'question': SHARED['QUESTIONS'][case],
                        'event_group': f'{region}:{date}', 'source_kind': 'real_imported_database'}
                    print(f"抽样 {case}：区域{region} {point}，积压{oracle['backlog']}，逾期{oracle['overdue']}", flush=True)
                finally:
                    client.__exit__(None, None, None)
    manifest = {'source_kind': 'real_imported_database', 'source_version': dict(batch),
        'region': region, 'development_date': date, 'checked_points': checked,
        'cases': selected, 'not_found': [c for c in SHARED['CASES'] if c not in selected],
        'sampling_rule': '按10/12/14/16/18时点各类取首个匹配；同区域日期按同一事件组，禁止当独立四事件。',
        'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat()}
    save(output/'real-cases.json', manifest)
    return manifest


def assess_real(data, case):
    results, oracle = data['results'], case['oracle']
    summaries = [r for r in results if r['tool'] == 'get_snapshot_summary']
    numeric = bool(summaries) and all(summaries[-1]['data']['metrics'][k]['value'] == oracle[k]
        for k in ('backlog', 'overdue', 'due_soon', 'recent_accepted', 'recent_completed'))
    groups = [r for r in results if r['tool'] == 'get_risk_distribution' and r['data']['dimension'] == 'courier_id']
    tasks = [row for r in results if r['tool'] == 'get_pending_tasks' for row in r['rows']]
    wanted = {t['order_id']: t for t in oracle['tasks']}
    rows_correct = all(t['order_id'] in wanted and all(t[k] == wanted[t['order_id']][k]
        for k in ('courier_id', 'risk_state', 'current_wait_minutes', 'remaining_minutes')) for t in tasks)
    group_oracle = {g['courier_id']: g for g in oracle['groups']['courier_id']}
    groups_correct = all(g['courier_id'] in group_oracle and all(g['metrics'][k]['value'] == group_oracle[g['courier_id']][k]
        for k in ('backlog', 'overdue', 'due_soon')) for r in groups for g in r['rows'])
    name = case['case']; criteria = case['criteria']
    if name == 'courier':
        needed = bool(groups) and any(r['tool'] == 'get_courier_workload' and any(
            g['courier_id'] == criteria['top_courier'] and g['metrics']['overdue']['value'] == criteria['top_overdue_count']
            for g in r['rows']) for r in results)
    elif name == 'window':
        needed = bool(groups) and len({t['courier_id'] for t in tasks if t['risk_state'] == 'overdue'
            and t['time_window_end_reference'] == criteria['common_window_end']}) >= 2
    elif name == 'spatial_missing':
        needed = bool(tasks) and any(r['tool'] == 'get_quality_summary' and
            r['data']['quality']['stop_coordinates']['available'] == criteria['visible_coordinates_available'] for r in results)
    else:
        overdue_ids = {t['order_id'] for t in oracle['tasks'] if t['risk_state'] == 'overdue'}
        needed = set(t['order_id'] for t in tasks if t['risk_state'] == 'overdue') == overdue_ids or any(
            not r['truncated'] and sum(g['metrics']['overdue']['value'] for g in r['rows']) == oracle['overdue'] for r in groups)
    return {'numeric_correct': numeric, 'returned_tasks_correct': rows_correct, 'returned_groups_correct': groups_correct,
        'necessary_evidence': needed and rows_correct and groups_correct, 'semantic_review': 'pending'}


def verify(args):
    output = Path(args.output); output.mkdir(parents=True, exist_ok=True)
    selection_path = output/'real-cases.json'
    if args.summarize:
        selection = json.loads(selection_path.read_text(encoding='utf-8'))
        names = [c for c in selection['cases'] if (output/f'{c}-fixed.json').exists() and (output/f'{c}-dynamic.json').exists()]
        assert names, '没有完整的真实数据配对结果'
        SHARED['summarize'](output, names, lambda d: assess_real(d, selection['cases'][d['case']]))
        print('真实源对照已重算；未抽到的类别仍标未找到。')
        return
    env = {**dotenv_values(ROOT/'.env'), **os.environ}
    selection = json.loads(selection_path.read_text(encoding='utf-8')) if selection_path.exists() else prepare(output, env, args.region, args.date)
    if args.region != selection['region'] or args.date != selection['development_date']:
        raise ValueError('已有抽样范围不同；请指定新输出目录，不能隐式改范围。')
    if args.prepare_only:
        print('真实案例已冻结；未找到：'+','.join(selection['not_found']))
        return
    if not args.live and args.mode != 'fixed':
        raise ValueError('动态方法需--live；只读抽样用--prepare-only，工具复算用--mode fixed。')
    if args.live and not SHARED['configured'](env):
        raise ValueError('真实模型配置不可用，未发送请求。')
    chosen = [args.case] if args.case else list(selection['cases'])
    for name in chosen:
        if name not in selection['cases']:
            raise ValueError(f'指定类别没有真实匹配：{name}')
        case = selection['cases'][name]
        modes = ('fixed', 'dynamic') if args.mode == 'both' else (args.mode,)
        for mode in modes:
            path = output/f'{name}-{mode}.json'
            if path.exists():
                raise FileExistsError(f'保留既有结果，不重放：{path}')
        app = app_for(output/name, env); client = enter_client(app)
        try:
            for mode in modes:
                account = f't13-real-{mode}'
                assert client.post('/api/auth/login', json={'account': account,
                    'password': 'temporary-evaluation-password'}).status_code == 200
                user = app.state.auth.query('SELECT * FROM users WHERE account=?', (account,))
                saved = app.state.snapshots.ready_metadata(user, case['snapshot']['snapshot_id'])
                assert saved['snapshot_fingerprint'] == case['snapshot']['snapshot_fingerprint'], '快照内容版本改变'
                print(f'真实 {name}/{mode} 开始', flush=True)
                result = SHARED['run_method'](app, client, user, case['snapshot'], name, mode, env, args.live, case['question'])
                result.update(data_kind='已导入上海LaDe真实开发快照，非合成或冻结评价',
                    source_kind='real_imported_database', source_version=case['source_version'], event_group=case['event_group'],
                    input_hash=case['snapshot']['visible_content_hash'])
                result['assessment'] = assess_real(result, case)
                save(output/f'{name}-{mode}.json', result)
                # 固定方法直接评测完成后取消queued占位，避免动态执行器执行它并增加成本。
                if mode == 'fixed':
                    app.state.logistics_runs.cancel(user, result['run_id'])
                print(f"真实 {name}/{mode}：{result['run_status']}，停止={result['stop_reason']}，模型/工具="
                    f"{result['budget']['model_calls']}/{result['budget']['tool_calls']}", flush=True)
        finally:
            client.__exit__(None, None, None)
    with source_connection(env) as db:
        after = db.execute('SELECT batch_id,source_sha256,content_sha256 FROM lade.import_batches WHERE batch_id=%s',
            (selection['source_version']['batch_id'],)).fetchone()
    assert dict(after) == selection['source_version'], '源批次版本改变'
    save(output/f"verification-{args.mode}-{'live' if args.live else 'tools'}.json", {
        'source_version_unchanged': True, 'cases_run': chosen, 'not_found': selection['not_found'],
        'live': args.live, 'model': env.get('SUPPLY_MODEL') if args.live else None,
        'source_hashes': {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in
            [Path(__file__), ROOT/'scripts/verify-logistics-planning.py', ROOT/'backend/logistics_model.py',
             ROOT/'backend/logistics_workflow.py', *sorted((ROOT/'project/prompts').glob('logistics_*.txt'))]}})
