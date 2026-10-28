"""T10宿主：真实run/证据授权、持久化预算和复用HTTP适配器；只使用本地HTTP mock。"""
from contextlib import contextmanager
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import httpx

from . import test_logistics_report as support
from .common import APIError
from project.agents.logistics_protocol import ProtocolError
from project.tests.test_logistics_protocol import plan, payload, report
from .logistics_model import output_mode


class LogisticsModelTests(unittest.TestCase):
    def test_report_quote_matches_actual_filtered_input_and_one_call(self):
        summary = self.call('get_snapshot_summary').json()
        tasks = self.call('get_pending_tasks', {'risk_state': 'overdue'}).json()
        refs = [summary['result_evidence_id'], tasks['result_evidence_id']]
        value = report()
        value['claims'][0].update(evidence_ids=[refs[0]],
            computed={'evidence_id': refs[0], 'metric': 'overdue',
                      'value': summary['data']['metrics']['overdue']['value']})
        question = '核对逾期规模与人员分布'
        requests, patched, _ = self.model([payload(value)])
        service = self.app.state.logistics_model
        quote = service.report_reservation(self.user, self.run, question, refs, {'old': 'completed'})
        with patched:
            result = service.generate('report', self.user, self.run, '冗长的收尾说明',
                evidence_ids=refs, compact=True, goal_question=question)
        self.assertEqual(quote['calls'], 1)
        self.assertEqual(quote['output_tokens'], 1200)
        self.assertEqual(quote['estimated_input_tokens'], result['request_budget_estimates'][0]['estimated_input_tokens'])
        self.assertEqual(quote['tokens'], quote['estimated_input_tokens']+quote['output_tokens']+512)
        self.assertEqual(json.loads(requests[0]['messages'][1]['content'])['question'], question)

    def test_verified_provider_mode_and_explicit_override(self):
        self.assertEqual(output_mode({'SUPPLY_MODEL': 'deepseek-chat'}), 'tool_call')
        self.assertEqual(output_mode({'SUPPLY_MODEL': 'deepseek-chat', 'LOGISTICS_MODEL_OUTPUT_MODE': 'json_object'}), 'json_object')
        self.assertEqual(output_mode({'SUPPLY_MODEL': 'other'}), 'json_text')
        self.assertEqual(output_mode({'SUPPLY_MODEL': 'deepseek-chat', 'SUPPLY_MODEL_BASE_URL': 'https://other.test'}), 'json_text')
    setUp = support.ReportTests.setUp
    tearDown = support.ReportTests.tearDown
    login = support.ReportTests.login
    create = support.ReportTests.create
    new_run = support.ReportTests.new_run
    call = support.ReportTests.call

    def model(self, responses, mode='json_text'):
        self.app.state.logistics_model.env = {'SUPPLY_MODEL': 'fixture', 'SUPPLY_MODEL_API_KEY': 'fixture',
            'SUPPLY_MODEL_BASE_URL': 'https://model.test', 'LOGISTICS_MODEL_OUTPUT_MODE': mode}
        requests = []
        iterator = iter(responses)
        def handler(request):
            requests.append(json.loads(request.content))
            response = next(iterator)
            if isinstance(response, Exception): raise response
            return response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return requests, patch('backend.investigation_model.httpx.Client', return_value=client), client

    def budget(self):
        return json.loads(self.app.state.logistics_runs.saved(self.run)['budget_json'])

    def test_adapter_reuses_transport_bound_scope_and_persists_repair(self):
        invalid = plan(); invalid['steps'][0]['parameters'] = {'region_ids': ['6']}
        requests, patched, client = self.model([payload(invalid), payload(plan())])
        with patched:
            result = self.app.state.logistics_model.generate('plan', self.user, self.run, '检查积压')
        self.assertTrue(client.is_closed)
        self.assertEqual(len(requests), 2)
        self.assertEqual(result['repairs'], 1)
        context = json.loads(requests[0]['messages'][1]['content'])
        self.assertEqual(context['server_scope']['region_ids'], ['5'])
        self.assertEqual(context['server_scope']['business_as_of'], '2000-07-02 10:00:00')
        self.assertEqual(result['scope']['run_id'], self.run)
        self.assertEqual(self.budget()['model_calls'], 2)
        self.assertEqual(self.budget()['model_tokens'], 20)
        self.assertEqual(self.budget()['model_prompt_versions']['plan']['version'], result['prompt_version'])
        self.assertEqual(self.budget()['model_prompt_versions']['plan']['sha256'], result['prompt_hash'])
        self.assertEqual(self.service.evidence_store.calls(self.user, self.run)['total_rows'], 0)

    def test_invalid_output_never_executes_business_tools_and_spends_calls(self):
        invalid = plan(); invalid['steps'][0]['tool'] = 'execute_sql'
        requests, patched, _ = self.model([payload(invalid), payload(invalid)])
        with patched, self.assertRaises(ProtocolError):
            self.app.state.logistics_model.generate('plan', self.user, self.run, '检查')
        self.assertEqual(len(requests), 2)
        self.assertEqual(self.budget()['model_calls'], 2)
        self.assertEqual(self.service.evidence_store.calls(self.user, self.run)['total_rows'], 0)

    def test_persisted_budget_unknown_usage_and_network_failure_block_retries(self):
        requests, patched, _ = self.model([RuntimeError('SECRET-KEY')])
        with patched, self.assertRaises(ProtocolError) as error:
            self.app.state.logistics_model.generate('plan', self.user, self.run, '检查')
        self.assertNotIn('SECRET', str(error.exception))
        self.assertEqual(self.budget()['model_calls'], 1)
        self.assertTrue(self.budget()['model_usage_unknown'])
        with patch('backend.investigation_model.model_session') as unused:
            with self.assertRaises(ProtocolError):
                self.app.state.logistics_model.generate('plan', self.user, self.run, '检查')
            unused.assert_not_called()
        self.assertEqual(self.budget()['model_calls'], 1)
        self.run = self.new_run('exhausted')
        value = self.budget(); value['model_calls'] = value['max_model_calls']
        self.data.db.execute('UPDATE logistics_runs SET budget_json=? WHERE run_id=?', (json.dumps(value), self.run))
        with patch('backend.logistics_model.model_session') as unused:
            with self.assertRaises(ProtocolError): self.app.state.logistics_model.generate('plan', self.user, self.run, '检查')
            unused.assert_not_called()

    def test_transport_diagnostic_is_persisted_on_actual_failed_attempt(self):
        invalid = plan(); invalid['steps'][0]['tool'] = 'execute_sql'
        requests, patched, client = self.model([payload(invalid), httpx.Response(503,
            content=b'PRIVATE-BODY', headers={'x-request-id': 'req-503'})])
        with patched, self.assertRaises(ProtocolError) as error:
            self.app.state.logistics_model.generate('plan', self.user, self.run, '检查')
        self.assertEqual(error.exception.code, 'model_transport_failed')
        self.assertEqual(len(requests), 2)
        self.assertTrue(client.is_closed)
        budget = self.budget()
        self.assertEqual(budget['model_calls'], 2)
        self.assertEqual(budget['model_tokens'], 10)
        self.assertEqual(budget['unknown_usage_calls'], 1)
        self.assertTrue(budget['model_transport_failed'])
        usage = budget['model_usage_records']
        self.assertNotIn('transport_failure', usage[0])
        self.assertEqual(usage[1]['request_attempt'], 1)
        self.assertEqual(usage[1]['kind'], 'plan')
        self.assertIsNone(usage[1]['total_tokens'])
        diagnostic = usage[1]['transport_failure']
        self.assertEqual(diagnostic['exception_type'], 'HTTPStatusError')
        self.assertEqual(diagnostic['http_status'], 503)
        self.assertEqual(diagnostic['provider_request_id'], 'req-503')
        self.assertNotIn('fixture', json.dumps(diagnostic))
        self.assertNotIn('PRIVATE-BODY', json.dumps(diagnostic))
        with patch('backend.logistics_model.model_session') as unused, self.assertRaises(ProtocolError):
            self.app.state.logistics_model.generate('plan', self.user, self.run, '检查')
        unused.assert_not_called()
        self.assertEqual(self.budget()['model_calls'], 2)

    def test_evidence_cross_run_and_computed_numbers_checked_against_store(self):
        result = self.call('get_snapshot_summary').json(); ref = result['result_evidence_id']
        value = report(); value['evidence_ids'] = [ref]
        value['claims'][0]['evidence_ids'] = [ref]; value['claims'][0]['computed']['evidence_id'] = ref
        self.app.state.logistics_model.validate('report', self.user, self.run, value, evidence_ids=[ref])
        value['claims'][0]['computed']['value'] = 0
        with self.assertRaisesRegex(ProtocolError, 'computed_mismatch'):
            self.app.state.logistics_model.validate('report', self.user, self.run, value, evidence_ids=[ref])
        another = self.new_run('different')
        with self.assertRaises(APIError): self.app.state.logistics_model.context(self.user, another, [ref])
        value = plan(); value['T'] = 'future'
        with self.assertRaises(ProtocolError): self.app.state.logistics_model.validate('plan', self.user, self.run, value)

    def test_revocation_before_call_and_during_response_blocks_delivery(self):
        requests, patched, _ = self.model([payload(plan())])
        self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'")
        with patched, self.assertRaises(APIError): self.app.state.logistics_model.generate('plan', self.user, self.run, '检查')
        self.assertEqual(requests, [])
        self.data.db.execute("UPDATE logistics_grants SET regions='[\"5\"]' WHERE account='reader'")
        @contextmanager
        def session(env, *, on_failure=None):
            def request(*args):
                self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'")
                return payload(plan())
            yield request
        with patch('backend.logistics_model.model_session', session), self.assertRaises(APIError):
            self.app.state.logistics_model.generate('plan', self.user, self.run, '检查')
        self.assertEqual(self.budget()['model_calls'], 1)
        self.assertEqual(self.budget()['model_tokens'], 10)

    def test_bad_config_and_nonowner_do_not_open_transport(self):
        service = self.app.state.logistics_model
        for base in ('http://model.test', 'https://user:SECRET@model.test', 'https://model.test?key=SECRET'):
            service.env = {'SUPPLY_MODEL': 'fixture', 'SUPPLY_MODEL_API_KEY': 'fixture', 'SUPPLY_MODEL_BASE_URL': base}
            with patch('backend.logistics_model.model_session') as unused, self.assertRaises(ProtocolError):
                service.generate('plan', self.user, self.run, '检查')
            unused.assert_not_called()
        reviewer = self.app.state.auth.query("SELECT * FROM users WHERE account='reviewer'")
        with self.assertRaises(APIError): service.context(reviewer, self.run)

    def test_context_compression_is_not_query_truncation(self):
        saved = self.call('get_pending_tasks', {'page_size': 20}).json()
        ref = saved['result_evidence_id']
        self.assertGreater(len(saved['rows']), 3)
        self.assertFalse(saved['truncated'])
        value = {'plan_version': 1, 'step_id': 's1', 'new_facts': [], 'hypothesis_assessments': [],
                 'missing': [], 'counterevidence': [], 'unresolved_questions': [], 'next_steps': []}
        requests, patched, _ = self.model([payload(value)])
        with patched:
            self.app.state.logistics_model.generate('observation', self.user, self.run, '步骤 s1',
                evidence_ids=[ref], plan=plan(), step_states={'s1': 'completed'}, compact=True, context_refs=[ref])
        context = json.loads(requests[0]['messages'][1]['content'])
        record = context['evidence'][ref]['record']
        self.assertFalse(record['truncated'])
        self.assertFalse(record['query_truncated'])
        self.assertTrue(record['context_truncated'])
        self.assertEqual(len(record['rows']), 3)
        self.assertEqual(context['overdue_coverage']['global_overdue'], saved['denominator']['overdue'])


if __name__ == '__main__': unittest.main()
