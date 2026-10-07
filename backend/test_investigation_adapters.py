"""Backend connection restrictions and durable audit adapter tests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import psycopg

from .data_store import DataStore
from .investigation_harness import InvestigationHarness
from .investigation_sql import InvestigationSQL, ROLE


class AdapterTests(unittest.TestCase):
    def test_dedicated_connection_required(self):
        tool = InvestigationSQL({'DATABASE_URL': 'must-not-be-used'}, lambda: {'domains': {}})
        with patch('backend.investigation_sql.psycopg.connect') as connect:
            result, _ = tool.call('execute_sql', {'sql': 'SELECT 1', 'parameters': {}})
        self.assertEqual(result['error']['code'], 'invalid_query')
        connect.assert_not_called()

    def test_connection_settings_role_and_driver_errors(self):
        scope = {'domains': {}}
        db = MagicMock()
        db.__enter__.return_value = db
        role = {'name': ROLE, 'rolsuper': False, 'rolcreaterole': False,
                'rolcreatedb': False, 'rolbypassrls': False}
        db.execute.return_value.fetchone.return_value = role
        db.cursor.return_value.__enter__.return_value.__iter__.return_value = iter([])
        tool = InvestigationSQL({'SUPPLY_AGENT_DATABASE_URL': 'dedicated'}, lambda: scope)
        args = {'sql': 'SELECT 1', 'parameters': {}}
        with patch('backend.investigation_sql.psycopg.connect', return_value=db) as connect:
            result, _ = tool.call('execute_sql', args)
            self.assertNotIn('error', result)
            self.assertEqual(connect.call_args.args, ('dedicated',))
            self.assertEqual(connect.call_args.kwargs['connect_timeout'], 3)
            statements = [call.args[0] for call in db.execute.call_args_list]
            self.assertEqual(statements[:4], ['SET TRANSACTION READ ONLY',
                "SET LOCAL statement_timeout = '3s'", "SET LOCAL lock_timeout = '1s'",
                'SET LOCAL search_path = pg_catalog, agent_read'])
            self.assertEqual(json.loads(db.execute.call_args.args[1][0]), scope)
            for key in ('rolsuper', 'rolcreaterole', 'rolcreatedb', 'rolbypassrls', 'name'):
                with self.subTest(key=key):
                    db.execute.return_value.fetchone.return_value = {**role, key: True if key != 'name' else 'owner'}
                    result, _ = tool.call('execute_sql', args)
                    self.assertEqual(result['error']['code'], 'invalid_query')
        with patch('backend.investigation_sql.psycopg.connect', side_effect=psycopg.OperationalError('secret DSN')):
            result, _ = tool.call('execute_sql', args)
            self.assertEqual(result['error']['code'], 'database_unavailable')
            self.assertNotIn('secret', str(result))
        db.execute.return_value.fetchone.return_value = role
        db.cursor.return_value.__enter__.return_value.execute.side_effect = psycopg.errors.QueryCanceled('secret SQL')
        with patch('backend.investigation_sql.psycopg.connect', return_value=db):
            result, _ = tool.call('execute_sql', args)
            self.assertEqual(result['error']['code'], '57014')
            self.assertNotIn('secret', str(result))

    def test_audit_restart_preserves_scope_and_other_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audit.sqlite'
            data = DataStore(path)
            try:
                first = InvestigationHarness(data, 'first', lambda: {})
                other = InvestigationHarness(data, 'other', lambda: {})
                ticket = first.start('query', {})
                first.set_scope(ticket, {'workspace': 'w'})
                first.finish(ticket, {'rows': []}, {})
                first.start('pending', {})
                other.start('pending', {})
            finally:
                data.db.close()
            data = DataStore(path)
            try:
                InvestigationHarness.interrupt(data, 'first')
                records = InvestigationHarness(data, 'first', lambda: {}).records()
                self.assertEqual([row['status'] for row in records], ['completed', 'interrupted'])
                self.assertEqual(json.loads(records[0]['scope_json']), {'workspace': 'w'})
                self.assertEqual(InvestigationHarness(data, 'other', lambda: {}).records()[0]['status'], 'started')
            finally:
                data.db.close()
