import tempfile
import json
import httpx
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from .auth import now, password_hash
from .main import create_app
from .supply_model import propose_with_model
from .supply_analysis import propose, request_questions, validate_plan
from .common import APIError


class SupplyTests(unittest.TestCase):
    def test_followup_answers_persist_without_query_and_inherit_scope(self):
        self.import_data()
        thread = self.thread()
        first = self.finish(self.message(thread))
        with patch('backend.supply.analyze') as analyze:
            answer = self.message(thread, '这个结果怎么算的')
            again = self.message(thread, '为什么')
            analyze.assert_not_called()
        self.assertEqual(answer['status'], 'completed')
        self.assertEqual(answer['result']['basedOnTaskId'], first['id'])
        self.assertIn(first['result']['summary'], answer['result']['answer'])
        self.assertEqual(again['result']['basedOnTaskId'], first['id'])
        self.assertEqual(self.client.get(f"/api/supply/tasks/{answer['id']}").json()['result'], answer['result'])
        changed = self.finish(self.message(thread, '那P012呢'))
        self.assertEqual(changed['plan']['intent'], 'inventory')
        self.assertEqual(changed['plan']['material'], 'P012')
        fresh = self.finish(self.message(thread, '检查数据质量'))
        self.assertEqual(fresh['plan']['material'], '')

    def test_followup_no_context_failure_and_factory_conflict(self):
        empty = self.message(self.thread(), '这个结果怎么算的')
        self.assertIn('哪个问题', empty['result']['answer'])
        thread = self.thread()
        with patch('backend.supply.analyze', side_effect=TimeoutError):
            failed = self.finish(self.message(thread))
        answer = self.message(thread, '这个结果怎么算的')
        self.assertIn('分析失败', answer['result']['answer'])
        self.assertEqual(answer['result']['basedOnTaskId'], failed['id'])
        conflict = self.message(thread, '换成B工厂')
        self.assertEqual(conflict['status'], 'awaiting_confirmation')
        self.assertIn('冲突', ' '.join(conflict['planning']['questions']))

    def test_model_followup_receives_evidence_and_recent_answers(self):
        self.import_data()
        thread = self.thread()
        first = self.finish(self.message(thread))
        self.message(thread, '为什么')
        service = self.client.app.state.supply
        service.env.update(SUPPLY_PLANNER='model', SUPPLY_MODEL='test-model', SUPPLY_MODEL_API_KEY='test-only')
        with patch('backend.supply.propose_with_model', return_value=(first['plan'], {'action': 'answer', 'answer': '建议核对来源批次。', 'understood': True, 'questions': []})) as model, patch('backend.supply.analyze') as analyze:
            result = self.finish(self.message(thread, '这个结果怎么算的'))
            analyze.assert_not_called()
        context = model.call_args.args[3]
        self.assertEqual(context['previous']['id'], first['id'])
        self.assertEqual(len(context['recent']), 2)
        self.assertEqual(context['previous']['result']['records'][0]['material'], 'P001')
        self.assertEqual(context['focus']['taskId'], first['id'])
        self.assertTrue(context['evidenceLimited'])
        self.assertEqual(result['result']['answer'], '建议核对来源批次。')

    def test_focused_record_after_first_five_and_legacy_report_context(self):
        from project.context.investigation import compact_task, focus_context
        rows = [{'factory': 'A', 'material': f'P{i:03}', 'quantity': i} for i in range(10)]
        task = compact_task({'id': 'task', 'text': '查P009', 'status': 'completed',
            'plan_json': json.dumps({'factory': 'A', 'material': 'P009'}), 'result_json': json.dumps({'records': rows}),
            'source_json': json.dumps({'snapshotHash': 'saved'})})
        self.assertEqual(task['result']['records'][0]['material'], 'P009')
        old = {'id': 'old', 'report': {'facts': [], 'modelSummary': {'summary': '旧报告', 'evidenceIds': ['old:id']}},
               'plans': [{'id': 'p', 'approval': None}]}
        context = focus_context({'previous': task, 'recent': []}, old)
        self.assertEqual(context['confirmedClaims'], [])
        self.assertEqual(context['focus']['snapshot'], {'snapshotHash': 'saved'})

    def test_report_memory_keeps_open_questions_and_rejection_without_promoting_hypotheses(self):
        from project.context.investigation import focus_context
        previous = {'id': 'task', 'plan': {}, 'source': {}, 'result': {}}
        run = {'id': 'run', 'report': {'status': 'partial', 'stopReason': '原因未查清', 'facts': [],
            'modelSummary': {'claims': [{'kind': 'reported', 'statement': '供应商称设备故障', 'evidenceIds': ['s']}],
                             'unresolvedQuestions': ['需要独立证明']}},
            'plans': [{'id': 'p', 'approval': {'decision': 'rejected', 'comment': '请核对来源工厂库存'}}]}
        context = focus_context({'previous': previous, 'recent': []}, run)
        self.assertEqual(context['confirmedClaims'], [])
        self.assertIn('需要独立证明', context['openQuestions'])
        self.assertEqual(context['rejectedPlans'][0]['reason'], '请核对来源工厂库存')

    def test_workflow_context_rechecks_live_permissions(self):
        service = self.client.app.state.supply
        context = {'focus': {'runId': 'protected-run'}, 'recent': [], 'previous': None}
        with patch.object(service.workflow, 'read_run', side_effect=APIError(403, '权限撤销')) as read:
            with self.assertRaises(APIError):
                service.check_context({'workspace_id': 'local-1'}, 1, context)
        read.assert_called_once()

    def test_context_snapshot_boundary_and_revoked_history_permissions(self):
        thread_id = self.thread()
        first = self.finish(self.message(thread_id))
        service = self.client.app.state.supply
        user = service.auth.query('SELECT id,account FROM users WHERE id=?', (1,))
        thread = service.thread(thread_id, 1)
        context = service.conversation_context(thread, user, {'scenarioId': 'different', 'asOf': 'later', 'days': 3})
        self.assertIsNone(context['previous'])
        self.assertEqual(context['recent'], [])
        service.env.update(SUPPLY_PLANNER='model', SUPPLY_MODEL='test-model', SUPPLY_MODEL_API_KEY='test-only')
        with patch.object(service.executor, 'submit'):
            task = self.message(thread_id, '这个结果怎么算的')
        original = service.check_plan
        def deny_history(actor, workspace, plan):
            if plan['material'] == first['plan']['material']:
                raise APIError(403, '历史数据权限已撤销')
            return original(actor, workspace, plan)
        with patch.object(service, 'check_plan', side_effect=deny_history), patch('backend.supply.propose_with_model') as model:
            service.plan(task['id'], 1)
            model.assert_not_called()
        self.assertEqual(service.data.one('SELECT status FROM supply_tasks WHERE id=?', (task['id'],))['status'], 'failed')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = {"AUTH_DB_PATH": str(Path(self.temp.name) / "auth.sqlite"), "DATA_DB_PATH": str(Path(self.temp.name) / "supply.sqlite"), "NODE_ENV": "test", "SUPPLY_PLANNER": "rules"}
        self.client = TestClient(create_app(self.env)).__enter__()
        for name in ("one", "two"):
            self.client.app.state.auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'email',?,?,?)", (f"{name}@test.invalid", name, password_hash("password123"), now()))
        self.login("one")

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def login(self, name):
        self.assertEqual(self.client.post("/api/auth/login", json={"account": f"{name}@test.invalid", "password": "password123"}).status_code, 200)

    def import_data(self, factory="A", content="material,quantity,unit\nP001,12.5,件\nP012,3,箱", strategy="insert"):
        draft = self.client.post("/api/data/imports/preview", data={"domain": "inventory", "factory": factory, "owner": "测试"}, files={"file": ("inventory.csv", content.encode())}).json()["draftId"]
        self.client.post(f"/api/data/imports/{draft}/check", json={"mapping": {"material": "material", "quantity": "quantity", "unit": "unit"}})
        result = self.client.post(f"/api/data/imports/{draft}/commit", json={"strategy": strategy, "businessKey": "material", "idempotencyKey": str(uuid.uuid4()), "isolateWarnings": True})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()["id"]

    def thread(self, factory="A"):
        return self.client.post("/api/supply/threads", json={"factory": factory, "requestId": str(uuid.uuid4())}).json()["id"]

    def message(self, thread, text="查询 P001 库存", **scope):
        result = self.client.post(f"/api/supply/threads/{thread}/messages", json={"text": text, "requestId": str(uuid.uuid4()), "scope": scope})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def finish(self, task):
        if task['status'] == 'awaiting_confirmation':
            result = self.client.post(f"/api/supply/tasks/{task['id']}/confirm", json={"confirmed": True, "plan": task["plan"]})
            self.assertEqual(result.status_code, 200, result.text)
        for _ in range(100):
            result = self.client.get(f"/api/supply/tasks/{task['id']}").json()
            if result["status"] not in ("planning", "queued", "running"):
                return result
            time.sleep(.01)
        self.fail("task did not finish")

    def test_inventory_quality_readiness_and_evidence_snapshot(self):
        batch = self.import_data()
        thread = self.thread()
        task = self.message(thread)
        self.assertEqual(task["status"], "queued")
        self.assertIsNone(task["result"])
        done = self.finish(task)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["result"]["records"][0]["normalized_quantity"], "12.5")
        self.assertEqual(done["result"]["records"][0]["batch_id"], batch)
        quality = self.finish(self.message(thread, "检查数据质量"))
        self.assertEqual(quality["result"]["issueCount"], 1)
        ready = self.message(thread, "检查未来三天缺料风险")
        self.assertEqual(ready["status"], "awaiting_confirmation")
        self.assertIn('尚未接入', ready['planning']['questions'][0])
        self.client.post(f"/api/supply/tasks/{ready['id']}/cancel", json={})
        self.import_data(content="material,quantity,unit\nP001,99,件", strategy="upsert")
        saved = self.client.get(f"/api/supply/tasks/{done['id']}").json()
        self.assertEqual(saved["result"]["records"][0]["normalized_quantity"], "12.5")
        self.assertEqual(len(self.client.get(f"/api/supply/threads/{thread}").json()["tasks"]), 3)

    def test_ownership_factory_idempotency_and_input_boundaries(self):
        self.import_data()
        thread = self.thread()
        body = {"text": "查询 P001 库存", "requestId": str(uuid.uuid4()), "scope": {}}
        first = self.client.post(f"/api/supply/threads/{thread}/messages", json=body).json()
        second = self.client.post(f"/api/supply/threads/{thread}/messages", json=body).json()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.client.post(f"/api/supply/threads/{thread}/messages", json={**body, "text": "其他问题"}).status_code, 409)
        self.assertEqual(self.client.post(f"/api/supply/tasks/{first['id']}/confirm", json={"confirmed": True, "plan": {**first["plan"], "factory": "B"}}).status_code, 400)
        self.client.post(f"/api/supply/tasks/{first['id']}/cancel", json={})
        self.assertEqual(self.client.post(f"/api/supply/threads/{thread}/messages", json={**body, "scope": {"material": []}}).status_code, 400)
        self.login("two")
        self.assertEqual(self.client.get(f"/api/supply/threads/{thread}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/supply/tasks/{first['id']}").status_code, 404)
        self.assertEqual(self.client.post(f"/api/supply/tasks/{first['id']}/cancel", json={}).status_code, 404)
        self.assertEqual(self.client.get('/api/supply/threads').json(), [])
        self.assertEqual(self.finish(self.message(self.thread()))["status"], "no_data")
        self.login("one")
        self.assertEqual(self.finish(self.message(self.thread("B")))["status"], "no_data")
        self.assertEqual(self.client.post('/api/supply/threads', json={"factory": "A", "requestId": str(uuid.uuid4())}, headers={"origin": "https://untrusted.invalid"}).status_code, 403)

    def test_conflict_confirmation_unsupported_scope_and_failure(self):
        thread = self.thread()
        task = self.message(thread, "查询未来一周 P001 库存", period="未来三天")
        self.assertEqual(task["plan"]["period"], "未来三天")
        self.assertEqual(task['status'], 'awaiting_confirmation')
        self.assertIsNone(task['result'])
        self.assertIn('手动范围不同', ' '.join(task['planning']['questions']))
        self.assertEqual(self.client.post(f"/api/supply/tasks/{task['id']}/confirm", json={'confirmed': True, 'plan': task['plan']}).status_code, 400)
        task['plan']['period'] = ''
        self.assertEqual(self.finish(task)['status'], 'no_data')
        with patch('backend.supply.analyze', side_effect=TimeoutError):
            result = self.finish(self.message(thread))
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["result"])
        self.assertEqual(self.finish(self.message(thread))["status"], "no_data")

    def test_cancel_and_restart_recovery(self):
        thread = self.thread()
        with patch.object(self.client.app.state.supply.executor, 'submit'):
            task = self.message(thread)
        cancelled = self.client.post(f"/api/supply/tasks/{task['id']}/cancel", json={}).json()
        self.assertEqual(cancelled["status"], "cancelled")
        self.client.app.state.supply.run(task["id"], 1)
        self.assertIsNone(self.client.get(f"/api/supply/tasks/{task['id']}").json()["result"])
        with patch.object(self.client.app.state.supply.executor, 'submit'):
            next_task = self.message(thread)
        self.client.__exit__(None, None, None)
        self.client = TestClient(create_app(self.env)).__enter__()
        self.login("one")
        self.assertEqual(self.client.get(f"/api/supply/tasks/{next_task['id']}").json()["status"], "failed")

    def test_model_proposal_persisted_and_duplicate_not_rebilled(self):
        service = self.client.app.state.supply
        service.env.update(SUPPLY_PLANNER='model', SUPPLY_MODEL='test-model', SUPPLY_MODEL_API_KEY='test-only')
        thread = self.thread()
        proposal = {"intent": "inventory", "factory": "A", "material": "P001", "supplier": "", "period": ""}
        with patch('backend.supply.propose_with_model', return_value=(proposal, {"tokens": 12, "mode": "model"})) as model:
            body = {"requestId": str(uuid.uuid4()), "text": "请调阅P001现存量", "scope": {}}
            first = self.client.post(f'/api/supply/threads/{thread}/messages', json=body).json()
            for _ in range(100):
                task = self.client.get(f"/api/supply/tasks/{first['id']}").json()
                if task['status'] != 'planning':
                    break
                time.sleep(.01)
            self.assertEqual(task['status'], 'awaiting_confirmation')
            self.assertIsNone(task['result'])
            self.assertEqual(task['planning']['tokens'], 12)
            self.assertEqual(self.client.post(f'/api/supply/threads/{thread}/messages', json=body).json()['id'], first['id'])
            model.assert_called_once()

    def test_clear_queries_skip_model_and_confirmation(self):
        self.import_data()
        service = self.client.app.state.supply
        service.env.update(SUPPLY_PLANNER='model', SUPPLY_MODEL='test-model', SUPPLY_MODEL_API_KEY='test-only')
        with patch('backend.supply.propose_with_model') as model:
            task = self.message(self.thread(), '帮我查一下 A厂 P001 当前库存有多少？')
            self.assertEqual(task['status'], 'queued')
            done = self.finish(task)
            self.assertEqual(done['status'], 'completed')
            self.assertEqual(done['planning']['execution'], 'automatic')
            self.assertEqual(done['result']['scope']['material'], 'P001')
            model.assert_not_called()

    def test_model_understood_query_runs_but_cannot_drop_conditions(self):
        service = self.client.app.state.supply
        service.env.update(SUPPLY_PLANNER='model', SUPPLY_MODEL='test-model', SUPPLY_MODEL_API_KEY='test-only')
        proposal = {'intent': 'inventory', 'factory': 'A', 'material': 'P001', 'supplier': '', 'period': ''}
        metadata = {'mode': 'model', 'tokens': 12, 'understood': True, 'questions': []}
        for text, status in [('请调阅P001现存量', 'no_data'), ('删除P001库存', 'awaiting_confirmation'),
                             ('查询昨天P001库存', 'awaiting_confirmation'), ('P002现存量', 'awaiting_confirmation')]:
            with self.subTest(text=text), patch('backend.supply.propose_with_model', return_value=(proposal.copy(), metadata.copy())):
                task = self.message(self.thread(), text)
                done = self.finish(task)
                self.assertEqual(done['status'], status)
                if status == 'awaiting_confirmation':
                    self.assertIsNone(done['result'])

    def test_missing_intent_is_clarified_then_executed(self):
        task = self.message(self.thread(), 'P001')
        self.assertEqual(task['status'], 'awaiting_confirmation')
        self.assertIn('分析类型', ' '.join(task['planning']['questions']))
        task['plan']['intent'] = 'inventory'
        done = self.finish(task)
        self.assertEqual(done['status'], 'no_data')
        self.assertEqual(done['planning']['execution'], 'clarified')

    def test_unsupported_conditions_never_queue_queries(self):
        with patch('backend.supply.analyze') as analyze:
            for text in ('查询 P001 和 P002 库存', '查询昨天 P001 库存', '查询B厂库存', '查询供应商甲库存'):
                with self.subTest(text=text):
                    task = self.message(self.thread(), text)
                    self.assertEqual(task['status'], 'awaiting_confirmation')
                    self.assertTrue(task['planning']['questions'])
                    self.assertIsNone(task['result'])
            analyze.assert_not_called()

    def test_chat_refresh_requeries_and_keeps_original_result(self):
        self.import_data()
        thread = self.thread()
        first = self.finish(self.message(thread))
        original = first['result']
        with patch('backend.supply.analyze', wraps=__import__('backend.supply', fromlist=['analyze']).analyze) as query:
            refreshed = self.finish(self.message(thread, '重新查询'))
            self.assertEqual(query.call_count, 1)
        self.assertNotEqual(refreshed['id'], first['id'])
        self.assertEqual(refreshed['planning']['action'], 'refresh')
        self.assertEqual(refreshed['result']['basedOnTaskId'], first['id'])
        self.assertIn('重新查询', refreshed['result']['refreshNotice'])
        self.assertEqual(self.client.get(f"/api/supply/tasks/{first['id']}").json()['result'], original)

    def test_explanation_keeps_timestamp_and_approval_is_not_executed(self):
        thread = self.thread()
        first = self.finish(self.message(thread))
        answer = self.message(thread, '这个缺口怎么算的')
        self.assertEqual(answer['result']['evidenceCapturedAt'], first['result']['capturedAt'])
        with patch.object(self.client.app.state.workflow, 'start_investigation') as start:
            approval = self.message(thread, '批准P001调拨')
            execution = self.message(thread, '立即下发调拨单')
            start.assert_not_called()
        self.assertEqual(approval['planning']['action'], 'approval_request')
        self.assertEqual(execution['planning']['action'], 'unsupported_execution')
        self.assertIn('未下发', execution['result']['answer'])

    def test_explicit_basis_cannot_cross_threads(self):
        first = self.finish(self.message(self.thread()))
        other = self.thread()
        response = self.client.post(f'/api/supply/threads/{other}/messages', json={
            'text': '这个结果怎么算的', 'requestId': str(uuid.uuid4()), 'scope': {}, 'basedOnTaskId': first['id']})
        self.assertEqual(response.status_code, 403)

    def test_explanation_cannot_change_material_and_model_cannot_downgrade_refresh(self):
        thread = self.thread()
        first = self.finish(self.message(thread))
        response = self.client.post(f'/api/supply/threads/{thread}/messages', json={
            'text': '解释 P999 库存怎么算的', 'requestId': str(uuid.uuid4()), 'scope': {}})
        self.assertEqual(response.status_code, 400)
        service = self.client.app.state.supply
        service.env.update(SUPPLY_PLANNER='model', SUPPLY_MODEL='test-model', SUPPLY_MODEL_API_KEY='test-only')
        with patch('backend.supply.propose_with_model', return_value=(first['plan'], {'action': 'explain', 'answer': '旧结果', 'understood': True, 'questions': []})), patch('backend.supply.analyze', wraps=__import__('backend.supply', fromlist=['analyze']).analyze) as query:
            refreshed = self.finish(self.message(thread, '重新查询最新现存量'))
        self.assertEqual(refreshed['planning']['action'], 'refresh')
        self.assertNotIn('answer', refreshed['result'])
        self.assertEqual(query.call_count, 1)

    def test_risk_selection_is_exact_and_cannot_become_query(self):
        thread = self.thread('all')
        first = self.finish(self.message(thread))
        service = self.client.app.state.supply
        source = {'scenarioId': 'fixture', 'asOf': '2026-10-05T00:00:00Z', 'days': 3}
        plan = {**first['plan'], 'intent': 'readiness', 'material': ''}
        calculation = {'scenarioId': 'fixture', 'asOf': source['asOf'], 'results': [
            {'factory': f, 'material': 'P001', 'revision': 'R1', 'shortage': '10', 'computable': True} for f in ('A', 'B')]}
        result = {**first['result'], 'scope': plan, 'calculation': calculation}
        service.data.db.execute("UPDATE supply_tasks SET plan_json=?,result_json=?,source_json=?,status='completed' WHERE id=?", (json.dumps(plan), json.dumps(result), json.dumps(source), first['id']))
        with patch.object(service.monitor, 'config', return_value={'scenario_id': 'fixture', 'as_of': source['asOf'], 'horizon_days': 3}), patch.object(service.workflow, 'from_calculation', return_value={'riskId': 'RISK-test', 'runId': 'INV-test', 'reused': False}) as launch:
            # A configured source is necessary; keep isolated SQLite fixtures.
            service.data.db.execute('INSERT INTO risk_monitor_configs(user_id,scenario_id,workspace_id,as_of,horizon_days) VALUES(?,?,?,?,3)', (1, 'fixture', 'local-1', source['asOf']))
            task = self.message(thread, '继续查为什么晚交')
            self.assertEqual(task['status'], 'awaiting_confirmation', task)
            self.assertEqual(len(task['planning']['riskOptions']), 2)
            launch.assert_not_called()
            self.assertEqual(self.client.post(f"/api/supply/tasks/{task['id']}/confirm", json={'confirmed': True, 'plan': plan}).status_code, 400)
            bad = self.client.post(f"/api/supply/tasks/{task['id']}/select-risk", json={'factory': 'A', 'material': 'P999', 'revision': 'R1'})
            self.assertEqual(bad.status_code, 400)
            chosen = self.client.post(f"/api/supply/tasks/{task['id']}/select-risk", json={'factory': 'B', 'material': 'P001', 'revision': 'R1'})
            self.assertEqual(chosen.status_code, 200, chosen.text)
            self.assertEqual(chosen.json()['result']['workflow']['selected']['factory'], 'B')
            repeated = self.client.post(f"/api/supply/tasks/{task['id']}/select-risk", json={'factory': 'B', 'material': 'P001', 'revision': 'R1'})
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(launch.call_count, 1)

    def test_permission_revoked_before_model_does_not_leave_task_planning(self):
        service = self.client.app.state.supply
        service.env.update(SUPPLY_PLANNER='model', SUPPLY_MODEL='test-model', SUPPLY_MODEL_API_KEY='test-only')
        with patch.object(service.executor, 'submit'):
            task = self.message(self.thread(), '请调阅P001现存量')
        with patch.object(service, 'task', side_effect=APIError(403, '权限已撤销')), patch('backend.supply.propose_with_model') as model:
            service.plan(task['id'], 1)
            model.assert_not_called()
        saved = service.data.one('SELECT status,error FROM supply_tasks WHERE id=?', (task['id'],))
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['error'], '权限已撤销')

    def test_model_network_error_never_retries_or_invents_usage(self):
        env = {'SUPPLY_MODEL': 'test-model', 'SUPPLY_MODEL_API_KEY': 'test-only'}
        scope = {'intent': '', 'factory': 'A', 'material': '', 'supplier': '', 'period': ''}
        with patch('backend.supply_model.httpx.Client', side_effect=TimeoutError) as transport:
            proposal, audit = propose_with_model(env, '查询库存', scope)
        transport.assert_called_once()
        self.assertIsNone(proposal)
        self.assertIsNone(audit['tokens'])

    def test_model_semantic_clarification_contract(self):
        scope = validate_plan({}, 'A')
        proposal = {**scope, 'intent': 'inventory', 'material': 'P001', 'clarification': ''}
        response = {'usage': {'total_tokens': 20}, 'choices': [{'message': {'tool_calls': [
            {'function': {'name': 'propose_scope', 'arguments': json.dumps(proposal)}}]}}]}
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
        with patch('backend.supply_model.httpx.Client', return_value=client):
            plan, audit = propose_with_model({'SUPPLY_MODEL': 'test', 'SUPPLY_MODEL_API_KEY': 'test'}, 'P001现存量', scope)
        self.assertEqual(plan['material'], 'P001')
        self.assertTrue(audit['understood'])
        self.assertEqual(audit['questions'], [])


class RequestScopeTests(unittest.TestCase):
    def test_followup_period_and_missing_evidence(self):
        from .supply_context import followup
        from project.context.investigation import grounded_answer
        scope = validate_plan({}, 'A')
        context = {'previous': {'id': 'test', 'plan': {**scope, 'intent': 'readiness', 'material': 'P001', 'period': '未来三天'}, 'status': 'insufficient_data', 'result': {'summary': '数据不足', 'missing': ['生产需求']}}}
        explanation, effective, text = followup('那未来一周呢', scope, context)
        self.assertFalse(explanation)
        self.assertEqual(effective['period'], '未来一周')
        self.assertEqual(effective['material'], 'P001')
        self.assertEqual(request_questions(text, effective, propose(text, effective), {'days': 3}), [])
        self.assertIn('生产需求', grounded_answer(context))

    def test_clear_and_ambiguous_requests(self):
        source = {'days': 3}
        scope = validate_plan({}, 'A')
        cases = [('查询P001库存', False), ('请检查当前工厂数据质量', False), ('检查P001未来3天缺料风险', False),
                 ('查询B厂库存', True), ('查询全部工厂库存', True), ('查询P001和P002库存', True),
                 ('查询未来三天库存', True), ('检查未来一个月缺料风险', True), ('P001', True),
                 ('查询库存并删除记录', True), ('查询除P001以外的库存', True), ('查看库存和质量问题', True)]
        for text, needs_clarification in cases:
            with self.subTest(text=text):
                self.assertEqual(bool(request_questions(text, scope, propose(text, scope), source)), needs_clarification)


if __name__ == '__main__':
    unittest.main()
