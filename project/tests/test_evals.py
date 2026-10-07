"""Evaluation selection and human-review gates; no model or backend services."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from project.evals.supply_agent import CASES, main


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'output.json'
        self.evaluate = Mock()
        self.load_live_env = Mock()

    def run_cli(self, *args):
        with patch.object(sys, 'argv', ['evaluation', *args, '--output', str(self.output)]), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return main(evaluate=self.evaluate, load_live_env=self.load_live_env)

    def test_single_case_is_offline_by_default_and_unknown_case_fails(self):
        case = json.loads(CASES.read_text(encoding='utf-8'))[0]
        self.evaluate.return_value = {'id': case['id'], 'contractPassed': True,
                                      'semanticPassed': None, 'tokens': 100}
        self.assertEqual(self.run_cli('--case', case['id']), 0)
        self.evaluate.assert_called_once()
        selected, env, live, review = self.evaluate.call_args.args
        self.assertEqual(selected, case)
        self.assertFalse(live)
        self.assertEqual(env['SUPPLY_MODEL'], 'fixture')
        self.assertEqual(json.loads(self.output.read_text(encoding='utf-8'))['mode'], 'offline')
        self.load_live_env.assert_not_called()
        self.evaluate.reset_mock()
        with self.assertRaises(SystemExit) as raised:
            self.run_cli('--case', 'unknown-case')
        self.assertEqual(raised.exception.code, 2)
        self.evaluate.assert_not_called()

    def review_files(self, mode='live'):
        result = self.root / 'saved.json'
        result.write_text(json.dumps({'mode': mode, 'results': [
            {'id': 'a', 'outputHash': 'hash-a', 'semanticPassed': None},
            {'id': 'b', 'outputHash': 'hash-b', 'semanticPassed': None}]}), encoding='utf-8')
        reviews = self.root / 'reviews.json'
        reviews.write_text(json.dumps({
            'a': {'outputHash': 'hash-a', 'claimsSupported': True, 'answerCorrect': True, 'conditionsPreserved': True},
            'b': {'outputHash': 'old-hash', 'claimsSupported': True, 'answerCorrect': True, 'conditionsPreserved': True},
        }), encoding='utf-8')
        return result, reviews

    def test_review_requires_matching_output_hash_and_never_calls_model(self):
        result, reviews = self.review_files()
        self.assertEqual(self.run_cli('--review-result', str(result), '--reviews', str(reviews)), 0)
        saved = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(saved['semanticPassed'], 1)
        self.assertEqual(saved['semanticPending'], 1)
        self.assertIsNone(saved['results'][1]['semanticPassed'])
        self.evaluate.assert_not_called()
        self.load_live_env.assert_not_called()

    def test_review_rejects_offline_outputs_non_boolean_labels_and_live_flag(self):
        for invalid in ('offline', 'non_boolean', 'live_flag'):
            with self.subTest(invalid=invalid):
                result, reviews = self.review_files('offline' if invalid == 'offline' else 'live')
                if invalid == 'non_boolean':
                    data = json.loads(reviews.read_text(encoding='utf-8'))
                    data['a']['claimsSupported'] = 1
                    reviews.write_text(json.dumps(data), encoding='utf-8')
                args = ['--review-result', str(result), '--reviews', str(reviews)]
                if invalid == 'live_flag':
                    args.append('--live')
                with self.assertRaises(SystemExit) as raised:
                    self.run_cli(*args)
                self.assertEqual(raised.exception.code, 2)
        self.evaluate.assert_not_called()
        self.load_live_env.assert_not_called()

    def test_live_requires_config_and_preserves_nonzero_contract_exit(self):
        self.load_live_env.return_value = None
        with self.assertRaises(SystemExit) as raised:
            self.run_cli('--live')
        self.assertEqual(raised.exception.code, 2)
        self.evaluate.assert_not_called()
        case = json.loads(CASES.read_text(encoding='utf-8'))[0]
        self.evaluate.return_value = {'id': case['id'], 'contractPassed': False,
                                      'semanticPassed': None, 'tokens': None}
        self.assertEqual(self.run_cli('--case', case['id']), 1)
        saved = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(saved['contractPassed'], 0)
        self.assertTrue(saved['usageUnknown'])
