"""Bounded conversation context and deterministic follow-up handling."""
import re

from .supply_analysis import normalize_question, propose
# Compatibility exports; internal callers use project.context directly.
from project.context.investigation import compact_task, focus_context, grounded_answer


ACTIONS = ('query', 'explain', 'refresh', 'investigate', 'simulate', 'approval_request', 'unsupported_execution')


def requested_action(text):
    """Bounded routing; the host still validates scope and referenced objects."""
    if re.search(r'解释|怎么算|如何算|怎么计算|如何计算', text):
        return 'explain'
    if re.search(r'批准|审批|驳回', text):
        return 'approval_request'
    if re.search(r'执行|下单|下发|付款|发货|创建.*单|立即调拨', text):
        return 'unsupported_execution'
    if re.search(r'重新查|重新计算|刷新|现在.*(缺|库存|风险)|最新.*(缺|库存|风险)', text):
        return 'refresh'
    if re.search(r'继续查|深入调查|调查|查清|核实.*原因', text):
        return 'investigate'
    if re.search(r'怎么解决|如何解决|怎么办|试算|哪个方案|比较.*方案|调拨还是加急', text):
        return 'simulate'
    if re.fullmatch(r'[请问那这个的呢？?\s]*(为什么|原因是什么)[呢？?\s]*', text):
        return 'explain'
    return 'query'


def followup(text, scope, context):
    normalized = normalize_question(text).strip(' \t\n。！？?！')
    explanation = bool(re.fullmatch(r'(?:那|那么|请|请问|这个|这|它|上面|上述|刚才|上次|上一轮|结果|问题|的|具体|再|一下|呢|呢？|该|应该|要|我|我们|帮我|怎么|如何|为什么|解决|处理|办|解释|说明|原因|建议|给出|有什么|是|什么|措施|下一步|做)+', normalized))
    previous = context.get('previous')
    explicit = propose(normalized, {**scope, 'intent': '', 'material': '', 'period': '', 'supplier': ''})
    action = requested_action(text)
    explanation = action == 'explain' or (explanation and action == 'query')
    continuation = action != 'query' or explanation or bool(re.match(r'^(那|那么|再|改|换|只看|还是|继续|同样)', normalized)) or not explicit['intent']
    effective = dict(scope)
    if previous and continuation:
        effective = dict(previous['plan'])
        for key, value in explicit.items():
            if value and (key != 'factory' or re.search(r'[AB]\s*(工厂|厂)', normalized, re.I)):
                effective[key] = value
        if re.search(r'(全部|所有)物料', normalized):
            effective['material'] = ''
        if re.search(r'(全部|所有)工厂', normalized):
            effective['factory'] = 'all'
        for key, value in scope.items():
            if value and (key != 'factory' or value != 'all'):
                effective[key] = value
    query_text = re.sub(r'^(那么|那|再|改成|改为|换成|只看|继续)\s*', '', normalized) if continuation else normalized
    return explanation, effective, query_text
