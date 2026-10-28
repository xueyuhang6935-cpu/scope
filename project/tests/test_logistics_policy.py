"""T12纯规则检查：规范化、证据稳定性、真实触发与可复现差异。"""
from copy import deepcopy
import unittest

from project.agents.logistics_policy import call_key, evidence_fingerprint, revision_triggers, plan_diff
from project.tests.test_logistics_protocol import plan


class PolicyTests(unittest.TestCase):
    def test_equivalent_parameters_and_snapshot_binding(self):
        key = call_key('snapshot-hash', 'get_snapshot_summary', {})
        self.assertEqual(key, call_key('snapshot-hash', 'get_snapshot_summary', {'recent_minutes': 60, 'horizon_minutes': 30}))
        self.assertNotEqual(key, call_key('different-hash', 'get_snapshot_summary', {}))
        with self.assertRaises(ValueError): call_key('snapshot-hash', 'get_snapshot_summary', {'scope': 'all'})

    def test_same_business_evidence_cannot_become_new_by_ids_or_order(self):
        first = {'run_id': 'r1', 'tool': 'get_pending_tasks', 'page': 1, 'sort': 'risk_priority',
                 'status': 'ok', 'rows': [{'order_id': 'A', 'evidence_id': 'e1'}, {'order_id': 'B', 'evidence_id': 'e2'}]}
        second = deepcopy(first); second.update(run_id='r2', call_id='new', sort='wait_minutes_desc')
        second['rows'].reverse(); second['rows'][0]['evidence_id'] = 'new-id'
        self.assertEqual(evidence_fingerprint(first), evidence_fingerprint(second))
        second['rows'][0]['order_id'] = 'C'
        self.assertNotEqual(evidence_fingerprint(first), evidence_fingerprint(second))
        expanded = {'tool': 'get_evidence', 'rows': [{'record': first}]}
        self.assertEqual(evidence_fingerprint(first), evidence_fingerprint(expanded))

    def test_four_triggers_require_evidence_or_saved_constraint(self):
        value = {'hypothesis_assessments': [{'verdict': 'not_supported', 'evidence_ids': ['e1']}], 'counterevidence': [],
                 'unresolved_questions': ['逾期覆盖不足']}
        triggers = revision_triggers(value, {'status': 'unavailable', 'truncated': True}, 'e1',
                                     constraint={'constraint_id': 'c1'})
        self.assertEqual([t['kind'] for t in triggers], ['evidence_not_supported', 'tool_unavailable', 'data_truncated', 'constraint_changed'])
        value['hypothesis_assessments'][0].update(verdict='unknown', evidence_ids=[])
        self.assertEqual(revision_triggers(value, {'status': 'ok', 'truncated': False}, 'e1'), [])

    def test_truncation_after_goal_satisfied_does_not_expand_investigation(self):
        enough = {'unresolved_questions': [], 'next_steps': [], 'missing': ['零逾期人员未完整展示']}
        self.assertEqual(revision_triggers(enough, {'status': 'ok', 'truncated': True}, 'e1'), [])
        enough['unresolved_questions'] = ['尚缺共同窗口的另一名人员明细']
        self.assertEqual(revision_triggers(enough, {'status': 'ok', 'truncated': True}, 'e1')[0]['kind'], 'data_truncated')

    def test_diff_keeps_before_and_after_without_mutating_inputs(self):
        old = plan(); new = deepcopy(old); new['version'] = 2
        new['steps'][1]['parameters']['dimension'] = 'aoi_id'
        new['steps'].append(dict(deepcopy(new['steps'][0]), step_id='s3'))
        before = deepcopy(old)
        diff = plan_diff(old, new)
        self.assertEqual(diff['changed'][0]['step_id'], 's2')
        self.assertEqual(diff['added'][0]['step_id'], 's3')
        self.assertEqual(old, before)
        self.assertEqual(plan_diff(old, new), diff)


if __name__ == '__main__': unittest.main()
