"""Revocation at cached tools/report gates and durable independent audit."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from .data_store import DataStore
from .investigation_agent import investigate
from .investigation_harness import InvestigationHarness


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'audit.sqlite'
        self.data = DataStore(self.path)
        self.scope = {'workspace': 'w', 'scenario': 's', 'asOf': '2026-10-04',
                      'factories': ['A', 'B'], 'domains': {d: ['A', 'B'] for d in
                      ('reference', 'inventory', 'purchase', 'production', 'quality', 'rules')}}
        self.revoked = False
        self.initial = copy.deepcopy(self.scope)
        def authorize():
            if self.revoked or any(self.scope[k] != self.initial[k] for k in ('workspace', 'scenario', 'asOf', 'factories')):
                raise PermissionError('revoked or changed')
            return copy.deepcopy(self.scope)
        self.harness = InvestigationHarness(self.data, 'run', authorize)

    def tearDown(self):
        self.data.db.close()
        self.temp.cleanup()

    def agent(self, next_tool, change):
        test = self
        class QueryTools:
            def call(self, name, args):
                if name == 'get_database_schema':
                    return {'tables': {'inventory_snapshots': {'columns': [{'name': 'quantity'}]}}}, {'tool': name, 'status': 'completed'}
                result = {'id': 'sql:test', 'table': 'sql_query',
                          'record': {'rows': [{'quantity': '100'}], 'scope': copy.deepcopy(test.scope)}}
                return result, {'tool': name, 'scope': copy.deepcopy(test.scope)}
        count = 0
        def handler(request):
            nonlocal count
            if count == 0:
                name, args = 'update_plan', {'reason': '开始调查', 'steps': ['查库存']}
            elif count == 1:
                name, args = 'get_database_schema', {'tables': ['inventory_snapshots']}
            elif count == 2:
                name, args = 'execute_sql', {'sql': 'SELECT quantity FROM inventory_snapshots', 'parameters': {}}
            else:
                change()
                name = next_tool
                args = {} if name == 'get_candidate_plans' else {'status': 'complete', 'claims': [
                    {'statement': '核对库存', 'kind': 'observed', 'evidenceIds': ['sql:test'], 'limitation': ''}],
                    'unresolvedQuestions': [], 'nextSteps': []}
            count += 1
            return httpx.Response(200, json={'choices': [{'message': {'tool_calls': [
                {'id': str(count), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}}]})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch('backend.investigation_agent.httpx.Client', return_value=client):
            report = investigate({'target': None, 'missing': [], 'candidates': [
                {'actions': [{'kind': 'transfer', 'factory': 'A', 'fromFactory': 'B'}]}]}, [],
                {'SUPPLY_INVESTIGATOR': 'model', 'SUPPLY_MODEL': 'test', 'SUPPLY_MODEL_API_KEY': 'test',
                 'SUPPLY_MODEL_BASE_URL': 'https://model.test'}, query_tools=QueryTools(), harness=self.harness)
        self.assertTrue(report['manualRequired'])
        self.assertIsNone(report['modelSummary'])
        calls = self.harness.records()
        self.assertEqual([c['status'] for c in calls], ['completed'] * 3 + ['failed'])
        self.assertEqual(report['evidence'], [])
        self.assertNotIn('items', json.loads(calls[-1]['result_json']))

    def test_revocation_before_cached_plan(self):
        self.agent('get_candidate_plans', lambda: setattr(self, 'revoked', True))

    def test_revocation_before_report(self):
        self.agent('submit_report', lambda: setattr(self, 'revoked', True))

    def test_domain_shrink_before_report(self):
        self.agent('submit_report', lambda: self.scope['domains'].__setitem__('inventory', ['A']))

    def test_factory_shrink_before_plan(self):
        self.agent('get_candidate_plans', lambda: self.scope.__setitem__('factories', ['A']))

    def test_range_change_stops_next_tool(self):
        self.agent('get_candidate_plans', lambda: self.scope.__setitem__('asOf', '2026-10-05'))

    def test_completed_query_survives_restart_and_pending_is_interrupted(self):
        ticket = self.harness.start('execute_sql', {'sql': 'SELECT 1', 'parameters': {}})
        self.harness.set_scope(ticket, self.scope)
        self.harness.finish(ticket, {'id': 'sql:kept', 'record': {'rows': [{'ok': 1}]}},
                            {'tool': 'execute_sql', 'scope': self.scope})
        self.harness.start('execute_sql', {'sql': 'SELECT 2', 'parameters': {}})
        self.data.db.close()
        self.data = DataStore(self.path)
        InvestigationHarness.interrupt(self.data, 'run')
        rows = InvestigationHarness(self.data, 'run', lambda: self.scope).records()
        self.assertEqual([r['status'] for r in rows], ['completed', 'interrupted'])
        self.assertEqual(json.loads(rows[0]['result_json'])['id'], 'sql:kept')
        self.assertEqual(json.loads(rows[0]['scope_json']), self.scope)


if __name__ == '__main__':
    unittest.main()
