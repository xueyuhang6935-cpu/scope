"""物流核心工具契约检查，无后端、FastAPI、配置或数据库依赖。"""
import unittest

from project.tools.logistics import LogisticsTools, REGISTRY, OUTPUT_SCHEMAS, validate_arguments


class LogisticsToolContractTests(unittest.TestCase):
    def test_registry_dispatch_and_defaults(self):
        received = []
        tools = LogisticsTools(lambda name, args: received.append((name, args)))
        self.assertEqual(set(REGISTRY), set(OUTPUT_SCHEMAS))
        self.assertEqual(len(tools.declarations()), 6)
        for name in REGISTRY:
            tools.call(name, {'evidence_id': 'evidence'} if name == 'get_evidence' else {})
        summary = dict(received)['get_snapshot_summary']
        self.assertEqual(summary, {'horizon_minutes': 30, 'recent_minutes': 60})
        self.assertTrue(all('snapshot_id' not in args for _, args in received))
        declarations = tools.declarations()
        declarations[0]['function']['parameters']['properties'].clear()
        self.assertEqual(validate_arguments('get_snapshot_summary', {}), summary)

    def test_bad_parameters_never_reach_executor(self):
        called = []
        tools = LogisticsTools(lambda *_: called.append(True))
        for name, args in (
                ('execute_sql', {'sql': 'SELECT 1'}), ('get_pending_tasks', {'page': True}),
                ('get_pending_tasks', {'page': 1.0}), ('get_pending_tasks', {'page_size': 101}),
                ('get_pending_tasks', {'page': 0}), ('get_pending_tasks', {'sort': 'sql_expression'}),
                ('get_pending_tasks', {'risk_state': 'completed'}), ('get_pending_tasks', {'courier_id': ' '}),
                ('get_pending_tasks', {'courier_id': 1}), ('get_pending_tasks', {'courier_id': 'x'*129}),
                ('get_risk_distribution', {'dimension': 'raw_json'}), ('get_snapshot_summary', {'horizon_minutes': 31}),
                ('get_snapshot_summary', {'recent_minutes': 0}), ('get_evidence', {}),
                ('get_evidence', {'evidence_id': 'x'*201}), ('get_quality_summary', [])):
            with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                tools.call(name, args)
        for key in ('snapshot_id', 'run_id', 'region_ids', 'city', 'business_as_of', 'workspace_id', 'sql', 'parameters'):
            for name in REGISTRY:
                with self.subTest(name=name, key=key), self.assertRaises(ValueError):
                    tools.call(name, {key: 'override'})
        self.assertEqual(called, [])


if __name__ == '__main__':
    unittest.main()
