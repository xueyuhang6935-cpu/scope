"""Backend assembly and compatible CLI for Agent evaluations."""
from contextlib import nullcontext
import os
from pathlib import Path
from unittest.mock import patch

import httpx

from project.evals import supply_agent as evaluation
from project.evals.supply_agent import CASES, FixtureQueries, report_arguments, offline_handler
from .investigation_agent import investigate
from .supply_model import propose_with_model, configured


def model_context(case, live):
    if live:
        return nullcontext()
    module = 'backend.supply_model' if case['kind'] == 'scope' else 'backend.investigation_model'
    return patch(module + '.httpx.Client', return_value=httpx.Client(transport=httpx.MockTransport(offline_handler(case))))


def evaluate(case, env, live=False, review=None):
    return evaluation.evaluate(case, env, live, review, investigate=investigate,
                               propose_with_model=propose_with_model, model_context=model_context)


def load_live_env():
    from dotenv import dotenv_values
    env = {**dotenv_values(Path(__file__).resolve().parents[1] / '.env'), **os.environ}
    return env if configured({**env, 'SUPPLY_PLANNER': 'model'}) else None


def main():
    return evaluation.main(evaluate=evaluate, load_live_env=load_live_env)


if __name__ == '__main__':
    raise SystemExit(main())
