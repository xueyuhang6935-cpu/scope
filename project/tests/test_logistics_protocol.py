"""T10纯协议攻击与模型修复边界；无网络和数据库。"""
from copy import deepcopy
from hashlib import sha256
import json
import unittest
from unittest.mock import patch

from project.agents.logistics_protocol import ProtocolError, validate_output, validate_plan, strict_json, next_step
from project.agents.logistics_model import generate, ModelBudget, model_body, model_output
from project.prompts.loader import load_prompt


def plan():
    return {'goal': '调查积压', 'version': 1, 'hypotheses': [{'hypothesis_id': 'h1', 'statement': '积压集中'}],
            'steps': [{'step_id': 's1', 'question': '当前积压多少', 'tool': 'get_snapshot_summary', 'parameters': {},
                       'depends_on': [], 'completion_condition': '取得汇总证据', 'status': 'pending'},
                      {'step_id': 's2', 'question': '按快递员分布如何', 'tool': 'get_risk_distribution', 'parameters': {'dimension': 'courier_id'},
                       'depends_on': ['s1'], 'completion_condition': '取得风险分布', 'status': 'pending'}], 'revision_reason': ''}


SCOPE = {'run_id': 'r1', 'workspace_id': 'w1', 'snapshot_id': 'snp1', 'business_as_of': '2000-07-02 10:00:00',
         'fingerprint': 'hash', 'metric_version': 'logistics-metrics-v1'}


def evidence():
    record = {k: SCOPE[k] for k in ('run_id', 'snapshot_id', 'business_as_of', 'fingerprint', 'metric_version')}
    record.update(tool='get_snapshot_summary', status='ok', truncated=False, data={'metrics': {'backlog': {'value': 5, 'status': 'ok'}}})
    return {'e1': {k: SCOPE[k] for k in ('run_id', 'workspace_id', 'snapshot_id')} | {'source_type': 'tool_result', 'record': record}}


def report():
    return {'status': 'complete', 'claims': [{'statement': '样本内积压5条', 'kind': 'computed', 'evidence_ids': ['e1'],
        'limitation': '', 'computed': {'evidence_id': 'e1', 'metric': 'backlog', 'value': 5}}], 'evidence_ids': ['e1'],
        'limitations': ['历史完成样本'], 'unresolved_questions': [], 'next_steps': []}


def payload(value, tokens=10, mode='json_text', kind='plan'):
    message = {'content': json.dumps(value, ensure_ascii=False)}
    if mode == 'tool_call':
        message = {'content': None, 'tool_calls': [{'id': 'p1', 'type': 'function', 'function': {'name': 'submit_'+kind, 'arguments': json.dumps(value)}}]}
    return {'choices': [{'finish_reason': 'tool_calls' if mode == 'tool_call' else 'stop', 'message': message}], 'usage': {'total_tokens': tokens}}


class ProtocolTests(unittest.TestCase):
    def test_missing_noncomputed_field_normalized_without_mutating_output(self):
        for kind in ('observed', 'inferred', 'reported'):
            value = report(); ev = evidence()
            value['claims'][0].update(kind=kind, limitation='来源存在局限')
            value['claims'][0].pop('computed')
            if kind == 'reported':
                ev['e1']['source_type'] = 'manual_statement'
            original = deepcopy(value)
            result = validate_output('report', value, scope=SCOPE, evidence=ev)
            self.assertIsNone(result['claims'][0]['computed'])
            self.assertEqual(value, original)

    def test_normalization_does_not_fill_real_metrics_or_other_required_fields(self):
        value = report(); value['claims'][0].pop('computed')
        with self.assertRaisesRegex(ProtocolError, 'missing_field'):
            self.validate('report', value)
        for field in ('evidence_ids', 'statement', 'kind'):
            value = report(); value['claims'][0].update(kind='observed')
            value['claims'][0].pop('computed'); value['claims'][0].pop(field)
            with self.assertRaises(ProtocolError):
                self.validate('report', value)
        value = report(); value['claims'][0].update(kind='observed')
        with self.assertRaisesRegex(ProtocolError, 'unexpected_computed'):
            self.validate('report', value)

    def test_model_normalization_avoids_repair_and_retains_audit(self):
        value = report(); value['claims'][0].update(kind='observed')
        value['claims'][0]['fact'] = {'type': 'summary', 'evidence_id': 'e1', 'metric': 'backlog'}
        value['claims'][0].pop('computed')
        calls = []
        def request(*args):
            calls.append(args)
            return payload(value)
        result = generate('report', '查看积压', scope=SCOPE, evidence=evidence(), plan=None,
                          step_states={}, request=request, budget=ModelBudget())
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['repairs'], 0)
        self.assertEqual(result['raw_value'], value)
        self.assertEqual(result['normalization_paths'], ['$.claims[0].computed'])
        self.assertIsNone(result['value']['claims'][0]['computed'])

    def validate(self, kind, value, **options):
        return validate_output(kind, value, scope=SCOPE, evidence=evidence(), **options)

    def test_derived_references_and_defaults_are_audited(self):
        value = report(); value.pop('evidence_ids')
        value['claims'][0].update(kind='observed'); value['claims'][0].pop('computed')
        value['claims'][0]['fact'] = {'type': 'summary', 'evidence_id': 'e1', 'metric': 'backlog'}
        value['claims'][0].pop('limitation')
        value['claims'].append(deepcopy(value['claims'][0]))
        result = generate('report', '查看积压', scope=SCOPE, evidence=evidence(), plan=None,
                          step_states={}, request=lambda *_: payload(value), budget=ModelBudget())
        self.assertEqual(result['value']['evidence_ids'], ['e1'])
        self.assertIn('不代表生产全量风险', result['value']['claims'][0]['limitation'])
        self.assertEqual(result['raw_value'], value)
        self.assertIn('$.evidence_ids', result['normalization_paths'])
        self.assertEqual(result['repairs'], 0)

    def test_default_limitation_does_not_hide_required_limits_or_complete_status(self):
        value = report(); value['claims'][0].update(kind='observed'); value['claims'][0].pop('computed')
        value['claims'][0].pop('limitation')
        ev = evidence(); ev['e1']['record']['truncated'] = True
        with self.assertRaisesRegex(ProtocolError, 'limitation_required'):
            validate_output('report', value, scope=SCOPE, evidence=ev)
        for kind in ('inferred', 'reported'):
            value['claims'][0]['kind'] = kind
            with self.assertRaisesRegex(ProtocolError, 'missing_field'):
                self.validate('report', value)
        value = report(); value.pop('status')
        with self.assertRaisesRegex(ProtocolError, 'missing_field'): self.validate('report', value)
        value = report(); value['unresolved_questions'] = ['目标尚未回答']
        with self.assertRaisesRegex(ProtocolError, 'incomplete_report'): self.validate('report', value)

    def test_invalid_local_references_not_repaired_by_total_list(self):
        value = report(); value['claims'][0]['evidence_ids'] = ['unknown']
        value['evidence_ids'] = ['e1']
        with self.assertRaisesRegex(ProtocolError, 'evidence_scope'): self.validate('report', value)

    def test_plan_graph_parameters_states_and_nonmutation(self):
        original = plan(); saved = deepcopy(original)
        result = self.validate('plan', original)
        self.assertEqual(original, saved)
        self.assertEqual(result['steps'][0]['parameters']['horizon_minutes'], 30)
        self.assertEqual(next_step(result, {})['step_id'], 's1')
        self.assertEqual(next_step(result, {'s1': 'completed'})['step_id'], 's2')
        self.assertIsNone(next_step(result, {'s1': 'failed'}))
        attacks = [('tool', 'execute_sql'), ('parameters', {'region_ids': ['6']}), ('parameters', {'T': 'future'}),
                   ('parameters', {'horizon_minutes': True}), ('status', 'completed'), ('depends_on', ['absent']), ('depends_on', ['s1'])]
        for key, value in attacks:
            with self.subTest(key=key, value=value), self.assertRaises(ProtocolError):
                wrong = plan(); wrong['steps'][0][key] = value; self.validate('plan', wrong)
        for key in ('scope', 'T', 'run_id', 'snapshot_id'):
            wrong = plan(); wrong[key] = 'override'
            with self.assertRaises(ProtocolError): self.validate('plan', wrong)
        cyclic = plan(); cyclic['steps'][0]['depends_on'] = ['s2']
        with self.assertRaisesRegex(ProtocolError, 'cyclic_dependency'): self.validate('plan', cyclic)
        duplicate = plan(); duplicate['steps'][1]['step_id'] = 's1'
        with self.assertRaises(ProtocolError): self.validate('plan', duplicate)
        unread = plan(); unread['steps'][0].update(tool='get_evidence', parameters={'evidence_id': 'not-read'})
        with self.assertRaises(ProtocolError): self.validate('plan', unread)

    def test_plan_revision_cannot_rewrite_execution(self):
        old = validate_plan(plan()); new = deepcopy(old); new.update(version=2, revision_reason='根据新证据修订')
        new['steps'][0]['status'] = 'completed'
        self.validate('plan', new, plan=old, step_states={'s1': 'completed'})
        new['steps'][0]['question'] = '改写已完成步骤'
        with self.assertRaises(ProtocolError): self.validate('plan', new, plan=old, step_states={'s1': 'completed'})
        new = deepcopy(old); new.update(version=2, revision_reason='')
        with self.assertRaises(ProtocolError): self.validate('plan', new, plan=old)

    def test_observation_requires_completed_step_and_actual_evidence(self):
        value = {'plan_version': 1, 'step_id': 's1', 'new_facts': [{'statement': '样本内积压5条', 'evidence_ids': ['e1']}],
                 'hypothesis_assessments': [{'hypothesis_id': 'h1', 'verdict': 'unknown', 'evidence_ids': [], 'reason': '尚未分组'}],
                 'missing': ['缺少排班'], 'counterevidence': [], 'unresolved_questions': ['是否真实容量不足'], 'next_steps': ['查看风险分布']}
        self.validate('observation', value, plan=plan(), step_states={'s1': 'completed'})
        with self.assertRaises(ProtocolError): self.validate('observation', value, plan=plan(), step_states={})
        value['new_facts'][0]['evidence_ids'] = ['invented']
        with self.assertRaises(ProtocolError): self.validate('observation', value, plan=plan(), step_states={'s1': 'completed'})

    def test_report_computed_citations_sources_and_partial(self):
        self.validate('report', report())
        wrong = report(); wrong['claims'][0]['computed']['value'] = 0
        with self.assertRaisesRegex(ProtocolError, 'computed_mismatch'): self.validate('report', wrong)
        wrong = report(); wrong['claims'][0].update(kind='inferred', computed=None)
        with self.assertRaisesRegex(ProtocolError, 'limitation_required'): self.validate('report', wrong)
        wrong['claims'][0]['limitation'] = '需要核实排班'; self.validate('report', wrong)
        wrong['claims'][0]['kind'] = 'reported'
        with self.assertRaisesRegex(ProtocolError, 'reported_source_required'): self.validate('report', wrong)
        wrong = report(); wrong['evidence_ids'] = []
        self.assertEqual(self.validate('report', wrong)['evidence_ids'], ['e1'])
        ev = evidence(); ev['e1']['run_id'] = 'other'
        with self.assertRaisesRegex(ProtocolError, 'evidence_scope'):
            validate_output('report', report(), scope=SCOPE, evidence=ev)
        partial = {'status': 'partial', 'claims': [], 'evidence_ids': [], 'limitations': [], 'unresolved_questions': ['没有有效证据'], 'next_steps': []}
        self.validate('report', partial)
        partial['status'] = 'complete'
        with self.assertRaises(ProtocolError): self.validate('report', partial)

    def test_strict_json_rejects_duplicate_nan_markdown_and_bools(self):
        for text in ('{"goal":1,"goal":2}', '{"x":NaN}', '{"x":1e999}', '```json\n{}\n```', '{} trailing', '[]'):
            with self.subTest(text=text), self.assertRaises(ProtocolError): strict_json(text)
        wrong = plan(); wrong['version'] = True
        with self.assertRaises(ProtocolError): self.validate('plan', wrong)

    def test_report_source_types_truncation_and_unavailable(self):
        ev = evidence(); value = report()
        value['claims'][0].update(kind='observed', computed=None)
        validate_output('report', value, scope=SCOPE, evidence=ev)
        ev['e1']['source_type'] = 'manual_statement'
        with self.assertRaises(ProtocolError): validate_output('report', value, scope=SCOPE, evidence=ev)
        value['claims'][0].update(kind='reported', limitation='人工陈述尚未复核')
        validate_output('report', value, scope=SCOPE, evidence=ev)
        ev = evidence(); ev['e1']['record']['truncated'] = True
        with self.assertRaises(ProtocolError): validate_output('report', report(), scope=SCOPE, evidence=ev)
        ev = evidence(); ev['e1']['record']['data']['metrics']['backlog'] = {'value': None, 'status': 'unavailable'}
        value = report(); value['claims'][0]['computed']['value'] = None
        with self.assertRaises(ProtocolError): validate_output('report', value, scope=SCOPE, evidence=ev)
        value['claims'][0]['limitation'] = '指标不可计算，不能当作零风险'
        validate_output('report', value, scope=SCOPE, evidence=ev)

    def test_modes_accept_only_matching_single_complete_protocol(self):
        for mode in ('json_text', 'json_object', 'json_schema', 'tool_call'):
            body = model_body('plan', [], mode, 200)
            self.assertEqual(body['stream'], False)
            self.assertEqual(model_output(payload(plan(), mode=mode), 'plan', mode), plan())
        wrong = payload(plan(), mode='tool_call'); wrong['choices'][0]['message']['tool_calls'][0]['function']['name'] = 'execute_sql'
        with self.assertRaises(ProtocolError): model_output(wrong, 'plan', 'tool_call')
        wrong = payload(plan()); wrong['choices'][0]['finish_reason'] = 'length'
        with self.assertRaises(ProtocolError): model_output(wrong, 'plan', 'json_text')

    def test_repairs_spend_total_budget_no_unvalidated_execution(self):
        invalid = plan(); invalid['steps'][0]['tool'] = 'execute_sql'
        responses = iter([payload(invalid), payload(plan())]); requests = []; reserved = []; usages = []
        budget = ModelBudget(max_calls=2)
        def request(body, started, seconds): requests.append(body); return next(responses)
        result = generate('plan', '调查积压', scope=SCOPE, evidence=evidence(), plan=None, step_states={}, request=request,
                          budget=budget, reserve=lambda: reserved.append(1), record_usage=usages.append)
        self.assertEqual(result['model_calls'], 2); self.assertEqual(result['repairs'], 1)
        self.assertEqual(len(reserved), 2); self.assertEqual(usages, [10, 10])
        self.assertNotIn('execute_sql', requests[1]['messages'][-1]['content'])
        for limit in (1, 2):
            calls = []
            with self.assertRaises(ProtocolError):
                generate('plan', '调查', scope=SCOPE, evidence={}, plan=None, step_states={}, budget=ModelBudget(max_calls=limit),
                         request=lambda *args: calls.append(1) or payload(invalid))
            self.assertEqual(len(calls), limit)

    def test_unknown_usage_and_transport_failure_do_not_retry(self):
        invalid = plan(); invalid['scope'] = 'bad'
        for response in (payload(invalid, tokens=None), None):
            calls = []; budget = ModelBudget()
            def request(*args):
                calls.append(1)
                if response is None: raise RuntimeError('SECRET-CONNECTION')
                return response
            with self.assertRaises(ProtocolError) as caught:
                generate('plan', '调查', scope=SCOPE, evidence={}, plan=None, step_states={}, budget=budget, request=request)
            self.assertEqual(len(calls), 1); self.assertTrue(budget.usage_unknown)
            self.assertNotIn('SECRET', str(caught.exception))

    def test_call_token_and_elapsed_budget_fence_response(self):
        for budget in (ModelBudget(max_calls=0), ModelBudget(max_tokens=5)):
            calls = []
            with self.assertRaisesRegex(ProtocolError, 'model_budget_exhausted'):
                generate('plan', '调查', scope=SCOPE, evidence={}, plan=None, step_states={}, budget=budget,
                         request=lambda *args: calls.append(1) or payload(plan(), tokens=10))
            self.assertEqual(len(calls), 0)  # 输入预算预检在发请求前拒绝。
        with patch('project.agents.logistics_model.time.monotonic', side_effect=[0, 0, 6]), self.assertRaisesRegex(ProtocolError, 'model_budget_exhausted'):
            generate('plan', '调查', scope=SCOPE, evidence={}, plan=None, step_states={}, budget=ModelBudget(max_seconds=5),
                     request=lambda *args: payload(plan()))
        for value in (True, float('nan'), float('inf')):
            with self.assertRaises(ValueError): ModelBudget(max_seconds=value)

    def test_four_prompts_are_hashed_and_authority_is_program_state(self):
        for kind in ('intent', 'plan', 'observation', 'report'):
            text, version = load_prompt('logistics_'+kind)
            self.assertIn(sha256(text.encode()).hexdigest()[:12], version)
            self.assertIn('server_scope', text)

    def test_targeted_repair_names_missing_computed_field(self):
        invalid = report(); invalid['claims'][0].pop('computed')
        responses = iter([payload(invalid), payload(report())]); requests = []
        def request(body, *_):
            requests.append(body)
            return next(responses)
        result = generate('report', '查看积压', scope=SCOPE, evidence=evidence(), plan=None,
                          step_states={}, request=request, budget=ModelBudget())
        instruction = requests[1]['messages'][-1]['content']
        self.assertIn('$.claims[0].computed', instruction)
        self.assertIn('真实的evidence_id、metric和value', instruction)
        self.assertEqual(result['repairs'], 1)

    def test_single_report_reserved_and_repair_uses_actual_remaining_budget(self):
        calls = []
        result = generate('report', '查看积压', scope=SCOPE, evidence=evidence(), plan=None,
                          step_states={}, request=lambda body, *_: calls.append(body) or payload(report()),
                          budget=ModelBudget(max_tokens=16000))
        estimate = result['request_budget_estimates'][0]
        self.assertFalse(estimate['repair_reserved'])
        self.assertLessEqual(estimate['estimated_input_tokens']+calls[0]['max_tokens']+512, 16000)
        calls.clear()
        with self.assertRaisesRegex(ProtocolError, 'report_budget_reserved'):
            generate('observation', '检查步骤', scope=SCOPE, evidence={}, plan=None,
                     step_states={}, request=lambda *_: calls.append(1), budget=ModelBudget(max_tokens=1000),
                     protected_tokens=900)
        self.assertEqual(calls, [])

        invalid = report(); invalid['claims'][0].pop('computed')
        with patch('project.agents.logistics_model.estimate_input_tokens', return_value=1000):
            calls = []
            result = generate('report', '查看积压', scope=SCOPE, evidence=evidence(), plan=None,
                step_states={}, request=lambda body, *_: calls.append(body) or payload(report()),
                budget=ModelBudget(max_tokens=3912))
            self.assertEqual(len(calls), 1)  # 恰够一次输入+完整输出+缓冲，不要求两份额度。
            self.assertEqual(result['repairs'], 0)
            calls.clear()
            with self.assertRaisesRegex(ProtocolError, 'model_budget_exhausted'):
                generate('report', '查看积压', scope=SCOPE, evidence=evidence(), plan=None,
                    step_states={}, request=lambda body, *_: calls.append(body) or payload(invalid, tokens=1600),
                    budget=ModelBudget(max_tokens=3912))
            self.assertEqual(len(calls), 1)  # 首次真实消耗后无法修复，不发第二次请求。
            calls.clear()
            responses = iter([payload(invalid, tokens=1100), payload(report())])
            result = generate('report', '查看积压', scope=SCOPE, evidence=evidence(), plan=None,
                step_states={}, request=lambda body, *_: calls.append(body) or next(responses),
                budget=ModelBudget(max_tokens=3912))
            self.assertEqual(result['repairs'], 1)
            self.assertEqual([body['max_tokens'] for body in calls], [2400, 1300])
            calls.clear()
            generate('report', '查看积压', scope=SCOPE, evidence=evidence(), plan=None,
                step_states={}, request=lambda body, *_: calls.append(body) or payload(report()),
                budget=ModelBudget(max_tokens=2712))
            self.assertEqual(calls[0]['max_tokens'], 1200)
            calls.clear()
            with self.assertRaisesRegex(ProtocolError, 'model_budget_exhausted'):
                generate('report', '查看积压', scope=SCOPE, evidence=evidence(), plan=None,
                    step_states={}, request=lambda *_: calls.append(1), budget=ModelBudget(max_tokens=2711))
            self.assertEqual(calls, [])
            with self.assertRaises(ProtocolError):
                generate('report', '查看积压', scope=SCOPE, evidence=evidence(), plan=None,
                    step_states={}, request=lambda body, *_: calls.append(body) or payload(invalid),
                    budget=ModelBudget(max_repairs=2))
            self.assertEqual(len(calls), 2)  # 报告始终最多修复一次。

    def test_tiny_observation_output_is_rejected_before_transport(self):
        calls = []
        with patch('project.agents.logistics_model.estimate_input_tokens', return_value=1000):
            with self.assertRaisesRegex(ProtocolError, 'report_budget_reserved'):
                generate('observation', '检查步骤', scope=SCOPE, evidence={}, plan=None,
                    step_states={}, request=lambda *_: calls.append(1), protected_tokens=1000,
                    budget=ModelBudget(max_tokens=1000+1000+512+184))
        self.assertEqual(calls, [])


if __name__ == '__main__': unittest.main()
