"""Agent decisions run with injected capabilities, without HTTP or backend imports."""
from contextlib import contextmanager
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from project.agents.investigator import investigate


PLAN = ('update_plan', {'reason': 'inspect', 'steps': ['read evidence']})
PARTIAL = ('submit_report', {'status': 'partial', 'claims': [],
                           'unresolvedQuestions': ['missing evidence'], 'nextSteps': []})


class InvestigatorTests(unittest.TestCase):
    def run_script(self, steps, *, options=None, evidence=(), question='inspect'):
        requests = []
        script = iter(steps)

        @contextmanager
        def session():
            def request(body, started, seconds):
                requests.append(deepcopy(body))
                name, args = next(script)
                return {'choices': [{'message': {'tool_calls': [{
                    'id': str(len(requests)), 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(args)}}]}}]}
            yield request

        report = investigate({'target': None, 'missing': [], 'candidates': [], 'question': question},
                             evidence, {'SUPPLY_INVESTIGATOR': 'model', **(options or {})},
                             model_ready=lambda: True, model_session=session)
        return report, requests

    def test_injected_session_plan_read_and_cited_report(self):
        evidence = [{'id': 'stock:1', 'table': 'inventory_snapshots', 'record': {'quantity': '3'}}]
        report, requests = self.run_script([PLAN, ('get_inventory', {}), ('submit_report', {
            'status': 'complete', 'claims': [{'statement': 'stock read', 'kind': 'observed',
                'evidenceIds': ['stock:1'], 'limitation': ''}], 'unresolvedQuestions': [], 'nextSteps': []})], evidence=evidence)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['modelSummary']['evidenceIds'], ['stock:1'])
        self.assertEqual(report['modelCalls'], 3)
        self.assertIsNone(report['tokens'])
        self.assertEqual([t['function']['name'] for t in requests[0]['tools']], ['update_plan'])
        self.assertEqual(json.loads(requests[2]['messages'][-1]['content'])['items'][0]['id'], 'stock:1')
        self.assertNotIn('model', requests[0])
        self.assertNotIn('Authorization', str(requests))

    def test_round_limits_and_final_report_gate(self):
        for value, expected in [('0', 2), ('invalid', 4), ('999', 24)]:
            with self.subTest(value=value):
                steps = [('unknown_' + str(i), {}) for i in range(expected)]
                report, requests = self.run_script(steps, options={'SUPPLY_AGENT_MAX_ROUNDS': value})
                self.assertEqual(len(requests), expected)
                self.assertEqual(report['modelCalls'], expected)
                self.assertEqual(report['status'], 'partial')
                self.assertTrue(report['manualRequired'])
                self.assertEqual([t['function']['name'] for t in requests[-1]['tools']], ['submit_report'])

    def test_repeated_calls_stop_without_spinning(self):
        report, requests = self.run_script([PLAN] + [('get_inventory', {})] * 3,
                                          options={'SUPPLY_AGENT_MAX_ROUNDS': '10'})
        self.assertEqual(len(requests), 4)
        self.assertTrue(report['manualRequired'])
        self.assertFalse(json.loads(requests[-1]['messages'][-1]['content']).get('error'))
        self.assertEqual(report['trace'][-1]['status'], 'error')

    def test_context_budget_stops_or_enters_closing(self):
        report, requests = self.run_script([PARTIAL], question='x' * 97000)
        self.assertEqual(requests, [])
        self.assertEqual(report['modelCalls'], 0)
        self.assertTrue(report['manualRequired'])
        report, requests = self.run_script([PARTIAL], question='x' * 82000)
        self.assertEqual(len(requests), 1)
        self.assertEqual([t['function']['name'] for t in requests[0]['tools']], ['submit_report'])
        self.assertEqual(report['status'], 'partial')

    def test_rules_replay_and_revocation_never_open_model_session(self):
        bundle = {'target': None, 'missing': [], 'candidates': []}
        ready, session = Mock(return_value=True), Mock()
        report = investigate(bundle, [], {}, model_ready=ready, model_session=session)
        self.assertEqual(report['mode'], 'rules')
        ready.assert_not_called()
        report = investigate(bundle, [], {'SUPPLY_INVESTIGATOR': 'model'}, model_allowed=False,
                             model_ready=ready, model_session=session)
        ready.assert_not_called()
        self.assertEqual(report['modelCalls'], 0)
        harness = Mock()
        harness.check.side_effect = PermissionError('revoked')
        report = investigate(bundle, [], {'SUPPLY_INVESTIGATOR': 'model'}, harness=harness,
                             model_ready=ready, model_session=session)
        self.assertTrue(report['accessInvalid'])
        self.assertEqual(report['evidence'], [])
        session.assert_not_called()
