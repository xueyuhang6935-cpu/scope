"""页面迁移：成果访问、引用、原调查关联、审批版本和处理待办。"""
import json
import unittest
from unittest.mock import patch

from . import test_logistics_report as support
from .logistics_chat import ChatService
from .logistics_snapshots import canonical


class PageIntegrationTests(unittest.TestCase):
    tearDown = support.ReportTests.tearDown
    login = support.ReportTests.login
    create = support.ReportTests.create
    new_run = support.ReportTests.new_run

    def setUp(self):
        with patch.object(ChatService, 'start'):
            support.ReportTests.setUp(self)
        self.app.state.logistics_runs.process(self.app.state.logistics_runs.claim())
        self.chat = self.app.state.logistics_chat
        self.service = self.app.state.logistics_collaboration
        self.entry = 'LKNOW-'+self.run
        self.base = '/api/logistics'

    def listing(self, query=''):
        response = self.client.get(self.base+'/knowledge'+query)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_legacy_archive_search_detail_download_and_no_execution(self):
        counts = [len(self.data.all('SELECT * FROM '+table)) for table in ('logistics_runs', 'logistics_tool_calls', 'logistics_conversations')]
        value = self.listing()['rows'][0]
        self.assertEqual(value['knowledge_id'], self.entry)
        self.assertEqual(value['result_status'], 'complete')
        self.assertEqual(self.listing('?search=G&region=5&status=complete')['total_rows'], 1)
        self.assertEqual(self.listing('?search=unmatched')['rows'], [])
        self.assertEqual(self.listing('?reference_from=2000-07-03T10:00')['rows'], [])
        self.assertEqual(self.client.get(self.base+'/knowledge?reference_from=2026-01-01T10:00').status_code, 400)
        detail = self.client.get(self.base+'/knowledge/'+self.entry)
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()['report']['summary']['metrics']['backlog']['value'], 5)
        download = self.client.get(self.base+'/knowledge/'+self.entry+'/download')
        self.assertEqual(download.status_code, 200)
        self.assertIn('2000-07-02 10:00:00', download.text)
        self.assertEqual(download.headers['Cache-Control'], 'no-store')
        destination = self.client.get(self.base+'/runs/'+self.run+'/destination').json()
        self.assertEqual(destination['path'], '/knowledge/'+self.entry)
        self.assertEqual(counts, [len(self.data.all('SELECT * FROM '+table)) for table in ('logistics_runs', 'logistics_tool_calls', 'logistics_conversations')])
        self.listing()
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_knowledge')), 1)

    def test_reference_is_persistent_explainable_idempotent_and_revocable(self):
        self.listing()
        conversation = self.client.post(self.base+'/conversations', json={}).json()
        path = self.base+'/conversations/'+conversation['conversation_id']
        body = {'knowledge_id': self.entry, 'context_version': conversation['version']}
        referenced = self.client.post(path+'/references', json=body)
        self.assertEqual(referenced.status_code, 200, referenced.text)
        self.assertNotIn('run_id', referenced.json()['context'])
        self.assertEqual(referenced.json()['context']['knowledge_ids'], [self.entry])
        self.assertEqual(self.client.post(path+'/references', json=body).json()['version'], referenced.json()['version'])
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_runs')), 1)
        recent, report, evidence, collaboration = self.chat.inputs(self.user, conversation['conversation_id'], referenced.json()['context'])
        self.assertEqual(report['historical_references'][0]['scope']['business_as_of'], '2000-07-02 10:00:00')
        self.assertTrue(evidence)
        self.assertIsNone(collaboration)
        answer = self.client.post(path+'/messages', json={'text': '解释引用成果', 'request_id': 'explain-ref', 'context_version': referenced.json()['version']})
        self.assertEqual(answer.status_code, 202, answer.text)
        with patch.object(self.chat.model, 'generate', return_value={'action': 'reply', 'params': {}, 'reply': '原参考时点观察到逾期。', 'evidence_ids': []}):
            self.chat.process(self.chat.claim())
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_runs')), 1)
        self.login('reviewer')
        self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.get(self.base+'/knowledge/'+self.entry).status_code, 200)
        self.login('reader')
        self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'")
        self.assertEqual(self.listing()['rows'], [])
        self.assertEqual(self.client.get(self.base+'/knowledge/'+self.entry+'/download').status_code, 403)
        self.assertTrue(self.client.get(path).json()['restricted'])
        self.assertTrue(all(row['status'] == 'restricted' for row in self.client.get(path+'/messages').json()['rows']))

    def test_association_preserves_run_and_is_owner_only_and_idempotent(self):
        path = self.base+'/conversations/for-run/'+self.run
        first = self.client.post(path, json={})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.client.post(path, json={}).json()['conversation_id'], first.json()['conversation_id'])
        self.chat.sync()
        messages = self.client.get(self.base+'/conversations/'+first.json()['conversation_id']+'/messages').json()['rows']
        self.assertTrue(any(row['result'] and row['result'].get('report') for row in messages))
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_runs')), 1)
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_conversations')), 1)
        destination = self.client.get(self.base+'/runs/'+self.run+'/destination').json()
        self.assertIn('/assistant/'+first.json()['conversation_id'], destination['path'])
        self.login('reviewer')
        self.assertEqual(self.client.post(path, json={}).status_code, 403)

    def test_approval_history_and_receipt_review_todos_are_real_states(self):
        original = self.service.mutate(self.user, self.run, 'propose', {'version': 0, 'strategy': 'verify_window', 'note': '先核查窗口'})
        reviewer = self.app.state.auth.query("SELECT * FROM users WHERE account='reviewer'")
        proposal = original['proposals'][-1]
        rejected = self.service.mutate(reviewer, self.run, 'decide', {'version': original['version'], 'proposal_version': 1,
            'proposal_hash': proposal['proposal_hash'], 'decision': 'rejected', 'reason': '补充顺序'})
        revised = self.service.mutate(self.user, self.run, 'propose', {'version': rejected['version'], 'strategy': 'verify_window', 'note': '补充窗口核查顺序'})
        self.login('reviewer')
        self.assertEqual(self.client.get(self.base+'/approvals?view=pending').json()['rows'][0]['proposal_version'], 2)
        history = self.client.get(self.base+'/approvals?view=history').json()['rows']
        self.assertEqual(history[0]['proposal_version'], 1)
        self.assertEqual(history[0]['decision']['reason'], '补充顺序')
        self.assertEqual(self.client.get(self.base+'/approvals?view=history&search=missing').json()['total_rows'], 0)
        proposal = revised['proposals'][-1]
        approved = self.service.mutate(reviewer, self.run, 'decide', {'version': revised['version'], 'proposal_version': 2,
            'proposal_hash': proposal['proposal_hash'], 'decision': 'approved', 'reason': '已核对'})
        self.login('reader')
        followup = self.service.mutate(self.user, self.run, 'followup', {'version': approved['version']})
        todos = self.client.get(self.base+'/approvals/todos?kind=awaiting_receipt').json()
        self.assertEqual(todos['total_rows'], 1)
        self.assertIsNone(todos['rows'][0]['conversation_id'])
        linked = self.client.post(self.base+'/conversations/for-run/'+self.run, json={}).json()
        self.assertEqual(self.client.get(self.base+'/approvals/todos?kind=awaiting_receipt').json()['rows'][0]['conversation_id'], linked['conversation_id'])
        receipt = self.service.mutate(self.user, self.run, 'receipt', {'version': followup['version'], 'result': 'recorded', 'simulated': True, 'note': '模拟核查', 'reference': ''})
        self.assertEqual(self.client.get(self.base+'/approvals/todos?kind=awaiting_receipt').json()['total_rows'], 0)
        self.assertEqual(self.client.get(self.base+'/approvals/todos?kind=awaiting_review').json()['total_rows'], 1)
        snapshot = self.create(business_as_of='2000-07-02 10:30:00').json()['snapshot_id']
        self.service.mutate(self.user, self.run, 'review', {'version': receipt['version'], 'snapshot_id': snapshot})
        self.assertEqual(self.client.get(self.base+'/approvals/todos?kind=awaiting_review').json()['total_rows'], 0)

    def test_startup_adopts_actionable_legacy_runs_once_without_execution(self):
        self.service.mutate(self.user, self.run, 'propose', {'version': 0, 'strategy': 'verify_window', 'note': '继续核对'})
        queued = self.new_run('startup-adoption')
        calls = len(self.data.all('SELECT * FROM logistics_tool_calls'))
        self.chat.migrate_pending()
        self.chat.migrate_pending()
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_conversations')), 2)
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_runs')), 2)
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_tool_calls')), calls)
        self.assertEqual(self.app.state.logistics_runs.saved(queued)['status'], 'queued')
        self.assertTrue(self.chat.conversation_for_run(self.user, self.run))
        self.assertTrue(self.chat.conversation_for_run(self.user, queued))

    def test_references_survive_scope_clarification_and_switching_original_run(self):
        self.listing()
        linked = self.client.post(self.base+'/conversations/for-run/'+self.run, json={}).json()
        self.chat.sync()
        path = self.base+'/conversations/'+linked['conversation_id']
        conversation = self.client.get(path).json()
        cited = self.client.post(path+'/references', json={'knowledge_id': self.entry, 'context_version': conversation['version']}).json()
        response = self.client.post(path+'/messages', json={'text': '补充另一个时点', 'request_id': 'clarify-new-scope', 'context_version': cited['version']})
        self.assertEqual(response.status_code, 202, response.text)
        with patch.object(self.chat.model, 'generate', return_value={'action': 'clarify', 'params': {'business_as_of': '2000-07-02 10:30:00'}, 'reply': '请确认区域', 'evidence_ids': []}):
            self.chat.process(self.chat.claim())
        current = self.client.get(path).json()
        self.assertEqual(current['context']['knowledge_ids'], [self.entry])
        self.assertNotIn('run_id', current['context'])
        response = self.client.post(path+'/messages', json={'text': '解释原调查', 'request_id': 'focus-original', 'context_version': current['version'], 'selection': {'run_id': self.run}})
        self.assertEqual(response.status_code, 202, response.text)
        restored = json.loads(self.data.one("SELECT context_json FROM logistics_messages WHERE request_key='user:focus-original'")['context_json'])
        self.assertEqual(restored['knowledge_ids'], [self.entry])
        self.assertEqual(restored['run_id'], self.run)
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_runs')), 1)

    def test_partial_model_result_is_not_mistaken_for_completed_template(self):
        report = self.app.state.logistics_reports.read(self.user, self.run)
        model = {'kind': 'report', 'source': 'program_stop_report', 'scope': report['scope'], 'value': {
            'status': 'partial', 'claims': [], 'evidence_ids': [], 'limitations': ['调查未完整完成'],
            'unresolved_questions': ['现场原因未确认'], 'next_steps': ['核对已有证据']}}
        self.data.db.execute("UPDATE logistics_runs SET mode='model',status='waiting',result_json=? WHERE run_id=?",
                             (canonical({'report': model, 'stop_reason': 'no_progress'}), self.run))
        values = self.listing('?status=partial')['rows']
        self.assertEqual(len(values), 1)
        detail = self.client.get(self.base+'/knowledge/'+self.entry).json()
        self.assertIsNotNone(detail['report'])
        self.assertEqual(detail['result_status'], 'partial')
        self.assertEqual(detail['stop_reason'], 'no_progress')
        self.assertEqual(detail['unresolved_questions'], ['现场原因未确认'])
        self.login('reviewer')
        self.assertEqual(self.client.get(self.base+'/knowledge/'+self.entry).status_code, 200)
        self.login('reader')
        self.data.db.execute("UPDATE logistics_runs SET result_json=? WHERE run_id=?", (canonical({'report': {**model, 'source': 'tampered'}}), self.run))
        self.assertEqual(self.client.get(self.base+'/knowledge/'+self.entry).status_code, 409)


if __name__ == '__main__':
    unittest.main()
