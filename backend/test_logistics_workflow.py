"""T11：真实LangGraph/SQLite及模拟HTTP模型，安全恢复与结果进入决策。"""
from copy import deepcopy
import json
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import httpx

from . import test_logistics_report as support
from .common import APIError
from .logistics_workflow import LogisticsWorkflow
from project.tests.test_logistics_protocol import plan, payload


def simulated(body):
    """测试模型按实际上下文选择下一工具参数，不预置工具证据。"""
    context = json.loads(body['messages'][1]['content'])
    kind = body['tools'][0]['function']['name'].removeprefix('submit_')
    current = context['plan']
    if kind == 'plan':
        if current is None:
            value = plan()
        else:
            value = deepcopy(current); value['version'] += 1; value['revision_reason'] = '根据新工具结果修订'
            for step in value['steps']:
                step['status'] = context['step_states'].get(step['step_id'], 'pending')
                if step['status'] == 'pending' and step['tool'] == 'get_risk_distribution':
                    summary = next(e['record'] for e in context['evidence'].values() if e['record'].get('tool') == 'get_snapshot_summary')
                    step['parameters']['dimension'] = 'courier_id' if summary['data']['metrics']['backlog']['value'] > 0 else 'aoi_id'
                    step['question'] = '依据实际积压选择分组'
    elif kind == 'observation':
        step = context['question'].split('步骤 ')[-1]
        ref = list(context['evidence'])[0]
        value = {'plan_version': current['version'], 'step_id': step,
            'new_facts': [{'statement': '已取得当前工具实际证据', 'evidence_ids': [ref]}],
            'hypothesis_assessments': [{'hypothesis_id': 'h1', 'verdict': 'not_supported', 'evidence_ids': [ref],
                'reason': '汇总只给总量，无法支持积压集中假设，需要查分布'}] if context['evidence'][ref]['record']['tool'] == 'get_snapshot_summary' else [],
            'missing': ['缺少现场排班'], 'counterevidence': [],
            'unresolved_questions': ['原因尚未确认'], 'next_steps': ['核对后续证据']}
    else:
        ref, item = next((r, e) for r, e in context['evidence'].items() if e['record'].get('tool') == 'get_snapshot_summary')
        metric = 'backlog' if 'backlog' in item['record']['data']['metrics'] else 'overdue'
        value = {'status': 'partial', 'claims': [{'statement': '当前样本内积压数量来自确定性汇总', 'kind': 'computed',
            'evidence_ids': [ref], 'limitation': '样本不代表生产全量',
            'computed': {'evidence_id': ref, 'metric': metric, 'value': item['record']['data']['metrics'][metric]['value']}}],
            'evidence_ids': [ref], 'limitations': ['原因未确认'], 'unresolved_questions': ['缺少排班及现场核实'], 'next_steps': ['人工核查']}
        if context.get('overdue_assessment_required'):
            coverage = context['overdue_coverage']; candidate = coverage['couriers'][0]
            refs = coverage['evidence_ids']
            value['overdue_assessment'] = {k: candidate[k] for k in ('courier_id', 'overdue_count', 'verdict')}
            value['overdue_assessment'].update(claim_index=len(value['claims']), global_overdue=coverage['global_overdue'],
                missing_evidence=coverage['missing_evidence'] if candidate['verdict'] == 'unknown' else [], evidence_ids=refs)
            value['claims'].append({'statement': '按程序摘要判断逾期集中程度', 'kind': 'observed',
                                    'evidence_ids': refs, 'limitation': '名单可能分页或压缩展示', 'computed': None})
            value['evidence_ids'] = list(dict.fromkeys(value['evidence_ids']+refs))
    return payload(value, mode='tool_call', kind=kind)


class WorkflowTests(unittest.TestCase):
    setUp = support.ReportTests.setUp
    tearDown = support.ReportTests.tearDown
    login = support.ReportTests.login
    create = support.ReportTests.create
    new_run = support.ReportTests.new_run
    call = support.ReportTests.call

    def enabled(self):
        self.app.state.logistics_model.env = {**self.env, 'SUPPLY_MODEL': 'fixture', 'SUPPLY_MODEL_API_KEY': 'fixture',
            'SUPPLY_MODEL_BASE_URL': 'https://model.test', 'LOGISTICS_MODEL_OUTPUT_MODE': 'tool_call'}

    def model_run(self, key='model'):
        self.enabled()
        snapshot = self.app.state.logistics_runs.saved(self.run)['snapshot_id']
        response = self.client.post('/api/logistics/runs', json={'snapshot_id': snapshot, 'idempotency_key': key,
            'mode': 'model', 'question': '积压集中在哪些人员，原因是否可确认'})
        self.assertEqual(response.status_code, 202, response.text)
        # 避免先前fixture确定性运行影响领取。
        self.client.post(f'/api/logistics/runs/{self.run}/cancel', json={})
        self.run = response.json()['run_id']
        return response

    def test_model_result_changes_next_plan_and_persists_links(self):
        self.model_run(); service = self.app.state.logistics_runs
        requests = []
        # 保存原factory避免patch递归。
        factory = httpx.Client
        def handler(request):
            body = json.loads(request.content); requests.append(body)
            return httpx.Response(200, json=simulated(body))
        with patch('backend.investigation_model.httpx.Client', side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs)):
            service.process(service.claim())
        row = service.saved(self.run)
        self.assertEqual(row['status'], 'completed', row)
        state = self.app.state.logistics_workflow.read(self.user, self.run)['state']
        self.assertEqual(state['report']['value']['status'], 'partial')
        template = self.app.state.logistics_reports.read(self.user, self.run)
        self.assertEqual(state['template_report_id'], template['report_id'])
        self.assertEqual(json.loads(row['result_json'])['template_report_id'], template['report_id'])
        self.assertGreaterEqual(len(state['plans']), 2)
        second = json.loads(requests[2]['messages'][1]['content'])
        self.assertTrue(second['evidence']); self.assertTrue(second['observations'])
        self.assertEqual(state['plans'][1]['value']['steps'][1]['parameters']['dimension'], 'courier_id')
        self.assertEqual(state['trace'][0]['tool'], 'get_snapshot_summary')
        self.assertEqual(len(state['trace']), 4)
        self.assertEqual(len(self.service.evidence_store.calls(self.user, self.run)['rows']), 4)
        for trace in state['trace']:
            self.assertIn(trace['evidence_id'], state['evidence_ids'])
            self.assertTrue(trace['call_id']); self.assertTrue(trace['step_id']); self.assertTrue(trace['plan_version'])
        self.assertEqual(row['thread_id'], 'logistics:'+self.run)
        self.assertEqual(self.client.get(f'/api/logistics/runs/{self.run}/investigation').status_code, 200)
        observations = [json.loads(b['messages'][1]['content']) for b in requests
                        if b['tools'][0]['function']['name'] == 'submit_observation']
        for context in observations:
            self.assertEqual(len(context['plan']['steps']), 1)
            self.assertEqual(context['plan']['steps'][0]['step_id'], context['question'].split('步骤 ')[-1])
            self.assertFalse(any(e['source_type'] == 'snapshot_task' for e in context['evidence'].values()))
            self.assertTrue(all('denominator' in e['record'] for e in context['evidence'].values()))
        usage = json.loads(row['budget_json'])['model_usage_records']
        self.assertEqual(len(usage), len(requests))
        self.assertEqual(sum(u['total_tokens'] for u in usage), json.loads(row['budget_json'])['model_tokens'])
        self.assertTrue(all(u['request_bytes'] > 0 for u in usage))

    def test_disk_resume_reuses_saved_model_plan_in_new_process(self):
        self.model_run(); service = self.app.state.logistics_runs
        factory = httpx.Client
        def handler(request): return httpx.Response(200, json=simulated(json.loads(request.content)))
        claimed = service.claim()
        with patch('backend.investigation_model.httpx.Client', side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs)):
            for _ in range(2):
                status, node, result = service.step(service.saved(self.run), {})
                service.commit(self.run, claimed['claim_token'], status, node, result)
        self.assertEqual(self.app.state.logistics_workflow.graph.get_state(self.app.state.logistics_workflow.config(self.run)).next, ('select',))
        self.data.db.execute('UPDATE logistics_runs SET lease_until=0 WHERE run_id=?', (self.run,))
        env = {**self.app.state.logistics_model.env, 'AUTH_DB_PATH': self.env['AUTH_DB_PATH'], 'DATA_DB_PATH': self.env['DATA_DB_PATH']}
        code = '''import json,sys,httpx
from unittest.mock import patch
from backend.main import create_app
from backend.test_logistics_workflow import simulated
env=json.loads(sys.argv[1]); app=create_app(env); factory=httpx.Client
def handler(request):return httpx.Response(200,json=simulated(json.loads(request.content)))
with patch('backend.investigation_model.httpx.Client',side_effect=lambda **kw:factory(transport=httpx.MockTransport(handler),**kw)):
    runs=app.state.logistics_runs; runs.process(runs.claim())
app.state.logistics_workflow.close();app.state.auth.db.close();app.state.data.db.close()
'''
        subprocess.run([sys.executable, '-X', 'utf8', '-c', code, json.dumps(env)], check=True, capture_output=True, timeout=30)
        row = service.saved(self.run); self.assertEqual(row['status'], 'completed', row)
        state = self.app.state.logistics_workflow.read(self.user, self.run)['state']
        self.assertEqual(len(state['plans']), 2)
        self.assertEqual(len(state['trace']), 4)
        self.assertEqual(json.loads(row['budget_json'])['model_calls'], 5)
        self.assertEqual(service.budgets.read(self.run)['model_calls'], 5)
        self.assertEqual(service.budgets.read(self.run)['plan_revisions'], 1)

    def test_inflight_operation_does_not_repeat_model_and_stops_partial(self):
        self.model_run(); service = self.app.state.logistics_runs; claimed = service.claim()
        status, node, result = service.step(claimed, {}); service.commit(self.run, claimed['claim_token'], status, node, result)
        self.data.db.execute("INSERT INTO logistics_graph_operations VALUES(?,?,'plan','started',NULL,NULL,'T')", (self.run, '0:0:plan'))
        with patch('backend.logistics_model.model_session') as request:
            service.process(service.saved(self.run)); request.assert_not_called()
        self.assertEqual(service.saved(self.run)['status'], 'waiting')
        self.assertEqual(service.saved(self.run)['result_json'].find('interrupted_operation') >= 0, True)

    def test_tool_journal_survives_checkpoint_lag_without_duplicate_call(self):
        self.model_run(); service = self.app.state.logistics_runs; workflow = self.app.state.logistics_workflow
        claimed = service.claim(); factory = httpx.Client
        def handler(request): return httpx.Response(200, json=simulated(json.loads(request.content)))
        with patch('backend.investigation_model.httpx.Client', side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs)):
            for _ in range(3):
                status, node, result = service.step(service.saved(self.run), {})
                service.commit(self.run, claimed['claim_token'], status, node, result)
            saved = workflow.graph.get_state(workflow.config(self.run))
            self.assertEqual(saved.next, ('execute',))
            # 工具结果事务提交，但没有图检查点；模拟此崩溃窗口。
            completed = workflow.node('execute', saved.values)
            self.assertEqual(len(completed['trace']), 1)
            workflow.close()
            restored = LogisticsWorkflow(self.app.state.logistics_model, self.app.state.logistics_reports)
            self.app.state.logistics_workflow = service.workflow = restored
            service.process(service.saved(self.run))
        self.assertEqual(service.saved(self.run)['status'], 'completed')
        self.assertEqual(self.service.evidence_store.calls(self.user, self.run)['total_rows'], 4)

    def test_revoked_during_response_and_budget_keep_no_false_report(self):
        self.model_run(); service = self.app.state.logistics_runs; factory = httpx.Client
        def revoked(request):
            self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'")
            return httpx.Response(200, json=simulated(json.loads(request.content)))
        with patch('backend.investigation_model.httpx.Client', side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(revoked), **kwargs)):
            service.process(service.claim())
        self.assertEqual(service.saved(self.run)['status'], 'failed')
        self.assertEqual(self.client.get(f'/api/logistics/runs/{self.run}/investigation').status_code, 403)
        self.data.db.execute("UPDATE logistics_grants SET regions='[\"5\"]' WHERE account='reader'")
        self.model_run('budget')
        budget = json.loads(service.saved(self.run)['budget_json']); budget['max_model_calls'] = 1
        self.data.db.execute('UPDATE logistics_runs SET budget_json=? WHERE run_id=?', (json.dumps(budget), self.run))
        def valid(request): return httpx.Response(200, json=simulated(json.loads(request.content)))
        with patch('backend.investigation_model.httpx.Client', side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(valid), **kwargs)):
            service.process(service.claim())
        self.assertEqual(service.saved(self.run)['status'], 'waiting')
        self.assertEqual(self.client.get(f'/api/logistics/runs/{self.run}/report').status_code, 404)

    def test_invalid_output_never_calls_tool_and_cancel_fences_model(self):
        self.model_run(); service = self.app.state.logistics_runs; factory = httpx.Client
        def invalid(request):
            value = plan(); value['steps'][0]['tool'] = 'execute_sql'
            return httpx.Response(200, json=payload(value, mode='tool_call'))
        with patch('backend.investigation_model.httpx.Client', side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(invalid), **kwargs)):
            service.process(service.claim())
        self.assertEqual(service.saved(self.run)['status'], 'waiting')
        self.assertEqual(self.service.evidence_store.calls(self.user, self.run)['total_rows'], 0)
        self.model_run('cancelled')
        def cancel(request):
            service.cancel(self.user, self.run)
            return httpx.Response(200, json=simulated(json.loads(request.content)))
        with patch('backend.investigation_model.httpx.Client', side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(cancel), **kwargs)):
            service.process(service.claim())
        self.assertEqual(service.saved(self.run)['status'], 'cancelled')
        self.assertEqual(self.service.evidence_store.calls(self.user, self.run)['total_rows'], 0)

    def test_permission_and_idempotency_scope(self):
        created = self.model_run(); service = self.app.state.logistics_runs
        snapshot = service.saved(self.run)['snapshot_id']
        again = self.client.post('/api/logistics/runs', json={'snapshot_id': snapshot, 'idempotency_key': 'model', 'mode': 'model', 'question': '不同问题'})
        self.assertEqual(again.status_code, 409)
        self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'")
        self.assertEqual(self.client.get(f'/api/logistics/runs/{self.run}/investigation').status_code, 403)
        with patch('backend.logistics_model.model_session') as unused:
            service.process(service.claim()); unused.assert_not_called()
        self.assertEqual(service.saved(self.run)['status'], 'failed')


if __name__ == '__main__': unittest.main()
