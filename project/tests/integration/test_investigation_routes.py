"""HTTP delegation and transaction ordering without a source PostgreSQL database."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.common import APIError, reply
from backend.investigation_routes import routes
from backend.investigation_service import InvestigationService
from backend.data_store import DataStore
from backend.supply_workflow import SupplyWorkflow


class RouteTests(unittest.TestCase):
    def test_routes_delegate_authenticated_user_and_request_fields(self):
        service = Mock()
        user = {'id': 1, 'account': 'reader'}
        service.monitor.authorize.return_value = user
        service.permissions.workspace.return_value = 'w'
        app = FastAPI()

        @app.middleware('http')
        async def body(request: Request, next_handler):
            request.state.body = await request.json() if request.method == 'POST' else {}
            return await next_handler(request)

        @app.exception_handler(APIError)
        async def api_error(request, error):
            return reply(error.body, error.status)

        app.include_router(routes(service))
        cases = [
            ('GET', '/investigations/r', 'read_run', (user, 'r', 'w'), {}),
            ('GET', '/business-access?eventId=e', 'access_info', (user, 'e'), {}),
            ('POST', '/business-access', 'set_member', (user, {'account': 'member'}), {'account': 'member'}),
            ('POST', '/risks/e/investigate', 'start_investigation', (user, 'e', 'key'), {'requestId': 'key'}),
            ('GET', '/risks/e/investigations', 'runs', (user, 'e'), {}),
            ('GET', '/investigations/r/tool-calls', 'tool_calls', (user, 'r'), {}),
            ('POST', '/investigations/r/cancel', 'cancel', (user, 'r'), {}),
            ('GET', '/approvals', 'approval_list', (user,), {}),
            ('GET', '/business-risks', 'shared_events', (user,), {}),
            ('GET', '/plans/p', 'plan_detail', (user, 'p'), {}),
            ('POST', '/plans/p/decision', 'decide', (user, 'p', {'decision': 'rejected'}), {'decision': 'rejected'}),
        ]
        with TestClient(app) as client:
            for verb, path, name, args, payload in cases:
                with self.subTest(path=path):
                    method = getattr(service, name)
                    method.return_value = {'delegated': name}
                    response = client.request(verb, '/api/supply' + path, **({'json': payload} if verb == 'POST' else {}))
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json(), {'delegated': name})
                    method.assert_called_once_with(*args)
                    method.reset_mock()
                    service.monitor.authorize.side_effect = APIError(403, 'denied')
                    response = client.request(verb, '/api/supply' + path, **({'json': payload} if verb == 'POST' else {}))
                    self.assertEqual(response.status_code, 403)
                    method.assert_not_called()
                    service.monitor.authorize.side_effect = None


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = InvestigationService.__new__(InvestigationService)
        self.service.env = {}
        self.service.lock = threading.RLock()
        self.service.ensure = Mock()
        self.service.access = Mock()
        self.service.validate_plan = Mock(return_value=None)
        self.service.schedule = Mock()
        self.plan = {'id': 'p', 'run_id': 'r', 'version': 2, 'content_hash': 'hash',
                     'status': 'candidate', 'content': {'totalCost': '10'}}
        self.run = {'id': 'r', 'workspace_id': 'w', 'requested_by': 'requester',
                    'status': 'awaiting_approval', 'input': {'factories': ['A']}}
        self.service.run_record = Mock(return_value=self.run)
        self.user = {'account': 'approver'}
        self.body = {'version': 2, 'contentHash': 'hash', 'decision': 'approved', 'comment': 'checked'}
        self.old = None
        self.events = []
        self.db = Mock()

        def execute(sql, args=None):
            self.events.append(sql)
            return Mock(fetchone=Mock(return_value=deepcopy(self.plan if 'FROM sc.candidate_plans' in sql else self.old)))

        self.db.execute.side_effect = execute

        @contextmanager
        def connection(*args):
            try:
                yield self.db
            except Exception:
                self.events.append('rollback')
                raise
            else:
                self.events.append('commit')

        self.patch = patch('backend.investigation_service.connection', connection)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.service.schedule.side_effect = lambda *args: self.events.append('schedule')

    def test_approval_rejects_self_and_stale_versions(self):
        for user, body, status in [(self.user, {**self.body, 'version': 1}, 409),
                                   ({'account': 'requester'}, self.body, 403)]:
            with self.subTest(user=user, body=body), self.assertRaises(APIError) as raised:
                self.service.decide(user, 'p', body)
            self.assertEqual(raised.exception.status, status)
        self.service.schedule.assert_not_called()
        self.assertFalse(any('INSERT INTO' in sql for sql in self.events))

    def test_committed_approval_precedes_resume_scheduling(self):
        result = self.service.decide(self.user, 'p', self.body)
        self.assertEqual(result['decision'], 'approved')
        self.assertEqual(self.events[-2:], ['commit', 'schedule'])
        self.service.schedule.assert_called_once_with('r')
        self.assertTrue(any("status='resuming'" in sql for sql in self.events))

    def test_repeated_approval_is_idempotent_and_conflicts_are_rejected(self):
        self.old = {'id': 'approval', 'approver': 'approver', 'decision': 'approved', 'comment': 'checked'}
        self.assertEqual(self.service.decide(self.user, 'p', self.body), self.old)
        with self.assertRaises(APIError) as raised:
            self.service.decide(self.user, 'p', {**self.body, 'decision': 'rejected'})
        self.assertEqual(raised.exception.status, 409)
        self.service.schedule.assert_not_called()
        self.assertFalse(any('INSERT INTO' in sql for sql in self.events))

    def test_stale_plan_commits_replan_before_returning_conflict(self):
        self.service.validate_plan.return_value = 'snapshot changed'
        with self.assertRaises(APIError) as raised:
            self.service.decide(self.user, 'p', self.body)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.events[-2:], ['commit', 'schedule'])
        self.assertTrue(any("status='replan_required'" in sql for sql in self.events))
        self.assertFalse(any('INSERT INTO' in sql for sql in self.events))

    def test_cancel_does_not_race_a_graph_transition(self):
        self.service.lock = Mock()
        self.service.lock.acquire.return_value = False
        with self.assertRaises(APIError) as raised:
            self.service.cancel(self.user, 'r')
        self.assertEqual(raised.exception.status, 409)
        self.db.execute.assert_not_called()

    def test_model_reservation_commits_before_agent_and_replay_disallows_request(self):
        self.service.env = {'SUPPLY_INVESTIGATOR': 'model'}
        self.service.data = Mock()
        self.run['model_attempted'] = False
        state = {'run_id': 'r', 'bundle': {}, 'evidence': []}
        execute = self.db.execute.side_effect

        def reserve(sql, args=None):
            if 'model_attempted=true' in sql:
                self.run['model_attempted'] = True
            return execute(sql, args)

        self.db.execute.side_effect = reserve
        allowed = []

        def agent(bundle, evidence, env, model_allowed, **kwargs):
            self.assertEqual(self.events[-1], 'commit')
            allowed.append(model_allowed)
            if model_allowed:
                raise RuntimeError('response lost after charge')
            return {'manualRequired': True}

        with patch('backend.investigation_service.InvestigationHarness'), \
                patch('backend.investigation_service.InvestigationSQL'), \
                patch('backend.investigation_service.investigate', side_effect=agent):
            with self.assertRaises(RuntimeError):
                self.service.run_investigation(state)
            result = self.service.run_investigation(state)
        self.assertEqual(allowed, [True, False])
        self.assertTrue(result['report']['manualRequired'])

    def test_finish_uses_persisted_approval_not_resume_payload(self):
        with self.assertRaises(APIError) as raised:
            self.service.finish_approval({'run_id': 'r', 'decision': {'approvalId': 'forged'}})
        self.assertEqual(raised.exception.status, 409)
        for decision, plan_status, expected in [('approved', 'approved', 'approved'),
                                                ('rejected', 'rejected', 'replan_required'),
                                                ('approved', 'stale', 'replan_required')]:
            self.old = {'decision': decision, 'status': plan_status}
            self.service.finish_approval({'run_id': 'r', 'decision': {'approvalId': 'ignored'}})
            self.assertEqual(self.db.execute.call_args.args[1], (expected, 'r'))


class AssemblyTests(unittest.TestCase):
    def test_checkpoint_path_single_executor_and_close_order(self):
        with tempfile.TemporaryDirectory() as directory:
            data = DataStore(Path(directory) / 'supply.sqlite')
            workflow = SupplyWorkflow(Mock(), data, Mock(), {})
            try:
                self.assertEqual(Path(workflow.checkpoint_db.execute('PRAGMA database_list').fetchone()[2]),
                                 Path(directory) / 'supply-graph.sqlite')
                self.assertIs(workflow.lock, workflow._workflow.lock)
                self.assertIs(workflow.executor, workflow._workflow.executor)
                workflow.process = Mock()
                workflow.schedule('r').result(timeout=3)
                workflow.process.assert_called_once_with('r')
            finally:
                workflow.close()
                data.db.close()
            with self.assertRaises(sqlite3.ProgrammingError):
                workflow.checkpoint_db.execute('SELECT 1')
            with self.assertRaises(RuntimeError):
                workflow.executor.submit(lambda: None)
