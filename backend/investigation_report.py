"""Structured model claims; citation validity is not semantic proof."""

CLAIM_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'statement': {'type': 'string', 'minLength': 1, 'maxLength': 500},
        'kind': {'type': 'string', 'enum': ['observed', 'reported', 'inferred']},
        'evidenceIds': {'type': 'array', 'minItems': 1, 'maxItems': 10,
                        'uniqueItems': True, 'items': {'type': 'string', 'maxLength': 200}},
        'limitation': {'type': 'string', 'maxLength': 500},
    },
    'required': ['statement', 'kind', 'evidenceIds', 'limitation'],
}
REPORT_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'status': {'type': 'string', 'enum': ['complete', 'partial']},
        'claims': {'type': 'array', 'maxItems': 8, 'items': CLAIM_SCHEMA},
        'unresolvedQuestions': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 500}},
        'nextSteps': {'type': 'array', 'maxItems': 5, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 500}},
    },
    'required': ['status', 'claims', 'unresolvedQuestions', 'nextSteps'],
}


def validate_report(args, evidence):
    if not isinstance(args, dict) or set(args) != set(REPORT_SCHEMA['required']):
        raise ValueError('报告需要 status、claims、unresolvedQuestions、nextSteps 四个字段。')
    if args['status'] not in ('complete', 'partial'):
        raise ValueError('status 必须为 complete 或 partial。')
    claims = args['claims']
    if not isinstance(claims, list) or len(claims) > 8:
        raise ValueError('claims 最多8条。')
    for key, limit in (('unresolvedQuestions', 8), ('nextSteps', 5)):
        values = args[key]
        if not isinstance(values, list) or len(values) > limit or any(
                not isinstance(v, str) or not v.strip() or len(v) > 500 for v in values):
            raise ValueError(f'{key} 最多{limit}条，每条1至500字。')
    if args['status'] == 'complete' and (not claims or args['unresolvedQuestions']):
        raise ValueError('完成报告需要有效结论且无未解问题，否则提交 partial。')
    if args['status'] == 'partial' and not args['unresolvedQuestions']:
        raise ValueError('partial 必须说明未完成事项；无证据时明确说明未获得有效证据。')
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != set(CLAIM_SCHEMA['required']):
            raise ValueError('每条结论需要 statement、kind、evidenceIds、limitation。')
        if not isinstance(claim['statement'], str) or not claim['statement'].strip() or len(claim['statement']) > 500:
            raise ValueError('每条结论须为1至500字。')
        if claim['kind'] not in ('observed', 'reported', 'inferred'):
            raise ValueError('结论类型无效。')
        refs = claim['evidenceIds']
        if (not isinstance(refs, list) or not 1 <= len(refs) <= 10 or
                any(not isinstance(r, str) or len(r) > 200 or r not in evidence for r in refs) or len(set(refs)) != len(refs)):
            raise ValueError('每条结论须引用1至10个已读取工具返回的不同证据ID；不能引用未读取证据。')
        limitation = claim['limitation']
        limited = any(evidence[r].get('provenance', {}).get('truncated') or
                      evidence[r].get('record', {}).get('truncated') for r in refs)
        if not isinstance(limitation, str) or len(limitation) > 500 or (
                (claim['kind'] in ('reported', 'inferred') or limited) and not limitation.strip()):
            raise ValueError('外部陈述、推断及引用截断结果的结论必须说明局限（最多500字）。')
    refs = list(dict.fromkeys(r for c in claims for r in c['evidenceIds']))
    # Legacy fields are derived, never accepted as a second source of truth.
    return {**args, 'summary': '\n'.join(c['statement'] for c in claims) or '未获得可支持结论的有效证据。',
            'evidenceIds': refs, 'qualification': '模型调查说明，需人工核对；关键数量、费用及可行性以确定性计算为准。'}


def plan_blockers(report, bundle):
    """Incomplete causal investigation alone does not invalidate deterministic plans."""
    reasons = []
    if report.get('accessInvalid'):
        reasons.append('调查授权或范围已失效。')
    if report.get('manualRequired'):
        reasons.append(report.get('stopReason') or '调查需要人工核查。')
    if not bundle.get('target') or not bundle['target'].get('computable'):
        reasons.append('缺少可计算的缺料对象或关键业务数据。')
    if not any(p.get('feasible') for p in bundle['candidates']):
        reasons.append('当前证据下没有可行的新方案（含已驳回方案排除），请补充业务数据或人工核查。')
    return list(dict.fromkeys(reasons))
