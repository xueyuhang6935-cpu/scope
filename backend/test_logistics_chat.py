"""连续对话：真实隔离业务服务、事务恢复、预算和跨账号审批。"""
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import json
import time
import unittest
from unittest.mock import patch

from . import test_logistics_report as support
from .common import APIError
from .logistics_chat import ChatService, chat_time, chat_regions
from .logistics_chat_model import ChatModel
from .logistics_runs import RunService
from .main import create_app
from fastapi.testclient import TestClient
from project.agents.logistics_chat import explicit_submit, validate, constraint_parameters
from project.agents.logistics_protocol import ProtocolError


class ChatTests(unittest.TestCase):
    tearDown = support.ReportTests.tearDown
    login = support.ReportTests.login
    create = support.ReportTests.create
    new_run = support.ReportTests.new_run

    def setUp(self):
        with patch.object(ChatService, 'start'):
            support.ReportTests.setUp(self)
        self.chat = self.app.state.logistics_chat
        self.path = '/api/logistics/conversations'
        response = self.client.post(self.path, json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.conversation = response.json()['conversation_id']
        self.count = 0

    def read(self):
        response = self.client.get(self.path+'/'+self.conversation)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def rows(self):
        return self.client.get(self.path+'/'+self.conversation+'/messages').json()['rows']

    def send(self, text, selection=None, command=None):
        self.count += 1
        body = {'text': text, 'request_id': 'request-'+str(self.count), 'context_version': self.read()['version'], 'selection': selection or {}}
        if command:
            body['command'] = command
        response = self.client.post(self.path+'/'+self.conversation+'/messages', json=body)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json(), body

    def process(self, action='reply', params=None, reply='依据已保存证据，原因尚未证实。'):
        candidate = {'action': action, 'params': params or {}, 'reply': reply, 'evidence_ids': []}
        row = self.chat.claim()
        self.assertIsNotNone(row)
        with patch.object(self.chat.model, 'generate', return_value=candidate):
            self.chat.process(row)
        return self.rows()[-1]

    def investigate(self):
        self.send('调查当前区域的揽收风险', {'region_ids': ['5'], 'business_as_of': '2000-07-02 10:00:00'})
        message = self.process('investigate')
        self.assertEqual(message['status'], 'waiting', message)
        run = message['result']['run_id']
        while self.app.state.logistics_runs.saved(run)['status'] == 'queued':
            self.app.state.logistics_runs.process(self.app.state.logistics_runs.claim())
        self.chat.sync()
        self.assertEqual(self.read()['context']['run_id'], run)
        return run

    def draft(self, ids=None):
        self.send('按当前方向生成方案')
        message = self.process('draft', {'strategy': 'verify_window', 'note': '先核查窗口', 'task_ids': ids or self.read()['context']['task_ids']})
        self.assertEqual(message['status'], 'replied', message)
        return message['result']['draft']

    def test_clarification_without_run_and_completed_report_multi_turn(self):
        self.send('调查区域5')
        answer = self.process('clarify', {'region_ids': ['5']}, '请补充完整参考日期和时点。')
        self.assertEqual(answer['status'], 'clarification')
        self.assertNotIn('run_id', self.read()['context'])
        run = self.investigate()
        count = len(self.data.all('SELECT * FROM logistics_tool_calls'))
        self.data.db.execute('UPDATE logistics_runs SET mode=? WHERE run_id=?', ('model', run))
        self.send('把刚才的报告解释得简单一点')
        self.assertEqual(self.process()['status'], 'replied')
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_tool_calls')), count)
        self.assertEqual(self.app.state.logistics_runs.saved(run)['status'], 'completed')
        self.assertTrue(self.rows()[-1]['text'])
        self.chat.sync()
        self.assertEqual(sum(r['result'] is not None and 'report' in r['result'] for r in self.rows()), 1)

    def test_idempotency_after_later_messages_and_invalid_parameters(self):
        original, body = self.send('调查区域5')
        self.process('clarify', {'region_ids': ['5']})
        self.send('我还没有确定日期')
        self.process('clarify')
        response = self.client.post(self.path+'/'+self.conversation+'/messages', json=body)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['message_id'], original['message_id'])
        self.assertEqual(self.client.post(self.path+'/'+self.conversation+'/messages', json={**body, 'text': '不同请求'}).status_code, 409)
        self.assertEqual(self.client.post(self.path+'/'+self.conversation+'/messages', json={**body, 'request_id': 'new', 'selection': {'region_ids': ['6']}}).status_code, 403)
        self.assertEqual(self.client.post(self.path+'/'+self.conversation+'/messages', json={**body, 'request_id': 'new', 'budget': 999}).status_code, 400)

    def test_draft_reorder_submit_reject_modify_approve_and_event_dedup(self):
        run = self.investigate()
        draft = self.draft()
        self.assertEqual(self.client.get('/api/logistics/approvals').json()['rows'], [])
        ids = [t['order_id'] for t in reversed(draft['content']['tasks'])]
        revised = self.draft(ids)
        self.assertEqual([a['order_id'] for a in revised['content']['actions']], ids)
        self.send('提交当前方案')
        self.process('submit', {'draft_version': revised['draft_version']})
        state = self.app.state.logistics_collaboration.read(self.user, run)
        self.assertEqual(state['proposals'][-1]['content'], revised['content'])
        self.login('reviewer')
        self.assertEqual(self.client.get(self.path+'/'+self.conversation).status_code, 403)
        reviewer = self.app.state.auth.query("SELECT * FROM users WHERE account='reviewer'")
        self.assertNotIn('drafts', self.app.state.logistics_collaboration.read(reviewer, run))
        proposal = state['proposals'][-1]
        self.app.state.logistics_collaboration.mutate(reviewer, run, 'decide', {'version': state['version'],
            'proposal_version': proposal['proposal_version'], 'proposal_hash': proposal['proposal_hash'], 'decision': 'rejected', 'reason': '第二条优先并完善说明'})
        self.login('reader')
        self.chat.sync()
        events = len(self.rows())
        self.chat.sync()
        self.assertEqual(len(self.rows()), events)
        self.assertIn('第二条优先', self.rows()[-1]['text'])
        self.send('按审核意见修改说明')
        message = self.process('draft', {'strategy': 'verify_window', 'task_ids': ids, 'note': '根据实际驳回意见补充核查步骤'})
        self.assertEqual(message['result']['draft']['draft_version'], 3)
        self.send('提交这个方案')
        self.process()
        state = self.app.state.logistics_collaboration.read(self.user, run)
        proposal = state['proposals'][-1]
        self.assertEqual(proposal['proposal_version'], 2)
        self.app.state.logistics_collaboration.mutate(reviewer, run, 'decide', {'version': state['version'],
            'proposal_version': 2, 'proposal_hash': proposal['proposal_hash'], 'decision': 'approved', 'reason': '核对完成'})
        self.chat.sync()
        self.assertIn('尚未执行', self.rows()[-1]['text'])

    def test_constraints_block_contact_and_fact_occurrence_time_remains_unverified(self):
        run = self.investigate()
        self.send('现场反馈有人手不足')
        message = self.process('fact', {'note': '现场反馈有人手不足', 'source': '用户现场反馈', 'occurred_at': '2000-07-02 11:00:00'})
        self.assertIn('未经验证', message['text'])
        self.send('暂时不要联系快递员')
        self.process('constraint', {'note': '禁止联系快递员', 'constraint_key': 'contact_courier', 'active': 'no'})
        self.send('生成联系快递员的方案')
        blocked = self.process('draft', {'strategy': 'contact_courier', 'note': '联系快递员'})
        self.assertEqual(blocked['status'], 'failed')
        state = self.app.state.logistics_collaboration.read(self.user, run)
        self.assertEqual(state['feedback'][0]['verification'], 'unverified')
        self.assertEqual(state['feedback'][0]['occurred_at'], '2000-07-02 11:00:00')
        self.assertEqual(state['drafts'], [])

    def test_investigation_budget_exhaustion_does_not_block_chat_but_blocks_followup(self):
        run = self.investigate()
        saved = self.app.state.logistics_runs.saved(run)
        budget = json.loads(saved['budget_json'])
        budget.update(model_calls=12)
        self.data.db.execute('UPDATE logistics_runs SET budget_json=? WHERE run_id=?', (json.dumps(budget), run))
        self.send('解释已有结果')
        self.assertEqual(self.process()['status'], 'replied')
        before = len(self.data.all('SELECT * FROM logistics_runs'))
        self.send('继续补查', {'mode': 'model'})
        self.assertEqual(self.process('followup')['status'], 'failed')
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_runs')), before)
        self.assertEqual(self.app.state.logistics_runs.budgets.read(run)['model_calls'], 12)

    def test_scope_change_late_result_does_not_replace_focus_and_revocation_hides_history(self):
        self.send('调查10点', {'region_ids': ['5'], 'business_as_of': '2000-07-02 10:00:00'})
        first = self.process('investigate')['result']['run_id']
        self.send('调查10点半', {'region_ids': ['5'], 'business_as_of': '2000-07-02 10:30:00'})
        second = self.process('investigate')['result']['run_id']
        while (claimed := self.app.state.logistics_runs.claim()):
            self.app.state.logistics_runs.process(claimed)
        self.chat.sync()
        self.assertEqual(self.read()['context']['run_id'], second)
        self.assertNotEqual(first, second)
        self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'")
        self.assertTrue(self.read()['restricted'])
        self.assertEqual(self.read()['context'], {})
        self.assertTrue(all(r['status'] == 'restricted' for r in self.rows()))
        self.assertTrue(all(r['result'] is None for r in self.rows()))

    def test_pending_recovery_and_unknown_model_request_is_not_replayed(self):
        row, _ = self.send('解释当前情况')
        claimed = self.chat.claim()
        self.chat.reserve(claimed, self.user, 2000)
        self.data.db.execute('UPDATE logistics_messages SET lease_until=0 WHERE message_id=?', (row['message_id'],))
        replacement = ChatService(self.app.state.logistics_collaboration, {})
        with patch.object(replacement.model, 'generate') as model:
            replacement.process(replacement.claim())
        model.assert_not_called()
        self.assertIn('没有自动重放', self.rows()[-1]['text'])
        operation = self.data.one('SELECT * FROM logistics_chat_operations WHERE message_id=?', (row['message_id'],))
        self.assertIsNone(operation['tokens'])
        self.assertEqual(operation['reserved_tokens'], 2000)
        self.send('新问题')
        with patch.object(replacement.model, 'generate', return_value={'action': 'clarify', 'params': {}, 'reply': '请确定范围', 'evidence_ids': []}):
            replacement.process(replacement.claim())
        self.assertEqual(self.rows()[-1]['status'], 'clarification')

    def test_action_recovery_snapshot_and_business_write_are_idempotent(self):
        message, _ = self.send('发起调查', {'region_ids': ['5'], 'business_as_of': '2000-07-02 10:00:00'})
        row = self.chat.claim()
        candidate = {'action': 'investigate', 'params': {}, 'reply': '', 'evidence_ids': []}
        self.data.db.execute("UPDATE logistics_chat_operations SET stage='action_ready',action_json=? WHERE message_id=?", (json.dumps(candidate), message['message_id']))
        op = self.data.one('SELECT input_json FROM logistics_chat_operations WHERE message_id=?', (message['message_id'],))
        self.chat.dispatch(self.user, row, json.loads(op['input_json']), candidate)
        snapshots = len(self.data.all('SELECT * FROM logistics_snapshots'))
        runs = len(self.data.all('SELECT * FROM logistics_runs'))
        self.chat.process(row)
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_snapshots')), snapshots)
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_runs')), runs)
        while (claimed := self.app.state.logistics_runs.claim()):
            self.app.state.logistics_runs.process(claimed)
        self.chat.sync()
        message, _ = self.send('生成草稿')
        row = self.chat.claim()
        candidate = {'action': 'draft', 'params': {'strategy': 'verify_window', 'note': '核查窗口'}, 'reply': '', 'evidence_ids': []}
        self.data.db.execute("UPDATE logistics_chat_operations SET stage='action_ready',action_json=? WHERE message_id=?", (json.dumps(candidate), message['message_id']))
        op = self.data.one('SELECT input_json FROM logistics_chat_operations WHERE message_id=?', (message['message_id'],))
        self.chat.dispatch(self.user, row, json.loads(op['input_json']), candidate)
        self.chat.process(row)
        state = self.app.state.logistics_collaboration.read(self.user, self.read()['context']['run_id'])
        self.assertEqual(len(state['drafts']), 1)

    def test_account_budget_persists_across_conversations(self):
        row, _ = self.send('请求理解')
        claimed = self.chat.claim()
        self.chat.reserve(claimed, self.user, 1000)
        self.chat.usage(claimed, 120, .1, None)
        second = self.client.post(self.path, json={}).json()['conversation_id']
        self.conversation = second
        self.send('另一会话请求理解')
        claimed = self.chat.claim()
        self.chat.daily_calls = 1
        with self.assertRaises(ProtocolError) as error:
            self.chat.reserve(claimed, self.user, 1000)
        self.assertEqual(error.exception.code, 'chat_account_budget')

    def test_explicit_submission_guard_and_protocol_bounds(self):
        self.assertEqual(constraint_parameters('先核查窗口，暂时不要联系快递员', {'active': 'no'})['active'], 'yes')
        self.assertEqual(constraint_parameters('解除联系快递员的限制', {'active': 'yes'})['active'], 'no')
        self.assertEqual(chat_time('2000-07-02 10:00'), '2000-07-02 10:00:00')
        self.assertEqual(chat_regions(['区域5']), ['5'])
        self.assertEqual(chat_regions(['区域6', '5']), ['5', '6'])
        self.assertEqual(chat_time('2000-07-02T10:00:00'), '2000-07-02 10:00:00')
        for value in ('10:00', '2026-07-02 10:00', '2000-02-30 10:00'):
            with self.assertRaises(APIError):
                chat_time(value)
        for text in ('提交当前方案', '提交这个方案', '把这个方案提交审批', '请提交方案'):
            self.assertTrue(explicit_submit(text), text)
        for text in ('不要提交方案', '如果我提交方案呢', '怎么提交方案', '他说“提交方案”', '提交方案？'):
            self.assertFalse(explicit_submit(text), text)
        with self.assertRaises(ProtocolError):
            validate({'action': 'reply', 'params': {'run_id': 'outside'}, 'reply': '', 'evidence_ids': []})
        with self.assertRaises(ProtocolError):
            validate({'action': 'reply', 'params': {}, 'reply': '', 'evidence_ids': ['unknown']})

    def test_report_selection_must_belong_to_conversation(self):
        first = self.investigate()
        self.send('调查新的时点', {'region_ids': ['5'], 'business_as_of': '2000-07-02 10:30:00'})
        second = self.process('investigate')['result']['run_id']
        while (claimed := self.app.state.logistics_runs.claim()):
            self.app.state.logistics_runs.process(claimed)
        self.chat.sync()
        self.send('解释这份报告', {'run_id': first})
        self.process()
        self.assertEqual(self.read()['context']['run_id'], first)
        body = {'text': '读取其他会话调查', 'request_id': 'attack', 'context_version': self.read()['version'], 'selection': {'run_id': self.run}}
        self.assertEqual(self.client.post(self.path+'/'+self.conversation+'/messages', json=body).status_code, 403)
        self.assertNotEqual(first, second)

    def test_feedback_during_investigation_reaches_draft_and_scope_change_resets_constraints(self):
        self.send('先核查窗口', {})
        self.process('constraint', {'note': '先核查窗口', 'constraint_key': 'instruction', 'active': 'yes'})
        run = self.investigate()
        self.assertIn('先核查窗口', self.app.state.logistics_runs.saved(run)['question'])
        draft = self.draft()
        self.assertIn('先核查窗口', draft['content']['feedback'][0]['text'])
        self.send('调查新的时点', {'region_ids': ['5'], 'business_as_of': '2000-07-02 10:30:00'})
        self.process('investigate')
        self.assertEqual(self.read()['context']['constraints'], [])

    def test_revocation_during_model_response_prevents_delivery(self):
        self.investigate()
        self.send('解释报告')
        def generate(*args):
            self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'")
            return {'action': 'reply', 'params': {}, 'reply': 'PRIVATE_MODEL_REPLY', 'evidence_ids': []}
        with patch.object(self.chat.model, 'generate', side_effect=generate):
            self.chat.process(self.chat.claim())
        self.assertNotIn('PRIVATE_MODEL_REPLY', json.dumps(self.rows()))
        self.assertTrue(self.read()['restricted'])

    def test_model_transport_reservation_usage_unknown_failure_and_schema_validation(self):
        calls, records = [], []
        candidate = {'action': 'clarify', 'params': {}, 'reply': '请确认完整时点', 'evidence_ids': []}
        messages = [{'role': 'system', 'content': '输出JSON'}, {'role': 'user', 'content': '调查区域5'}]
        @contextmanager
        def session(env, on_failure=None):
            def request(body, started, seconds):
                self.assertTrue(calls)
                self.assertEqual(body['tool_choice']['function']['name'], 'submit_chat')
                return {'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [{'type': 'function', 'function': {'name': 'submit_chat', 'arguments': json.dumps(candidate)}}]}}], 'usage': {'total_tokens': 100}}
            yield request
        model = ChatModel({'SUPPLY_MODEL': 'deepseek-chat', 'SUPPLY_MODEL_API_KEY': 'fake', 'SUPPLY_MODEL_BASE_URL': 'https://api.deepseek.com'})
        with patch('backend.logistics_chat_model.model_session', session):
            result = model.generate(messages, [], calls.append, lambda *args: records.append(args))
        self.assertEqual(result, candidate)
        self.assertEqual(records[0][0], 100)
        self.assertEqual(len(calls), 1)
        @contextmanager
        def failing(env, on_failure=None):
            def request(*args):
                on_failure({'failure_reason': 'read_timeout', 'exception_type': 'ReadTimeout'})
                raise RuntimeError('SECRET_URL_OR_KEY')
            yield request
        with patch('backend.logistics_chat_model.model_session', failing), self.assertRaises(ProtocolError) as error:
            model.generate(messages, [], calls.append, lambda *args: records.append(args))
        self.assertEqual(error.exception.code, 'model_transport_failed')
        self.assertIsNone(records[-1][0])
        self.assertEqual(records[-1][3]['failure_reason'], 'read_timeout')
        self.assertNotIn('SECRET', str(records))
        self.assertEqual(len(calls), 2)

    def test_partial_report_explanation_keeps_saved_evidence_without_new_calls(self):
        run = self.investigate()
        report = self.app.state.logistics_reports.read(self.user, run)
        scope = self.app.state.logistics_model.context(self.user, run)[0]
        partial = {'scope': scope, 'source': 'program_stop_report', 'value': {
            'evidence_ids': [report['summary_evidence_id']], 'limitations': ['预算耗尽'], 'claims': []}}
        before = len(self.data.all('SELECT * FROM logistics_tool_calls'))
        with patch.object(self.chat, 'saved_report', return_value=partial):
            recent, saved, evidence, state = self.chat.inputs(self.user, self.conversation, self.read()['context'])
        self.assertIn(report['summary_evidence_id'], evidence)
        self.assertEqual(saved, partial)
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_tool_calls')), before)

    def test_running_model_constraint_reuses_existing_versioned_entry_without_replay(self):
        run = self.app.state.logistics_runs.saved(self.run)
        self.data.db.execute("UPDATE logistics_runs SET mode='model' WHERE run_id=?", (self.run,))
        context = {'run_id': self.run, 'snapshot_id': run['snapshot_id'], 'region_ids': ['5'],
                   'business_as_of': '2000-07-02 10:00:00', 'task_ids': []}
        self.data.db.execute('UPDATE logistics_conversations SET context_json=? WHERE conversation_id=?', (json.dumps(context), self.conversation))
        self.send('先核查窗口，暂时不要联系快递员')
        self.assertEqual(self.process()['status'], 'replied')
        workflow = self.app.state.logistics_workflow
        first = workflow.latest_constraint(self.run)
        self.assertIn('不要联系快递员', first['instruction'])
        operation = self.read()['context']['constraints'][-1]['id']
        workflow.constrain(self.user, self.run, {'instruction': '只核查窗口'}, operation_id='next')
        repeated = workflow.constrain(self.user, self.run, {'instruction': first['instruction']}, operation_id=operation)
        self.assertEqual(repeated['version'], first['version'])
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_plan_constraints WHERE run_id=?', (self.run,))), 2)

    def test_app_restart_recovers_queued_message_from_disk(self):
        self.send('调查区域5')
        self.client.__exit__(None, None, None)
        self.app = create_app(self.env)
        with patch.object(RunService, 'start'), patch.object(ChatService, 'start'):
            self.client = TestClient(self.app).__enter__()
        self.data = self.app.state.data
        self.chat = self.app.state.logistics_chat
        self.login('reader')
        self.assertEqual(self.rows()[0]['status'], 'saved')
        self.assertEqual(self.process('clarify', {'region_ids': ['5']})['status'], 'clarification')
        self.assertEqual(self.read()['context']['region_ids'], ['5'])

    def test_new_scope_clarification_does_not_mix_previous_report(self):
        self.investigate()
        self.send('换成区域6调查，先确认条件')
        with self.data.lock:
            self.data.db.execute("UPDATE logistics_grants SET regions='[\"5\",\"6\"]' WHERE account='reader'")
        message = self.process('clarify', {'region_ids': ['6']})
        self.assertEqual(message['status'], 'clarification')
        context = self.read()['context']
        self.assertEqual(context['region_ids'], ['6'])
        self.assertNotIn('run_id', context)

    def test_known_scope_panel_conflict_does_not_modify_draft(self):
        self.investigate()
        self.send('修改原方案', {'business_as_of': '2000-07-02 10:30:00'})
        message = self.process('draft', {'strategy': 'verify_window', 'note': '核查窗口'})
        self.assertEqual(message['status'], 'clarification')
        self.assertIn('范围', message['text'])
        self.assertEqual(self.app.state.logistics_collaboration.read(self.user, self.read()['context']['run_id'])['drafts'], [])

    def test_saved_counterevidence_and_open_questions_survive_recent_message_limit(self):
        run = self.investigate()
        report = self.app.state.logistics_reports.read(self.user, run)
        self.data.db.execute("UPDATE logistics_runs SET mode='model' WHERE run_id=?", (run,))
        fact = {'statement': '窗口缺失不能确认原因', 'evidence_ids': [report['quality_evidence_id']]}
        investigation = {'state': {'report': {'value': {'limitations': ['历史样本限制'], 'unresolved_questions': ['现场原因未核验']}},
            'observations': [{'value': {'counterevidence': [fact]}}]}}
        with patch.object(self.app.state.logistics_workflow, 'read', return_value=investigation):
            recent, saved, evidence, state = self.chat.inputs(self.user, self.conversation, self.read()['context'])
        self.assertEqual(saved['investigation_history']['counterevidence'], [fact])
        self.assertEqual(saved['investigation_history']['unresolved_questions'], ['现场原因未核验'])


if __name__ == '__main__':
    unittest.main()
