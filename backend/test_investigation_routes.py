"""HTTP delegation and transaction ordering without a source PostgreSQL database."""
from contextlib import contextmanager
from copy import deepcopy
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from .common import APIError, reply
from .investigation_routes import routes
from .investigation_service import InvestigationService


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
        self.service.process = Mock()
        self.service.executor = Mock()
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
        self.service.executor.submit.side_effect = lambda *args: self.events.append('schedule')

    def test_approval_rejects_self_and_stale_versions(self):
        for user, body, status in [(self.user, {**self.body, 'version': 1}, 409),
                                   ({'account': 'requester'}, self.body, 403)]:
            with self.subTest(user=user, body=body), self.assertRaises(APIError) as raised:
                self.service.decide(user, 'p', body)
            self.assertEqual(raised.exception.status, status)
        self.service.executor.submit.assert_not_called()
        self.assertFalse(any('INSERT INTO' in sql for sql in self.events))

    def test_committed_approval_precedes_resume_scheduling(self):
        result = self.service.decide(self.user, 'p', self.body)
        self.assertEqual(result['decision'], 'approved')
        self.assertEqual(self.events[-2:], ['commit', 'schedule'])
        self.service.executor.submit.assert_called_once_with(self.service.process, 'r')
        self.assertTrue(any("status='resuming'" in sql for sql in self.events))

    def test_repeated_approval_is_idempotent_and_conflicts_are_rejected(self):
        self.old = {'id': 'approval', 'approver': 'approver', 'decision': 'approved', 'comment': 'checked'}
        self.assertEqual(self.service.decide(self.user, 'p', self.body), self.old)
        with self.assertRaises(APIError) as raised:
            self.service.decide(self.user, 'p', {**self.body, 'decision': 'rejected'})
        self.assertEqual(raised.exception.status, 409)
        self.service.executor.submit.assert_not_called()
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
