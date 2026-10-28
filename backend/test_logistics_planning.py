"""T13 证据评分、独立夹具复算与模型窗口上下文检查；不调用真实供应商。"""
from copy import deepcopy
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

import httpx

from . import test_logistics_workflow as support

EVAL = runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/verify-logistics-planning.py'))
REAL = runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/verify-logistics-planning-real.py'))


class PlanningTests(unittest.TestCase):
    def test_capture_wrapper_forwards_failure_diagnostic_and_persists_it(self):
        test = support.WorkflowTests(); test.setUp()
        try:
            test.enabled()
            calls = []
            def handler(request):
                calls.append(request)
                raise httpx.ReadTimeout('PRIVATE-BODY')
            client = httpx.Client(transport=httpx.MockTransport(handler))
            with patch('backend.investigation_model.httpx.Client', return_value=client):
                with EVAL['capture_model_proposals'](True) as attempts:
                    with self.assertRaises(EVAL['ProtocolError']) as error:
                        test.app.state.logistics_model.generate('plan', test.user, test.run, '检查')
            self.assertEqual(error.exception.code, 'model_transport_failed')
            self.assertEqual(len(calls), 1)
            self.assertEqual(attempts, [{'kind': 'plan', 'value': None, 'error': 'model_transport_failed'}])
            budget = json.loads(test.app.state.logistics_runs.saved(test.run)['budget_json'])
            self.assertEqual(budget['model_calls'], 1)
            self.assertEqual(budget['unknown_usage_calls'], 1)
            self.assertEqual(budget['model_usage_records'][0]['transport_failure']['failure_reason'], 'read_timeout')
            self.assertNotIn('PRIVATE-BODY', json.dumps(budget))
        finally:
            test.tearDown()

    def test_real_selection_and_oracle_do_not_fix_eight_or_use_future(self):
        rows = EVAL['records']('courier')[:3]
        rows.append({**rows[0], 'order_id': 'FUTURE', 'accept_time': '07-02 10:01:00', 'lng': '', 'lat': ''})
        matched, oracle, criteria = REAL['classify'](rows, '2000-07-02 10:00:00')
        self.assertEqual(oracle['backlog'], 3)
        self.assertEqual(criteria['top_overdue_count'], 3)
        self.assertEqual(criteria['visible_coordinates_available'], 3)
        self.assertTrue(matched['courier'])
        self.assertTrue(matched['truncated'])
        self.assertFalse(REAL['classify'](rows[:2], '2000-07-02 10:00:00')[0]['truncated'])
        self.assertFalse(matched['spatial_missing'])
        future = deepcopy(rows)
        for row in future:
            row['pickup_time'] = '07-03 18:00:00'
        self.assertEqual(REAL['classify'](rows, '2000-07-02 10:00:00'),
                         REAL['classify'](future, '2000-07-02 10:00:00'))
        data = {'results': [{'tool': 'get_snapshot_summary', 'data': {'metrics':
            {k: {'value': oracle[k]} for k in ('backlog', 'overdue', 'due_soon', 'recent_accepted', 'recent_completed')}}}]}
        case = {'case': 'courier', 'oracle': oracle, 'criteria': criteria}
        self.assertTrue(REAL['assess_real'](data, case)['numeric_correct'])
        data['results'][0]['data']['metrics']['overdue']['value'] = 6
        self.assertFalse(REAL['assess_real'](data, case)['numeric_correct'])

    def test_fixed_cases_and_scorer_reject_missing_or_wrong_metrics(self):
        results = []
        for case in EVAL['CASES']:
            with self.subTest(case=case):
                result = EVAL['run_case'](case, 'fixed', {}, False)
                self.assertTrue(result['assessment']['numeric_correct'])
                self.assertTrue(result['assessment']['necessary_evidence'])
                self.assertFalse(result['assessment']['model_report_present'])
                self.assertEqual(result['budget']['tool_calls'], 5)
                self.assertEqual(result['budget']['model_calls'], 0)
                self.assertEqual([r['tool'] for r in result['results']], [name for name, _ in EVAL['FIXED']])
                corrupted = deepcopy(result['results'])
                corrupted[0]['data']['metrics']['backlog']['value'] = 999
                self.assertFalse(EVAL['assess'](case, corrupted, None)['numeric_correct'])
                self.assertFalse(EVAL['assess'](case, [], None)['necessary_evidence'])
                results.append(result)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for result in results:
                (output/f"{result['case']}-fixed.json").write_text(json.dumps(result), encoding='utf-8')
                stopped = {**result, 'run_status': 'waiting', 'stop_reason': 'token_limit',
                           'report': {'claims': [{'statement': '程序部分报告'}]}}
                (output/f"{result['case']}-dynamic.json").write_text(json.dumps(stopped), encoding='utf-8')
            summary = EVAL['summarize'](output)
            self.assertTrue(all(p['same_input'] and p['same_snapshot_content'] for p in summary['cases']))
            self.assertTrue(all(not p['dynamic']['assessment']['model_report_present'] for p in summary['cases']))
            self.assertTrue(all(p['dynamic']['report_source'] == 'program_stop_report' for p in summary['cases']))

    def test_compact_context_preserves_visible_window_and_truncation(self):
        test = support.WorkflowTests(); test.setUp()
        try:
            test.enabled()
            summary = test.service.call(test.user, test.run, 'get_snapshot_summary', {})
            pending = test.service.call(test.user, test.run, 'get_pending_tasks', {})
            refs = [summary['result_evidence_id'], pending['result_evidence_id']]
            seen = []; factory = httpx.Client
            def handler(request):
                body = json.loads(request.content); seen.append(json.loads(body['messages'][1]['content']))
                return httpx.Response(200, json=support.simulated(body))
            with patch('backend.investigation_model.httpx.Client', side_effect=lambda **kw:
                       factory(transport=httpx.MockTransport(handler), **kw)):
                test.app.state.logistics_model.generate('report', test.user, test.run, '核查窗口',
                                                       evidence_ids=refs, compact=True)
            view = seen[0]['evidence'][pending['result_evidence_id']]['record']
            self.assertTrue(view['context_truncated'])
            self.assertEqual(len(view['rows']), 3)
            self.assertEqual(view['rows'][0]['time_window_end_reference'], pending['rows'][0]['time_window_end_reference'])
            self.assertNotIn('pickup_time', view['rows'][0])
        finally:
            test.tearDown()


if __name__ == '__main__':
    unittest.main()
