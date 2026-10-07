"""Versioned, reviewable prompts; versions include a content hash for audit."""
from hashlib import sha256
from pathlib import Path


def load_prompt(name):
    text = (Path(__file__).with_name('prompts') / (name + '.txt')).read_text(encoding='utf-8')
    return text, f'{name}-v2-{sha256(text.encode()).hexdigest()[:12]}'


SCOPE_PROMPT, SCOPE_PROMPT_VERSION = load_prompt('scope_parser')
INVESTIGATOR_PROMPT, INVESTIGATOR_PROMPT_VERSION = load_prompt('investigator')
