"""覆盖、截断和集中程度的定向回归；不调用网络或数据库。"""
from copy import deepcopy
import json
import unittest

from project.agents.logistics_coverage import overdue_coverage, goal_evidence_status, goal_followup
from project.agents.logistics_model import generate, ModelBudget, build_messages, proposal_schema
from project.agents.logistics_protocol import validate_output, ProtocolError, validate_report_facts, report_fact_options, REPORT_SCHEMA
from project.tests.test_logistics_protocol import SCOPE, report, payload, evidence


def fixture(total=3, counts=(('12477', 2), ('9485', 1))):
    header = {k: SCOPE[k] for k in ('run_id', 'snapshot_id', 'business_as_of', 'fingerprint', 'metric_version')}
    record = {**header, 'tool': 'get_risk_distribution', 'status': 'ok',
              'data': {'dimension': 'courier_id'}, 'denominator': {'overdue': total},
              'truncated': True, 'total_rows': 28, 'rows': [
                  {'courier_id': c, 'metrics': {'overdue': {'value': n, 'denominator': total, 'status': 'ok'}}}
                  for c, n in counts]}
    return {'e1': {k: SCOPE[k] for k in ('run_id', 'workspace_id', 'snapshot_id')} |
            {'source_type': 'tool_result', 'record': record}}


def assessed_report(ev):
    coverage = overdue_coverage(ev, SCOPE)
    candidate = coverage['couriers'][0]
    value = report()
    value['claims'][0].update(kind='observed', computed=None, statement='重点人员逾期占全局超过一半', limitation='人员名单分页展示')
    value['overdue_assessment'] = {k: candidate[k] for k in ('courier_id', 'overdue_count', 'verdict')}
    value['overdue_assessment'].update(global_overdue=coverage['global_overdue'],
                                     claim_index=0, missing_evidence=[], evidence_ids=['e1'])
    return value


def citation_fixture():
    ev = {'A': evidence()['e1'], 'B': fixture(counts=(('12477', 2),))['e1']}
    ev['A']['record']['data']['metrics']['overdue'] = {'value': 3, 'status': 'ok'}
    ev['C'] = deepcopy(ev['B'])
    ev['C']['record'].update(tool='get_pending_tasks', data={'risk_state': 'overdue'},
        rows=[{'evidence_id': 't3', 'courier_id': '9485', 'risk_state': 'overdue'}])
    value = assessed_report(fixture())
    value['claims'][0]['evidence_ids'] = ['A', 'B']
    value['overdue_assessment']['evidence_ids'] = ['B', 'A', 'C']
    value['evidence_ids'] = ['A', 'B', 'C']
    return ev, value


class CoverageTests(unittest.TestCase):
    def test_body_proof_allows_extra_assessment_ref_and_preserves_raw_audit(self):
        ev, value = citation_fixture()
        original = deepcopy(value)
        result = generate('report', '逾期是否过半集中', scope=SCOPE, evidence=ev, plan=None,
                          step_states={}, request=lambda *_: payload(value), budget=ModelBudget())
        final = result['value']
        self.assertEqual(result['repairs'], 0)
        self.assertEqual(final['overdue_assessment']['evidence_ids'], ['A', 'B'])
        self.assertEqual(final['evidence_ids'], ['A', 'B'])
        self.assertIn('66.7%，超过一半', final['claims'][0]['statement'])
        self.assertIn('尚缺1条逾期归属', final['claims'][0]['limitation'])
        self.assertNotIn('已完整归属', final['claims'][0]['limitation'])
        self.assertEqual(result['raw_value'], original)
        self.assertEqual(value, original)
        self.assertEqual(validate_output('report', final, scope=SCOPE, evidence=ev), final)
        with self.assertRaisesRegex(ProtocolError, 'overdue_assessment_claim'):
            validate_output('report', value, scope=SCOPE, evidence=ev)

    def test_assessment_refs_optional_only_for_new_proposals_and_single_proof_suffices(self):
        ev, value = citation_fixture()
        value['claims'][0]['evidence_ids'] = ['B']  # 聚合工具同时提供分子与全局分母。
        value['overdue_assessment'].pop('evidence_ids')
        value.pop('evidence_ids')
        self.assertNotIn('evidence_ids', proposal_schema('report')['properties']['overdue_assessment']['required'])
        self.assertIn('evidence_ids', REPORT_SCHEMA['properties']['overdue_assessment']['required'])
        result = generate('report', '逾期是否过半集中', scope=SCOPE, evidence=ev, plan=None,
                          step_states={}, request=lambda *_: payload(value), budget=ModelBudget())
        self.assertEqual(result['repairs'], 0)
        self.assertEqual(result['value']['overdue_assessment']['evidence_ids'], ['B'])
        self.assertEqual(result['value']['evidence_ids'], ['B'])
        self.assertNotIn('evidence_ids', result['raw_value']['overdue_assessment'])
        with self.assertRaisesRegex(ProtocolError, 'missing_field'):
            validate_output('report', value, scope=SCOPE, evidence=ev)

    def test_assessment_only_evidence_cannot_supply_missing_body_proof(self):
        ev, value = citation_fixture()
        value['claims'][0]['evidence_ids'] = ['A']  # 只有全局分母，缺少人员计数。
        with self.assertRaisesRegex(ProtocolError, 'overdue_assessment_mismatch'):
            validate_output('report', value, scope=SCOPE, evidence=ev, render_limits=True)
        value['claims'][0]['evidence_ids'] = ['B']
        ev['B']['record'].pop('denominator')  # 判断中的A不能代补正文缺少的分母。
        with self.assertRaisesRegex(ProtocolError, 'overdue_assessment_mismatch'):
            validate_output('report', value, scope=SCOPE, evidence=ev, render_limits=True)

    def test_body_proof_still_checks_numbers_person_verdict_and_binding(self):
        ev, value = citation_fixture()
        for key, wrong_value in (('courier_id', '9485'), ('overdue_count', 1),
                                 ('global_overdue', 4), ('verdict', 'not_over_half')):
            with self.subTest(key=key):
                wrong = deepcopy(value); wrong['overdue_assessment'][key] = wrong_value
                with self.assertRaisesRegex(ProtocolError, 'overdue_assessment_mismatch'):
                    validate_output('report', wrong, scope=SCOPE, evidence=ev, render_limits=True)
        for index, code in ((1, 'overdue_assessment_claim'), (-1, 'invalid_number'), (True, 'invalid_type')):
            with self.subTest(index=index):
                wrong = deepcopy(value); wrong['overdue_assessment']['claim_index'] = index
                with self.assertRaisesRegex(ProtocolError, code):
                    validate_output('report', wrong, scope=SCOPE, evidence=ev, render_limits=True)
        value['claims'][0].update(kind='computed', computed={'evidence_id': 'A', 'metric': 'backlog', 'value': 5})
        with self.assertRaisesRegex(ProtocolError, 'overdue_assessment_claim'):
            validate_output('report', value, scope=SCOPE, evidence=ev, render_limits=True)

    def test_citations_keep_scope_checks_in_body_and_optional_assessment(self):
        for location in ('body', 'assessment'):
            for scope_key in ('run_id', 'snapshot_id', 'workspace_id'):
                with self.subTest(location=location, scope_key=scope_key):
                    ev, value = citation_fixture()
                    ev['foreign'] = deepcopy(ev['C']); ev['foreign'][scope_key] = 'foreign'
                    refs = value['claims'][0]['evidence_ids'] if location == 'body' else value['overdue_assessment']['evidence_ids']
                    refs.append('foreign')
                    with self.assertRaisesRegex(ProtocolError, 'evidence_scope'):
                        validate_output('report', value, scope=SCOPE, evidence=ev, render_limits=True)
            ev, value = citation_fixture()
            refs = value['claims'][0]['evidence_ids'] if location == 'body' else value['overdue_assessment']['evidence_ids']
            refs.append('unread')
            with self.assertRaisesRegex(ProtocolError, 'evidence_scope'):
                validate_output('report', value, scope=SCOPE, evidence=ev, render_limits=True)

    def test_ref_used_by_another_body_claim_remains_in_total_citations(self):
        ev, value = citation_fixture()
        value['claims'].append({'statement': '归属观察仍需核对', 'kind': 'inferred',
            'evidence_ids': ['C'], 'computed': None, 'limitation': '待核实推断'})
        final = validate_output('report', value, scope=SCOPE, evidence=ev, render_limits=True)
        self.assertEqual(final['overdue_assessment']['evidence_ids'], ['A', 'B'])
        self.assertEqual(final['evidence_ids'], ['A', 'B', 'C'])

    def test_next_page_is_from_real_contiguous_query_and_can_fill_gap(self):
        ev = fixture(); record = ev['e1']['record']
        record.update(tool='get_pending_tasks', page=1, page_size=2, total_rows=3,
            data={'risk_state': 'overdue'}, rows=[{'evidence_id': 't'+c, 'courier_id': c,
            'risk_state': 'overdue'} for c in ('a', 'b')])
        args = {'risk_state': 'overdue', 'sort': 'overdue_minutes_desc', 'page': 1, 'page_size': 2,
                'horizon_minutes': 30, 'recent_minutes': 60}
        trace = [{'tool': 'get_pending_tasks', 'parameters': args, 'evidence_id': 'e1', 'status': 'ok'}]
        candidate = goal_followup('核对逾期规模与人员分布，首页不足补查', ev, SCOPE, trace)
        self.assertEqual(candidate['parameters'], {**args, 'page': 2})
        record['page'] = 2; trace[0]['parameters']['page'] = 2
        self.assertIsNone(goal_followup('核对逾期规模', ev, SCOPE, trace))
        record['status'] = 'error'
        self.assertIsNone(goal_followup('核对逾期规模', ev, SCOPE, trace))

    def test_report_view_filters_goal_but_full_evidence_still_validates(self):
        from project.tests.test_logistics_protocol import evidence
        ev = fixture(); ev['summary'] = evidence()['e1']
        ev['summary']['record']['data']['metrics']['overdue'] = {'value': 3, 'status': 'ok'}
        messages, *_ = build_messages('report', '核对逾期规模和人员分布', scope=SCOPE,
            evidence=ev, plan={'irrelevant': 'x'*10000}, step_states={'s1': 'completed'}, observations=[{'old': 'x'}])
        context = json.loads(messages[1]['content'])
        self.assertEqual(context['plan'], {})
        self.assertEqual(context['step_states'], {})
        self.assertEqual(context['observations'], [])
        self.assertEqual(context['evidence']['summary']['record']['data']['metrics'].keys(), {'overdue'})
        self.assertFalse(any(o['fact'].get('metric') == 'backlog' for o in context['report_fact_options']))
        self.assertIn('backlog', ev['summary']['record']['data']['metrics'])
        wrong = assessed_report(ev); wrong['claims'][0]['evidence_ids'] = ['other-run']
        with self.assertRaisesRegex(ProtocolError, 'evidence_scope'):
            validate_output('report', wrong, scope=SCOPE, evidence=ev, render_limits=True)

    def test_new_limits_are_generated_and_old_reports_are_unchanged(self):
        ev = fixture(); value = assessed_report(ev)
        value['claims'][0]['limitation'] = '完整覆盖仍可能遗漏同口径逾期'
        value['limitations'] = ['get_quality_summary工具不可用']
        ev['quality'] = deepcopy(ev['e1'])
        ev['quality']['record'].update(tool='get_quality_summary', status='unavailable',
            data={'quality': {'accept_gps': {'unavailable': 1}}}, rows=[], truncated=False)
        legacy = validate_output('report', value, scope=SCOPE, evidence=ev)
        self.assertEqual(legacy['claims'][0]['limitation'], value['claims'][0]['limitation'])
        result = validate_output('report', value, scope=SCOPE, evidence=ev, render_limits=True)
        self.assertIn('同口径逾期已完整归属', result['claims'][0]['limitation'])
        self.assertNotIn('可能遗漏', result['claims'][0]['limitation'])
        self.assertTrue(any('accept_gps字段' in s for s in result['limitations']))
        self.assertFalse(any('工具不可用' in s for s in result['limitations']))
        value['claims'][0].update(kind='inferred', limitation='')
        value.pop('overdue_assessment')
        with self.assertRaisesRegex(ProtocolError, 'limitation_required'):
            validate_output('report', value, scope=SCOPE, evidence=ev, render_limits=True)

    def test_focus_is_from_counts_not_longest_task_and_ties_need_all_loads(self):
        ev = fixture(total=3)
        self.assertEqual(overdue_coverage(ev, SCOPE)['focus_courier_ids'], ['12477'])
        ev = fixture(total=4, counts=(('a', 2), ('b', 2)))
        status = goal_evidence_status('是否逾期集中，重点人员样本负荷', ev, SCOPE)
        self.assertEqual(status['coverage']['focus_courier_ids'], ['a', 'b'])
        self.assertEqual(status['missing_workload_ids'], ['a', 'b'])
        ev = fixture(total=10, counts=(('a', 1),))
        self.assertEqual(overdue_coverage(ev, SCOPE)['focus_courier_ids'], [])
        ev['e1']['record']['data']['sort'] = 'overdue_desc'
        self.assertEqual(overdue_coverage(ev, SCOPE)['focus_courier_ids'], [])  # 并列边界未跨过。
        ev['e1']['record']['rows'].append({'courier_id': 'zero', 'metrics': {'overdue': {'value': 0, 'denominator': 10, 'status': 'ok'}}})
        self.assertEqual(overdue_coverage(ev, SCOPE)['focus_courier_ids'], ['a'])

    def test_fact_summary_cannot_rename_backlog_as_visible(self):
        from project.tests.test_logistics_protocol import evidence
        ev = evidence(); value = report()
        value['claims'][0]['statement'] = '可见任务5条，因此GPS缺失'
        result = validate_output('report', value, scope=SCOPE, evidence=ev)
        self.assertEqual(result['claims'][0]['statement'], '当前样本内积压5条。')
        value['claims'][0].update(kind='observed', computed=None,
            fact={'type': 'summary', 'evidence_id': 'e1', 'metric': 'visible_tasks'})
        with self.assertRaisesRegex(ProtocolError, 'report_fact_mismatch'):
            validate_output('report', value, scope=SCOPE, evidence=ev)
        options = report_fact_options(ev, SCOPE)
        self.assertEqual(options, [{'fact': {'type': 'summary', 'evidence_id': 'e1', 'metric': 'backlog'}, 'evidence_ids': ['e1']}])

    def test_load_fact_rejects_task_proxy_and_wrong_person(self):
        ev = fixture(); value = assessed_report(ev)
        value.pop('overdue_assessment')
        value['claims'][0]['fact'] = {'type': 'workload', 'evidence_id': 'e1', 'courier_id': '12477'}
        with self.assertRaisesRegex(ProtocolError, 'report_fact_mismatch'):
            validate_output('report', value, scope=SCOPE, evidence=ev)
        ev['e1']['record'].update(tool='get_courier_workload', rows=[{
            'courier_id': '12477', 'visible_tasks': 42, 'completed_tasks': 39,
            'metrics': {k: {'value': n} for k, n in (('backlog', 3), ('overdue', 2), ('due_soon', 0), ('recent_accepted', 0), ('recent_completed', 0))}}])
        result = validate_output('report', value, scope=SCOPE, evidence=ev)
        self.assertIn('可见任务42条', result['claims'][0]['statement'])
        self.assertIn('积压3条', result['claims'][0]['statement'])
        value['claims'][0]['fact']['courier_id'] = 'wrong'
        with self.assertRaisesRegex(ProtocolError, 'report_fact_mismatch'):
            validate_output('report', value, scope=SCOPE, evidence=ev)

    def test_window_fact_cannot_expand_subset_to_all_or_ignore_conflict(self):
        ev = fixture(counts=(('a', 1), ('b', 1), ('c', 1)))
        ev['e1']['record'].update(tool='get_pending_tasks', data={'risk_state': 'overdue'}, rows=[
            {'evidence_id': f't{i}', 'courier_id': c, 'risk_state': 'overdue',
             'time_window_start_reference': '09:00', 'time_window_end_reference': '11:00'} for i, c in enumerate(('a', 'b'))])
        value = assessed_report(fixture()); value.pop('overdue_assessment')
        value['claims'][0]['fact'] = {'type': 'window', 'courier_ids': ['a', 'b'], 'coverage': 'observed_subset'}
        self.assertIn('已观察逾期任务子集', validate_output('report', value, scope=SCOPE, evidence=ev)['claims'][0]['statement'])
        value['claims'][0]['fact']['coverage'] = 'all_overdue'
        with self.assertRaisesRegex(ProtocolError, 'report_fact_mismatch'):
            validate_output('report', value, scope=SCOPE, evidence=ev)
        ev['e1']['record']['rows'].append({'evidence_id': 't2', 'courier_id': 'c', 'risk_state': 'overdue',
            'time_window_start_reference': '06:00', 'time_window_end_reference': '08:00'})
        with self.assertRaisesRegex(ProtocolError, 'report_fact_mismatch'):
            validate_output('report', value, scope=SCOPE, evidence=ev)

    def test_new_report_requires_numeric_binding_and_rejects_unsupported_cause(self):
        value = report(); value['claims'][0].update(kind='observed', computed=None)
        with self.assertRaisesRegex(ProtocolError, 'report_fact_required'):
            validate_report_facts(value)
        value['claims'][0].update(statement='GPS不可用证明人员未到场', kind='inferred')
        with self.assertRaisesRegex(ProtocolError, 'unsupported_cause'):
            validate_report_facts(value)
        value['claims'][0]['statement'] = 'GPS不可用不能证明人员未到场'
        validate_report_facts(value)
        self.assertTrue(value['claims'][0]['statement'].startswith('待核实推断：'))

    def test_ownership_fact_states_global_denominator_and_partial_bounds(self):
        ev = fixture(); value = assessed_report(ev); value.pop('overdue_assessment')
        value['claims'][0]['fact'] = {'type': 'ownership'}
        result = validate_output('report', value, scope=SCOPE, evidence=ev)
        self.assertIn('归属完整覆盖', result['claims'][0]['statement'])
        self.assertIn('全局逾期3条', result['claims'][0]['statement'])
        ev['e1']['record'].update(tool='get_pending_tasks', data={'risk_state': 'overdue'}, rows=[{
            'evidence_id': 't1', 'courier_id': '12477', 'risk_state': 'overdue'}])
        result = validate_output('report', value, scope=SCOPE, evidence=ev)
        self.assertIn('归属仍部分覆盖', result['claims'][0]['statement'])
        self.assertIn('下界', result['claims'][0]['statement'])

    def test_redundant_computed_binding_is_removed_only_when_identical(self):
        from project.tests.test_logistics_protocol import evidence
        value = report(); value['claims'][0]['fact'] = {'type': 'summary', 'evidence_id': 'e1', 'metric': 'backlog'}
        result = validate_output('report', value, scope=SCOPE, evidence=evidence())
        self.assertNotIn('fact', result['claims'][0])
        value['claims'][0]['fact']['metric'] = 'visible_tasks'
        with self.assertRaisesRegex(ProtocolError, 'report_fact_mismatch'):
            validate_output('report', value, scope=SCOPE, evidence=evidence())

    def test_goal_checks_workload_before_finishing_and_ignores_unknown_causes(self):
        ev = fixture()
        goal = '逾期是否集中于单人，定位重点人员及其样本负荷。不查根因、运力或派单。'
        status = goal_evidence_status(goal, ev, SCOPE)
        self.assertTrue(status['supported']); self.assertFalse(status['ready'])
        ev['load'] = deepcopy(ev['e1'])
        ev['load']['record'].update(tool='get_courier_workload', data={'courier_id': '12477'},
            rows=[{'courier_id': '12477', 'visible_tasks': 42, 'completed_tasks': 39,
                   'metrics': {k: {'value': n} for k, n in (
                       ('backlog', 3), ('overdue', 2), ('due_soon', 0), ('recent_accepted', 0), ('recent_completed', 0))}}])
        self.assertTrue(goal_evidence_status(goal, ev, SCOPE)['ready'])
        self.assertFalse(goal_evidence_status('调查逾期集中原因', ev, SCOPE)['ready'])

    def test_host_instructions_do_not_hide_missing_goal_evidence(self):
        ev = fixture(); value = assessed_report(ev)
        result_value = deepcopy(value)
        result_value.update(status='partial', unresolved_questions=['重点人员样本负荷未查询'])
        replies = iter([payload(value), payload(result_value)])
        result = generate('report', '以保存证据回答，目标外未确认原因写局限', goal_question='逾期是否集中，重点人员样本负荷如何',
                          scope=SCOPE, evidence=ev, plan=None, step_states={},
                          request=lambda *_: next(replies), budget=ModelBudget())
        self.assertEqual(result['repairs'], 1)
        self.assertEqual(result['value']['status'], 'partial')

    def test_common_window_accumulates_saved_pages_for_observed_subset(self):
        ev = fixture(counts=(('a', 1), ('b', 1), ('c', 1)))
        for i, courier in enumerate(('a', 'b'), 1):
            ev[f'p{i}'] = deepcopy(ev['e1'])
            ev[f'p{i}']['record'].update(tool='get_pending_tasks', data={'risk_state': 'overdue'},
                rows=[{'evidence_id': f'LTASK:r1:{i}', 'courier_id': courier, 'risk_state': 'overdue',
                       'time_window_start_reference': '09:00', 'time_window_end_reference': '11:00'}])
        status = goal_evidence_status('逾期是否存在共同窗口', ev, SCOPE)
        self.assertTrue(status['ready'])  # 只证明已观察的两人子集。
        self.assertFalse(goal_evidence_status('所有逾期人员是否有共同窗口', ev, SCOPE)['ready'])
        ev.pop('p2')
        self.assertFalse(goal_evidence_status('逾期是否存在共同窗口', ev, SCOPE)['ready'])

    def test_truncated_person_list_but_complete_overdue_ownership(self):
        ev = fixture(); original = deepcopy(ev)
        c = overdue_coverage(ev, SCOPE)
        self.assertEqual(c['ownership_coverage'], 'complete')
        self.assertEqual(c['person_list_coverage'], 'partial')
        self.assertEqual(c['covered_overdue'], 3)
        self.assertEqual(c['couriers'][0]['verdict'], 'over_half')
        self.assertAlmostEqual(c['couriers'][0]['share'], 2 / 3)
        self.assertEqual(ev, original)

    def test_partial_ownership_does_not_block_individual_majority(self):
        c = overdue_coverage(fixture(counts=(('12477', 2),)), SCOPE)
        self.assertEqual(c['ownership_coverage'], 'partial')
        self.assertEqual(c['couriers'][0]['verdict'], 'over_half')
        c = overdue_coverage(fixture(total=5), SCOPE)
        self.assertEqual(c['ownership_coverage'], 'partial')
        self.assertEqual(c['couriers'][0]['verdict'], 'not_over_half')

    def test_detail_pages_deduplicate_and_preserve_lower_bounds(self):
        ev = fixture()
        r = ev['e1']['record']
        r.update(tool='get_pending_tasks', data={'risk_state': 'overdue'}, rows=[
            {'evidence_id': 'LTASK:r1:1', 'courier_id': '12477', 'risk_state': 'overdue'},
            {'evidence_id': 'LTASK:r1:2', 'courier_id': '12477', 'risk_state': 'overdue'}])
        ev['e2'] = deepcopy(ev['e1'])
        c = overdue_coverage(ev, SCOPE)
        self.assertEqual(c['covered_overdue'], 2)
        self.assertEqual(c['couriers'][0]['verdict'], 'over_half')
        r['denominator']['overdue'] = 5
        ev.pop('e2')
        self.assertEqual(overdue_coverage(ev, SCOPE)['couriers'][0]['verdict'], 'unknown')
        ev['e2'] = deepcopy(ev['e1'])
        ev['e2']['record']['rows'] = [{'evidence_id': f'LTASK:r1:{i}', 'courier_id': '9485', 'risk_state': 'overdue'} for i in (3, 4, 5)]
        self.assertEqual(overdue_coverage(ev, SCOPE)['ownership_coverage'], 'complete')

    def test_scope_missing_windows_and_contradictions_are_unknown(self):
        for key, value in (('business_as_of', 'future'), ('metric_version', 'other'),
                           ('fingerprint', 'other'), ('status', 'unavailable')):
            ev = fixture(); ev['e1']['record'][key] = value
            self.assertEqual(overdue_coverage(ev, SCOPE)['ownership_coverage'], 'unknown')
        ev = fixture(total=None)
        self.assertEqual(overdue_coverage(ev, SCOPE)['ownership_coverage'], 'unknown')
        ev = fixture(); ev['e2'] = deepcopy(ev['e1'])
        ev['e2']['record']['rows'][0]['metrics']['overdue']['value'] = 1
        self.assertEqual(overdue_coverage(ev, SCOPE)['ownership_coverage'], 'unknown')
        self.assertEqual(overdue_coverage(fixture(total=2), SCOPE)['ownership_coverage'], 'unknown')

    def test_aggregate_and_details_are_not_added_and_filters_do_not_replace_global(self):
        ev = fixture(); ev['e2'] = deepcopy(ev['e1'])
        ev['e2']['record'].update(tool='get_pending_tasks', data={'risk_state': 'overdue', 'courier_id': '12477'},
                                 rows=[{'evidence_id': 'LTASK:r1:1', 'courier_id': '12477', 'risk_state': 'overdue'}], total_rows=1)
        self.assertEqual(overdue_coverage(ev, SCOPE)['covered_overdue'], 3)
        ev.pop('e1')
        self.assertEqual(overdue_coverage(ev, SCOPE)['global_overdue'], 3)
        self.assertEqual(overdue_coverage(ev, SCOPE)['couriers'][0]['verdict'], 'unknown')

    def test_report_rejects_wrong_majority_or_false_unknown(self):
        ev = fixture(); value = assessed_report(ev)
        validate_output('report', value, scope=SCOPE, evidence=ev)
        value['claims'][0]['statement'] = '2/3未过半，无法确认'
        corrected = validate_output('report', value, scope=SCOPE, evidence=ev)
        self.assertIn('66.7%，超过一半', corrected['claims'][0]['statement'])
        self.assertEqual(value['claims'][0]['statement'], '2/3未过半，无法确认')
        for verdict in ('not_over_half', 'unknown'):
            wrong = deepcopy(value); wrong['overdue_assessment']['verdict'] = verdict
            with self.assertRaisesRegex(ProtocolError, 'overdue_assessment_mismatch'):
                validate_output('report', wrong, scope=SCOPE, evidence=ev)
        wrong = deepcopy(value); wrong['overdue_assessment']['global_overdue'] = 2
        with self.assertRaisesRegex(ProtocolError, 'overdue_assessment_mismatch'):
            validate_output('report', wrong, scope=SCOPE, evidence=ev)

    def test_generation_requires_assessment_and_repairs_without_tools(self):
        ev = fixture(); correct = assessed_report(ev)
        wrong = deepcopy(correct); wrong.pop('overdue_assessment')
        responses = iter([payload(wrong), payload(correct)])
        requests = []
        def request(body, *_):
            requests.append(body)
            return next(responses)
        result = generate('report', '是否集中于某个人', scope=SCOPE, evidence=ev, plan=None,
                          step_states={}, request=request, budget=ModelBudget())
        self.assertEqual(result['repairs'], 1)
        context = json.loads(requests[0]['messages'][1]['content'])
        self.assertEqual(context['overdue_coverage']['ownership_coverage'], 'complete')
        self.assertTrue(context['overdue_assessment_required'])
        self.assertEqual(len(requests), 2)

    def test_unrelated_goal_does_not_require_concentration_claim(self):
        ev = fixture(); value = assessed_report(ev); value.pop('overdue_assessment')
        result = generate('report', '核查任务列表', scope=SCOPE, evidence=ev, plan=None,
                          step_states={}, request=lambda *_: payload(value), budget=ModelBudget())
        self.assertEqual(result['repairs'], 0)

    def test_half_zero_and_conflicting_task_identity(self):
        c = overdue_coverage(fixture(total=4), SCOPE)
        self.assertEqual(c['couriers'][0]['verdict'], 'not_over_half')
        c = overdue_coverage(fixture(total=0, counts=(('12477', 0),)), SCOPE)
        self.assertEqual(c['couriers'][0]['verdict'], 'no_overdue')
        ev = fixture(); record = ev['e1']['record']
        record.update(tool='get_pending_tasks', data={'risk_state': 'overdue'}, rows=[
            {'evidence_id': 'LTASK:r1:1', 'courier_id': '12477', 'risk_state': 'overdue'},
            {'evidence_id': 'LTASK:r1:1', 'courier_id': '9485', 'risk_state': 'overdue'}])
        self.assertEqual(overdue_coverage(ev, SCOPE)['ownership_coverage'], 'unknown')

    def test_unknown_concentration_cannot_complete_goal(self):
        ev = fixture()
        ev['e1']['record'].update(tool='get_pending_tasks', data={'risk_state': 'overdue'}, rows=[
            {'evidence_id': 'LTASK:r1:1', 'courier_id': '12477', 'risk_state': 'overdue'}])
        value = assessed_report(ev)
        value['overdue_assessment']['missing_evidence'] = ['缺少该人员完整逾期计数，已知下界未过半']
        repaired = deepcopy(value)
        repaired.update(status='partial', unresolved_questions=['无法确认该人员是否过半'])
        responses = iter([payload(value), payload(repaired)])
        result = generate('report', '逾期是否过半集中', scope=SCOPE, evidence=ev, plan=None,
                          step_states={}, request=lambda *_: next(responses), budget=ModelBudget())
        self.assertEqual(result['value']['status'], 'partial')
        self.assertEqual(result['repairs'], 1)


if __name__ == '__main__':
    unittest.main()
