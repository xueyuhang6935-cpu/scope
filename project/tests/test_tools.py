"""Tool contracts exercised without database, backend or model services."""
import unittest
from unittest.mock import MagicMock, Mock

from project.tools.investigation_sql import InvestigationSQL, DatabaseQueryError, TABLES
from project.tools.executor import InvestigationHarness


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.scope = {'workspace': 'w', 'scenario': 's', 'asOf': '2026-10-04',
                      'factories': ['A'], 'domains': {'reference': ['A']}}
        self.authorize = Mock(return_value=self.scope)
        self.db = MagicMock()
        self.db.execute.return_value.fetchall.return_value = [
            {'table_name': t, 'column_name': 'id', 'data_type': 'text'}
            for t, entry in TABLES.items() if entry[0] == 'reference']
        self.connect = Mock(return_value=self.db)
        self.db.__enter__.return_value = self.db
        self.tool = InvestigationSQL(self.connect, self.authorize)

    def query(self, rows):
        self.db.cursor.return_value.__enter__.return_value.__iter__.return_value = iter(
            {'payload': row} for row in rows)
        return self.tool.call('execute_sql', {'sql': 'SELECT id FROM factories', 'parameters': {}})

    def schema(self):
        return self.tool.call('get_database_schema', {'tables': ['factories']})

    def test_schema_gate_and_parameter_binding(self):
        result, audit = self.query([])
        self.assertEqual(result['error']['code'], 'invalid_query')
        self.connect.assert_not_called()
        schema, audit = self.schema()
        self.assertEqual(schema['tables']['factories']['columns'][0]['name'], 'id')
        self.assertEqual(audit['status'], 'completed')
        result, audit = self.query(['{"id": "A", "amount": 1.25}'])
        self.assertEqual(result['record']['rows'], [{'id': 'A', 'amount': '1.25'}])
        self.assertEqual(result['provenance']['tables'], ['factories'])
        self.assertEqual(audit['evidenceId'], result['id'])
        self.assertEqual(self.authorize.call_count, 3)
        cursor = self.db.cursor.return_value.__enter__.return_value
        self.assertIn('agent_read.factories', cursor.execute.call_args.args[0])
        self.assertIsNone(cursor.execute.call_args.args[1])

    def test_result_bounds_and_duplicate_columns(self):
        self.schema()
        for rows, count in [(['{"id":1}'] * 101, 100), (['{"id":"' + 'x' * 16000 + '"}'], 0)]:
            with self.subTest(count=count):
                result, audit = self.query(rows)
                self.assertEqual(len(result['record']['rows']), count)
                self.assertTrue(result['record']['truncated'])
                self.assertTrue(result['provenance']['truncated'])
        result, audit = self.query(['{"id":1,"id":2}'])
        self.assertEqual(result['error']['code'], 'invalid_query')
        self.assertEqual(audit['status'], 'error')

    def test_revocation_and_database_errors_are_sanitized(self):
        self.schema()
        self.connect.reset_mock()
        self.authorize.side_effect = PermissionError('secret credential')
        result, audit = self.query([])
        self.assertEqual(result['error']['code'], 'scope_unavailable')
        self.assertFalse(result['error']['retryable'])
        self.assertNotIn('secret', str(result))
        self.connect.assert_not_called()
        self.authorize.side_effect = None
        for state in ('42703', '42P01', '42883', '42601', '57014', '42501', '22012', '22P02', None):
            self.connect.side_effect = DatabaseQueryError(state)
            result, audit = self.query([])
            self.assertEqual(result['error']['code'], state or 'database_unavailable')
            self.assertTrue(result['error']['retryable'])

    def test_missing_views_do_not_unlock_queries(self):
        self.db.execute.return_value.fetchall.return_value = []
        result, audit = self.schema()
        self.assertIn('error', result)
        self.assertEqual(self.tool.schema_tables_seen, set())

    def test_executor_lifecycle_and_changed_scope(self):
        store = Mock()
        harness = InvestigationHarness(store, self.authorize)
        ticket = harness.start('execute_sql', {'sql': 'SELECT 1'})
        store.start.assert_called_once_with(ticket[0], 'execute_sql', {'sql': 'SELECT 1'})
        harness.set_scope(ticket, self.scope)
        store.set_scope.assert_called_once_with(ticket[0], self.scope)
        audit = {}
        harness.finish(ticket, {'error': {'code': 'denied'}}, audit)
        self.assertEqual(audit['callId'], ticket[0])
        self.assertGreaterEqual(audit['elapsedMs'], 0)
        store.finish.assert_called_once()
        evidence = [{'table': 'sql_query', 'record': {'scope': {**self.scope, 'asOf': 'old'}}}]
        with self.assertRaises(PermissionError):
            harness.check(evidence)
        evidence[0]['record']['scope'] = {**self.scope, 'domains': {'reference': ['A', 'B']}}
        with self.assertRaises(PermissionError):
            harness.check(evidence)
