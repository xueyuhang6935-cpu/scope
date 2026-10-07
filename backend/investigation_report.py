"""Business plan blockers and compatibility exports for model reports."""
from project.agents.report_protocol import CLAIM_SCHEMA, REPORT_SCHEMA, validate_report


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
