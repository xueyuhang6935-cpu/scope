"""重放已保存真实开发证据；可选同证据、同模型、同预算的报告阶段配对。"""
import argparse
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from backend.investigation_model import model_session
from backend.logistics_model import configured
from project.agents.logistics_coverage import overdue_coverage, goal_evidence_status
from project.agents.logistics_model import generate, ModelBudget
from project.agents.logistics_protocol import REPORT_SCHEMA, ProtocolError, validate_output
from project.prompts.loader import load_prompt


def replay_input(path):
    saved = json.loads(path.read_text(encoding='utf-8'))
    if not saved['results']:
        return saved, {}, {}
    first = saved['results'][0]
    scope = {k: first[k] for k in ('run_id', 'snapshot_id', 'fingerprint', 'business_as_of', 'metric_version', 'city', 'region_ids')}
    scope['workspace_id'] = 'shanghai-logistics'
    evidence = {}
    traces = saved['trace'] or [{'evidence_id': r['result_evidence_id'], 'reused': False} for r in saved['results']]
    for trace, record in zip(traces, saved['results']):
        if trace['reused']:
            continue
        evidence[trace['evidence_id']] = {k: scope[k] for k in ('run_id', 'workspace_id', 'snapshot_id')} | {
            'source_type': 'tool_result', 'record': record}
    return saved, scope, evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    source = ROOT / 'test-results/logistics-t13-round-v5/stability'
    replay = []
    for path in sorted(source.glob('*-*-*.json')):
        saved, scope, evidence = replay_input(path)
        coverage = overdue_coverage(evidence, scope)
        if evidence:
            assert coverage['global_overdue'] == 3
            assert coverage['ownership_coverage'] != 'unknown', (path.name, coverage)
        else:
            assert coverage['ownership_coverage'] == 'unknown'
        if evidence and saved['case'] == 'courier':
            assert coverage['couriers'][0]['courier_id'] == '12477'
            assert coverage['couriers'][0]['verdict'] == 'over_half'
        replay.append({'source': path.name, 'source_sha256': sha256(path.read_bytes()).hexdigest(),
                       'has_evidence': bool(evidence), 'coverage': coverage,
                       'goal_status': goal_evidence_status(saved['question'], evidence, scope)})
        facts = []
        for ref, item in evidence.items():
            record = item['record']
            if record.get('tool') == 'get_snapshot_summary':
                for metric in record.get('data', {}).get('metrics', {}):
                    facts.append({'statement': '故意把积压说成可见任务', 'kind': 'computed', 'evidence_ids': [ref],
                        'limitation': '仅重放已保存证据', 'computed': {'evidence_id': ref, 'metric': metric,
                        'value': record['data']['metrics'][metric]['value']}})
            elif record.get('tool') == 'get_courier_workload' and record.get('status') == 'ok':
                for row in record.get('rows', [])[:1]:
                    facts.append({'statement': '错误负荷提议', 'kind': 'observed', 'evidence_ids': [ref],
                        'limitation': '仅重放已保存证据', 'computed': None,
                        'fact': {'type': 'workload', 'evidence_id': ref, 'courier_id': row['courier_id']}})
        # 每组最多16句，保存新验证结果，不覆盖旧报告或重新查询数据库。
        generated = []
        for offset in range(0, len(facts), 16):
            value = {'status': 'partial', 'claims': facts[offset:offset+16], 'limitations': ['历史证据离线重放'],
                'unresolved_questions': ['非完整调查，不计稳定成功'], 'next_steps': []}
            generated.extend(validate_output('report', value, scope=scope, evidence=evidence)['claims'])
        replay[-1]['generated_fact_replay'] = generated
    (output / 'replay.json').write_text(json.dumps(replay, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'真实重放：{sum(r["has_evidence"] for r in replay)}份有证据记录通过，其余保留无证据状态。', flush=True)
    if not args.live:
        return
    env = {**dotenv_values(ROOT / '.env'), **os.environ}
    if not configured(env):
        raise ValueError('真实模型配置不可用，未发送请求')
    prompt, version = load_prompt('logistics_report')
    legacy_prompt = prompt.split('优先使用宿主overdue_coverage')[0]
    legacy_properties = deepcopy(REPORT_SCHEMA['properties'])
    legacy_properties.pop('overdue_assessment')
    # 仅报告阶段配对：不重新规划或查询，不替代T13完整稳定性对照。
    for filename in ('courier-dynamic-1.json', 'courier-dynamic-2.json'):
        saved, scope, evidence = replay_input(source / filename)
        for mode in ('legacy', 'coverage'):
            budget = ModelBudget()
            responses = []
            print(f'{filename}/{mode} 开始', flush=True)
            try:
                with model_session(env) as transport:
                    def request(*arguments):
                        response = transport(*arguments)
                        responses.append(response)
                        return response
                    if mode == 'legacy':
                        with patch('project.agents.logistics_model.overdue_coverage', return_value=None), \
                             patch('project.agents.logistics_model.load_prompt', return_value=(legacy_prompt, 'legacy-replay')), \
                             patch.dict(REPORT_SCHEMA['properties'], legacy_properties, clear=True):
                            result = generate('report', saved['question'], scope=scope, evidence=evidence,
                                plan={}, step_states={}, observations=[], request=request, budget=budget)
                    else:
                        result = generate('report', saved['question'], scope=scope, evidence=evidence,
                            plan={}, step_states={}, observations=[], request=request, budget=budget)
                outcome = {'result': result, 'error': None}
            except ProtocolError as error:
                outcome = {'result': None, 'error': error.code, 'error_path': error.path}
            outcome.update(mode=mode, source=filename, model=env.get('SUPPLY_MODEL'),
                calls=budget.calls, known_tokens=budget.tokens, usage_unknown=budget.usage_unknown,
                limits={'calls': budget.max_calls, 'tokens': budget.max_tokens, 'seconds': budget.max_seconds},
                evidence_sha256=sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(),
                scope=scope, validation_scope='保存真实证据的报告阶段重放；非完整调查，不证明T13验收通过')
            outcome['responses'] = responses
            (output / f'{filename[:-5]}-{mode}.json').write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'{mode}: error={outcome["error"]}, calls={budget.calls}, tokens={budget.tokens}', flush=True)


if __name__ == '__main__':
    main()
