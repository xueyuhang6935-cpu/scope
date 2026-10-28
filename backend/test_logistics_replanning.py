"""T12实际图/磁盘/HTTP边界：重规划证据、家族限额、重复与部分报告。"""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import httpx

from . import test_logistics_workflow as workflow_support
from .logistics_runs import RunService
from .logistics_workflow import LogisticsWorkflow
from project.agents.logistics_protocol import ProtocolError
from project.tests.test_logistics_protocol import plan, payload

simulated = workflow_support.simulated


def response(body, initial=None, empty=False):
    context = json.loads(body['messages'][1]['content'])
    kind = body['tools'][0]['function']['name'].removeprefix('submit_')
    if kind == 'plan':
        value = deepcopy(context['plan'] or initial or plan())
        if context['plan']:
            value['version'] += 1; value['revision_reason'] = '根据实际触发证据修订'
            for step in value['steps']:
                step['status'] = context['step_states'].get(step['step_id'], 'pending')
        return payload(value, mode='tool_call', kind=kind)
    result = simulated(body)
    if kind == 'observation':
        value = json.loads(result['choices'][0]['message']['tool_calls'][0]['function']['arguments'])
        value['hypothesis_assessments'] = []
        if empty: value['new_facts'] = []
        return payload(value, mode='tool_call', kind=kind)
    return result


class ReplanningTests(unittest.TestCase):
    def page_run(self):
        self.model_run()
        self.data.db.execute('UPDATE logistics_runs SET question=? WHERE run_id=?',
            ('核对逾期规模和人员分布，首页不足时补必要证据。不查根因。', self.run))
        initial = plan()
        initial['steps'] = [{**initial['steps'][0], 'tool': 'get_pending_tasks',
            'parameters': {'risk_state': 'overdue', 'page': 1, 'page_size': 1,
                           'sort': 'overdue_minutes_desc'}}]
        self.limit(max_model_calls=3)
        return initial

    def test_report_only_transition_fills_one_missing_page_without_model_observation(self):
        initial = self.page_run()
        requests, saved = self.process(lambda body: response(body, initial))
        pages = [t for t in saved['state']['trace'] if t['tool'] == 'get_pending_tasks']
        self.assertEqual([t['parameters']['page'] for t in pages], [1, 2])
        self.assertEqual(pages[1]['origin'], 'host_goal_requirement')
        self.assertEqual(saved['family_budget']['model_calls'], 3)
        self.assertEqual(len(saved['revisions']), 0)
        host = self.app.state.logistics_workflow
        host.actor = self.user
        self.assertTrue(host.goal_status(saved['state'])['ready'])
        self.assertEqual(sum(o.get('source') == 'program_goal_requirement_check'
                             for o in saved['state']['observations']), 1)

    def test_failed_page_is_not_retried_and_keeps_real_gap(self):
        initial = self.page_run(); compute = self.service.compute
        def broken(user, run, tool, args):
            result = compute(user, run, tool, args)
            if tool == 'get_pending_tasks' and args['page'] == 2:
                result.update(status='error', data=None, rows=[])
            return result
        with patch.object(self.service, 'compute', side_effect=broken):
            _, saved = self.process(lambda body: response(body, initial))
        pages = [t for t in saved['state']['trace'] if t['tool'] == 'get_pending_tasks']
        self.assertEqual([t['parameters']['page'] for t in pages], [1, 2])
        self.assertEqual(pages[1]['status'], 'error')
        self.assertEqual(saved['state']['report']['value']['status'], 'partial')
        # 检查点落盘的尝试记录重建后仍保留；恢复已完成节点不重发补查。
        host = self.app.state.logistics_workflow
        host.close()
        restored = LogisticsWorkflow(self.app.state.logistics_model, self.app.state.logistics_reports)
        self.app.state.logistics_workflow = restored
        self.app.state.logistics_runs.workflow = restored
        restored.actor = self.user
        after = restored.read(self.user, self.run)['state']
        self.assertEqual(after['trace'], saved['state']['trace'])
        with patch.object(self.app.state.logistics_tools, 'call') as unused:
            self.assertEqual(restored.select(after)['route'], 'report')
            unused.assert_not_called()

    def test_page_followup_does_not_spend_report_budget_when_insufficient(self):
        initial = self.page_run()
        host = self.app.state.logistics_workflow
        with patch.object(host, 'followup_fits', return_value=False):
            _, saved = self.process(lambda body: response(body, initial))
        self.assertEqual([t['parameters']['page'] for t in saved['state']['trace']
                          if t['tool'] == 'get_pending_tasks'], [1])
        self.assertEqual(saved['state']['report']['value']['status'], 'partial')

    def test_followup_quote_includes_new_context_before_spending_tool_call(self):
        self.model_run()
        tasks = self.call('get_pending_tasks', {'risk_state': 'overdue', 'page_size': 1}).json()
        host = self.app.state.logistics_workflow; host.actor = self.user
        state = {'run_id': self.run, 'question': '核对逾期规模与人员分布',
                 'evidence_ids': [tasks['result_evidence_id']], 'step_states': {}}
        quote = host.model.report_reservation(self.user, self.run, state['question'], state['evidence_ids'], {})
        self.limit(model_tokens=24000-quote['tokens']-1)
        before = self.app.state.logistics_runs.budgets.read(self.run)
        candidate = {'tool': 'get_pending_tasks', 'parameters': {'risk_state': 'overdue', 'page_size': 1, 'page': 2}}
        self.assertFalse(host.followup_fits(state, candidate))  # 当前报告尚可生成，新增证据后额度不足。
        self.assertEqual(self.app.state.logistics_runs.budgets.read(self.run)['tool_calls'], before['tool_calls'])

    def focus_run(self):
        for row in self.records:
            row['courier_id'] = 'leader'
        self.run = self.new_run('focus-fixture')
        self.model_run()
        self.data.db.execute('UPDATE logistics_runs SET question=? WHERE run_id=?',
            ('核对逾期集中及重点人员样本负荷。不查根因。', self.run))
        initial = plan()
        initial['steps'] = [{**initial['steps'][0], 'question': '按逾期计数核对人员',
            'tool': 'get_risk_distribution', 'parameters': {'dimension': 'courier_id', 'sort': 'overdue_desc', 'page_size': 100}}]
        return initial

    def test_focus_binding_changes_only_target_step_and_saves_provenance(self):
        initial = self.focus_run()
        initial['steps'].append({'step_id': 'load', 'question': '核对重点人员负荷',
            'tool': 'get_courier_workload', 'parameters': {'courier_id': '$first_courier'},
            'depends_on': ['s1'], 'completion_condition': '取得重点人员样本负荷', 'status': 'pending'})
        requests, saved = self.process(lambda body: response(body, initial))
        loads = [t for t in saved['state']['trace'] if t['tool'] == 'get_courier_workload']
        self.assertEqual(len(loads), 1)
        self.assertEqual(loads[0]['parameters']['courier_id'], 'leader')
        self.assertEqual(loads[0]['parameter_binding']['original_parameters']['courier_id'], '$first_courier')
        self.assertTrue(loads[0]['parameter_binding']['evidence_ids'])
        self.assertEqual(saved['state']['plans'][0]['value']['steps'][1]['parameters']['courier_id'], '$first_courier')
        self.assertEqual(len(saved['revisions']), 0)

    def test_missing_load_is_supplemented_once_without_model_observation(self):
        initial = self.focus_run()
        requests, saved = self.process(lambda body: response(body, initial))
        loads = [t for t in saved['state']['trace'] if t['tool'] == 'get_courier_workload']
        self.assertEqual(len(loads), 1)
        self.assertEqual(loads[0]['origin'], 'host_goal_requirement')
        self.assertEqual(loads[0]['parameters']['courier_id'], 'leader')
        self.assertTrue(any(o.get('source') == 'program_goal_requirement_check' for o in saved['state']['observations']))
        self.assertFalse(any(r['tools'][0]['function']['name'] == 'submit_observation' and
            json.loads(r['messages'][1]['content'])['question'].endswith(loads[0]['step_id']) for r in requests))

    def test_failed_goal_load_is_not_retried_and_keeps_partial(self):
        initial = self.focus_run(); compute = self.service.compute
        def fail_load(user, run, tool, args):
            value = compute(user, run, tool, args)
            if tool == 'get_courier_workload':
                value.update(status='error', data=None, rows=[])
            return value
        with patch.object(self.service, 'compute', side_effect=fail_load):
            requests, saved = self.process(lambda body: response(body, initial))
        self.assertEqual(sum(t['tool'] == 'get_courier_workload' for t in saved['state']['trace']), 1)
        self.assertEqual(saved['state']['report']['value']['status'], 'partial')

    def test_goal_supplement_obeys_existing_tool_limit(self):
        initial = self.focus_run(); self.limit(max_tool_calls=1)
        requests, saved = self.process(lambda body: response(body, initial))
        self.assertEqual(saved['state']['stop_reason'], 'tool_call_limit')
        self.assertEqual(saved['family_budget']['tool_calls'], 1)
        self.assertEqual(saved['state']['report']['source'], 'program_stop_report')

    def test_tied_leaders_each_get_one_load_and_existing_load_is_not_requeried(self):
        self.model_run()
        self.data.db.execute('UPDATE logistics_runs SET question=? WHERE run_id=?',
            ('核对逾期集中及重点人员样本负荷。不查根因。', self.run))
        initial = plan()
        initial['steps'] = [{**initial['steps'][0], 'tool': 'get_risk_distribution',
            'parameters': {'dimension': 'courier_id', 'sort': 'overdue_desc', 'page_size': 100}}]
        requests, saved = self.process(lambda body: response(body, initial))
        loads = [t for t in saved['state']['trace'] if t['tool'] == 'get_courier_workload']
        self.assertEqual(sorted(t['parameters']['courier_id'] for t in loads), ['001', '002'])
        self.assertEqual(len(loads), 2)

    def test_authorized_explicit_other_person_query_is_not_silently_rebound(self):
        initial = self.focus_run()
        initial['steps'].append({'step_id': 'other', 'question': '核查指定人员other',
            'tool': 'get_courier_workload', 'parameters': {'courier_id': 'other'},
            'depends_on': ['s1'], 'completion_condition': '取得指定人员记录', 'status': 'pending'})
        # 直接选择此模型步骤；宿主先补目标缺口是独立、有标记的步骤。
        host = self.app.state.logistics_workflow
        with patch.object(host, 'goal_status', return_value={'ready': False, 'supported': False}), \
             patch.object(host, 'report_reserve', return_value=False), \
             patch.object(host, 'constraint_trigger', return_value=[]):
            state = {'run_id': self.run, 'plan': initial, 'step_states': {'s1': 'completed'}, 'trace': []}
            selected = host.select(state)
        self.assertEqual(selected['selected']['parameters']['courier_id'], 'other')
        self.assertIsNone(selected['selected_binding'])

    def test_observation_preflight_routes_to_report_without_spending_reserved_tokens(self):
        self.model_run()
        host = self.app.state.logistics_workflow
        with patch.object(host, 'report_reserve', return_value=False), \
             patch.object(host.model, 'report_reservation', return_value={'tokens': 24000, 'calls': 2}):
            requests, saved = self.process()
        self.assertEqual([r['tools'][0]['function']['name'] for r in requests], ['submit_plan', 'submit_report'])
        self.assertTrue(saved['state']['budget_report_only'])
        self.assertTrue(any(o.get('source') == 'program_budget_check' for o in saved['state']['observations']))
        self.assertEqual(self.app.state.logistics_runs.saved(self.run)['status'], 'completed')

    def test_goal_complete_skips_unnecessary_steps_and_observation(self):
        self.model_run()
        self.data.db.execute('UPDATE logistics_runs SET question=? WHERE run_id=?', ('核对逾期规模与逾期分布覆盖', self.run))
        initial = plan()
        initial['steps'].append({**deepcopy(initial['steps'][1]), 'step_id': 'extra',
                                 'depends_on': ['s2'], 'question': '再次查询全部人员'})
        requests, saved = self.process(lambda body: response(body, initial))
        state = saved['state']
        self.assertNotIn('extra', [t['step_id'] for t in state['trace']])
        self.assertTrue(any(o.get('source') == 'program_goal_check' for o in state['observations']))
        self.assertEqual(len(saved['revisions']), 0)
        self.assertTrue(state['report'])

    setUp = workflow_support.WorkflowTests.setUp
    tearDown = workflow_support.WorkflowTests.tearDown
    login = workflow_support.WorkflowTests.login
    create = workflow_support.WorkflowTests.create
    new_run = workflow_support.WorkflowTests.new_run
    call = workflow_support.WorkflowTests.call
    enabled = workflow_support.WorkflowTests.enabled
    model_run = workflow_support.WorkflowTests.model_run

    def limit(self, **changes):
        runs = self.app.state.logistics_runs
        value = json.loads(runs.saved(self.run)['budget_json']); value.update(changes)
        self.data.db.execute('UPDATE logistics_runs SET budget_json=? WHERE run_id=?', (json.dumps(value), self.run))

    def process(self, model=response):
        runs = self.app.state.logistics_runs
        for queued in self.data.all("SELECT run_id FROM logistics_runs WHERE status='queued' AND run_id!=?", (self.run,)):
            runs.cancel(self.user, queued['run_id'])
        requests = []; factory = httpx.Client
        def handler(request):
            body = json.loads(request.content); requests.append(body)
            return httpx.Response(200, json=model(body))
        with patch('backend.investigation_model.httpx.Client', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            runs.process(runs.claim())
        return requests, self.app.state.logistics_workflow.read(self.user, self.run)

    def test_evidence_trigger_records_old_new_diff_and_integrity(self):
        self.model_run()
        requests, saved = self.process(simulated)
        self.assertEqual(len(saved['revisions']), 1)
        revision = saved['revisions'][0]
        self.assertEqual(revision['triggers'][0]['kind'], 'evidence_not_supported')
        self.assertEqual(revision['old_plan']['version'], 1)
        self.assertEqual(revision['new_plan']['version'], 2)
        self.assertTrue(revision['diff']['changed'])
        self.assertIn(revision['triggers'][0]['evidence_ids'][0], saved['state']['evidence_ids'])
        self.data.db.execute("UPDATE logistics_plan_revisions SET record_json='{}' WHERE run_id=?", (self.run,))
        self.assertEqual(self.client.get(f'/api/logistics/runs/{self.run}/investigation').status_code, 409)

    def test_missing_text_without_actual_trigger_does_not_replan(self):
        self.model_run(); _, saved = self.process()
        self.assertEqual(len(saved['state']['plans']), 1)
        self.assertEqual(saved['revisions'], [])
        self.assertEqual(self.app.state.logistics_runs.saved(self.run)['status'], 'completed')

    def test_truncated_result_and_unavailable_tool_trigger_real_revisions(self):
        for index, (tool, args, expected) in enumerate([('get_pending_tasks', {'page_size': 1}, 'data_truncated'),
                                      ('get_quality_summary', {}, 'tool_unavailable')]):
            with self.subTest(tool=tool):
                self.body['business_as_of'] = f'2000-07-02 10:00:0{index+1}'
                self.run = self.new_run('fixture-'+tool)
                initial = plan(); initial['steps'][0].update(tool=tool, parameters=args)
                self.model_run(tool)
                _, saved = self.process(lambda body: response(body, initial))
                self.assertEqual(saved['revisions'][0]['triggers'][0]['kind'], expected)
                self.assertEqual(self.app.state.logistics_runs.saved(self.run)['status'], 'completed')
        self.assertEqual(saved['family_budget']['plan_revisions'], 1)

    def test_constraint_change_is_authorized_versioned_and_enters_replan(self):
        self.model_run(); changed = False
        def model(body):
            nonlocal changed
            if body['tools'][0]['function']['name'] == 'submit_observation' and not changed:
                changed = True
                value = self.client.post(f'/api/logistics/runs/{self.run}/constraints', json={'instruction': '只比较AOI，原因留人工核查'})
                self.assertEqual(value.status_code, 200)
            return response(body)
        requests, saved = self.process(model)
        self.assertEqual(saved['revisions'][0]['triggers'][0]['kind'], 'constraint_changed')
        self.assertTrue(saved['revisions'][0]['triggers'][0]['constraint_id'].startswith('LCON-'))
        self.assertIn('只比较AOI', requests[2]['messages'][1]['content'])
        self.assertEqual(self.client.post(f'/api/logistics/runs/{self.run}/constraints', json={'instruction': 'x'}).status_code, 409)
        self.assertEqual(self.client.post(f'/api/logistics/runs/{self.run}/constraints', json={'instruction': 'x', 'region_ids': ['6']}).status_code, 400)

    def test_duplicate_parameters_reuse_evidence_and_two_empty_observations_stop(self):
        self.model_run(); initial = plan()
        initial['steps'] = [dict(deepcopy(initial['steps'][0]), step_id=f's{i}', depends_on=[])
                            for i in range(1, 5)]
        initial['steps'][1]['parameters'] = {'horizon_minutes': 30, 'recent_minutes': 60}
        requests, saved = self.process(lambda body: response(body, initial))
        state = saved['state']
        self.assertEqual(state['stop_reason'], 'no_progress')
        self.assertEqual(len(state['trace']), 3)
        self.assertEqual([t['reused'] for t in state['trace']], [False, True, True])
        self.assertEqual(len(requests), 2)  # 一次计划、一次真实观察；重复不调模型。
        self.assertEqual(saved['family_budget']['tool_calls'], 1)
        self.assertEqual(state['report']['source'], 'program_stop_report')
        self.assertEqual(state['report']['value']['claims'][0]['computed']['value'], 5)
        self.assertEqual(state['unfinished_steps'][0]['step_id'], 's4')
        self.assertEqual(self.call('get_snapshot_summary').status_code, 409)
        self.assertEqual(self.client.post(f'/api/logistics/runs/{self.run}/report', json={}).status_code, 409)
        with patch('backend.logistics_model.model_session') as unused:
            with self.assertRaises(ProtocolError):
                self.app.state.logistics_model.generate('plan', self.user, self.run, '继续')
            unused.assert_not_called()
        # 换幂等键不会清零连续空转状态。
        self.model_run('repeat-root')
        with patch('backend.logistics_model.model_session') as unused:
            _, second = self.process(); unused.assert_not_called()
        self.assertEqual(second['state']['stop_reason'], 'no_progress')

    def test_revision_limit_yields_partial_without_another_model_request(self):
        self.model_run(); self.limit(max_plan_revisions=0)
        requests, saved = self.process(simulated)
        self.assertEqual(len(requests), 2)
        self.assertEqual(saved['state']['stop_reason'], 'revision_limit')
        self.assertEqual(saved['state']['report']['value']['status'], 'partial')
        self.assertEqual(saved['family_budget']['plan_revisions'], 0)
        self.assertTrue(saved['state']['evidence_ids'])

    def test_family_limits_shared_by_parent_and_same_snapshot_new_root(self):
        self.model_run(); root = self.run; runs = self.app.state.logistics_runs
        snapshot = runs.saved(root)['snapshot_id']
        self.limit(model_calls=12, tool_calls=16, model_tokens=24000, plan_revisions=3)
        recreated = self.create().json()['snapshot_id']
        self.assertNotEqual(snapshot, recreated)
        for key, parent, snap in [('child', root, snapshot), ('fresh-key', None, snapshot), ('recreated', None, recreated)]:
            body = {'snapshot_id': snap, 'mode': 'model', 'question': '继续调查', 'idempotency_key': key}
            if parent: body['parent_run_id'] = parent
            created = self.client.post('/api/logistics/runs', json=body)
            self.assertEqual(created.status_code, 202)
            run = created.json()['run_id']
            self.assertEqual(created.json()['family_id'], runs.saved(root)['family_id'])
            with patch('backend.logistics_model.model_session') as unused:
                with self.assertRaises(ProtocolError):
                    self.app.state.logistics_model.generate('plan', self.user, run, '继续')
                unused.assert_not_called()
            self.assertEqual(self.client.post(f'/api/logistics/runs/{run}/tools/get_snapshot_summary', json={}).status_code, 409)
            with self.assertRaises(ProtocolError): runs.budgets.reserve(run, 'plan_revisions')
        self.assertEqual(runs.budgets.read(root)['model_calls'], 12)
        self.assertEqual(runs.budgets.read(root)['tool_calls'], 16)

    def test_missing_usage_receipt_stays_unknown_and_call_limit_still_stops(self):
        self.model_run(); self.limit(max_model_calls=2)
        def missing(body):
            value = response(body); value.pop('usage'); return value
        requests, saved = self.process(missing)
        self.assertEqual(len(requests), 2)
        self.assertEqual(saved['state']['stop_reason'], 'model_call_limit')
        self.assertTrue(saved['family_budget']['model_usage_unknown'])
        self.assertEqual(saved['family_budget']['unknown_usage_calls'], 2)
        self.assertEqual(saved['family_budget']['model_tokens'], 0)  # 已知用量为0，实际用量明确未知。

    def test_last_known_call_is_reserved_for_report_without_fabricating_observation(self):
        self.model_run(); self.limit(max_model_calls=2)
        requests, saved = self.process()
        self.assertEqual([r['tools'][0]['function']['name'] for r in requests], ['submit_plan', 'submit_report'])
        self.assertEqual(self.app.state.logistics_runs.saved(self.run)['status'], 'completed')
        self.assertEqual(saved['state']['report']['value']['status'], 'partial')
        self.assertTrue(all(o['source'] == 'program_requirement_check' for o in saved['state']['observations']))
        self.assertTrue(saved['state']['step_states'].get('s2', 'pending') == 'pending')

    def test_partial_quality_missing_does_not_reject_complete_target_report(self):
        self.model_run()
        def complete(body):
            result = response(body)
            if body['tools'][0]['function']['name'] == 'submit_report':
                value = json.loads(result['choices'][0]['message']['tool_calls'][0]['function']['arguments'])
                value.update(status='complete', unresolved_questions=[], next_steps=[])
                return payload(value, mode='tool_call', kind='report')
            return result
        _, saved = self.process(complete)
        quality = next(t for t in saved['state']['trace'] if t['tool'] == 'get_quality_summary')
        self.assertEqual(quality['status'], 'unavailable')
        self.assertEqual(self.app.state.logistics_runs.saved(self.run)['status'], 'completed')
        self.assertEqual(saved['state']['report']['value']['status'], 'complete')

    def test_token_tool_and_active_limits_create_partial_and_keep_saved_evidence(self):
        for index, (counter, limit, reason) in enumerate([('model_tokens', 20, 'token_limit'), ('tool_calls', 0, 'tool_call_limit'),
                                       ('active_seconds', 0, 'active_time_limit')]):
            with self.subTest(counter=counter):
                self.body['business_as_of'] = f'2000-07-02 10:00:0{index+1}'
                self.run = self.new_run(counter+'-fixture')
                self.model_run(counter)
                self.limit(**{'max_'+counter: limit})
                requests, saved = self.process()
                self.assertEqual(saved['state']['stop_reason'], reason)
                self.assertEqual(saved['state']['report']['value']['status'], 'partial')
                self.assertEqual(self.app.state.logistics_runs.saved(self.run)['status'], 'waiting')

    def test_budget_reconstruction_preserves_unknown_and_external_tool_count(self):
        self.call('get_snapshot_summary')
        runs = self.app.state.logistics_runs
        self.limit(model_calls=2, model_usage_unknown=True, unknown_usage_calls=1, standalone_seconds=3)
        before = runs.budgets.read(self.run)
        restored = RunService(self.app.state.snapshots)
        self.assertEqual(restored.budgets.read(self.run), before)
        self.assertEqual(before['tool_calls'], 1)
        self.assertEqual(before['unknown_usage_calls'], 1)
        self.assertGreaterEqual(before['active_seconds'], 3)

    def test_inflight_response_time_limit_and_revocation_do_not_deliver_model_report(self):
        self.model_run()
        def consume_time(body):
            self.data.db.execute('UPDATE logistics_runs SET active_seconds=120 WHERE run_id=?', (self.run,))
            return response(body)
        requests, saved = self.process(consume_time)
        self.assertEqual(len(requests), 1)
        self.assertEqual(saved['state']['stop_reason'], 'active_time_limit')
        self.assertEqual(saved['state']['report']['source'], 'program_stop_report')
        self.assertEqual(saved['family_budget']['model_calls'], 1)
        self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'")
        self.assertEqual(self.client.get(f'/api/logistics/runs/{self.run}/investigation').status_code, 403)
        self.assertEqual(self.client.post(f'/api/logistics/runs/{self.run}/constraints', json={'instruction':'改查其他区域'}).status_code, 403)

    def test_required_tool_failure_cannot_loop_or_fake_template_report(self):
        self.model_run(); compute = self.service.compute
        def fail_quality(user, run, tool, args):
            result = compute(user, run, tool, args)
            if tool == 'get_quality_summary':
                result.update(status='error', data=None)
            return result
        with patch.object(self.service, 'compute', side_effect=fail_quality):
            requests, saved = self.process()
        self.assertEqual(saved['state']['stop_reason'], 'required_evidence_unavailable')
        self.assertEqual(saved['state']['report']['source'], 'program_stop_report')
        self.assertEqual(len(requests), 3)  # 计划和两次观察；必需取数失败后不生成模型报告。
        self.assertEqual(self.client.get(f'/api/logistics/runs/{self.run}/report').status_code, 404)
        self.assertEqual(sum(t['tool'] == 'get_quality_summary' for t in saved['state']['trace']), 1)

    def test_two_actual_empty_observations_stop(self):
        self.model_run()
        requests, saved = self.process(lambda body: response(body, empty=True))
        self.assertEqual(len(requests), 3)
        self.assertEqual(saved['state']['stop_reason'], 'no_progress')
        self.assertEqual(saved['family_budget']['tool_calls'], 2)

    def test_legacy_audit_counts_and_family_backfill_cannot_reset_budget(self):
        self.call('get_snapshot_summary')
        root = self.run; second = self.new_run('legacy-sibling')
        self.call('get_snapshot_summary', run=second)
        for run in (root, second):
            value = json.loads(self.app.state.logistics_runs.saved(run)['budget_json'])
            value['tool_calls'] = 0
            self.data.db.execute('UPDATE logistics_runs SET family_id=NULL,family_key=NULL,budget_json=? WHERE run_id=?', (json.dumps(value), run))
        restored = RunService(self.app.state.snapshots)
        family = restored.saved(root)['family_id']
        self.assertEqual(restored.saved(second)['family_id'], family)
        self.assertEqual(restored.budgets.read(second)['tool_calls'], 2)
        again = RunService(self.app.state.snapshots)
        self.assertEqual(again.saved(second)['family_id'], family)
        self.assertEqual(again.budgets.read(second)['tool_calls'], 2)

    def test_constraint_changed_during_plan_replans_before_first_tool(self):
        self.model_run(); changed = False
        def model(body):
            nonlocal changed
            if body['tools'][0]['function']['name'] == 'submit_plan' and not changed:
                changed = True
                result = self.client.post(f'/api/logistics/runs/{self.run}/constraints', json={'instruction': '仅做分布比较，不调查原因'})
                self.assertEqual(result.status_code, 200)
            return response(body)
        requests, saved = self.process(model)
        self.assertEqual([r['tools'][0]['function']['name'] for r in requests[:2]], ['submit_plan', 'submit_plan'])
        self.assertEqual(saved['revisions'][0]['triggers'][0]['kind'], 'constraint_changed')
        self.assertEqual(saved['state']['trace'][0]['plan_version'], 2)

    def test_failed_dependency_cannot_be_reported_as_finished(self):
        self.model_run(); original = self.service.compute
        def broken(user, run, tool, args):
            value = original(user, run, tool, args)
            if tool == 'get_snapshot_summary': value.update(status='error', data=None)
            return value
        with patch.object(self.service, 'compute', side_effect=broken):
            requests, saved = self.process()
        self.assertEqual(saved['state']['stop_reason'], 'blocked_dependencies')
        self.assertEqual(saved['state']['unfinished_steps'][0]['status'], 'failed')
        self.assertEqual(len(saved['revisions']), 1)
        self.assertEqual(saved['revisions'][0]['triggers'][0]['kind'], 'tool_unavailable')
        self.assertEqual(len(requests), 2)

    def test_legacy_trace_checkpoint_resumes_without_new_fields(self):
        self.model_run(); runs = self.app.state.logistics_runs
        claimed = runs.claim(); workflow = self.app.state.logistics_workflow
        factory = httpx.Client
        def handler(request): return httpx.Response(200, json=simulated(json.loads(request.content)))
        with patch('backend.investigation_model.httpx.Client', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            for _ in range(4):
                status, node, result = runs.step(runs.saved(self.run), {})
                runs.commit(self.run, claimed['claim_token'], status, node, result)
        snapshot = workflow.graph.get_state(workflow.config(self.run))
        self.assertEqual(snapshot.next, ('observe',))
        state = dict(snapshot.values)
        for trace in state['trace']:
            for key in ('call_key', 'parameters', 'reused', 'evidence_fingerprint'): trace.pop(key, None)
        workflow.graph.update_state(workflow.config(self.run), state, as_node='execute')
        workflow.close()
        self.app.state.logistics_workflow = LogisticsWorkflow(self.app.state.logistics_model, self.app.state.logistics_reports)
        runs.workflow = self.app.state.logistics_workflow
        with patch('backend.investigation_model.httpx.Client', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)):
            runs.process(runs.saved(self.run))
        self.assertEqual(runs.saved(self.run)['status'], 'completed')
        self.assertEqual(runs.budgets.read(self.run)['tool_calls'], 4)


if __name__ == '__main__': unittest.main()
