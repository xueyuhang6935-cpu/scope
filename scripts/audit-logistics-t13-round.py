"""只读复查T13产物：成本分项、修订回放、配对与逐条报告审核输入。"""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
ROUND = runpy.run_path(str(ROOT/'scripts/verify-logistics-t13-round.py'))
REAL, SHARED = ROUND['REAL'], ROUND['SHARED']


def changes(data):
    answer = []
    evidence = {t['evidence_id']: r for t, r in zip(data['trace'], data['results'])}
    for revision in data['revisions']:
        previous = next((t for t in reversed(data['trace']) if t['plan_version'] == revision['old_plan']['version']), None)
        observation = next((o['value'] for o in reversed(data['observations']) if previous and
            o['value']['plan_version'] == previous['plan_version'] and o['value']['step_id'] == previous['step_id']), None)
        replay = SHARED['revision_triggers'](observation, evidence[previous['evidence_id']], previous['evidence_id']) if observation else []
        diff = revision['diff']
        added = diff['added']+[s['after'] for s in diff['changed'] if
            (s['before']['tool'], s['before']['parameters']) != (s['after']['tool'], s['after']['parameters'])]
        executed = [s for s in added if any(t.get('origin') == 'model' and not t.get('parameter_binding') and
            not t.get('reused') and t['plan_version'] >= revision['new_plan']['version'] and
            SHARED['call_key'](data['snapshot_fingerprint'], t['tool'], t['parameters']) ==
            SHARED['call_key'](data['snapshot_fingerprint'], s['tool'], s['parameters']) for t in data['trace'])]
        answer.append({'version': revision['new_plan']['version'], 'trigger_kinds': [t['kind'] for t in revision['triggers']],
            'trigger_replay_matches': replay == revision['triggers'], 'added_or_changed': added, 'executed': executed,
            'reason': revision['reason']})
    return answer


def costs(data):
    phases = {}; plan_calls = 0
    for receipt in data['model_usage_records']:
        phase = receipt['kind']
        if phase == 'plan':
            # 验证器修复仍属同一次规划提议，不能误算成一次业务修订。
            proposed = data.get('plans', [])
            first_calls = (proposed[0]['model_calls'] if proposed else 2)
            phase = 'planning' if plan_calls < first_calls else 'revision'
            plan_calls += 1
        value = phases.setdefault(phase, {'calls': 0, 'known_total_tokens': 0, 'unknown_total_calls': 0,
            'known_prompt_tokens': 0, 'unknown_prompt_calls': 0, 'known_completion_tokens': 0, 'unknown_completion_calls': 0})
        value['calls'] += 1
        for source, known, unknown in [('total_tokens', 'known_total_tokens', 'unknown_total_calls'),
            ('prompt_tokens', 'known_prompt_tokens', 'unknown_prompt_calls'),
            ('completion_tokens', 'known_completion_tokens', 'unknown_completion_calls')]:
            if receipt.get(source) is None: value[unknown] += 1
            else: value[known] += receipt[source]
    return phases


def safety(data, spec):
    violations = []
    for result in data['results']:
        if result.get('snapshot_id') != data['snapshot_id'] or result.get('region_ids') != spec['snapshot']['region_ids']:
            violations.append('tool_scope_mismatch')
        for row in result.get('rows', []):
            if 'accept_time_reference' not in row: continue
            if row['accept_time_reference'] > spec['snapshot']['business_as_of']: violations.append('future_accept')
            if not row.get('completion_observed') and any(k in row for k in
                ('pickup_time', 'pickup_time_reference', 'ds', 'duration_minutes', 'is_overdue')):
                violations.append('hidden_completion_exposed')
    return sorted(set(violations))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--reviews', help='逐条审核JSON，键为stage/file，值含claim_reviews及goal_covered')
    parser.add_argument('--label', default='audited', help='复查版本标签；原始结果与既有复查均不覆盖')
    args = parser.parse_args(); output = Path(args.output)
    cases = json.loads((output/'cases.json').read_text(encoding='utf-8'))['cases']
    reviews = json.loads(Path(args.reviews).read_text(encoding='utf-8')) if args.reviews else {}
    rows = []; worksheet = ['# T13逐条审核工作表', '', '来源为模型原始报告；程序停止报告单列。审核必须核对声明、局限、原始工具及独立答案。', '']
    for stage in ('development', 'stability'):
        for path in sorted((output/stage).glob('*-*-*.json')):
            data = json.loads(path.read_text(encoding='utf-8'))
            if 'case' not in data: continue
            key = str(path.relative_to(output)).replace('\\', '/')
            assessment = REAL['assess_real'](data, cases[data['case']])
            model_report = bool(data['report']) and (data['mode'] == 'fixed' or data['run_status'] == 'completed')
            claims = (data['report'] or {}).get('claims', [])
            review = reviews.get(key, {})
            claim_reviews = review.get('claim_reviews', [])
            reviewed = len(claim_reviews) == len(claims) and all(c.get('verdict') in ('pass', 'fail') for c in claim_reviews)
            semantic = reviewed and all(c['verdict'] == 'pass' for c in claim_reviews) and review.get('goal_covered') is True
            violations = safety(data, cases[data['case']])
            paths = changes(data)
            rows.append({'file': key, 'output_hash': ROUND['digest'](path), 'case': data['case'], 'mode': data['mode'],
                'stage': stage, 'repetition': data['repetition'], 'assessment': assessment, 'model_report': model_report,
                'report_status': (data['report'] or {}).get('status'), 'run_status': data['run_status'],
                'stop_reason': data['stop_reason'], 'budget': data['budget'], 'cost_phases': costs(data),
                'elapsed_seconds': data['elapsed_seconds'], 'safety_violations': violations, 'path_changes': paths,
                'host_contributions': [t for t in data['trace'] if t.get('origin') == 'host_goal_requirement' or t.get('parameter_binding')],
                'semantic_review': review or {'status': 'pending'},
                'valid_report': bool(model_report and semantic and not violations and assessment['numeric_correct'] and
                                     assessment['necessary_evidence']),
                'snapshot_id': data['snapshot_id'], 'input_hash': data['input_hash'], 'question': data['question']})
            worksheet.extend([f'## {key}', '', f"模型报告={model_report}；运行={data['run_status']}；停止={data['stop_reason']}；必要证据={assessment['necessary_evidence']}", ''])
            for i, claim in enumerate(claims, 1):
                worksheet.extend([f"- C{i}（{claim['kind']}）：{claim['statement']}", f"  - 局限：{claim['limitation']}",
                    f"  - 引用：{', '.join(claim['evidence_ids'])}"])
            worksheet.extend(['', '未解问题：'+json.dumps((data['report'] or {}).get('unresolved_questions'), ensure_ascii=False), ''])
    pairs = []
    for stage in ('development', 'stability'):
        for case in ROUND['CASES']:
            for repetition in range(1, 4 if stage == 'stability' else 2):
                pair = {r['mode']: r for r in rows if (r['stage'], r['case'], r['repetition']) == (stage, case, repetition)}
                if len(pair) != 2: continue
                assert all(pair['fixed'][k] == pair['dynamic'][k] for k in ('snapshot_id', 'input_hash', 'question'))
                pairs.append({'stage': stage, 'case': case, 'repetition': repetition, 'same_input': True,
                    'fixed_valid': pair['fixed']['valid_report'], 'dynamic_valid': pair['dynamic']['valid_report'],
                    'dynamic_minus_fixed_tokens': pair['dynamic']['budget']['model_tokens']-pair['fixed']['budget']['model_tokens'],
                    'cost_unknown': pair['fixed']['budget']['model_usage_unknown'] or pair['dynamic']['budget']['model_usage_unknown']})
    REAL['save'](output/f'{args.label}-summary.json', {'runs': rows, 'pairs': pairs, 'run_count': len(rows),
        'reviewer': 'Codex逐条内部审核；非独立人工盲审', 'safety_note': '检查已返回证据的范围/未来完成字段；结合权限和快照回归，不声明审计了未记录的原始HTTP全文。',
        'stability_valid_counts': {f'{c}/{m}': sum(r['valid_report'] for r in rows if
            (r['stage'], r['case'], r['mode']) == ('stability', c, m)) for c in ROUND['CASES'] for m in ('fixed', 'dynamic')}})
    (output/'review-worksheet.md').write_text('\n'.join(worksheet), encoding='utf-8')
    print(f'已复查{len(rows)}次运行和{len(pairs)}组配对；待审不得计为有效报告。')


if __name__ == '__main__': main()
