"""证据驱动的修订、差异和停止策略；不访问数据库或模型。"""
from copy import deepcopy
from hashlib import sha256
import json

from project.tools.logistics import validate_arguments


STOP_REASONS = ('model_call_limit', 'tool_call_limit', 'revision_limit', 'token_limit', 'active_time_limit',
                'no_progress', 'blocked_dependencies', 'interrupted_operation', 'model_transport_failed',
                'invalid_model_output', 'required_evidence_unavailable', 'evidence_limit', 'authorization_changed', 'cancelled')


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def call_key(snapshot_fingerprint, tool, parameters):
    return digest([snapshot_fingerprint, tool, validate_arguments(tool, parameters)])


def evidence_fingerprint(record):
    # 去除运行/定位ID，重复保存同一内容不能制造新观察；保留状态、口径、排序和分母。
    excluded = {'run_id', 'snapshot_id', 'evidence_id', 'call_id', 'result_evidence_id', 'result_fingerprint', 'sort', 'page', 'page_size'}
    if record.get('tool') == 'get_evidence' and len(record.get('rows', [])) == 1:
        record = record['rows'][0].get('record', record)
    def business(value):
        if isinstance(value, dict):
            return {k: business(v) for k, v in value.items() if k not in excluded}
        return sorted((business(v) for v in value), key=digest) if isinstance(value, list) else value
    return digest(business(record))


def revision_triggers(observation, record, evidence_id, *, constraint=None):
    triggers = []
    rejected = [a for a in observation.get('hypothesis_assessments', [])
                if a['verdict'] == 'not_supported' and a['evidence_ids']]
    if rejected or observation.get('counterevidence'):
        refs = list(dict.fromkeys([r for a in rejected+observation.get('counterevidence', []) for r in a['evidence_ids']]))
        triggers.append({'kind': 'evidence_not_supported', 'evidence_ids': refs, 'reason': '保存证据不支持原假设或出现反证'})
    if record.get('status') in ('error', 'unavailable'):
        triggers.append({'kind': 'tool_unavailable', 'evidence_ids': [evidence_id], 'reason': '实际工具结果不可用'})
    if record.get('truncated') and (observation.get('unresolved_questions') or observation.get('next_steps')):
        triggers.append({'kind': 'data_truncated', 'evidence_ids': [evidence_id], 'reason': '实际工具结果被分页截断'})
    if constraint:
        triggers.append({'kind': 'constraint_changed', 'evidence_ids': [], 'constraint_id': constraint['constraint_id'],
                         'reason': '已授权的调查约束版本发生变化'})
    return triggers


def plan_diff(before, after):
    old, new = ({s['step_id']: s for s in p['steps']} for p in (before, after))
    return {'added': [deepcopy(new[k]) for k in sorted(new.keys()-old.keys())],
            'removed': [deepcopy(old[k]) for k in sorted(old.keys()-new.keys())],
            'changed': [{'step_id': k, 'before': deepcopy(old[k]), 'after': deepcopy(new[k])}
                        for k in sorted(old.keys() & new.keys()) if old[k] != new[k]],
            'hypotheses': {'before': before['hypotheses'], 'after': after['hypotheses']}}
