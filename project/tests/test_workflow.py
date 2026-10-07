"""Real LangGraph/SQLite recovery with injected, deterministic business services."""
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from project.workflows.investigation import InvestigationWorkflow


class Services:
    """Business persistence fixture; SQLite checkpoints themselves are real."""
    def __init__(self):
        self.lock = threading.RLock()
        self.runs = {'r': 'queued'}
        self.approvals = {}
        self.stages = []
        self.model_calls = 0
        self.attempted = set()
        self.fail_after_model = False
        self.blocked = False
        self.children = {}
        self.interrupted = []

    def prepare_run(self, run_id):
        status = self.runs[run_id]
        if status not in ('queued', 'running', 'resuming', 'awaiting_approval', 'replan_required', 'replanned'):
            return None
        if status == 'queued':
            self.runs[run_id] = 'running'
        return {'status': status}, self.approvals.get(run_id)

    def record_stage(self, state, stage):
        self.stages.append((state['run_id'], stage))

    def collect_data(self, state):
        return {'bundle': {'snapshotHash': 'snapshot'}, 'evidence': [{'id': 'e1'}],
                'query_context': {'scenario': 's', 'workspace': 'w'}}

    def run_investigation(self, state):
        run_id = state['run_id']
        if run_id in self.attempted:
            return {'report': {'manualRequired': True, 'evidence': state['evidence']}}
        self.attempted.add(run_id)
        self.model_calls += 1
        if self.fail_after_model:
            raise RuntimeError('crash after billed request')
        return {'report': {'evidence': state['evidence']}}

    def save_plans(self, state):
        blocked = self.blocked or state['report'].get('manualRequired', False)
        self.runs[state['run_id']] = 'manual_required' if blocked else 'awaiting_approval'
        return {'plan_ids': ['p1'], 'blocked': blocked, 'report': state['report']}

    def finish_approval(self, state):
        if state['run_id'] not in self.approvals:
            raise PermissionError('no committed approval')
        self.runs[state['run_id']] = 'approved'
        return {}

    def replan(self, run_id):
        if run_id in self.children:
            return self.children[run_id]
        if self.runs[run_id] != 'replan_required':
            return None
        child = run_id + '-child'
        self.children[run_id] = child
        self.runs[run_id] = 'replanned'
        self.runs[child] = 'queued'
        return child

    def interrupt_calls(self, run_id, ensure_table=False):
        self.interrupted.append((run_id, ensure_table))

    def fail_run(self, run_id, error):
        self.runs[run_id] = 'failed'

    def recoverable_runs(self):
        return [{'id': key} for key, status in self.runs.items()
                if status in ('queued', 'running', 'resuming', 'awaiting_approval', 'replan_required')]

    def has_recovery_config(self):
        return True


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.services = Services()
        self.open_runner()

    def open_runner(self):
        self.db = sqlite3.connect(Path(self.temp.name) / 'supply-graph.sqlite', check_same_thread=False)
        self.runner = InvestigationWorkflow(self.services, SqliteSaver(self.db))
        runner, db = self.runner, self.db
        self.addCleanup(lambda: (runner.close(), db.close()))

    def state(self, run_id='r'):
        return self.runner.graph.get_state({'configurable': {'thread_id': run_id}})

    def test_pause_restart_resume_and_repeated_delivery(self):
        self.runner.process('r')
        saved = self.state()
        self.assertEqual(saved.next, ('approval',))
        self.assertTrue(saved.interrupts)
        self.assertEqual(saved.interrupts[0].value['planIds'], ['p1'])
        before = deepcopy(saved.values)
        self.runner.close()
        self.db.close()
        self.open_runner()
        self.assertEqual(self.state().values, before)
        self.runner.process('r')
        self.assertEqual(self.services.model_calls, 1)
        self.services.approvals['r'] = {'id': 'approval'}
        self.services.runs['r'] = 'resuming'
        self.runner.process('r')
        self.runner.process('r')
        self.assertEqual(self.services.runs['r'], 'approved')
        self.assertEqual(self.state().next, ())
        self.assertEqual(self.services.stages.count(('r', 'finish')), 1)
        self.assertEqual(self.services.model_calls, 1)
        self.assertEqual(self.state().values['evidence'], [{'id': 'e1'}])

    def test_crash_between_plans_checkpoint_and_approval_interrupt(self):
        graph = self.runner.graph
        before_approval = graph.builder.compile(checkpointer=graph.checkpointer, interrupt_before=['approval'])
        before_approval.invoke({'run_id': 'r'}, {'configurable': {'thread_id': 'r'}}, durability='sync')
        self.assertEqual(self.state().next, ('approval',))
        self.assertFalse(self.state().interrupts)
        self.runner.process('r')
        self.assertTrue(self.state().interrupts)
        self.assertEqual(self.services.runs['r'], 'awaiting_approval')
        self.services.approvals['r'] = {'id': 'approval'}
        self.runner.process('r')
        self.assertEqual(self.services.runs['r'], 'approved')
        self.assertEqual(self.services.model_calls, 1)

    def test_resume_payload_cannot_replace_committed_approval(self):
        self.runner.process('r')
        with self.assertRaises(PermissionError):
            self.runner.graph.invoke(Command(resume={'approvalId': 'forged'}),
                                     {'configurable': {'thread_id': 'r'}}, durability='sync')
        self.assertNotEqual(self.services.runs['r'], 'approved')

    def test_interrupted_model_attempt_is_not_rebilled_on_checkpoint_replay(self):
        self.services.fail_after_model = True
        with self.assertLogs('project.workflows.investigation', level='ERROR'):
            self.runner.process('r')
        self.assertEqual(self.services.runs['r'], 'failed')
        self.assertIn(('r', False), self.services.interrupted)
        self.assertEqual(self.state().next, ('investigate',))
        self.services.runs['r'] = 'running'
        self.runner.close()
        self.db.close()
        self.open_runner()
        self.runner.process('r')
        self.assertEqual(self.services.model_calls, 1)
        self.assertEqual(self.services.runs['r'], 'manual_required')
        self.assertEqual(self.state().next, ())

    def test_replan_follows_one_child_with_a_separate_thread(self):
        self.services.runs['r'] = 'replan_required'
        self.runner.process('r')
        self.runner.process('r')
        self.assertEqual(self.services.children, {'r': 'r-child'})
        self.assertEqual(self.services.model_calls, 1)
        self.assertFalse(self.state().values)
        self.assertEqual(self.state('r-child').next, ('approval',))

    def test_recover_is_serial_and_missing_source_does_not_block_startup(self):
        self.runner.recover()
        self.runner.close()
        self.assertEqual(self.services.runs['r'], 'awaiting_approval')
        self.assertEqual(self.services.interrupted, [('r', True)])
        self.db.close()
        self.open_runner()
        self.runner.recover()
        self.runner.close()
        self.assertEqual(self.services.model_calls, 1)
        self.services.recoverable_runs = Mock(side_effect=RuntimeError('source unavailable'))
        self.runner.recover()
        self.services.has_recovery_config = Mock(return_value=False)
        self.services.recoverable_runs.reset_mock()
        self.runner.recover()
        self.services.recoverable_runs.assert_not_called()

    def test_blocked_plans_end_without_approval(self):
        self.services.blocked = True
        self.runner.process('r')
        self.assertEqual(self.services.runs['r'], 'manual_required')
        self.assertEqual(self.state().next, ())
        self.assertNotIn(('r', 'approval'), self.services.stages)
