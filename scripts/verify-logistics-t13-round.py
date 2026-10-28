"""T13固定快照的开发/重复对照；每次备份隔离库，生产预算和源数据不改。"""
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import runpy
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values
from backend.auth import now, password_hash
from backend.logistics_access import WORKSPACE
from backend.logistics_repository import source_connection

REAL = runpy.run_path(str(ROOT/'scripts/verify-logistics-planning-real.py'))
SHARED = REAL['SHARED']
CASES = ('courier', 'window', 'truncated')
QUESTIONS = {
    'courier': '核对当前逾期是否集中于单人（过半），定位重点人员及其样本负荷。只查这个目标，不查根因、运力或派单。',
    'window': '检验当前逾期集中于单人的假设；若证据不支持，核对逾期是否分散于多人及是否存在共同窗口。只查这个目标，不查根因、运力或派单。',
    'truncated': '核对当前逾期规模和人员分布。先查每页2条逾期明细，判断首页是否足够；不足时补必要证据。只查逾期，不要求全部积压、零逾期人员或AOI排名。'}
FIXED = {
    'courier': [('get_snapshot_summary', {}),
        ('get_risk_distribution', {'dimension': 'courier_id', 'sort': 'overdue_desc', 'page_size': 100}),
        ('get_courier_workload', {'courier_id': '$first_courier'}),
        ('get_pending_tasks', {'risk_state': 'overdue', 'page_size': 20}), ('get_quality_summary', {})],
    'window': [('get_snapshot_summary', {}),
        ('get_risk_distribution', {'dimension': 'courier_id', 'sort': 'overdue_desc', 'page_size': 100}),
        ('get_pending_tasks', {'risk_state': 'overdue', 'page_size': 20}), ('get_quality_summary', {})],
    'truncated': [('get_snapshot_summary', {}),
        ('get_pending_tasks', {'risk_state': 'overdue', 'page_size': 2, 'page': 1}),
        ('get_pending_tasks', {'risk_state': 'overdue', 'page_size': 2, 'page': 2}),
        ('get_risk_distribution', {'dimension': 'courier_id', 'sort': 'overdue_desc', 'page_size': 100}),
        ('get_quality_summary', {})]}


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def backup(source, target):
    # SQLite原生backup保证一致性；输入仅为先前隔离评测库。
    with sqlite3.connect(f'{source.resolve().as_uri()}?mode=ro', uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def setup(output):
    output.mkdir(parents=True)
    old = ROOT/'test-results/logistics-t13-real'
    selection = json.loads((old/'dev-v2/real-cases.json').read_text(encoding='utf-8'))
    for case in CASES:
        origin = old/('dev-v1' if case != 'truncated' else 'dev-v2')
        manifest = json.loads((origin/'real-cases.json').read_text(encoding='utf-8'))
        spec = manifest['cases'][case]
        spec['question'] = QUESTIONS[case]
        selection['cases'][case] = spec
        seed = output/'seeds'/case; seed.mkdir(parents=True)
        for filename in ('auth.sqlite', 'business.sqlite'):
            backup(origin/case/filename, seed/filename)
    REAL['save'](output/'cases.json', selection)
    contract = {'version': 'T13-budget-goal-v2', 'questions': QUESTIONS, 'fixed_steps': FIXED,
        'thresholds': {'successful_dynamic_per_case': '>=2/3', 'numeric_and_scope': '100%',
                       'repeatable_revision_types': '>=2', 'incremental_value': '质量提升或等质量降低成本'},
        'event_groups': {c: selection['cases'][c]['event_group'] for c in CASES},
        'note': '三个目标、一个区域日期事件组；稳定运行每方法每例3次，不择优、不提前停止。',
        'limits': {'model_calls': 12, 'tool_calls': 16, 'plan_revisions': 3, 'model_tokens': 24000, 'active_seconds': 120}}
    REAL['save'](output/'contract.json', contract)


def frozen_hashes():
    files = [Path(__file__), ROOT/'scripts/audit-logistics-t13-round.py', ROOT/'scripts/verify-logistics-planning.py', ROOT/'scripts/verify-logistics-planning-real.py',
        ROOT/'backend/logistics_model.py', ROOT/'backend/logistics_workflow.py', ROOT/'project/agents/logistics_model.py',
        ROOT/'project/agents/logistics_coverage.py',
        ROOT/'project/tools/logistics.py', ROOT/'project/agents/logistics_policy.py', ROOT/'project/agents/logistics_protocol.py',
        ROOT/'backend/logistics_budget.py', ROOT/'backend/logistics_report.py', ROOT/'backend/logistics_tools.py',
        ROOT/'backend/logistics_metrics.py', ROOT/'backend/logistics_snapshots.py', ROOT/'project/workflows/logistics.py',
        *sorted((ROOT/'project/prompts').glob('logistics_*.txt'))]
    return {str(p.relative_to(ROOT)): digest(p) for p in files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--stage', choices=('development', 'stability', 'summary'), required=True)
    parser.add_argument('--output', default='test-results/logistics-t13-round-v5')
    args = parser.parse_args(); output = Path(args.output)
    if args.stage == 'summary':
        summarize(output); return
    if not args.live:
        parser.error('必须显式--live；不伪造真实模型结果。')
    env = {**dotenv_values(ROOT/'.env'), **os.environ}
    assert SHARED['configured'](env), '真实模型未配置'
    if not (output/'cases.json').exists():
        setup(output)
    selection = json.loads((output/'cases.json').read_text(encoding='utf-8'))
    stage = output/args.stage; stage.mkdir(exist_ok=True)
    lock = stage/'frozen.json'
    freeze = {'source_hashes': frozen_hashes(), 'cases_hash': digest(output/'cases.json'),
              'contract_hash': digest(output/'contract.json'), 'model': env['SUPPLY_MODEL'],
              'repetitions': 3 if args.stage == 'stability' else 1}
    if lock.exists():
        assert json.loads(lock.read_text(encoding='utf-8')) == freeze, '本阶段已冻结，禁止混用代码版本'
    else:
        REAL['save'](lock, freeze)
    for repeat in range(1, freeze['repetitions']+1):
        for name in CASES:
            spec = selection['cases'][name]
            for mode in ('fixed', 'dynamic'):
                path = stage/f'{name}-{mode}-{repeat}.json'
                if path.exists():
                    print('保留既有结果：'+path.name, flush=True); continue
                folder = stage/f'{name}-{mode}-{repeat}'; folder.mkdir()
                for filename in ('auth.sqlite', 'business.sqlite'):
                    backup(output/'seeds'/name/filename, folder/filename)
                app = REAL['app_for'](folder, env); client = REAL['enter_client'](app)
                try:
                    account = f't13-{args.stage}-{mode}-{repeat}'
                    app.state.auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'email','T13',?,?)",
                        (account, password_hash('temporary-evaluation-password'), now()))
                    app.state.access.sync()
                    app.state.data.db.execute('INSERT INTO logistics_grants VALUES(?,?,?,?,?)',
                        (WORKSPACE, account, 'investigator', 'Shanghai', json.dumps(spec['snapshot']['region_ids'])))
                    assert client.post('/api/auth/login', json={'account': account,
                        'password': 'temporary-evaluation-password'}).status_code == 200
                    user = app.state.auth.query('SELECT * FROM users WHERE account=?', (account,))
                    meta = app.state.snapshots.ready_metadata(user, spec['snapshot']['snapshot_id'])
                    assert meta['snapshot_fingerprint'] == spec['snapshot']['snapshot_fingerprint']
                    print(f'{args.stage}/{name}/{mode}/{repeat} 开始', flush=True)
                    result = SHARED['run_method'](app, client, user, meta, name, mode, env, True, spec['question'], FIXED[name])
                    result.update(data_kind='已导入真实开发快照', source_kind='real_imported_database',
                        input_hash=meta['visible_content_hash'], source_version=spec['source_version'],
                        event_group=spec['event_group'], repetition=repeat, stage=args.stage)
                    result['assessment'] = REAL['assess_real'](result, spec)
                    REAL['save'](path, result)
                    print(f"{path.name}：{result['run_status']}，{result['stop_reason']}，模型/工具="
                        f"{result['budget']['model_calls']}/{result['budget']['tool_calls']}，证据={result['assessment']['necessary_evidence']}", flush=True)
                finally:
                    client.__exit__(None, None, None)
    with REAL['source_connection'](env) as db:
        after = db.execute('SELECT batch_id,source_sha256,content_sha256 FROM lade.import_batches WHERE batch_id=%s',
                          (selection['source_version']['batch_id'],)).fetchone()
    assert dict(after) == selection['source_version']
    assert frozen_hashes() == freeze['source_hashes'], '运行期间代码变化，必须单列该阶段结果'
    REAL['save'](stage/'finished.json', {'source_version_unchanged': True, 'freeze_hash': digest(lock),
        'finished_at': datetime.now(timezone(timedelta(hours=8))).isoformat()})


def summarize(output):
    selection = json.loads((output/'cases.json').read_text(encoding='utf-8'))
    rows = []
    for stage in ('development', 'stability'):
        for path in sorted((output/stage).glob('*-*-*.json')):
            if path.name == 'frozen.json': continue
            data = json.loads(path.read_text(encoding='utf-8'))
            if 'case' not in data: continue
            check = REAL['assess_real'](data, selection['cases'][data['case']])
            has_model = bool(data['report']) and (data['mode'] == 'fixed' or data['run_status'] == 'completed')
            objective = bool(has_model and check['numeric_correct'] and check['necessary_evidence'])
            rows.append({'file': str(path.relative_to(output)), 'output_hash': digest(path),
                'case': data['case'], 'mode': data['mode'], 'stage': stage, 'repetition': data['repetition'],
                'assessment': check, 'model_report': has_model, 'objective_check_passed': objective,
                'budget': data['budget'], 'elapsed_seconds': data['elapsed_seconds'],
                'snapshot_id': data['snapshot_id'], 'question': data['question'],
                'revision_kinds': sorted({t['kind'] for r in data['revisions'] for t in r['triggers']}),
                'semantic_review': 'pending', 'report_status': data['report']['status'] if data['report'] else None})
    for stage in ('development', 'stability'):
        for case in CASES:
            for repeat in range(1, 4 if stage == 'stability' else 2):
                pair = [r for r in rows if (r['stage'], r['case'], r['repetition']) == (stage, case, repeat)]
                assert len(pair) == 2 and len({r['snapshot_id'] for r in pair}) == 1 and len({r['question'] for r in pair}) == 1
    REAL['save'](output/'summary.json', {'runs': rows, 'run_count': len(rows),
        'stability_objective_counts': {f'{c}/{m}': sum(r['objective_check_passed'] for r in rows
            if r['stage'] == 'stability' and r['case'] == c and r['mode'] == m) for c in CASES for m in ('fixed', 'dynamic')},
        'note': '必要证据/数字自动检查不是语义审核；同事件组不当作独立事件；未知Token单列。'})
    print('全部配对及逐次结果已汇总。')


if __name__ == '__main__': main()
