"""Behavioral boundaries for the extracted context and report protocol."""
from copy import deepcopy
import json
import subprocess
import sys
import unittest

from project.context.investigation import compact_task, focus_context, grounded_answer
from project.agents.report_protocol import validate_report


class ContextReportTests(unittest.TestCase):
    def test_selected_record_and_snapshot_survive_compaction(self):
        row = {'id': 'task', 'text': 'inspect', 'status': 'completed',
               'plan_json': json.dumps({'factory': 'A', 'material': 'P009'}),
               'source_json': json.dumps({'snapshotHash': 'fixed', 'asOf': '2000-07-02T10:00:00'}),
               'result_json': json.dumps({'records': [
                   {'factory': 'A', 'material': f'P{i:03}', 'quantity': i} for i in range(10)]})}
        before = deepcopy(row)
        task = compact_task(row)
        self.assertEqual(row, before)
        self.assertEqual(len(task['result']['records']), 5)
        self.assertEqual(task['result']['records'][0]['material'], 'P009')
        context = {'previous': task}
        self.assertIs(focus_context(context), context)
        self.assertEqual(context['focus']['snapshot'], json.loads(row['source_json']))
        self.assertEqual(context['focus']['scope'], json.loads(row['plan_json']))
        self.assertTrue(context['evidenceLimited'])

    def test_partial_questions_rejections_and_claim_qualification(self):
        run = {'id': 'run', 'input': {'replanning': {'rejectedPlans': [{'planId': 'old', 'reason': 'constraint'}]}},
               'report': {'status': 'partial', 'stopReason': 'missing proof', 'modelSummary': {
                   'claims': [{'kind': 'reported', 'statement': 'unverified'},
                              {'kind': 'inferred', 'statement': 'hypothesis'},
                              {'kind': 'observed', 'statement': 'observed count', 'evidenceIds': ['e1']}],
                   'unresolvedQuestions': ['contradicting evidence']}},
               'plans': [{'id': 'new', 'approval': {'decision': 'rejected', 'comment': 'no added staff'}}]}
        context = focus_context({'previous': {'id': 'task', 'plan': {}, 'source': {}, 'result': {}}}, run)
        self.assertEqual([c['statement'] for c in context['confirmedClaims']], ['observed count'])
        self.assertTrue(context['confirmedClaims'][0]['qualification'])
        self.assertEqual(context['openQuestions'], ['missing proof', 'contradicting evidence'])
        self.assertEqual([p['reason'] for p in context['rejectedPlans']], ['constraint', 'no added staff'])

    def test_legacy_report_does_not_become_a_verified_claim(self):
        context = focus_context({'previous': None}, {'id': 'old', 'report': {
            'modelSummary': {'summary': 'legacy text', 'evidenceIds': ['e1']}}})
        self.assertEqual(context['confirmedClaims'], [])
        self.assertIsNone(context['focus'])
        self.assertTrue(grounded_answer(context))

    def test_partial_and_legacy_fields_are_derived(self):
        args = {'status': 'partial', 'claims': [{'statement': 'source statement', 'kind': 'reported',
                'evidenceIds': ['e1'], 'limitation': 'not independently verified'}],
                'unresolvedQuestions': ['need verification'], 'nextSteps': []}
        result = validate_report(args, {'e1': {'record': {'truncated': True}}})
        self.assertEqual(result['summary'], 'source statement')
        self.assertEqual(result['evidenceIds'], ['e1'])
        self.assertEqual(result['status'], 'partial')
        for invalid in (
            {**args, 'unresolvedQuestions': []},
            {**args, 'status': 'complete'},
            {'summary': 'legacy submission', 'evidenceIds': ['e1']},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_report(invalid, {'e1': {}})

    def test_fabricated_and_truncated_evidence_rejected(self):
        args = {'status': 'complete', 'claims': [{'statement': 'claim', 'kind': 'observed',
                'evidenceIds': ['e1'], 'limitation': ''}], 'unresolvedQuestions': [], 'nextSteps': []}
        for evidence in ({}, {'e1': {'provenance': {'truncated': True}}}):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                validate_report(args, evidence)
        self.assertEqual(validate_report(args, {'e1': {}})['summary'], 'claim')

    def test_extracted_modules_do_not_import_backend(self):
        subprocess.run([sys.executable, '-c',
                        'import sys; import project.context.investigation; '
                        'import project.agents.report_protocol; import project.prompts.loader; '
                        'import project.tools.investigation_sql; import project.tools.executor; '
                        'import project.agents.investigator; import project.tools.investigation_dispatch; '
                        'import project.workflows.investigation; '
                        'assert not any(n == "backend" or n.startswith("backend.") for n in sys.modules)'],
                       check=True, capture_output=True)

    def test_compatibility_exports_have_one_implementation(self):
        from backend import supply_context, investigation_report
        self.assertIs(supply_context.compact_task, compact_task)
        self.assertIs(supply_context.focus_context, focus_context)
        self.assertIs(supply_context.grounded_answer, grounded_answer)
        self.assertIs(investigation_report.validate_report, validate_report)
