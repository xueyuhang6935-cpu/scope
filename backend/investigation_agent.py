"""Compatibility assembly for the migrated investigation decision loop."""
from project.agents.investigator import investigate as run_investigation
from .investigation_model import model_session
from .supply_model import configured


def investigate(bundle, evidence, env, model_allowed=True, query_tools=None, harness=None, evidence_reader=None):
    options = {key: env[key] for key in (
        'SUPPLY_INVESTIGATOR', 'SUPPLY_AGENT_MAX_ROUNDS',
        'SUPPLY_AGENT_MAX_SECONDS', 'SUPPLY_AGENT_MAX_TOKENS') if key in env}
    return run_investigation(
        bundle, evidence, options, model_allowed, query_tools, harness, evidence_reader,
        model_ready=lambda: configured({**env, 'SUPPLY_PLANNER': 'model'}),
        model_session=lambda: model_session(env))
