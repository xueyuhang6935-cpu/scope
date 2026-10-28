"""Demo 闭环：真实 HTTP、版本/撤权/自批、事务幂等和时点隔离复核。"""
import json
import unittest
from concurrent.futures import ThreadPoolExecutor

from . import test_logistics_report as support
from .logistics_collaboration import LogisticsCollaboration
from scripts.reference_logistics_metrics import reference


class CollaborationTests(unittest.TestCase):
    tearDown = support.ReportTests.tearDown
    login = support.ReportTests.login
    create = support.ReportTests.create
    new_run = support.ReportTests.new_run

    def setUp(self):
        support.ReportTests.setUp(self)
        self.app.state.logistics_runs.process(self.app.state.logistics_runs.claim())
        self.service = self.app.state.logistics_collaboration
        self.path = f'/api/logistics/runs/{self.run}/collaboration'

    def read(self):
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def post(self, action, body=None, expected=200):
        response = self.client.post(self.path+'/'+action, json={'version': self.read()['version'], **(body or {})})
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def propose(self, note='联系快递员核查优先任务'):
        return self.post('propose', {'strategy': 'contact_courier', 'note': note})

    def decision(self, state, result='approved', reason='范围和证据已核对'):
        proposal = state['proposals'][-1]
        return {'version': state['version'], 'proposal_version': proposal['proposal_version'],
                'proposal_hash': proposal['proposal_hash'], 'decision': result, 'reason': reason}

    def approved(self):
        state = self.propose()
        self.login('reviewer')
        response = self.client.post(self.path+'/decide', json=self.decision(state))
        self.assertEqual(response.status_code, 200, response.text)
        self.login('reader')
        return response.json()

    def receipted(self):
        self.approved()
        self.post('followup')
        return self.post('receipt', {'result': 'recorded', 'simulated': True,
                                     'note': '仅模拟核查完成', 'reference': ''})

    def test_feedback_rejection_changed_revision_approval_receipt_review_and_persistence(self):
        state = self.post('feedback', {'kind': 'reported', 'text': '演示陈述：现场人员不足', 'task_limit': 2, 'simulated': True})
        self.assertEqual(state['feedback'][0]['verification'], 'unverified')
        self.assertEqual(state['task_limit'], 5)  # 未验证陈述不能改变结构化约束。
        self.post('feedback', {'kind': 'constraint', 'text': '先核查两条任务', 'task_limit': 2, 'simulated': True})
        state = self.propose()
        self.assertEqual(len(state['proposals'][-1]['content']['tasks']), 2)
        self.login('reviewer')
        rejected = self.client.post(self.path+'/decide', json=self.decision(state, 'rejected', '先核对窗口，暂不联系快递员'))
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertIsNone(rejected.json()['followup'])
        self.login('reader')
        self.post('propose', {'strategy': 'contact_courier', 'note': '联系快递员核查优先任务'}, expected=409)
        revised = self.post('propose', {'strategy': 'verify_window', 'note': '先核对窗口和两条任务'})
        self.assertEqual(revised['proposals'][-1]['previous_rejection']['reason'], '先核对窗口，暂不联系快递员')
        self.login('reviewer')
        approved = self.client.post(self.path+'/decide', json=self.decision(revised))
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertIsNone(approved.json()['followup'])
        self.assertEqual(self.client.get('/api/logistics/approvals').json()['rows'][0]['status'], 'approved')
        self.login('reader')
        followup = self.post('followup')
        self.assertFalse(followup['followup']['external_dispatch'])
        receipt = self.post('receipt', {'result': 'recorded', 'simulated': True, 'note': '模拟内部核查结果', 'reference': ''})
        self.assertEqual(receipt['followup']['status'], 'simulated')
        new_id = self.create(business_as_of='2000-07-02 10:30:00').json()['snapshot_id']
        reviewed = self.post('review', {'snapshot_id': new_id})
        result = reviewed['review']
        old = reference(self.records, '2000-07-02 10:00:00')
        new = reference(self.records, '2000-07-02 10:30:00')
        old_risk = [task for task in old['tasks'] if task['risk_state'] != 'normal']
        pending = set(new['order'])
        self.assertEqual(result['old_risk_queue']['total'], len(old_risk))
        self.assertEqual(result['old_risk_queue']['completed'], sum(task['order_id'] not in pending for task in old_risk))
        inflow = [task for task in self.records if '07-02 10:00:00' < task['accept_time'] <= '07-02 10:30:00']
        self.assertEqual(result['new_inflow']['total'], len(inflow))
        self.assertEqual(result['new_inflow']['metrics']['backlog']['value'], reference(inflow, '2000-07-02 10:30:00')['backlog'])
        self.assertFalse(result['attribution_supported'])
        self.assertEqual(result['after_metrics']['backlog']['value'], new['backlog'])
        self.assertEqual(self.app.state.logistics_runs.saved(self.run)['status'], 'completed')
        LogisticsCollaboration(self.app.state.logistics_reports)
        self.assertEqual(self.read(), reviewed)
        events = self.client.get(f'/api/logistics/runs/{self.run}/events').json()['rows']
        self.assertTrue(any(event['kind'] == 'collaboration_review' for event in events))

    def test_todo_full_counts_role_filters_latest_state_and_pagination(self):
        state = self.propose()
        for unused in range(6):
            self.records[0]['order_id'] = f'TODO-{unused}'  # 独立内容快照，不复用同一调查家族预算。
            run_id = self.new_run(key=f'todo-{unused}')
            self.app.state.logistics_runs.process(self.app.state.logistics_runs.claim())
            self.service.mutate(self.app.state.auth.query("SELECT * FROM users WHERE account='reader'"),
                                run_id, 'propose', {'version': 0, 'strategy': 'contact_courier', 'note': '核对待办分页'})
        response = self.client.get('/api/logistics/approvals/todos?page_size=2').json()
        self.assertEqual({item['kind']: item['count'] for item in response['categories']},
                         {'my_pending': 7, 'needs_changes': 0, 'awaiting_receipt': 0, 'awaiting_review': 0})
        self.assertEqual(response['total_rows'], 7)
        self.assertEqual(len(response['rows']), 2)
        self.assertEqual(len(self.client.get('/api/logistics/approvals/todos?page=4&page_size=2').json()['rows']), 1)
        self.login('reviewer')
        response = self.client.get('/api/logistics/approvals/todos?kind=to_review').json()
        self.assertEqual(response['categories'], [{'kind': 'to_review', 'name': '我可审核的建议', 'count': 7}])
        self.assertEqual(self.client.post(self.path+'/decide', json=self.decision(state, 'rejected', '需要修改')).status_code, 200)
        self.login('reader')
        response = self.client.get('/api/logistics/approvals/todos?kind=needs_changes').json()
        self.assertEqual(response['total_rows'], 1)
        self.assertEqual(response['rows'][0]['run_id'], self.run)
        self.assertEqual({item['kind']: item['count'] for item in response['categories']}['my_pending'], 6)
        self.assertEqual(self.client.get('/api/logistics/approvals/todos?kind=wrong').status_code, 400)
        self.assertEqual(self.client.get('/api/logistics/approvals/todos?region_ids=6').status_code, 400)
        self.data.db.execute("INSERT INTO logistics_grants VALUES('shanghai-logistics','reader','approver','Shanghai','[\"5\"]')")
        response = self.client.get('/api/logistics/approvals/todos?kind=to_review').json()
        self.assertEqual(response['total_rows'], 0)  # 具有审核角色也不能自批
        self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'")
        response = self.client.get('/api/logistics/approvals/todos').json()
        self.assertTrue(all(item['count'] == 0 for item in response['categories']))
        self.assertEqual(response['rows'], [])

    def test_self_approval_stale_version_hash_duplicate_and_concurrent_decision(self):
        # 即便发起人同时拥有审核权仍然不能自批。
        self.data.db.execute("INSERT INTO logistics_grants VALUES('shanghai-logistics','reader','approver','Shanghai','[\"5\"]')")
        state = self.propose()
        self.assertFalse(state['can_approve'])
        decision = self.decision(state)
        self.assertEqual(self.client.post(self.path+'/decide', json=decision).status_code, 403)
        self.login('reviewer')
        self.assertEqual(self.client.post(self.path+'/decide', json={**decision, 'proposal_hash': 'wrong'}).status_code, 409)
        reviewer = self.app.state.auth.query("SELECT * FROM users WHERE account='reviewer'")
        opposite = self.decision(state, 'rejected', '需要修订')
        def attempt(body):
            from .common import APIError
            try:
                self.service.mutate(reviewer, self.run, 'decide', body)
                return 200
            except APIError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [decision, opposite]))
        self.assertEqual(sorted(results), [200, 409])
        successful = decision if results[0] == 200 else opposite
        version = self.read()['version']
        self.assertEqual(self.client.post(self.path+'/decide', json=successful).status_code, 200)
        self.assertEqual(self.read()['version'], version)
        self.assertEqual(self.client.post(self.path+'/decide', json={**successful, 'reason': '不同请求'}).status_code, 409)

    def test_feedback_invalidates_pending_and_revocation_blocks_read_decision_followup(self):
        state = self.propose()
        body = self.decision(state)
        changed = self.post('feedback', {'kind': 'constraint', 'text': '只核查一条', 'task_limit': 1, 'simulated': True})
        self.assertEqual(changed['proposals'][0]['status'], 'invalidated')
        self.login('reviewer')
        self.assertEqual(self.client.post(self.path+'/decide', json=body).status_code, 409)
        self.login('reader')
        self.propose('只核查第一条任务')
        current = self.read()
        self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reviewer'")
        self.login('reviewer')
        self.assertEqual(self.client.get(self.path).status_code, 403)
        self.assertEqual(self.client.get('/api/logistics/approvals').json()['rows'], [])
        self.assertEqual(self.client.post(self.path+'/decide', json=self.decision(current)).status_code, 403)
        self.data.db.execute("UPDATE logistics_grants SET regions='[\"5\"]' WHERE account='reviewer'")
        self.login('reader')
        self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'")
        self.assertEqual(self.client.get(self.path).status_code, 403)

    def test_receipt_requires_approval_no_duplicate_and_real_reference(self):
        self.post('followup', expected=409)
        self.approved()
        self.post('feedback', {'kind': 'constraint', 'text': '改写批准内容', 'task_limit': 1, 'simulated': True}, expected=409)
        body = {'version': self.read()['version']}
        first = self.client.post(self.path+'/followup', json=body)
        second = self.client.post(self.path+'/followup', json=body)
        self.assertEqual(first.json(), second.json())
        self.post('receipt', {'result': 'recorded', 'simulated': False, 'note': '人工记录', 'reference': ''}, expected=400)
        receipt = self.post('receipt', {'result': 'recorded', 'simulated': False, 'note': '人工记录，尚未外部核验', 'reference': 'MANUAL-1'})
        self.assertEqual(receipt['followup']['status'], 'reported_executed')
        self.assertEqual(receipt['followup']['receipt']['verification'], 'unverified_external_report')
        self.post('receipt', {'result': 'not_executed', 'simulated': True, 'note': '不能覆盖', 'reference': ''}, expected=409)

    def test_review_rejects_old_scope_and_missing_cohort_and_keeps_future_hidden(self):
        state = self.receipted()
        self.post('review', {'snapshot_id': self.app.state.logistics_runs.saved(self.run)['snapshot_id']}, expected=409)
        new_id = self.create(business_as_of='2000-07-02 10:30:00').json()['snapshot_id']
        saved = self.data.one('SELECT metadata_json FROM logistics_snapshots WHERE snapshot_id=?', (new_id,))
        metadata = json.loads(saved['metadata_json'])
        changed = {**metadata, 'data_version': {'changed': True}}
        self.data.db.execute('UPDATE logistics_snapshots SET metadata_json=? WHERE snapshot_id=?', (json.dumps(changed), new_id))
        self.post('review', {'snapshot_id': new_id}, expected=409)
        self.data.db.execute('UPDATE logistics_snapshots SET metadata_json=? WHERE snapshot_id=?', (saved['metadata_json'], new_id))
        row = self.data.one('SELECT * FROM logistics_snapshot_rows WHERE snapshot_id=? ORDER BY row_index LIMIT 1', (new_id,))
        self.data.db.execute('DELETE FROM logistics_snapshot_rows WHERE snapshot_id=? AND row_index=?', (new_id, row['row_index']))
        self.post('review', {'snapshot_id': new_id}, expected=409)
        self.assertEqual(self.read()['version'], state['version'])
        original_rows = self.app.state.snapshots.read_all(self.user, self.app.state.logistics_runs.saved(self.run)['snapshot_id'])[1]
        for task in original_rows:
            if not task['completion_observed']:
                self.assertNotIn('pickup_time_reference', task)

    def test_boundary_validation_and_approved_reviewer_revocation(self):
        for action, body in [('feedback', {'kind': 'constraint', 'text': '说明', 'task_limit': 0, 'simulated': True}),
                             ('propose', {'strategy': [], 'note': '说明'})]:
            self.post(action, body, expected=400)
        self.assertEqual(self.client.post(self.path+'/followup', json={'version': 0, 'region_ids': ['6']}).status_code, 400)
        self.approved()
        self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reviewer'")
        self.post('followup', expected=403)


if __name__ == '__main__':
    unittest.main()
