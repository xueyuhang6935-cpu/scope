"""T13 开发对照：真实工具/隔离快照；--live 才调用真实模型，不是冻结评测。"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
import runpy
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values
from backend.logistics_model import configured
from backend import logistics_model as model_host
from project.agents.logistics_model import model_output
from project.agents.logistics_protocol import ProtocolError
from scripts.reference_logistics_metrics import reference
from project.agents.logistics_policy import revision_triggers, call_key

CASES = ('courier', 'window', 'spatial_missing', 'truncated')
QUESTION = ('调查当前揽收风险，首步get_risk_distribution按courier_id及overdue_desc检验“逾期集中于单人”。'
            '初始只规划2步，下一步核对人员负荷；证据否定假设时改查多人共同窗口。'
            '明细首查每页2条，可聚合/分页。描述各用一句短话，不查根因、不派单。')
QUESTIONS = {case: QUESTION for case in CASES}
QUESTIONS['spatial_missing'] = ('调查当前风险，首步get_quality_summary检验“空间信息足够定位”。'
    '初始只规划2步；证据否定时改查时效明细并声明限制。描述各用一句短话，不查根因、不派单。')
QUESTIONS['truncated'] = ('调查当前逾期风险规模与分布。先读取每页2条逾期明细；'
    '初始只规划2步；按实际覆盖决定聚合或分页，不用局部代表全量。描述各用一句短话，不查根因、不派单。')
# 固定顺序只查询一次明细，不按结果修订；三项报告必需取数在双方成本内。
FIXED = [('get_snapshot_summary', {}),
         ('get_risk_distribution', {'dimension': 'courier_id', 'sort': 'overdue_desc'}),
         ('get_courier_workload', {}),
         ('get_pending_tasks', {'page_size': 2}), ('get_quality_summary', {})]


def records(case):
    from backend.test_logistics_snapshots import task
    rows = []
    for i in range(8):
        concentrated = case == 'courier'
        end = '07-02 09:50:00' if i < 6 or case == 'truncated' else '07-02 11:30:00'
        if case == 'spatial_missing' and i < 6:
            end = '07-02 10:10:00'
        rows.append(task(f'T13-{i+1:02}', source_row=i+2, source_file='fixtures/t13-development.csv',
            courier_id='001' if concentrated and i < 6 else f'{i+1:03}',
            aoi_id=f'{i%2+1:03}', accept_time='07-02 09:30:00',
            pickup_time='07-02 12:00:00', time_window_start='07-02 09:00:00', time_window_end=end,
            lng='' if case == 'spatial_missing' else '121.4', lat='' if case == 'spatial_missing' else '31.2',
            accept_gps_time='', accept_gps_lng='', accept_gps_lat=''))
    return rows


def assess(case, results, report):
    """确定性必要证据检查；自然语言语义另行逐条审核，不以关键词冒充正确性。"""
    oracle = reference(records(case), '2000-07-02 10:00:00')
    summaries = [r for r in results if r['tool'] == 'get_snapshot_summary']
    numeric = bool(summaries) and all(summaries[-1]['data']['metrics'][k]['value'] == oracle[k]
                                         for k in ('backlog', 'overdue', 'due_soon'))
    tasks = [r for r in results if r['tool'] == 'get_pending_tasks']
    groups = [r for r in results if r['tool'] == 'get_risk_distribution' and r['data']['dimension'] == 'courier_id']
    if case == 'courier':
        needed = bool(groups) and any(r['tool'] == 'get_courier_workload' and
                  any(row['courier_id'] == '001' and row['metrics']['overdue']['value'] == 6 for row in r['rows'])
                  for r in results)
    elif case == 'window':
        overdue = {row['order_id']: row for r in tasks for row in r['rows'] if row['risk_state'] == 'overdue'}
        needed = bool(groups) and len({row['courier_id'] for row in overdue.values()}) >= 2 and \
            len({row['time_window_end_reference'] for row in overdue.values()}) == 1
    elif case == 'spatial_missing':
        needed = bool(tasks) and any(r['tool'] == 'get_quality_summary' and
                  r['data']['quality']['stop_coordinates']['available'] == 0 for r in results)
    else:
        ids = {row['order_id'] for r in tasks for row in r['rows']}
        needed = len(ids) == oracle['backlog'] or any(r['total_rows'] == r['returned_rows'] and
                  sum(row['metrics']['overdue']['value'] for row in r['rows']) == oracle['overdue'] for r in groups)
    return {'numeric_correct': numeric, 'necessary_evidence': needed,
            'model_report_present': bool(report and report.get('claims')),
            'semantic_review': 'pending', 'oracle': oracle}


def run_case(case, mode, env, live):
    from backend.test_logistics_workflow import WorkflowTests
    test = WorkflowTests(); test.setUp()
    try:
        runs = test.app.state.logistics_runs
        runs.cancel(test.user, test.run)
        test.records[:] = records(case)
        snapshot = test.create().json()
        result = run_method(test.app, test.client, test.user, snapshot, case, mode, env, live, QUESTIONS[case])
        result.update(data_kind='合成七月开发夹具，非真实业务发现，非冻结评测',
                      input_hash=sha256(json.dumps(records(case), sort_keys=True).encode()).hexdigest(),
                      assessment=assess(case, result['results'], result['report']))
        return result
    finally:
        test.tearDown()


@contextmanager
def capture_model_proposals(live):
    """只保存评测协议提议及受控错误；不记录请求、配置、凭证或异常原文。"""
    attempts = []
    original = model_host.model_session
    @contextmanager
    def captured(env, *, on_failure=None):
        with original(env, on_failure=on_failure) as request:
            def receive(body, *args):
                kind = body['tools'][0]['function']['name'].removeprefix('submit_')
                attempt = {'kind': kind, 'value': None, 'error': None}
                attempts.append(attempt)
                try:
                    response = request(body, *args)
                except Exception:
                    attempt['error'] = 'model_transport_failed'
                    raise
                try:
                    attempt['value'] = model_output(response, kind, 'tool_call')
                except ProtocolError as exc:
                    attempt['error'] = exc.code
                return response
            yield receive
    if live:
        with patch.object(model_host, 'model_session', captured):
            yield attempts
    else:
        yield attempts


def run_method(app, client, user, snapshot, case, mode, env, live, question, fixed_steps=None):
    with capture_model_proposals(live) as attempts:
        """共享真实工具/模型执行；数据由调用方创建的已授权快照提供。"""
        runs, tools = app.state.logistics_runs, app.state.logistics_tools
        started = time.monotonic()
        created = client.post('/api/logistics/runs', json={'snapshot_id': snapshot['snapshot_id'],
            'idempotency_key': f't13-{case}-{mode}', 'mode': 'model' if mode == 'dynamic' else 'deterministic',
            'question': question})
        assert created.status_code == 202, created.text
        run_id = created.json()['run_id']
        app.state.logistics_model.env = {**env, 'LOGISTICS_MODEL_OUTPUT_MODE': 'tool_call'}
        report = None; state = {}; error = None; results = []
        if mode == 'dynamic':
            assert live, '离线不伪造动态模型'
            # 使用正式执行器及心跳，真实HTTP可能超过10秒租约；不延长或绕过预算。
            runs.start()
            deadline = time.monotonic()+180
            while time.monotonic() < deadline:
                row = runs.saved(run_id)
                if row['status'] in ('completed', 'waiting', 'failed', 'cancelled'):
                    break
                time.sleep(.25)
            else:
                runs.cancel(user, run_id)
            state = app.state.logistics_workflow.read(user, run_id)['state']
            report = (state.get('report') or {}).get('value')
        else:
            refs = []
            for tool, args in (fixed_steps or FIXED):
                if tool == 'get_courier_workload' and args.get('courier_id') == '$first_courier':
                    distribution = next(r for r in results if r['tool'] == 'get_risk_distribution')
                    args = {**args, 'courier_id': distribution['rows'][0]['courier_id']}
                result = tools.call(user, run_id, tool, args)
                refs.append(result['result_evidence_id'])
                results.append({**result, 'arguments': args})
            if live:
                try:
                    report = app.state.logistics_model.generate('report', user, run_id, question,
                        evidence_ids=refs, compact=True)['value']
                except Exception as exc:
                    # 不输出供应商异常原文或配置；失败仍保留已取得证据与实际费用。
                    error = getattr(exc, 'code', type(exc).__name__)
        for trace in state.get('trace', []):
            result = tools.evidence_store.read(user, run_id, trace['evidence_id'])['record']
            results.append({**result, 'arguments': trace['parameters']})
        row = runs.saved(run_id)
        return {'case': case, 'mode': mode, 'live': live, 'question': question,
            'snapshot_id': snapshot['snapshot_id'], 'snapshot_fingerprint': snapshot['snapshot_fingerprint'], 'run_id': run_id,
            'run_status': row['status'], 'stop_reason': state.get('stop_reason'), 'error': error or row['error_code'],
            'elapsed_seconds': round(time.monotonic()-started, 3), 'budget': runs.budgets.read(run_id),
            'model_usage_records': json.loads(row['budget_json']).get('model_usage_records', []),
            'model_proposals': attempts,
            'plans': state.get('plans', []), 'revisions': state.get('revision_history', []),
            'observations': state.get('observations', []), 'trace': state.get('trace', []),
            'results': results, 'report': report}


def summarize(output, case_names=CASES, assessor=None):
    """从不可变原始输出重算，程序停止报告不能算作模型回答完成。"""
    pairs = []
    for case in case_names:
        pair = {'case': case}
        raw = {}
        for mode in ('fixed', 'dynamic'):
            path = output/f'{case}-{mode}.json'
            data = json.loads(path.read_text(encoding='utf-8')); raw[mode] = data
            assessment = assessor(data) if assessor else assess(case, data['results'], data['report'])
            model_report = bool(data['report']) and (mode == 'fixed' or data['run_status'] == 'completed')
            assessment['model_report_present'] = model_report and bool(data['report'].get('claims'))
            changes = []
            evidence = {t['evidence_id']: r for t, r in zip(data['trace'], data['results'])}
            for revision in data['revisions']:
                old_version = revision['old_plan']['version']
                previous = next((t for t in reversed(data['trace']) if t['plan_version'] == old_version), None)
                observation = next((o['value'] for o in reversed(data['observations']) if previous and
                    o['value']['plan_version'] == old_version and o['value']['step_id'] == previous['step_id']), None)
                verified = []
                if previous and observation:
                    verified = revision_triggers(observation, evidence[previous['evidence_id']], previous['evidence_id'])
                diff = revision['diff']
                changed = [s for s in diff['changed'] if s['before']['tool'] != s['after']['tool'] or
                           s['before']['parameters'] != s['after']['parameters']]
                added = diff['added']+[s['after'] for s in changed]
                executed = [s for s in added if any(t['plan_version'] >= revision['new_plan']['version'] and
                    call_key(data['snapshot_fingerprint'], t['tool'], t['parameters']) ==
                    call_key(data['snapshot_fingerprint'], s['tool'], s['parameters']) for t in data['trace'])]
                changes.append({'version': revision['new_plan']['version'],
                    'trigger_kinds': [t['kind'] for t in revision['triggers']],
                    'trigger_replay_matches': verified == revision['triggers'],
                    'changed_or_added_steps': added, 'executed_changed_steps': executed})
            pair[mode] = {'output_hash': sha256(path.read_bytes()).hexdigest(),
                'run_status': data['run_status'], 'stop_reason': data['stop_reason'],
                'report_source': 'model' if model_report else 'program_stop_report' if data['report'] else 'none',
                'assessment': assessment, 'budget': data['budget'], 'elapsed_seconds': data['elapsed_seconds'],
                'effective_result_count': len({r.get('result_fingerprint', json.dumps(r, sort_keys=True)) for r in data['results']}),
                'path_changes': changes}
        pair['same_input'] = raw['fixed']['input_hash'] == raw['dynamic']['input_hash']
        pair['same_snapshot_content'] = raw['fixed']['snapshot_fingerprint'] == raw['dynamic']['snapshot_fingerprint']
        pair['same_snapshot_id'] = (raw['fixed']['snapshot_id'] == raw['dynamic']['snapshot_id']) \
            if raw['fixed'].get('snapshot_id') and raw['dynamic'].get('snapshot_id') else None
        assert pair['same_input'] and pair['same_snapshot_content'], '对照输入不一致'
        if raw['fixed'].get('source_kind') == 'real_imported_database':
            assert pair['same_snapshot_id'], '真实源对照必须绑定同一个已保存快照'
        pairs.append(pair)
    completed = sum(p['dynamic']['assessment']['model_report_present'] for p in pairs)
    summary = {'cases': pairs, 'semantic_review': '见t13-planning-value.md逐条审核；非独立盲审',
        'conclusion': '尚未证明增量价值；预算内模型报告未完成，G3未通过。' if not completed else
                      f'动态模型报告{completed}/{len(pairs)}；增量价值与G3需结合语义审核及路径证据判断。',
        'data_kind': raw['fixed']['data_kind'],
        'limits': '每方法每例一次，历史失败全部保留；不代表冻结评价或真实运营收益。'}
    path = output/'comparison.json'
    with path.open('x', encoding='utf-8') as file:
        json.dump(summary, file, ensure_ascii=False, indent=2)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--source', choices=('database', 'synthetic'), default='database')
    parser.add_argument('--prepare-only', action='store_true', help='仅从已导入数据抽样并冻结快照，不调用模型')
    parser.add_argument('--region', default='5')
    parser.add_argument('--date', default='2000-07-02', help='仅七月1—14日开发区间')
    parser.add_argument('--case', choices=CASES)
    parser.add_argument('--mode', choices=('fixed', 'dynamic', 'both'), default='both')
    parser.add_argument('--output', default='test-results/logistics-t13-real')
    parser.add_argument('--summarize', action='store_true', help='只读取既有四组对照，重算并创建comparison.json，不调用模型')
    args = parser.parse_args()
    if args.source == 'database':
        runpy.run_path(str(ROOT/'scripts/verify-logistics-planning-real.py'))['verify'](args)
        return
    if args.prepare_only:
        parser.error('--prepare-only只适用于database')
    if args.summarize:
        summarize(Path(args.output))
        print('对照表已生成；失败与程序部分报告均如实保留。')
        return
    if not args.live and args.mode != 'fixed':
        parser.error('动态路径需显式--live；离线仅--mode fixed验证工具与评分器。')
    env = {**dotenv_values(ROOT/'.env'), **os.environ}
    if args.live and not configured(env):
        parser.error('真实模型配置不可用，未发送请求。')
    output = Path(args.output); output.mkdir(parents=True, exist_ok=True)
    for case in ([args.case] if args.case else CASES):
        for mode in (('fixed', 'dynamic') if args.mode == 'both' else (args.mode,)):
            path = output / f'{case}-{mode}.json'
            if path.exists():
                parser.error(f'产物已存在：{path}；复查请指定新输出目录，不覆盖失败结果。')
            print(f'{case}/{mode} 开始', flush=True)
            result = run_case(case, mode, env, args.live)
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f"{case}/{mode}：{result['run_status']}，停止={result['stop_reason']}，"
                  f"模型={result['budget']['model_calls']}，工具={result['budget']['tool_calls']}，"
                  f"必要证据={result['assessment']['necessary_evidence']}", flush=True)
    manifest = {'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat(), 'live': args.live,
        'model': env.get('SUPPLY_MODEL') if args.live else None,
        'source_hashes': {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in
            [Path(__file__), ROOT/'backend/logistics_workflow.py', ROOT/'backend/logistics_model.py',
             ROOT/'project/tools/logistics.py', *sorted((ROOT/'project/prompts').glob('logistics_*.txt'))]},
        'note': '逐例保留结果；语义尚需审核；金额无供应商价格配置，使用调用/已知Token及耗时，不编造账单。'}
    (output/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
