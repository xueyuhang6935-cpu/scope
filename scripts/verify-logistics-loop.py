"""T11显式真实模型开发案例：手算夹具、临时SQLite，未接正式源任务。"""
import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values
from backend.test_logistics_workflow import WorkflowTests
from backend.logistics_model import configured


def verify(output, selected_case=None):
    env = {**dotenv_values(ROOT/'.env'), **os.environ}
    if not configured(env):
        raise SystemExit('真实模型配置不可用，未发送请求。')
    proof = {'task': 'T11', 'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
             'model': env['SUPPLY_MODEL'], 'data_kind': '独立手算开发夹具；非正式业务数据或冻结评测案例', 'cases': []}
    questions = ['仅定位当前积压最多的快递员，不调查原因。用最少步骤查看分布与必要指标，报告样本局限。',
                 '仅定位当前逾期风险最多的AOI，不调查原因。用最少步骤查看分布与必要指标，报告样本局限。']
    for index, question in enumerate(questions, 1):
        if selected_case is not None and index != selected_case:
            continue
        test = WorkflowTests(); test.setUp()
        started = time.monotonic()
        try:
            service = test.app.state.logistics_runs
            test.app.state.logistics_model.env = {**env, 'LOGISTICS_MODEL_OUTPUT_MODE': 'tool_call'}
            old = service.saved(test.run)
            test.client.post(f'/api/logistics/runs/{test.run}/cancel', json={})
            created = test.client.post('/api/logistics/runs', json={'snapshot_id': old['snapshot_id'], 'idempotency_key': f't11-live-{index}',
                'mode': 'model', 'question': question})
            assert created.status_code == 202, created.text
            run_id = created.json()['run_id']; service.start()
            previous = None
            for _ in range(340):
                row = service.read(test.user, run_id)
                if row['current_node'] != previous:
                    previous = row['current_node']; print(f'案例{index}：{previous}', flush=True)
                if row['status'] in ('completed', 'waiting', 'failed', 'cancelled'):
                    break
                time.sleep(.5)
            state = test.app.state.logistics_workflow.read(test.user, run_id)['state']
            budget = row['budget']
            plans = state.get('plans', [])
            case = {'case': index, 'question': question, 'run_id': run_id, 'thread_id': row['thread_id'],
                    'run_status': row['status'], 'error_code': row['error_code'], 'stop_reason': state.get('stop_reason'),
                    'elapsed_seconds': round(time.monotonic()-started, 3), 'budget': budget,
                    'plan_count': len(plans), 'observation_count': len(state.get('observations', [])),
                    'trace': state.get('trace', []), 'report': state.get('report'),
                    'plans': plans, 'observations': state.get('observations', [])}
            case['model_selected_first_tool'] = plans[0]['value']['steps'][0]['tool'] if plans else None
            case['result_in_next_decision'] = bool(state.get('observations')) and bool(state.get('report') or len(plans) > 1)
            case['protocol_passed'] = row['status'] == 'completed' and case['result_in_next_decision']
            proof['cases'].append(case)
            print(f"案例{index}：{row['status']}，调用{budget['model_calls']}，已知token{budget.get('model_tokens',0)}", flush=True)
        finally:
            test.tearDown()
        path = Path(output); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
    proof['all_passed'] = all(case['protocol_passed'] for case in proof['cases'])
    path.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
    if not proof['all_passed']:
        raise SystemExit('真实开发案例存在未完成项，详见产物；不自动重试案例。')
    print('所选T11真实开发案例协议通过；自然语言结论仍须人工核查。', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', default='test-results/logistics-t11/live-cases.json')
    parser.add_argument('--case', type=int, choices=(1, 2), help='仅复查指定案例，避免重复调用已通过案例')
    args = parser.parse_args()
    if not args.live:
        parser.error('必须显式提供--live，运行两个真实模型开发案例。')
    verify(args.output, args.case)
