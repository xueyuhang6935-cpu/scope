"""仅组装后端已授权的上下文；不查询数据库。"""
from copy import deepcopy
import json

from project.agents.logistics_protocol import ProtocolError
from project.prompts.loader import load_prompt


def build_messages(text, context, recent, report=None, evidence=None, collaboration=None):
    prompt, version = load_prompt('logistics_chat')
    if collaboration:
        collaboration = deepcopy(collaboration)
        collaboration['drafts'] = collaboration.get('drafts', [])[-1:]
        collaboration['proposals'] = collaboration.get('proposals', [])[-2:]
        for item in collaboration['drafts']+collaboration['proposals']:
            content = item['content']
            content['tasks'] = [{k: t[k] for k in ('order_id', 'evidence_id', 'risk_state') if k in t} for t in content['tasks']]
            # 持续约束与人工事实在权威反馈记录中携带，版本内容只保留引用视图。
            content.pop('feedback', None)
        if collaboration.get('review'):
            collaboration['review'] = {k: collaboration['review'][k] for k in ('business_as_of', 'limitations')}
    if report and report.get('focus_tasks'):
        report = deepcopy(report)
        report['focus_tasks'] = [{k: t[k] for k in ('order_id', 'evidence_id', 'risk_state', 'current_wait_minutes', 'current_overdue_minutes') if k in t} for t in report['focus_tasks']]
    value = {'message': text, 'context': deepcopy(context), 'recent_messages': recent[-8:],
             'report': report, 'evidence': evidence or {}, 'collaboration': collaboration}
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > 48000:
        raise ProtocolError('context_budget')
    return [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': encoded}], version
