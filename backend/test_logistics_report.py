"""T08：证据审计事务、引用范围、数字复算、下载/页面和持久化。"""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from . import test_logistics_tools as support
from .common import APIError
from .logistics_evidence import EvidenceStore
from .logistics_snapshots import canonical, content_hash
from .main import create_app
from scripts.reference_logistics_metrics import fixture_records, reference


class ReportTests(unittest.TestCase):
    setUp = support.LogisticsToolsTests.setUp
    tearDown = support.LogisticsToolsTests.tearDown
    login = support.LogisticsToolsTests.login
    create = support.LogisticsToolsTests.create
    new_run = support.LogisticsToolsTests.new_run
    call = support.LogisticsToolsTests.call

    def report(self):
        response = self.client.post(f'/api/logistics/runs/{self.run}/report', json={})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_call_audit_fingerprints_task_evidence_and_reconstruction(self):
        result = self.call('get_pending_tasks', {'page_size': 2}).json()
        audit = self.client.get(f'/api/logistics/runs/{self.run}/calls?page_size=1').json()
        row = audit['rows'][0]
        self.assertEqual(row['call_id'], result['call_id'])
        self.assertEqual(row['status'], 'ok')
        self.assertEqual(row['args_summary']['page_size'], 2)
        self.assertGreaterEqual(row['elapsed_ms'], 0)
        self.assertTrue(row['finished_at'])
        record = self.client.get(f"/api/logistics/runs/{self.run}/evidence/{result['result_evidence_id']}").json()
        business = {k: v for k, v in result.items() if k not in ('call_id', 'result_evidence_id', 'result_fingerprint')}
        self.assertEqual(record['record'], business)
        self.assertEqual(record['result_fingerprint'], content_hash(business))
        for task in result['rows']:
            evidence = EvidenceStore(self.app.state.logistics_runs).read(self.user, self.run, task['evidence_id'])
            self.assertEqual(evidence['record']['order_id'], task['order_id'])
            self.assertNotIn('pickup_time', evidence['record'])
        expanded = self.call('get_evidence', {'evidence_id': result['result_evidence_id']}).json()
        self.assertEqual(expanded['rows'][0]['record'], business)
        self.assertEqual(expanded['rows'][0]['source_type'], 'tool_result')

    def test_report_numbers_references_page_download_and_idempotency(self):
        report = self.report()
        expected = reference(self.records, '2000-07-02 10:00:00')
        for key, metric in report['summary']['metrics'].items():
            self.assertEqual(metric['value'], expected[key])
        self.assertEqual([r['order_id'] for r in report['focus_tasks']], expected['order'])
        self.assertEqual(report['task_list']['total_rows'], 5)
        for task in report['focus_tasks']:
            response = self.client.get(f"/api/logistics/runs/{self.run}/evidence/{task['evidence_id']}")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['record']['order_id'], task['order_id'])
        base = f'/api/logistics/runs/{self.run}/report'
        saved = self.data.one('SELECT * FROM logistics_reports WHERE run_id=?', (self.run,))
        self.assertEqual(json.loads(saved['result_json']), report)
        self.assertEqual(saved['result_fingerprint'], content_hash(report))
        self.assertEqual(self.client.get(base).json(), report)
        markdown = self.client.get(base+'/markdown')
        page = self.client.get(base+'/view')
        self.assertEqual(markdown.status_code, 200)
        self.assertEqual(page.status_code, 200)
        self.assertIn('attachment;', markdown.headers['Content-Disposition'])
        self.assertIn('text/markdown', markdown.headers['Content-Type'])
        self.assertEqual(markdown.headers['Cache-Control'], 'no-store')
        self.assertIn('default-src', page.headers['Content-Security-Policy'])
        self.assertIn('样本内积压', page.text)
        self.assertIn(report['summary_evidence_id'], page.text)
        self.assertNotIn('pickup_time', markdown.text)
        self.assertNotIn('ds', markdown.text)
        counts = len(self.data.all('SELECT * FROM logistics_tool_calls'))
        self.assertEqual(self.report(), report)
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_tool_calls')), counts)
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_reports')), 1)

    def test_cross_run_snapshot_workspace_and_live_permissions_block_evidence_download(self):
        report = self.report()
        evidence_id = report['summary_evidence_id']
        second = self.new_run('other')
        self.assertEqual(self.client.get(f'/api/logistics/runs/{second}/evidence/{evidence_id}').status_code, 403)
        self.assertEqual(self.call('get_evidence', {'evidence_id': evidence_id}, second).status_code, 403)
        base = f'/api/logistics/runs/{self.run}'
        self.login('reviewer')
        self.assertEqual(self.client.get(base+'/report/markdown').status_code, 200)
        self.assertEqual(self.client.post(base+'/report', json={}).status_code, 403)
        self.login('reader')
        paths = [base+suffix for suffix in ('/report', '/report/markdown', '/report/view', '/calls', '/evidence/'+evidence_id)]
        for path in paths:
            self.assertEqual(self.client.get(path, headers={'X-Workspace-Id': 'foreign'}).status_code, 403)
        for change, restore in (
                ("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'", "UPDATE logistics_grants SET regions='[\"5\"]' WHERE account='reader'"),
                ("UPDATE business_members SET enabled=0 WHERE account='reader'", "UPDATE business_members SET enabled=1 WHERE account='reader'"),
                ("UPDATE logistics_evidence SET workspace_id='foreign'", "UPDATE logistics_evidence SET workspace_id='shanghai-logistics'")):
            self.data.db.execute(change)
            for path in paths if 'logistics_evidence' not in change else paths[:3]+paths[-1:]:
                self.assertEqual(self.client.get(path).status_code, 403, path)
            self.data.db.execute(restore)
        snapshot = self.app.state.logistics_runs.saved(self.run)['snapshot_id']
        self.data.db.execute('UPDATE logistics_evidence SET snapshot_id=? WHERE evidence_id=?', ('other', evidence_id))
        self.assertEqual(self.client.get(paths[-1]).status_code, 403)
        self.data.db.execute('UPDATE logistics_evidence SET snapshot_id=? WHERE evidence_id=?', (snapshot, evidence_id))
        self.data.db.execute("UPDATE database_source_connections SET batch_id='other'")
        self.assertEqual(self.client.get(base+'/report/markdown').status_code, 409)
        self.data.db.execute("UPDATE database_source_connections SET batch_id='batch'")
        self.app.state.auth.query("DELETE FROM users WHERE account='reader'")
        self.assertEqual(self.client.get(base+'/report/markdown').status_code, 401)

    def test_failed_tool_audit_safe_args_and_atomic_evidence_save(self):
        with patch('backend.logistics_tools.calculate', side_effect=RuntimeError('SECRET_CONNECTION')):
            failed = self.call('get_snapshot_summary').json()
        audit = self.data.one('SELECT * FROM logistics_tool_calls WHERE call_id=?', (failed['call_id'],))
        self.assertEqual(audit['status'], 'error')
        self.assertEqual(audit['error_code'], 'tool_execution_failed')
        self.assertNotIn('SECRET_CONNECTION', json.dumps(self.data.all('SELECT * FROM logistics_evidence')))
        self.call('get_evidence', {'evidence_id': 'SECRET_COOKIE'})
        audit = self.data.all('SELECT * FROM logistics_tool_calls')[-1]
        self.assertEqual(audit['status'], 'error')
        self.assertNotIn('SECRET_COOKIE', audit['args_summary_json'])
        before = len(self.data.all('SELECT * FROM logistics_evidence'))
        original = self.service.evidence_store.save
        counter = [0]
        def fail_after_root(*args):
            original(*args)
            counter[0] += 1
            if counter[0] == 2:
                raise RuntimeError('PERSIST_SECRET')
        with patch.object(self.service.evidence_store, 'save', side_effect=fail_after_root):
            response = self.call('get_pending_tasks')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_evidence')), before)
        self.assertNotIn('PERSIST_SECRET', response.text)
        self.assertTrue(all(r['status'] != 'started' for r in self.data.all('SELECT * FROM logistics_tool_calls')))

    def test_missing_empty_truncation_and_failed_report_not_saved(self):
        self.records[0]['time_window_end'] = ''
        self.run = self.new_run('missing')
        report = self.report()
        self.assertIsNone(report['summary']['metrics']['overdue']['value'])
        self.assertIn('不可计算', self.client.get(f'/api/logistics/runs/{self.run}/report/markdown').text)
        self.assertEqual(report['status'], 'unavailable')
        self.records.clear()
        self.run = self.new_run('empty')
        self.assertEqual(self.report()['status'], 'empty')
        self.records[:] = [{**fixture_records()[0], 'batch_id': 'batch', 'order_id': str(i), 'source_row': i+2} for i in range(25)]
        self.run = self.new_run('many')
        report = self.report()
        self.assertEqual(report['task_list'], {'total_rows': 25, 'returned_rows': 20, 'truncated': True})
        self.assertEqual(report['summary']['metrics']['backlog']['value'], 25)
        self.run = self.new_run('failed')
        with patch('backend.logistics_tools.calculate', side_effect=RuntimeError('FAILED')):
            response = self.client.post(f'/api/logistics/runs/{self.run}/report', json={})
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(self.data.one('SELECT * FROM logistics_reports WHERE run_id=?', (self.run,)))

    def test_disk_lifespan_recovery_and_integrity_failure(self):
        report = self.report()
        ticket = self.service.evidence_store.start(self.run, self.app.state.snapshots.ready_metadata(self.user, report['scope']['snapshot_id']),
                                                  'get_snapshot_summary', {})
        restarted = create_app(self.env)
        with TestClient(restarted):
            self.assertEqual(restarted.state.logistics_reports.read(self.user, self.run), report)
        self.assertEqual(self.data.one('SELECT status FROM logistics_tool_calls WHERE call_id=?', (ticket[0],))['status'], 'interrupted')
        self.data.db.execute("UPDATE logistics_evidence SET payload_json='{}' WHERE evidence_id=?", (report['summary_evidence_id'],))
        self.assertEqual(self.client.get(f'/api/logistics/runs/{self.run}/report/markdown').status_code, 409)

    def test_future_data_does_not_enter_report_evidence_or_exports(self):
        before = self.report()
        for row in self.records:
            row['quality_flags'] = ['FUTURE_SECRET']
            if row['pickup_time'] > '07-02 10:00:00':
                row.update(pickup_time='07-04 18:00:00', ds='0704', pickup_gps_time='07-05 00:00:00',
                           pickup_gps_lng='FUTURE_SECRET', duration_minutes=999, is_late=True)
        self.run = self.new_run('future')
        after = self.report()
        for key in ('summary', 'quality', 'limitations', 'follow_up', 'task_list'):
            self.assertEqual(before[key], after[key])
        self.assertEqual([{k: v for k, v in t.items() if k != 'evidence_id'} for t in before['focus_tasks']],
                         [{k: v for k, v in t.items() if k != 'evidence_id'} for t in after['focus_tasks']])
        evidence = json.dumps(self.data.all('SELECT * FROM logistics_evidence'))
        markdown = self.client.get(f'/api/logistics/runs/{self.run}/report/markdown').text
        page = self.client.get(f'/api/logistics/runs/{self.run}/report/view').text
        for value in (evidence, json.dumps(after), markdown, page):
            self.assertNotIn('FUTURE_SECRET', value)
        self.assertNotIn('pickup_time', json.dumps(after['focus_tasks']))

    def test_report_content_refuses_client_override_and_evidence_mismatch(self):
        base = f'/api/logistics/runs/{self.run}/report'
        self.assertEqual(self.client.post(base, json={'metrics': {'backlog': 0}}).status_code, 400)
        self.assertEqual(self.client.get(base+'/markdown?path=secret').status_code, 400)
        self.assertEqual(self.client.get(base).status_code, 404)
        report = self.report()
        report['summary']['metrics']['backlog']['value'] = 0
        self.data.db.execute('UPDATE logistics_reports SET result_json=?,result_fingerprint=? WHERE run_id=?',
                             (canonical(report), content_hash(report), self.run))
        self.assertEqual(self.client.get(base+'/markdown').status_code, 409)


if __name__ == '__main__':
    unittest.main()
