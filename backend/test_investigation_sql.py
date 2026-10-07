"""SQL boundary, real scoped database reads, and model error-repair/evidence loop."""
import json
import os
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

import httpx
import psycopg
from dotenv import dotenv_values

from .investigation_agent import investigate
from .investigation_sql import InvestigationSQL, TABLES, validate_sql
from .test_supply_workflow import report_args


class ValidationTests(unittest.TestCase):
    def test_select_join_cte_parameters(self):
        query = validate_sql('WITH inv AS (SELECT * FROM inventory_snapshots WHERE material_id=%(m)s) SELECT factory_id, sum(quantity) AS total FROM inv GROUP BY factory_id', {'m': 'P001'}, TABLES)
        self.assertIn('agent_read.inventory_snapshots', query)
        self.assertIn('%(m)s', query)

    def test_escape_boundaries(self):
        bad = [
            'SELECT 1; DELETE FROM inventory_snapshots', 'DELETE FROM inventory_snapshots',
            'WITH x AS (DELETE FROM inventory_snapshots RETURNING *) SELECT * FROM x',
            'SELECT * INTO stolen FROM inventory_snapshots', 'SELECT * FROM inventory_snapshots FOR UPDATE',
            'SELECT * FROM supply_demo.inventory_snapshots', 'SELECT * FROM pg_catalog.pg_roles',
            'WITH pg_roles AS (SELECT * FROM pg_roles) SELECT * FROM pg_roles',
            "SELECT set_config('scope.agent_context','{}',true)", "SELECT pg_read_file('/etc/passwd')",
            'SELECT pg_sleep(20)', 'SELECT * FROM generate_series(1,1000000000)',
            "SELECT 'pg_authid'::regclass", "SELECT CAST('x' AS custom_type)",
            'WITH RECURSIVE x AS (SELECT 1 UNION ALL SELECT * FROM x) SELECT * FROM x',
            'COPY inventory_snapshots TO STDOUT', 'SELECT * FROM expected_results',
        ]
        for query in bad:
            with self.subTest(query=query), self.assertRaises(ValueError):
                validate_sql(query, {}, TABLES)
        with self.assertRaises(ValueError):
            validate_sql('SELECT * FROM purchase_order_lines', {}, {'inventory_snapshots'})

    def test_literal_percent_and_placeholder(self):
        result = validate_sql("SELECT %(m)s AS material, '100%' AS label, '%(m)s' AS literal", {'m': 'P001'}, TABLES)
        self.assertIn("'100%%'", result)
        self.assertIn("'%%(m)s'", result)
        self.assertIn('%(m)s AS material', result)

    def test_query_error_repair_and_evidence(self):
        seen = []
        class QueryTools:
            def call(self, name, args):
                if name == 'get_database_schema':
                    return {'tables': {'inventory_snapshots': {'columns': [{'name': 'quantity'}]}}}, {'tool': name, 'status': 'completed'}
                if 'bad' in args.get('sql', ''):
                    return {'error': {'message': '列不存在', 'retryable': True}}, {'tool': name, 'status': 'error'}
                return {'id': 'sql:actual', 'table': 'sql_query', 'record': {'rows': [{'quantity': '100'}]}}, {'tool': name, 'status': 'completed'}
        calls = [('update_plan', {'reason': '调查', 'steps': ['读取结构', '核查库存']}),
                 ('get_database_schema', {'tables': ['inventory_snapshots']}),
                 ('execute_sql', {'sql': 'SELECT bad FROM inventory_snapshots', 'parameters': {}}),
                 ('execute_sql', {'sql': 'SELECT quantity FROM inventory_snapshots', 'parameters': {}}),
                 ('submit_report', report_args('库存证据已核对', ['sql:actual']))]
        def handler(request):
            index = len(seen)
            seen.append(json.loads(request.content))
            name, args = calls[index]
            return httpx.Response(200, json={'usage': {'total_tokens': 100}, 'choices': [{'message': {'tool_calls': [
                {'id': str(index), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}}]})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch('backend.investigation_model.httpx.Client', return_value=client):
            report = investigate({'target': None, 'missing': [], 'candidates': []}, [],
                {'SUPPLY_INVESTIGATOR': 'model', 'SUPPLY_MODEL': 'test', 'SUPPLY_MODEL_API_KEY': 'test', 'SUPPLY_MODEL_BASE_URL': 'https://model.test'}, query_tools=QueryTools())
        self.assertFalse(report.get('manualRequired'))
        self.assertEqual(report['modelSummary']['evidenceIds'], ['sql:actual'])
        self.assertEqual(report['evidence'][0]['id'], 'sql:actual')
        self.assertIn('列不存在', seen[3]['messages'][-1]['content'])

    def test_schema_gate_and_cte_provenance(self):
        refs = set()
        validate_sql('WITH x AS (SELECT * FROM inventory_snapshots) SELECT * FROM x JOIN materials m ON x.material_id=m.id', {}, TABLES, referenced_tables=refs)
        self.assertEqual(refs, {'inventory_snapshots', 'materials'})
        tool = InvestigationSQL({}, lambda: {'domains': {'inventory': ['A'], 'reference': ['A']}})
        with patch('backend.investigation_sql.psycopg.connect') as connect:
            result, _ = tool.call('execute_sql', {'sql': 'WITH x AS (SELECT * FROM inventory_snapshots) SELECT * FROM x', 'parameters': {}})
        connect.assert_not_called()
        self.assertIn('读取这些表的字段', result['error']['message'])


@unittest.skipUnless(os.getenv('SCOPE_DATABASE_INTEGRATION') == '1', 'Requires installed local read-only views')
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.env = dotenv_values(Path(__file__).resolve().parents[1] / '.env')
        self.scope = {'workspace': 'sql-test-workspace', 'scenario': 'supplier_delay', 'asOf': '2026-10-05T07:00:00+08:00',
                      'domains': {d: ['A'] for d in ('reference', 'inventory', 'purchase', 'quality', 'production', 'rules')}}
        self.tool = InvestigationSQL(self.env, lambda: self.scope)
        for offset in range(0, len(TABLES), 6):
            result, _ = self.tool.call('get_database_schema', {'tables': list(TABLES)[offset:offset + 6]})
            self.assertNotIn('error', result, result)

    def query(self, sql, parameters=None):
        return self.tool.call('execute_sql', {'sql': sql, 'parameters': parameters or {}})[0]

    def test_real_query_scope_and_schema(self):
        schema, _ = self.tool.call('get_database_schema', {})
        self.assertEqual(len(schema['tables']), len(TABLES))
        detail, _ = self.tool.call('get_database_schema', {'tables': ['inventory_snapshots']})
        self.assertIn('quantity', [c['name'] for c in detail['tables']['inventory_snapshots']['columns']])
        result = self.query('SELECT factory_id, scenario_id, SUM(quantity) AS total FROM inventory_snapshots WHERE material_id=%(m)s GROUP BY factory_id, scenario_id', {'m': 'P001'})
        self.assertEqual([r['factory_id'] for r in result['record']['rows']], ['A'])
        self.assertEqual(result['provenance']['tables'], ['inventory_snapshots'])
        self.assertEqual(result['provenance']['sourceType'], 'database_query')
        self.assertEqual(result['record']['rows'][0]['scenario_id'], 'supplier_delay')
        self.assertEqual(self.query("SELECT * FROM inventory_snapshots WHERE factory_id='B'")['record']['rows'], [])
        self.assertEqual(self.query("SELECT * FROM inventory_snapshots WHERE scenario_id='normal'")['record']['rows'], [])
        self.assertEqual(self.query("SELECT * FROM inventory_snapshots WHERE ingested_at > '2026-10-05T07:00:00+08:00'")['record']['rows'], [])
        self.assertEqual(self.query("SELECT * FROM transfer_routes")['record']['rows'], [])
        joined = self.query('SELECT i.factory_id FROM inventory_reservations r JOIN inventory_snapshots i ON i.id=r.snapshot_id')
        self.assertTrue(all(r['factory_id'] == 'A' for r in joined['record']['rows']))

    def test_database_enforces_readonly_and_scope_without_model_filters(self):
        with psycopg.connect(self.env['SUPPLY_AGENT_DATABASE_URL']) as db:
            self.assertEqual(db.execute('SHOW transaction_read_only').fetchone()[0], 'on')
            self.assertEqual(db.execute('SELECT count(*) FROM agent_read.inventory_snapshots').fetchone()[0], 0)
            db.execute("SELECT set_config('scope.agent_context',%s,true)", (json.dumps(self.scope),))
            self.assertEqual(db.execute('SELECT DISTINCT factory_id FROM agent_read.inventory_snapshots').fetchall(), [('A',)])
        for query in ('SELECT * FROM supply_demo.inventory_snapshots', 'SELECT * FROM sc.commitment_changes', 'DELETE FROM agent_read.inventory_snapshots'):
            with self.subTest(query=query), self.assertRaises(psycopg.Error):
                with psycopg.connect(self.env['SUPPLY_AGENT_DATABASE_URL']) as db:
                    db.execute(query)

    def test_domain_revocation_truncation_and_error_recovery(self):
        self.scope['domains']['purchase'] = []
        self.assertIn('error', self.query('SELECT * FROM purchase_order_lines'))
        self.assertEqual(self.query('SELECT nonexistent FROM inventory_snapshots')['error']['code'], '42703')
        self.assertTrue(self.query("SELECT 1 AS ok")['record']['rows'])
        result = self.query('SELECT a.id AS a, b.id AS b, c.id AS c FROM inventory_snapshots a CROSS JOIN inventory_snapshots b CROSS JOIN inventory_snapshots c')
        self.assertTrue(result['record']['truncated'])
        self.assertLessEqual(len(result['record']['rows']), 100)
        literal = self.query("SELECT %(m)s AS material, '100%' AS label, '%(m)s' AS literal", {'m': "P001'; DROP TABLE x;--"})
        self.assertEqual(literal['record']['rows'][0]['label'], '100%')
        self.assertEqual(literal['record']['rows'][0]['literal'], '%(m)s')
        def revoked():
            raise RuntimeError('revoked')
        self.tool.authorize = revoked
        self.assertFalse(self.query('SELECT 1')['error']['retryable'])

    def test_workspace_injected_changes_and_asof_isolation(self):
        workspace = 'sql-test-' + uuid.uuid4().hex
        self.scope['workspace'] = workspace
        purchase = self.query('SELECT id FROM purchase_order_lines LIMIT 1')['record']['rows'][0]['id']
        identifiers = [uuid.uuid4().hex for _ in range(3)]
        try:
            with psycopg.connect(self.env['ANALYST_DATABASE_URL']) as db:
                for identifier, space, known in zip(identifiers, [workspace, workspace + '-other', workspace],
                        ['2026-10-05T07:00:00+08:00', '2026-10-05T07:00:00+08:00', '2026-10-06T07:00:00+08:00']):
                    db.execute('INSERT INTO sc.commitment_changes(id,workspace_id,scenario_id,request_id,purchase_line_id,installment,available_at,known_at,note) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (identifier, space, 'supplier_delay', identifier, purchase, 'test', known, known, 'SQL isolation test'))
            rows = self.query('SELECT id FROM commitment_changes')['record']['rows']
            self.assertEqual(rows, [{'id': identifiers[0]}])
        finally:
            with psycopg.connect(self.env['ANALYST_DATABASE_URL']) as db:
                db.execute('DELETE FROM sc.commitment_changes WHERE id=ANY(%s)', (identifiers,))


if __name__ == '__main__':
    unittest.main()
