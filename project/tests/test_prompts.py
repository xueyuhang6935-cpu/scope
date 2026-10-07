"""Prompt content/version compatibility and path-independent resource loading."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from project.prompts import loader


class PromptMigrationTests(unittest.TestCase):
    def test_versions_and_compatibility_exports(self):
        from backend import supply_prompts as legacy
        self.assertEqual(loader.SCOPE_PROMPT_VERSION, 'scope_parser-v2-65f396b4dd08')
        self.assertEqual(loader.INVESTIGATOR_PROMPT_VERSION, 'investigator-v2-b94a85e7a36b')
        self.assertIs(legacy.load_prompt, loader.load_prompt)
        for name in ('SCOPE_PROMPT', 'SCOPE_PROMPT_VERSION', 'INVESTIGATOR_PROMPT', 'INVESTIGATOR_PROMPT_VERSION'):
            self.assertEqual(getattr(legacy, name), getattr(loader, name))

    def test_loading_from_another_working_directory(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as other:
            result = subprocess.run(
                [sys.executable, '-c', 'import json; from project.prompts.loader import load_prompt; '
                 'print(json.dumps([load_prompt(n)[1] for n in ("scope_parser", "investigator")]))'],
                cwd=other, env={**os.environ, 'PYTHONPATH': str(root)},
                capture_output=True, text=True, check=True,
            )
        self.assertEqual(json.loads(result.stdout), [loader.SCOPE_PROMPT_VERSION, loader.INVESTIGATOR_PROMPT_VERSION])
