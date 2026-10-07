"""Agent loop through the backend model adapter, using scripted HTTP responses."""
import json
import unittest
from unittest.mock import patch

import httpx

from backend.investigation_agent import investigate


def report_args(statement='已核对库存', refs=None, kind='observed', limitation='', status='complete', questions=None):
    return {'status': status, 'claims': [{'statement': statement, 'kind': kind,
             'evidenceIds': refs or ['inventory_snapshots:stock'], 'limitation': limitation}],
            'unresolvedQuestions': questions or [], 'nextSteps': []}


class AgentTests(unittest.TestCase):
    def run_agent(self, calls, usages=None, replanning=None, initial_plan=True, env=None, evidence=None, evidence_reader=None, candidates=None):
        if initial_plan:
            calls = [('update_plan', {'reason': '开始调查', 'steps': ['读取证据', '提交报告']})] + calls
            if usages:
                usages = [100] + usages
        seen = []
        def handler(request):
            index = len(seen)
            seen.append(json.loads(request.content))
            name, args = calls[index]
            return httpx.Response(200, json={'usage': {'total_tokens': usages[index] if usages else 100}, 'choices': [{'message': {'tool_calls': [{'id': f'call-{index}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}}]})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        if evidence is None:
            evidence = [{'id': 'inventory_snapshots:stock', 'table': 'inventory_snapshots', 'record': {'quantity': '400'}}]
        with patch('backend.investigation_model.httpx.Client', return_value=client):
            report = investigate({'target': None, 'missing': [], 'candidates': candidates or [], 'replanning': replanning}, evidence,
                {'SUPPLY_INVESTIGATOR': 'model', 'SUPPLY_MODEL': 'test', 'SUPPLY_MODEL_API_KEY': 'fake-test-key', 'SUPPLY_MODEL_BASE_URL': 'https://model.test', 'SUPPLY_AGENT_MAX_ROUNDS': '8', **(env or {})},
                evidence_reader=evidence_reader)
        return report, seen

    def test_cited_tool_report_and_unknown_usage(self):
        report, calls = self.run_agent([('get_inventory', {}), ('submit_report', report_args())], [None, 100])
        self.assertEqual(report['modelSummary']['evidenceIds'], ['inventory_snapshots:stock'])
        self.assertIsNone(report['tokens'])
        self.assertEqual(len(calls), 3)
        self.assertTrue(report['promptVersion'].startswith('investigator-v2-'))
        self.assertFalse(report.get('manualRequired'))

    def test_fabricated_evidence_budget_and_replay(self):
        report, calls = self.run_agent([('submit_report', report_args(refs=['unread:source']))] * 3)
        self.assertTrue(report['manualRequired'])
        self.assertEqual(len(calls), 4)
        report, calls = self.run_agent([(name, {}) for name in ('get_inventory', 'get_production_plan', 'get_supply_evidence', 'get_candidate_plans')], env={'SUPPLY_AGENT_MAX_ROUNDS': '5'})
        self.assertEqual(len(calls), 5)
        self.assertEqual([t['function']['name'] for t in calls[-1]['tools']], ['submit_report'])
        self.assertTrue(report['manualRequired'])
        with patch('backend.investigation_model.httpx.Client') as client:
            report = investigate({'target': None, 'missing': [], 'candidates': []}, [], {'SUPPLY_INVESTIGATOR': 'model'}, model_allowed=False)
            client.assert_not_called()
            self.assertTrue(report['manualRequired'])

    def test_invalid_report_replans_and_reads_missing_evidence(self):
        report, calls = self.run_agent([
            ('submit_report', report_args(refs=['unread:source'])),
            ('update_plan', {'reason': '缺少已读取证据', 'steps': ['读取库存', '提交引用说明']}),
            ('get_inventory', {}),
            ('submit_report', report_args('已补查库存')),
        ])
        self.assertFalse(report.get('manualRequired'))
        self.assertEqual(report['planRevisions'][1]['reason'], '缺少已读取证据')
        self.assertTrue(json.loads(calls[2]['messages'][-1]['content'])['error']['retryable'])
        self.assertEqual(report['modelCalls'], 5)
        self.assertEqual(report['modelSummary']['evidenceIds'], ['inventory_snapshots:stock'])

    def test_invalid_tool_is_corrected_without_execution(self):
        report, calls = self.run_agent([
            ('unknown_tool', {}), ('get_inventory', {}),
            ('submit_report', report_args('已修正工具')),
        ])
        self.assertFalse(report.get('manualRequired'))
        self.assertIn('error', json.loads(calls[2]['messages'][-1]['content']))

    def test_replanning_feedback_reaches_model(self):
        feedback = {'round': 1, 'reason': '来源库存需核查', 'parentRunId': 'INV-parent'}
        report, calls = self.run_agent([('get_inventory', {}), ('submit_report', report_args('已重新核查库存'))], replanning=feedback)
        self.assertEqual(json.loads(calls[0]['messages'][1]['content'])['replanning'], feedback)
        self.assertFalse(report.get('manualRequired'))

    def test_transport_failure_is_not_retried(self):
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout('unknown response')
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch('backend.investigation_model.httpx.Client', return_value=client):
            report = investigate({'target': None, 'missing': [], 'candidates': []}, [],
                {'SUPPLY_INVESTIGATOR': 'model', 'SUPPLY_MODEL': 'test', 'SUPPLY_MODEL_API_KEY': 'test'})
        self.assertEqual(len(calls), 1)
        self.assertTrue(report['manualRequired'])

    def test_plan_gate_is_enforced_even_when_model_ignores_tools(self):
        report, calls = self.run_agent([
            ('get_inventory', {}),
            ('update_plan', {'reason': '先规划', 'steps': ['读取库存']}),
            ('get_inventory', {}), ('submit_report', report_args()),
        ], initial_plan=False)
        self.assertEqual([s['function']['name'] for s in calls[0]['tools']], ['update_plan'])
        self.assertEqual(report['trace'][0]['status'], 'error')
        self.assertEqual(report['status'], 'complete')

    def test_partial_requires_reason_and_does_not_invalidate_feasible_plans(self):
        from backend.investigation_report import plan_blockers
        report, _ = self.run_agent([('get_inventory', {}), ('submit_report', report_args(
            status='partial', questions=['尚未取得晚交原因的独立证据']))])
        self.assertEqual(report['status'], 'partial')
        bundle = {'target': {'computable': True}, 'candidates': [{'feasible': True}]}
        self.assertEqual(plan_blockers(report, bundle), [])
        bundle['target']['computable'] = False
        self.assertTrue(plan_blockers(report, bundle))
        bundle['target']['computable'] = True
        bundle['candidates'][0]['feasible'] = False
        self.assertTrue(plan_blockers(report, bundle))

    def test_claim_validation_and_legacy_fields_are_derived(self):
        from project.agents.report_protocol import validate_report
        evidence = {'inventory_snapshots:stock': {'record': {'truncated': True}}}
        for args in (report_args(), report_args(kind='reported'), report_args(kind='inferred'),
                     report_args(status='partial'), report_args(refs=['fake'])):
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_report(args, evidence)
        result = validate_report(report_args(kind='reported', limitation='仅确认其如此报告，结果被截断'), evidence)
        self.assertEqual(result['summary'], result['claims'][0]['statement'])
        self.assertEqual(result['evidenceIds'], ['inventory_snapshots:stock'])
        with self.assertRaises(ValueError):
            validate_report({'summary': '旧协议不可用于新提交', 'evidenceIds': ['inventory_snapshots:stock']}, evidence)

    def test_final_round_can_submit_partial_without_evidence(self):
        partial = {'status': 'partial', 'claims': [], 'unresolvedQuestions': ['未获得有效证据。'], 'nextSteps': ['补查库存']}
        report, calls = self.run_agent([('submit_report', partial)], env={'SUPPLY_AGENT_MAX_ROUNDS': '2'})
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['modelSummary']['claims'], [])
        self.assertTrue(report['manualRequired'])
        self.assertEqual([s['function']['name'] for s in calls[-1]['tools']], ['submit_report'])

    def test_near_token_budget_reserves_report_call(self):
        report, calls = self.run_agent([('get_inventory', {}), ('submit_report', report_args())],
            usages=[700, 50], env={'SUPPLY_AGENT_MAX_TOKENS': '1000'})
        self.assertEqual(report['status'], 'complete')
        self.assertEqual([s['function']['name'] for s in calls[-1]['tools']], ['submit_report'])

    def test_time_budget_reserves_partial_report(self):
        partial = {'status': 'partial', 'claims': [], 'unresolvedQuestions': ['时间不足，尚未取证'], 'nextSteps': []}
        # Clock calls: start, first-loop, request timeout, response, second-loop, timeout, response.
        with patch('project.agents.investigator.time.monotonic', side_effect=[0, 0, 0, 1, 11, 11, 12]):
            report, calls = self.run_agent([('submit_report', partial)], env={'SUPPLY_AGENT_MAX_SECONDS': '15'})
        self.assertEqual(report['status'], 'partial')
        self.assertEqual([s['function']['name'] for s in calls[-1]['tools']], ['submit_report'])

    def test_transport_failure_preserves_completed_evidence(self):
        report, _ = self.run_agent([('get_inventory', {})])  # Next mocked response raises: no network retry.
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['evidence'][0]['id'], 'inventory_snapshots:stock')
        self.assertEqual(report['trace'][-1]['tool'], 'get_inventory')
        self.assertEqual(report['modelSummary']['claims'], [])

    def test_snapshot_truncation_is_visible_to_model(self):
        evidence = [{'id': f'inventory_snapshots:{i}', 'table': 'inventory_snapshots', 'record': {'quantity': str(i)}} for i in range(21)]
        report, calls = self.run_agent([('get_inventory', {}), ('submit_report', report_args(
            refs=['inventory_snapshots:0'], limitation='仅查看了部分结果'))], evidence=evidence)
        tool_result = json.loads(calls[2]['messages'][-1]['content'])
        self.assertTrue(tool_result['truncated'])
        self.assertEqual(len(tool_result['items']), 20)
        self.assertEqual(report['status'], 'complete')

    def test_evidence_expansion_and_candidate_results_are_citable(self):
        evidence = {'id': 'sql:prior', 'table': 'sql_query', 'record': {'rows': [{'quantity': '800'}]}}
        reads = []
        def reader(identifier):
            reads.append(identifier)
            if identifier != evidence['id']:
                raise ValueError('不在关联调查内')
            return evidence
        report, _ = self.run_agent([('get_evidence', {'evidenceId': 'sql:prior'}),
            ('submit_report', report_args(refs=['sql:prior']))], evidence_reader=reader)
        self.assertEqual(reads, ['sql:prior'])
        self.assertEqual(report['status'], 'complete')
        self.assertIn(evidence, report['evidence'])
        report, calls = self.run_agent([('get_candidate_plans', {}),
            ('submit_report', report_args('已读取后端预计算方案', ['candidate:0']))],
            candidates=[{'kind': 'transfer', 'totalCost': '800', 'feasible': True}])
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(json.loads(calls[2]['messages'][-1]['content'])['items'][0]['record']['totalCost'], '800')
