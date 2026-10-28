"""保存真实证据的报告阶段核验；不重新取数，不重写历史结果。"""
import argparse
import json
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values
from backend.investigation_model import model_session
from backend.logistics_model import compact_evidence, output_mode
from project.agents.logistics_model import generate, ModelBudget
from project.agents.logistics_protocol import ProtocolError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', required=True, action='store_true')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output); output.mkdir(parents=True, exist_ok=False)
    env = {**dotenv_values(ROOT/'.env'), **os.environ}
    read = runpy.run_path(str(ROOT/'scripts/verify-logistics-overdue-coverage.py'))['replay_input']
    for name in ('courier-dynamic-1', 'courier-dynamic-2', 'window-dynamic-3'):
        source = ROOT/f'test-results/logistics-t13-round-v5/stability/{name}.json'
        saved, scope, evidence = read(source)
        budget = ModelBudget(); mode = output_mode(env)
        responses = []
        try:
            with model_session(env) as transport:
                def request(body, started, seconds):
                    response = transport({**body, 'thinking': {'type': 'disabled'}}, started, seconds)
                    responses.append(response)
                    return response
                result = generate('report', saved['question'], scope=scope, evidence=evidence,
                    plan={}, step_states={}, observations=[], evidence_context=compact_evidence(evidence, scope=scope),
                    mode=mode, request=request, budget=budget)
            outcome = {'result': result, 'error': None}
        except ProtocolError as error:
            outcome = {'result': None, 'error': error.code, 'error_path': error.path}
        outcome.update(source=name, mode=mode, model=env['SUPPLY_MODEL'], calls=budget.calls,
                       known_tokens=budget.tokens, usage_unknown=budget.usage_unknown, responses=responses,
                       note='真实已保存开发证据的报告阶段核验；非完整调查稳定性实验')
        (output/f'{name}.json').write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding='utf-8')
        print(name, mode, outcome['error'], budget.calls, budget.tokens, flush=True)


if __name__ == '__main__':
    main()
