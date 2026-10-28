"""T06：HTTP、真实磁盘/进程恢复、租约隔离与执行生命周期。"""
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from .logistics_runs import RunService
from .main import create_app
from . import test_logistics_snapshots as snapshot_tests


class RunTests(unittest.TestCase):
    login = snapshot_tests.SnapshotTests.login
    create = snapshot_tests.SnapshotTests.create
    tearDown = snapshot_tests.SnapshotTests.tearDown

    def setUp(self):
        with patch.object(RunService, 'start'):
            snapshot_tests.SnapshotTests.setUp(self)
        self.runs = self.app.state.logistics_runs
        self.user = self.app.state.auth.query("SELECT * FROM users WHERE account='reader'")
        self.snapshot = self.create().json()['snapshot_id']
        self.body_run = {'snapshot_id': self.snapshot, 'idempotency_key': 'request-1'}

    def new(self, key='request-1'):
        response = self.client.post('/api/logistics/runs', json={**self.body_run, 'idempotency_key': key})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['run_id']

    def until(self, predicate, seconds=5):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail('condition timed out')

    def test_http_idempotency_conflicts_and_cancellation(self):
        run_id = self.new()
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: self.runs.create(self.user, self.body_run)['run_id'], range(12)))
        self.assertEqual(set(ids), {run_id})
        self.assertEqual(len(self.data.all('SELECT * FROM logistics_runs')), 1)
        self.assertEqual(self.client.get('/api/logistics/runs/'+run_id).json()['status'], 'queued')
        other = self.create().json()['snapshot_id']
        self.assertEqual(self.client.post('/api/logistics/runs', json={**self.body_run, 'snapshot_id': other}).status_code, 409)
        for changes in ({'idempotency_key': ''}, {'budget': {}}, {'parent_run_id': 1}, {'status': 'completed'}):
            self.assertEqual(self.client.post('/api/logistics/runs', json={**self.body_run, **changes}).status_code, 400)
        cancelled = self.client.post('/api/logistics/runs/'+run_id+'/cancel', json={}).json()
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertTrue(cancelled['cancel_requested'])
        self.assertIsNone(self.runs.claim())
        self.assertEqual(self.new(), run_id)
        self.assertNotIn('claim_token', cancelled)

    def test_permissions_parent_scope_and_revocation(self):
        run_id = self.new()
        self.login('reviewer')
        self.assertEqual(self.client.get('/api/logistics/runs/'+run_id).status_code, 200)
        self.assertEqual(self.client.post('/api/logistics/runs', json=self.body_run).status_code, 403)
        self.assertEqual(self.client.post('/api/logistics/runs/'+run_id+'/cancel', json={}).status_code, 403)
        self.login('reader')
        child = self.client.post('/api/logistics/runs', json={**self.body_run, 'idempotency_key': 'child', 'parent_run_id': run_id})
        self.assertEqual(child.status_code, 202)
        self.assertEqual(child.json()['parent_run_id'], run_id)
        self.assertEqual(self.client.get('/api/logistics/runs/'+run_id, headers={'X-Workspace-Id': 'other'}).status_code, 403)
        self.data.db.execute("UPDATE logistics_runs SET workspace_id='other' WHERE run_id=?", (run_id,))
        self.assertEqual(self.client.get('/api/logistics/runs/'+run_id).status_code, 403)
        self.data.db.execute("UPDATE logistics_runs SET workspace_id='shanghai-logistics' WHERE run_id=?", (run_id,))
        self.data.db.execute("DELETE FROM logistics_grants WHERE account='reader'")
        self.assertEqual(self.client.get('/api/logistics/runs/'+run_id).status_code, 403)
        claimed = self.runs.claim()
        self.runs.process(claimed)
        self.assertEqual(self.runs.saved(run_id)['error_code'], 'authorization_changed')

    def test_fencing_expired_lease_and_idempotent_schema(self):
        run_id = self.new()
        old = self.runs.claim()
        self.assertIsNone(self.runs.claim())
        self.assertTrue(self.runs.renew(run_id, old['claim_token']))
        self.data.db.execute('UPDATE logistics_runs SET lease_until=? WHERE run_id=?', (time.time()-.1, run_id))
        self.assertFalse(self.runs.commit(run_id, old['claim_token'], 'completed', 'done', {'stale': True}))
        self.assertFalse(self.runs.renew(run_id, old['claim_token']))
        replacement = RunService(self.app.state.snapshots)
        new = replacement.claim()
        self.assertNotEqual(old['claim_token'], new['claim_token'])
        self.assertFalse(self.runs.commit(run_id, old['claim_token'], 'failed', 'validate'))
        replacement.process(new)
        self.assertEqual(self.runs.saved(run_id)['status'], 'completed')

    def test_bounded_queue_heartbeat_disconnect_and_running_cancel(self):
        entered, release = threading.Event(), threading.Event()
        original = self.runs.step
        def blocked(row, metadata):
            entered.set()
            release.wait(5)
            return original(row, metadata)
        self.runs.step = blocked
        self.runs.heartbeat_seconds = .02
        first = self.new()
        second = self.new('request-2')
        self.runs.start()
        try:
            self.assertTrue(entered.wait(3))
            initial = self.runs.saved(first)['heartbeat_at']
            self.until(lambda: self.runs.saved(first)['heartbeat_at'] > initial)
            self.assertGreater(self.runs.saved(first)['active_seconds'], 0)
            self.assertEqual(self.runs.saved(second)['status'], 'queued')
            self.assertEqual(self.runs.saved(second)['active_seconds'], 0)
            # 模拟页面不再连接：完成由服务线程驱动，无浏览器请求。
            self.runs.cancel(self.user, first)
            release.set()
            self.until(lambda: self.runs.saved(second)['status'] == 'completed')
            self.assertEqual(self.runs.saved(first)['status'], 'cancelled')
            self.assertIsNone(self.runs.saved(first)['result_json'])
        finally:
            release.set()
            self.runs.close()

    def test_failed_waiting_budget_and_cancel_waiting(self):
        def failure(*args):
            raise RuntimeError('DO_NOT_PERSIST_SECRET')
        self.runs.step = failure
        failed = self.new()
        self.runs.process(self.runs.claim())
        self.assertEqual(self.runs.saved(failed)['error_code'], 'execution_failed')
        self.assertNotIn('DO_NOT_PERSIST_SECRET', json.dumps(self.runs.saved(failed)))
        waiting = self.new('waiting')
        self.runs.step = lambda *_: ('waiting', 'finish', None)
        self.runs.process(self.runs.claim())
        saved = self.runs.saved(waiting)
        self.assertEqual(saved['status'], 'waiting')
        self.assertIsNone(saved['lease_until'])
        self.assertIsNone(self.runs.claim())
        self.assertEqual(self.runs.saved(waiting)['active_seconds'], saved['active_seconds'])
        self.runs.cancel(self.user, waiting)
        budget = self.new('budget')
        self.data.db.execute('UPDATE logistics_runs SET active_seconds=120 WHERE run_id=?', (budget,))
        self.runs.process(self.runs.claim())
        self.assertEqual(self.runs.saved(budget)['error_code'], 'active_budget_exhausted')

    def test_permission_changed_during_step_blocks_result(self):
        run_id = self.new()
        def revoke(row, metadata):
            self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'")
            return 'completed', 'done', {'should_not_commit': True}
        self.runs.step = revoke
        self.runs.process(self.runs.claim())
        self.assertEqual(self.runs.saved(run_id)['status'], 'failed')
        self.assertIsNone(self.runs.saved(run_id)['result_json'])

    def test_graceful_stop_saves_safe_node_then_new_executor_completes(self):
        run_id = self.new()
        original = self.runs.step
        def stop_at_boundary(row, metadata):
            result = original(row, metadata)
            self.runs.stop.set()
            return result
        self.runs.step = stop_at_boundary
        self.runs.process(self.runs.claim())
        self.assertEqual(self.runs.saved(run_id)['status'], 'queued')
        self.assertEqual(self.runs.saved(run_id)['current_node'], 'finish')
        restarted = RunService(self.app.state.snapshots)
        restarted.start()
        try:
            self.until(lambda: self.runs.saved(run_id)['status'] == 'completed')
        finally:
            restarted.close()

    def test_app_lifespan_recovers_queue_and_stops_threads_before_database(self):
        run_id = self.new()
        restarted = create_app(self.env)
        with TestClient(restarted):
            self.until(lambda: restarted.state.logistics_runs.saved(run_id)['status'] == 'completed')
        self.assertFalse(restarted.state.logistics_runs.worker.is_alive())
        self.assertFalse(restarted.state.logistics_runs.heartbeat.is_alive())
        self.assertEqual(self.runs.saved(run_id)['status'], 'completed')

    def test_heartbeat_budget_fences_blocked_step(self):
        run_id = self.new()
        entered, release = threading.Event(), threading.Event()
        def blocked(*_):
            entered.set()
            release.wait(3)
            return 'completed', 'done', {'late': True}
        self.runs.step = blocked
        self.runs.heartbeat_seconds = .01
        self.runs.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.data.lock:
                self.data.db.execute('UPDATE logistics_runs SET active_seconds=120 WHERE run_id=?', (run_id,))
            self.until(lambda: self.runs.saved(run_id)['status'] == 'failed')
            self.assertEqual(self.runs.saved(run_id)['error_code'], 'active_budget_exhausted')
            release.set()
        finally:
            release.set()
            self.runs.close()
        self.assertIsNone(self.runs.saved(run_id)['result_json'])

    def test_real_process_crash_restart_from_persisted_node(self):
        run_id = self.new()
        code = '''
import json, sys, time
from backend.main import create_app
app = create_app(json.loads(sys.argv[1]))
runs = app.state.logistics_runs
original = runs.step
def safe_boundary(row, metadata):
    if row['current_node'] == 'finish':
        print('SAFE_NODE', flush=True)
        time.sleep(60)
    return original(row, metadata)
runs.step = safe_boundary
runs.start()
'''
        process = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', code, json.dumps(self.env)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        try:
            self.until(lambda: self.runs.saved(run_id)['current_node'] == 'finish', seconds=10)
            self.assertEqual(self.runs.saved(run_id)['status'], 'running')
        finally:
            process.kill()
            process.communicate(timeout=10)
        old_token = self.runs.saved(run_id)['claim_token']
        self.assertIsNone(self.runs.claim())  # 不提前抢占尚未到期的失联租约。
        self.data.db.execute('UPDATE logistics_runs SET lease_until=? WHERE run_id=?', (time.time()-.1, run_id))
        # 真实新进程，只从磁盘已存节点恢复，不调用 validate。
        recover_code = '''
import json, sys, time
from backend.main import create_app
app = create_app(json.loads(sys.argv[1]))
runs = app.state.logistics_runs
original = runs.step
def only_finish(row, metadata):
    assert row['current_node'] == 'finish'
    return original(row, metadata)
runs.step = only_finish
runs.start()
deadline=time.monotonic()+10
while runs.saved(sys.argv[2])['status'] != 'completed' and time.monotonic()<deadline:
    time.sleep(.02)
runs.close()
assert runs.saved(sys.argv[2])['status'] == 'completed'
app.state.auth.db.close()
app.state.data.db.close()
'''
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', recover_code, json.dumps(self.env), run_id],
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.runs.commit(run_id, old_token, 'failed', 'validate'))
        self.assertEqual(self.runs.read(self.user, run_id)['result']['kind'], 'deterministic-report')
        self.assertTrue(self.runs.read(self.user, run_id)['result']['report_id'])


if __name__ == '__main__':
    unittest.main()
