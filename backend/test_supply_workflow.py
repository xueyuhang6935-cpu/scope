"""Actual PostgreSQL scenarios and durable LangGraph/permission lifecycle."""
import os
import copy
import json
import time
import uuid
import unittest
from unittest.mock import patch
import httpx
from decimal import Decimal

from .database_connection import connection
from .shortage import load_snapshot, calculate
from .supply_planning import build_candidates, simulate, PLANNING_TABLES
from .investigation_agent import investigate
from .common import APIError
from . import test_shortage as foundation


@unittest.skipUnless(os.getenv('SCOPE_DATABASE_INTEGRATION') == '1', 'Requires local PostgreSQL')
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        foundation.RiskLifecycleTests.setUp(self)
        self.env['SUPPLY_INVESTIGATOR'] = 'rules'
        self.app.state.workflow.env['SUPPLY_INVESTIGATOR'] = 'rules'

    login = foundation.RiskLifecycleTests.login
    post = foundation.RiskLifecycleTests.post
    select = foundation.RiskLifecycleTests.select
    events = foundation.RiskLifecycleTests.events
    inject = foundation.RiskLifecycleTests.inject

    def tearDown(self):
        self.app.state.workflow.executor.shutdown(wait=True)
        if self.app.state.workflow.ready:
            with connection(self.app.state.monitor.env, False) as db:
                for row in self.app.state.data.all('SELECT workspace_id FROM risk_monitor_configs'):
                    key = row['workspace_id']
                    db.execute('DELETE FROM sc.plan_approvals WHERE plan_id IN (SELECT p.id FROM sc.candidate_plans p JOIN sc.investigations r ON r.id=p.run_id WHERE r.workspace_id=%s)', (key,))
                    db.execute('DELETE FROM sc.candidate_plans WHERE run_id IN (SELECT id FROM sc.investigations WHERE workspace_id=%s)', (key,))
                    db.execute('DELETE FROM sc.investigations WHERE workspace_id=%s', (key,))
        foundation.RiskLifecycleTests.tearDown(self)

    def test_planning_oracles(self):
        with connection(self.app.state.monitor.env) as db:
            for scenario in ('supplier_delay', 'partial_transfer', 'late_transfer', 'normal', 'missing_unit'):
                s = db.execute('SELECT * FROM supply_demo.scenarios WHERE id=%s', (scenario,)).fetchone()
                snap = load_snapshot(db, scenario, s['initial_as_of'])
                for table in PLANNING_TABLES:
                    snap['tables'][table] = db.execute(f'SELECT * FROM supply_demo.{table} WHERE scenario_id=%s ORDER BY id', (scenario,)).fetchall()
                event = {'factory': 'A', 'material': s['focus_material'], 'revision': s['focus_material'] + '-R1'}
                bundle = build_candidates(snap, event, 3, ['A', 'B'])
                plans = {p['kind']: p for p in bundle['candidates']}
                if scenario in ('normal', 'missing_unit'):
                    self.assertFalse(plans)
                    continue
                if scenario == 'supplier_delay':
                    self.assertEqual(Decimal(plans['transfer']['covered']), 400)
                    self.assertEqual(Decimal(plans['transfer']['totalCost']), 800)
                    self.assertEqual(Decimal(plans['expedite']['covered']), 400)
                    self.assertEqual(Decimal(plans['expedite']['totalCost']), 3000)
                    scoped = build_candidates(snap, event, 3, ['A'])['candidates'][0]
                    self.assertFalse(scoped['feasible'])
                    self.assertIsNone(scoped['donor'])
                    trial = simulate(snap, event, plans['expedite']['actions'], 14)
                    original = calculate(snap, 14, 'A', event['material'])['results'][0]
                    self.assertEqual(sum(Decimal(s['quantity']) for s in trial['supplies']), sum(Decimal(s['quantity']) for s in original['supplies']))
                    invalid = copy.deepcopy(snap)
                    invalid['tables']['expedite_options'][0]['ready_at'] = snap['scenario']['window_start']
                    self.assertTrue(next(p for p in build_candidates(invalid,event,3,['A','B'])['candidates'] if p['kind']=='expedite')['feasible'])
                    invalid['tables']['expedite_options'][0]['valid_until'] = snap['scenario']['window_start']
                    self.assertFalse(next(p for p in build_candidates(invalid,event,3,['A','B'])['candidates'] if p['kind']=='expedite')['feasible'])
                    ids = {e['id'] for e in bundle['evidence']}
                    self.assertTrue(set(plans['transfer']['sourceEvidence']).issubset(ids))
                elif scenario == 'partial_transfer':
                    self.assertEqual(Decimal(plans['transfer']['covered']), 250)
                    self.assertEqual(Decimal(plans['expedite']['covered']), 150)
                    self.assertTrue(plans['combined']['complete'])
                    self.assertEqual(Decimal(plans['combined']['totalCost']), 3500)
                else:
                    self.assertFalse(plans['transfer']['feasible'])

    def test_chat_query_explain_refresh_investigate_simulate_and_approval(self):
        self.select()
        thread = self.post('/api/supply/threads', {'factory': 'A', 'requestId': str(uuid.uuid4())})['id']

        def message(text, key=None):
            task = self.post(f'/api/supply/threads/{thread}/messages', {'text': text, 'requestId': key or str(uuid.uuid4()), 'scope': {}})
            for _ in range(200):
                if task['status'] not in ('planning', 'queued', 'running'):
                    return task
                time.sleep(.02)
                task = self.client.get(f"/api/supply/tasks/{task['id']}").json()
            self.fail('聊天任务未完成')

        first = message('检查 P001 未来三天缺料')
        self.assertEqual(first['status'], 'completed', first)
        explanation = message('这个缺口怎么算的')
        self.assertIn('400', explanation['result']['answer'])
        self.assertEqual(explanation['result']['evidenceCapturedAt'], first['result']['capturedAt'])
        refreshed = message('现在还是缺吗')
        self.assertEqual(refreshed['status'], 'completed', refreshed)
        self.assertIn('截止时点未变化', refreshed['result']['refreshNotice'])
        key = str(uuid.uuid4())
        action = message('继续查为什么晚交', key)
        self.assertEqual(action['status'], 'completed', action)
        link = action['result']['workflow']
        self.event_id = link['riskId']
        again = message('继续查为什么晚交', key)
        self.assertEqual(again['result']['workflow']['runId'], link['runId'])
        for _ in range(200):
            response = self.client.get(f"/api/supply/investigations/{link['runId']}")
            self.assertEqual(response.status_code, 200, response.text)
            run = response.json()
            if run['status'] not in ('queued', 'running'):
                break
            time.sleep(.05)
        self.assertEqual(run['status'], 'awaiting_approval', run)
        self.assertEqual(run['stage'], 'approval')
        self.assertEqual(run['input']['question'], '继续查为什么晚交')
        self.assertEqual(run['report']['question'], '继续查为什么晚交')
        comparison = message('怎么解决，哪个方案更好')
        self.assertEqual(comparison['status'], 'completed', comparison)
        self.assertTrue(comparison['result']['workflow']['reused'])
        self.assertEqual(comparison['result']['workflow']['runId'], run['id'])
        approval = message('批准这个方案')
        self.assertEqual(approval['planning']['action'], 'approval_request')
        with connection(self.app.state.monitor.env) as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM sc.plan_approvals WHERE plan_id IN (SELECT id FROM sc.candidate_plans WHERE run_id=%s)', (run['id'],)).fetchone()['n'], 0)
        plan = next(p for p in run['plans'] if p['content']['feasible'])
        self.post(f"/api/supply/tasks/{comparison['id']}/select-plan", {'planId': 'PLAN-other'}, 400)
        selected = self.post(f"/api/supply/tasks/{comparison['id']}/select-plan", {'planId': plan['id']})
        self.assertEqual(selected['result']['workflow']['planId'], plan['id'])
        linked_approval = message('批准这个方案')
        self.assertEqual(linked_approval['result']['workflow']['planId'], plan['id'])
        decision = {'version': plan['version'], 'contentHash': plan['content_hash'], 'decision': 'approved', 'comment': '已核对聊天方案'}
        self.post(f"/api/supply/plans/{plan['id']}/decision", decision, 403)
        self.grant()
        self.login('risk-two@test.invalid')
        # A grant does not bypass the currently selected workspace boundary.
        self.assertEqual(self.client.get(f"/api/supply/investigations/{run['id']}").status_code, 404)
        workspace = self.app.state.monitor.config(1)['workspace_id']
        self.client.headers['X-Workspace-Id'] = workspace
        self.assertEqual(self.client.get(f"/api/supply/investigations/{run['id']}").status_code, 200)
        self.post(f"/api/supply/plans/{plan['id']}/decision", {**decision, 'version': -1}, 409)
        self.post(f"/api/supply/plans/{plan['id']}/decision", decision)
        self.client.headers.pop('X-Workspace-Id')

    def test_chat_simulation_replans_when_saved_candidates_are_stale(self):
        self.select()
        thread = self.post('/api/supply/threads', {'factory': 'A', 'requestId': str(uuid.uuid4())})['id']
        def query():
            task = self.post(f'/api/supply/threads/{thread}/messages', {'text': '检查 P001 未来三天缺料', 'scope': {}, 'requestId': str(uuid.uuid4())})
            for _ in range(200):
                row = self.client.get(f"/api/supply/tasks/{task['id']}").json()
                if row['status'] not in ('queued', 'running'):
                    return row
                time.sleep(.02)
            self.fail('计算超时')
        query()
        first = self.post(f'/api/supply/threads/{thread}/messages', {'text': '怎么解决', 'scope': {}, 'requestId': str(uuid.uuid4())})
        link = first['result']['workflow']
        self.event_id = link['riskId']
        for _ in range(200):
            run = self.client.get(f"/api/supply/investigations/{link['runId']}").json()
            if run['status'] == 'awaiting_approval':
                break
            time.sleep(.05)
        self.assertEqual(run['status'], 'awaiting_approval')
        self.inject('2026-10-08T08:00:00+08:00')
        query()
        changed = self.post(f'/api/supply/threads/{thread}/messages', {'text': '哪个方案更好', 'scope': {}, 'requestId': str(uuid.uuid4())})
        self.assertEqual(changed['status'], 'completed', changed)
        self.assertFalse(changed['result']['workflow']['reused'])
        child = self.wait_child(run)
        self.assertNotEqual(child['id'], run['id'])
        self.assertEqual(child['input']['windowDays'], 3)
        old_plan = next(p for p in run['plans'] if p['content']['feasible'])
        self.grant()
        self.login('risk-two@test.invalid')
        self.post(f"/api/supply/plans/{old_plan['id']}/decision", {'version': old_plan['version'], 'contentHash': old_plan['content_hash'], 'decision': 'approved', 'comment': '旧版不应批准'}, 409)

    def test_chat_old_snapshot_cannot_launch_investigation(self):
        self.select()
        thread = self.post('/api/supply/threads', {'factory': 'A', 'requestId': str(uuid.uuid4())})['id']
        task = self.post(f'/api/supply/threads/{thread}/messages', {'text': '检查 P001 未来三天缺料', 'scope': {}, 'requestId': str(uuid.uuid4())})
        for _ in range(200):
            saved = self.client.get(f"/api/supply/tasks/{task['id']}").json()
            if saved['status'] not in ('queued', 'running'):
                break
            time.sleep(.02)
        original = saved['result']
        self.inject('2026-10-08T08:00:00+08:00')
        action = self.post(f'/api/supply/threads/{thread}/messages', {'text': '继续查为什么晚交', 'scope': {}, 'requestId': str(uuid.uuid4())})
        self.assertEqual(action['status'], 'failed', action)
        self.assertIn('重新计算', action['error'])
        self.assertEqual(self.client.get(f"/api/supply/tasks/{task['id']}").json()['result'], original)

    def start_run(self):
        self.select()
        self.post('/api/supply/monitor/scan')
        self.event_id = self.events()[0]['id']
        result = self.post(f'/api/supply/risks/{self.event_id}/investigate', {'requestId': str(uuid.uuid4())})
        for _ in range(100):
            rows = self.client.get(f'/api/supply/risks/{self.event_id}/investigations').json()
            if rows and rows[0]['status'] not in ('queued', 'running'):
                self.assertEqual(rows[0]['status'], 'awaiting_approval', rows)
                return rows[0]
            time.sleep(.05)
        self.fail(f'Timed out {result}')

    def grant(self, limit='10000', factories=None, role='approver'):
        workspace = self.client.get(f'/api/supply/business-access?eventId={self.event_id}').json()['workspaceId']
        self.post('/api/supply/business-access', {'workspaceId': workspace, 'account': 'risk-two@test.invalid', 'role': role, 'factories': factories or ['A', 'B'], 'approvalLimit': limit})

    def wait_child(self, parent):
        for _ in range(100):
            rows = self.client.get(f'/api/supply/risks/{self.event_id}/investigations').json()
            child = next((r for r in rows if r['input'].get('replanning', {}).get('parentRunId') == parent['id']), None)
            if child and child['status'] not in ('queued', 'running'):
                return child
            time.sleep(.05)
        self.fail('Automatic replanning did not finish')

    def reject(self, run):
        plan = next(p for p in run['plans'] if p['content']['feasible'])
        return self.post(f"/api/supply/plans/{plan['id']}/decision", {
            'version': plan['version'], 'contentHash': plan['content_hash'], 'decision': 'rejected', 'comment': '请核查并更换方案'})

    def test_replan_limit_stops_chain(self):
        run = self.start_run()
        self.app.state.workflow.env['SUPPLY_MAX_REPLANS'] = '1'
        self.grant()
        self.login('risk-two@test.invalid')
        self.reject(run)
        child = self.wait_child(run)
        self.assertEqual(child['status'], 'awaiting_approval')
        self.reject(child)
        self.app.state.workflow.executor.shutdown(wait=True)
        with connection(self.app.state.workflow.env) as db:
            saved = self.app.state.workflow.run_record(db, child['id'])
            self.assertEqual(saved['status'], 'manual_required')
            self.assertIn('上限', saved['error'])
            self.assertEqual(db.execute('SELECT count(*) AS n FROM sc.investigations WHERE event_id=%s', (run['event_id'],)).fetchone()['n'], 2)

    def test_replan_stops_when_all_alternatives_rejected(self):
        run = self.start_run()
        self.grant()
        self.login('risk-two@test.invalid')
        self.reject(run)
        child = self.wait_child(run)
        self.reject(child)
        last = self.wait_child(child)
        self.assertEqual(last['status'], 'manual_required')
        self.assertFalse(any(p['content']['feasible'] for p in last['plans']))
        self.assertIn('没有可行的新方案', last['report']['stopReason'])

    def test_stale_plan_read_automatically_replans(self):
        run = self.start_run()
        self.inject('2026-10-08T08:00:00+08:00')
        response = self.client.get(f"/api/supply/plans/{run['plans'][0]['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'stale')
        child = self.wait_child(run)
        self.assertNotEqual(child['plans'][0]['content']['snapshotHash'], run['plans'][0]['content']['snapshotHash'])

    def test_recover_pending_replan_and_revoked_requester(self):
        run = self.start_run()
        workflow = self.app.state.workflow
        with connection(workflow.env, False) as db:
            db.execute("UPDATE sc.investigations SET status='replan_required',error='模拟提交后中断' WHERE id=%s", (run['id'],))
        workflow.recover()
        child = self.wait_child(run)
        workflow.recover()
        workflow.executor.shutdown(wait=True)
        with workflow.lock, connection(workflow.env, False) as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM sc.investigations WHERE event_id=%s', (run['event_id'],)).fetchone()['n'], 2)
            db.execute("UPDATE sc.investigations SET status='replan_required' WHERE id=%s", (child['id'],))
        with patch.object(workflow, 'access', side_effect=foundation.APIError(403, 'revoked')):
            workflow.process(child['id'])
        with connection(workflow.env) as db:
            self.assertEqual(workflow.run_record(db, child['id'])['status'], 'manual_required')
            self.assertEqual(db.execute('SELECT count(*) AS n FROM sc.investigations WHERE event_id=%s', (run['event_id'],)).fetchone()['n'], 2)

    def test_durable_approval_and_permissions(self):
        run = self.start_run()
        plan = next(p for p in run['plans'] if p['content']['kind'] == 'transfer')
        path = f"/api/supply/plans/{plan['id']}/decision"
        body = {'version': plan['version'], 'contentHash': plan['content_hash'], 'decision': 'approved', 'comment': 'Checked donor and arrival'}
        self.post(path, body, 403)
        self.grant(limit='100')
        self.login('risk-two@test.invalid')
        self.post(path, body, 403)
        self.login()
        self.grant()
        # Persistent pause survives process/service reconstruction.
        from .main import create_app
        from fastapi.testclient import TestClient
        self.client.__exit__(None, None, None)
        self.app = create_app(self.env)
        self.client = TestClient(self.app).__enter__()
        old = self.app.state.workflow
        with old.lock:
            saved = old.graph.get_state({'configurable': {'thread_id': run['id']}})
        self.assertEqual(saved.next, ('approval',))
        # Rewind to the durable plans boundary, simulating a crash immediately
        # before interrupt is established. Recovery must pause, then resume once.
        with old.lock:
            old.graph.update_state({'configurable': {'thread_id': run['id']}}, {}, as_node='plans')
        old.process(run['id'])
        with old.lock:
            self.assertTrue(old.graph.get_state({'configurable': {'thread_id': run['id']}}).interrupts)
        self.login('risk-two@test.invalid')
        self.post(path, {**body, 'version': 999}, 409)
        approved = self.post(path, body)
        self.assertEqual(self.post(path, body)['id'], approved['id'])
        old.executor.shutdown(wait=True)
        with connection(old.env) as db:
            self.assertEqual(db.execute('SELECT status FROM sc.investigations WHERE id=%s', (run['id'],)).fetchone()['status'], 'approved')
        self.assertEqual(self.client.get(f"/api/supply/plans/{plan['id']}").json()['status'], 'approved')

    def test_sql_model_workflow_persists_evidence_and_trace(self):
        seen = []
        def handler(request):
            body = json.loads(request.content)
            seen.append(body)
            if len(seen) == 1:
                name, args = 'update_plan', {'reason': '开始调查', 'steps': ['核对库存证据']}
            elif len(seen) == 2:
                name, args = 'get_database_schema', {'tables': ['inventory_snapshots']}
            elif len(seen) == 3:
                name, args = 'execute_sql', {'sql': 'SELECT factory_id, SUM(quantity) AS total FROM inventory_snapshots WHERE material_id=%(m)s GROUP BY factory_id', 'parameters': {'m': 'P001'}}
            else:
                evidence = json.loads(body['messages'][-1]['content'])
                name, args = 'submit_report', report_args('已查询授权工厂库存，缺料与可调拨量以确定性方案为准。', [evidence['id']])
            return httpx.Response(200, json={'usage': {'total_tokens': 100}, 'choices': [{'message': {'tool_calls': [
                {'id': str(len(seen)), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}}]})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.app.state.workflow.env.update(SUPPLY_INVESTIGATOR='model', SUPPLY_MODEL='test', SUPPLY_MODEL_API_KEY='test', SUPPLY_MODEL_BASE_URL='https://model.test')
        with patch('backend.investigation_model.httpx.Client', return_value=client):
            run = self.start_run()
        self.assertEqual(run['report']['modelCalls'], 4)
        sql_evidence = next(e for e in run['report']['evidence'] if e['id'].startswith('sql:'))
        self.assertEqual(run['report']['modelSummary']['evidenceIds'], [sql_evidence['id']])
        self.assertEqual({r['factory_id'] for r in sql_evidence['record']['rows']}, {'A', 'B'})
        self.assertEqual(run['report']['trace'][2]['parameters'], {'m': 'P001'})
        self.assertEqual(run['report']['trace'][2]['scope']['workspace'], run['workspace_id'])
        calls = self.client.get(f"/api/supply/investigations/{run['id']}/tool-calls")
        self.assertEqual(calls.status_code, 200)
        self.assertEqual([c['status'] for c in calls.json()], ['completed'] * 4)
        self.assertEqual(calls.json()[2]['result']['id'], run['report']['modelSummary']['evidenceIds'][0])

    def test_changed_snapshot_and_revocation(self):
        run = self.start_run()
        plan = run['plans'][0]
        self.grant()
        self.inject('2026-10-08T08:00:00+08:00')
        self.login('risk-two@test.invalid')
        self.post(f"/api/supply/plans/{plan['id']}/decision", {'version': plan['version'], 'contentHash': plan['content_hash'], 'decision': 'approved', 'comment': 'test'}, 409)
        self.wait_child(run)
        self.login()
        self.grant(role='revoked')
        self.login('risk-two@test.invalid')
        self.assertEqual(self.client.get(f"/api/supply/plans/{plan['id']}").status_code, 403)

    def test_report_save_rechecks_domain_and_scope(self):
        run = self.start_run()
        workflow = self.app.state.workflow
        state = workflow.graph.get_state({'configurable': {'thread_id': run['id']}}).values
        from .investigation_harness import InvestigationHarness
        harness = InvestigationHarness(workflow.data, run['id'], lambda: workflow.authorize_investigation(state, run))
        harness.check(state['report']['evidence'], state['bundle']['candidates'])
        with workflow.data.lock:
            workflow.data.db.execute("UPDATE business_roles SET domains='[]' WHERE workspace_id=? AND account=?",
                                     (run['workspace_id'], run['requested_by']))
        with self.assertRaises(PermissionError):
            harness.check(state['report']['evidence'], state['bundle']['candidates'])
        self.assertTrue(workflow.plans(state)['blocked'])
        with connection(workflow.env) as db:
            saved = workflow.run_record(db, run['id'])
        self.assertEqual(saved['status'], 'manual_required')

    def test_evidence_reader_checks_snapshot_membership_and_revocation(self):
        run = self.start_run()
        workflow = self.app.state.workflow
        state = workflow.graph.get_state({'configurable': {'thread_id': run['id']}}).values
        identifier = state['evidence'][0]['id']
        self.assertEqual(workflow.read_evidence(state, run, identifier)['id'], identifier)
        # A live read of historical evidence must remain within the explicit parent.
        sql_evidence = {'id': 'sql:parent-only', 'table': 'sql_query', 'record': {
            'rows': [{'quantity': '800'}], 'scope': workflow.authorize_investigation(state, run)}}
        from psycopg.types.json import Jsonb
        with connection(workflow.env, False) as db:
            saved = {**run['report'], 'evidence': run['report']['evidence'] + [sql_evidence]}
            db.execute('UPDATE sc.investigations SET report=%s WHERE id=%s', (Jsonb(saved), run['id']))
        child = {**run, 'id': 'test-child', 'input': {**run['input'], 'replanning': {'parentRunId': run['id']}}}
        self.assertEqual(workflow.read_evidence(state, child, sql_evidence['id'])['id'], sql_evidence['id'])
        with connection(workflow.env, False) as db:
            saved.pop('snapshotHash')
            db.execute('UPDATE sc.investigations SET report=%s WHERE id=%s', (Jsonb(saved), run['id']))
        with self.assertRaisesRegex(ValueError, '历史证据快照'):
            workflow.read_evidence(state, child, sql_evidence['id'])
        with self.assertRaises(ValueError):
            workflow.read_evidence(state, run, 'sql:unrelated')
        changed = {**state, 'bundle': {**state['bundle'], 'snapshotHash': 'different'}}
        with self.assertRaisesRegex(ValueError, '快照已变化'):
            workflow.read_evidence(changed, run, identifier)
        with workflow.data.lock:
            workflow.data.db.execute("UPDATE business_roles SET domains='[]' WHERE workspace_id=? AND account=?",
                                     (run['workspace_id'], run['requested_by']))
        with self.assertRaises((PermissionError, APIError)):
            workflow.read_evidence(state, run, identifier)

    def test_factory_scope_rejection_and_replan(self):
        run = self.start_run()
        plan = run['plans'][0]
        self.grant(factories=['A'])
        self.login('risk-two@test.invalid')
        self.assertEqual(self.client.get(f"/api/supply/plans/{plan['id']}").status_code, 403)
        self.assertEqual(self.client.get(f'/api/supply/risks/{self.event_id}/investigations').json(), [])
        self.assertEqual(len(self.client.get('/api/supply/business-risks').json()), 1)
        self.post(f'/api/supply/risks/{self.event_id}/investigate', {'requestId': str(uuid.uuid4())}, 403)
        self.login()
        self.grant()
        self.login('risk-two@test.invalid')
        body = {'version': plan['version'], 'contentHash': plan['content_hash'], 'decision': 'rejected', 'comment': 'Please recheck source inventory'}
        self.post(f"/api/supply/plans/{plan['id']}/decision", body)
        self.login()
        for _ in range(100):
            rows = self.client.get(f'/api/supply/risks/{self.event_id}/investigations').json()
            if rows[0]['id'] != run['id'] and rows[0]['status'] not in ('queued', 'running'):
                break
            time.sleep(.05)
        child = rows[0]
        self.assertEqual(child['status'], 'awaiting_approval')
        self.assertEqual(child['input']['replanning']['parentRunId'], run['id'])
        self.assertEqual(child['input']['replanning']['reason'], body['comment'])
        self.assertEqual(rows[1]['status'], 'replanned')
        rejected = next(p for p in child['plans'] if p['content']['kind'] == plan['content']['kind'])
        self.assertFalse(rejected['content']['feasible'])
        self.assertEqual(self.app.state.workflow.replan(run['id']), child['id'])
        self.app.state.workflow.process(run['id'])
        self.assertEqual(len(self.client.get(f'/api/supply/risks/{self.event_id}/investigations').json()), 2)
        self.post(f"/api/supply/investigations/{child['id']}/cancel")
        key = str(uuid.uuid4())
        started = self.post(f'/api/supply/risks/{self.event_id}/investigate', {'requestId': key})
        self.assertEqual(self.post(f'/api/supply/risks/{self.event_id}/investigate', {'requestId': key})['id'], started['id'])


def report_args(statement='已核对库存', refs=None, kind='observed', limitation='', status='complete', questions=None):
    return {'status': status, 'claims': [{'statement': statement, 'kind': kind,
             'evidenceIds': refs or ['inventory_snapshots:stock'], 'limitation': limitation}],
            'unresolvedQuestions': questions or [], 'nextSteps': []}


class AgentTests(unittest.TestCase):
    def run_agent(self, calls, usages=None, replanning=None, initial_plan=True, env=None, evidence=None, evidence_reader=None, candidates=None):
        if initial_plan:
            calls = [('update_plan', {'reason': '开始调查', 'steps': ['读取证据', '提交报告']})] + calls
            if usages:
                usages = [100] + usages
        seen = []
        def handler(request):
            index = len(seen)
            seen.append(json.loads(request.content))
            name, args = calls[index]
            return httpx.Response(200, json={'usage': {'total_tokens': usages[index] if usages else 100}, 'choices': [{'message': {'tool_calls': [{'id': f'call-{index}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}}]})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        if evidence is None:
            evidence = [{'id': 'inventory_snapshots:stock', 'table': 'inventory_snapshots', 'record': {'quantity': '400'}}]
        with patch('backend.investigation_model.httpx.Client', return_value=client):
            report = investigate({'target': None, 'missing': [], 'candidates': candidates or [], 'replanning': replanning}, evidence,
                {'SUPPLY_INVESTIGATOR': 'model', 'SUPPLY_MODEL': 'test', 'SUPPLY_MODEL_API_KEY': 'fake-test-key', 'SUPPLY_MODEL_BASE_URL': 'https://model.test', 'SUPPLY_AGENT_MAX_ROUNDS': '8', **(env or {})},
                evidence_reader=evidence_reader)
        return report, seen

    def test_cited_tool_report_and_unknown_usage(self):
        report, calls = self.run_agent([('get_inventory', {}), ('submit_report', report_args())], [None, 100])
        self.assertEqual(report['modelSummary']['evidenceIds'], ['inventory_snapshots:stock'])
        self.assertIsNone(report['tokens'])
        self.assertEqual(len(calls), 3)
        self.assertTrue(report['promptVersion'].startswith('investigator-v2-'))
        self.assertFalse(report.get('manualRequired'))

    def test_fabricated_evidence_budget_and_replay(self):
        report, calls = self.run_agent([('submit_report', report_args(refs=['unread:source']))] * 3)
        self.assertTrue(report['manualRequired'])
        self.assertEqual(len(calls), 4)
        report, calls = self.run_agent([(name, {}) for name in ('get_inventory', 'get_production_plan', 'get_supply_evidence', 'get_candidate_plans')], env={'SUPPLY_AGENT_MAX_ROUNDS': '5'})
        self.assertEqual(len(calls), 5)
        self.assertEqual([t['function']['name'] for t in calls[-1]['tools']], ['submit_report'])
        self.assertTrue(report['manualRequired'])
        with patch('backend.investigation_model.httpx.Client') as client:
            report = investigate({'target': None, 'missing': [], 'candidates': []}, [], {'SUPPLY_INVESTIGATOR': 'model'}, model_allowed=False)
            client.assert_not_called()
            self.assertTrue(report['manualRequired'])

    def test_invalid_report_replans_and_reads_missing_evidence(self):
        report, calls = self.run_agent([
            ('submit_report', report_args(refs=['unread:source'])),
            ('update_plan', {'reason': '缺少已读取证据', 'steps': ['读取库存', '提交引用说明']}),
            ('get_inventory', {}),
            ('submit_report', report_args('已补查库存')),
        ])
        self.assertFalse(report.get('manualRequired'))
        self.assertEqual(report['planRevisions'][1]['reason'], '缺少已读取证据')
        self.assertTrue(json.loads(calls[2]['messages'][-1]['content'])['error']['retryable'])
        self.assertEqual(report['modelCalls'], 5)
        self.assertEqual(report['modelSummary']['evidenceIds'], ['inventory_snapshots:stock'])

    def test_invalid_tool_is_corrected_without_execution(self):
        report, calls = self.run_agent([
            ('unknown_tool', {}), ('get_inventory', {}),
            ('submit_report', report_args('已修正工具')),
        ])
        self.assertFalse(report.get('manualRequired'))
        self.assertIn('error', json.loads(calls[2]['messages'][-1]['content']))

    def test_replanning_feedback_reaches_model(self):
        feedback = {'round': 1, 'reason': '来源库存需核查', 'parentRunId': 'INV-parent'}
        report, calls = self.run_agent([('get_inventory', {}), ('submit_report', report_args('已重新核查库存'))], replanning=feedback)
        self.assertEqual(json.loads(calls[0]['messages'][1]['content'])['replanning'], feedback)
        self.assertFalse(report.get('manualRequired'))

    def test_transport_failure_is_not_retried(self):
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout('unknown response')
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch('backend.investigation_model.httpx.Client', return_value=client):
            report = investigate({'target': None, 'missing': [], 'candidates': []}, [],
                {'SUPPLY_INVESTIGATOR': 'model', 'SUPPLY_MODEL': 'test', 'SUPPLY_MODEL_API_KEY': 'test'})
        self.assertEqual(len(calls), 1)
        self.assertTrue(report['manualRequired'])

    def test_plan_gate_is_enforced_even_when_model_ignores_tools(self):
        report, calls = self.run_agent([
            ('get_inventory', {}),
            ('update_plan', {'reason': '先规划', 'steps': ['读取库存']}),
            ('get_inventory', {}), ('submit_report', report_args()),
        ], initial_plan=False)
        self.assertEqual([s['function']['name'] for s in calls[0]['tools']], ['update_plan'])
        self.assertEqual(report['trace'][0]['status'], 'error')
        self.assertEqual(report['status'], 'complete')

    def test_partial_requires_reason_and_does_not_invalidate_feasible_plans(self):
        from .investigation_report import plan_blockers
        report, _ = self.run_agent([('get_inventory', {}), ('submit_report', report_args(
            status='partial', questions=['尚未取得晚交原因的独立证据']))])
        self.assertEqual(report['status'], 'partial')
        bundle = {'target': {'computable': True}, 'candidates': [{'feasible': True}]}
        self.assertEqual(plan_blockers(report, bundle), [])
        bundle['target']['computable'] = False
        self.assertTrue(plan_blockers(report, bundle))
        bundle['target']['computable'] = True
        bundle['candidates'][0]['feasible'] = False
        self.assertTrue(plan_blockers(report, bundle))

    def test_claim_validation_and_legacy_fields_are_derived(self):
        from project.agents.report_protocol import validate_report
        evidence = {'inventory_snapshots:stock': {'record': {'truncated': True}}}
        for args in (report_args(), report_args(kind='reported'), report_args(kind='inferred'),
                     report_args(status='partial'), report_args(refs=['fake'])):
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_report(args, evidence)
        result = validate_report(report_args(kind='reported', limitation='仅确认其如此报告，结果被截断'), evidence)
        self.assertEqual(result['summary'], result['claims'][0]['statement'])
        self.assertEqual(result['evidenceIds'], ['inventory_snapshots:stock'])
        with self.assertRaises(ValueError):
            validate_report({'summary': '旧协议不可用于新提交', 'evidenceIds': ['inventory_snapshots:stock']}, evidence)

    def test_final_round_can_submit_partial_without_evidence(self):
        partial = {'status': 'partial', 'claims': [], 'unresolvedQuestions': ['未获得有效证据。'], 'nextSteps': ['补查库存']}
        report, calls = self.run_agent([('submit_report', partial)], env={'SUPPLY_AGENT_MAX_ROUNDS': '2'})
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['modelSummary']['claims'], [])
        self.assertTrue(report['manualRequired'])
        self.assertEqual([s['function']['name'] for s in calls[-1]['tools']], ['submit_report'])

    def test_near_token_budget_reserves_report_call(self):
        report, calls = self.run_agent([('get_inventory', {}), ('submit_report', report_args())],
            usages=[700, 50], env={'SUPPLY_AGENT_MAX_TOKENS': '1000'})
        self.assertEqual(report['status'], 'complete')
        self.assertEqual([s['function']['name'] for s in calls[-1]['tools']], ['submit_report'])

    def test_time_budget_reserves_partial_report(self):
        partial = {'status': 'partial', 'claims': [], 'unresolvedQuestions': ['时间不足，尚未取证'], 'nextSteps': []}
        # Clock calls: start, first-loop, request timeout, response, second-loop, timeout, response.
        with patch('project.agents.investigator.time.monotonic', side_effect=[0, 0, 0, 1, 11, 11, 12]):
            report, calls = self.run_agent([('submit_report', partial)], env={'SUPPLY_AGENT_MAX_SECONDS': '15'})
        self.assertEqual(report['status'], 'partial')
        self.assertEqual([s['function']['name'] for s in calls[-1]['tools']], ['submit_report'])

    def test_transport_failure_preserves_completed_evidence(self):
        report, _ = self.run_agent([('get_inventory', {})])  # Next mocked response raises: no network retry.
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['evidence'][0]['id'], 'inventory_snapshots:stock')
        self.assertEqual(report['trace'][-1]['tool'], 'get_inventory')
        self.assertEqual(report['modelSummary']['claims'], [])

    def test_snapshot_truncation_is_visible_to_model(self):
        evidence = [{'id': f'inventory_snapshots:{i}', 'table': 'inventory_snapshots', 'record': {'quantity': str(i)}} for i in range(21)]
        report, calls = self.run_agent([('get_inventory', {}), ('submit_report', report_args(
            refs=['inventory_snapshots:0'], limitation='仅查看了部分结果'))], evidence=evidence)
        tool_result = json.loads(calls[2]['messages'][-1]['content'])
        self.assertTrue(tool_result['truncated'])
        self.assertEqual(len(tool_result['items']), 20)
        self.assertEqual(report['status'], 'complete')

    def test_evidence_expansion_and_candidate_results_are_citable(self):
        evidence = {'id': 'sql:prior', 'table': 'sql_query', 'record': {'rows': [{'quantity': '800'}]}}
        reads = []
        def reader(identifier):
            reads.append(identifier)
            if identifier != evidence['id']:
                raise ValueError('不在关联调查内')
            return evidence
        report, _ = self.run_agent([('get_evidence', {'evidenceId': 'sql:prior'}),
            ('submit_report', report_args(refs=['sql:prior']))], evidence_reader=reader)
        self.assertEqual(reads, ['sql:prior'])
        self.assertEqual(report['status'], 'complete')
        self.assertIn(evidence, report['evidence'])
        report, calls = self.run_agent([('get_candidate_plans', {}),
            ('submit_report', report_args('已读取后端预计算方案', ['candidate:0']))],
            candidates=[{'kind': 'transfer', 'totalCost': '800', 'feasible': True}])
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(json.loads(calls[2]['messages'][-1]['content'])['items'][0]['record']['totalCost'], '800')
