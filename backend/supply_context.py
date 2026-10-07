"""Bounded conversation context and deterministic follow-up handling."""
import json
import re

from .supply_analysis import normalize_question, propose


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


def compact_task(row):
    result = json.loads(row.get('result_json') or 'null')
    plan = json.loads(row['plan_json'])
    selected = ((result or {}).get('workflow') or {}).get('selected') or plan

    def relevant(items):
        # Stable partition: prioritize the selected object before applying limits.
        def matches(item):
            return all(not selected.get(k) or selected[k] == 'all' or item.get(k) == selected[k]
                       for k in ('factory', 'material', 'revision'))
        return sorted(items, key=lambda item: not matches(item))[:5]

    compact = None
    if result:
        compact = {key: result[key] for key in ('summary', 'answer', 'missing', 'recommendations', 'limitations', 'capturedAt') if key in result}
        compact['records'] = relevant(result.get('records', []))
        calculation = result.get('calculation')
        if calculation:
            compact['calculation'] = {key: calculation.get(key) for key in ('scenarioName', 'asOf', 'windowStart', 'windowEnd', 'missing')}
            compact['calculation']['results'] = [{k: item.get(k) for k in ('factory', 'material', 'revision', 'computable', 'opening', 'totalDemand', 'shortage', 'firstShortageAt', 'missing', 'evidenceIds')}
                                                 for item in relevant(calculation.get('results', []))]
        compact['evidenceLimited'] = True
    if compact and len(json.dumps(compact, ensure_ascii=False)) > 12000:
        compact = {key: compact[key] for key in ('summary', 'answer', 'capturedAt') if key in compact}
        compact['evidenceLimited'] = True
    if compact and result.get('workflow'):
        compact['workflow'] = result['workflow']
    return {'id': row['id'], 'text': row['text'], 'status': row['status'],
            'plan': plan, 'source': json.loads(row.get('source_json') or 'null'),
            'error': row.get('error'), 'result': compact}


def focus_context(context, run=None):
    """Build a bounded, deterministic memory from the selected saved result."""
    previous = context.get('previous')
    result = (previous or {}).get('result') or {}
    report = (run or {}).get('report') or {}
    model = report.get('modelSummary') or {}
    claims = [dict(c, kind='observed') for c in report.get('facts', [])]
    # Model-observed claims are labelled as model statements, not independently verified.
    claims.extend({**c, 'qualification': '模型观察，仍需核对证据'} for c in model.get('claims', []) if c['kind'] == 'observed')
    for item in result.get('calculation', {}).get('results', []):
        if item.get('computable'):
            claims.append({'kind': 'observed', 'statement': f"{item['factory']}厂 {item['material']} 保存的最大缺口为 {item['shortage']}，此处未重新计算。",
                           'evidenceIds': (item.get('evidenceIds') or [])[:10]})
    context.update(focus={'taskId': previous['id'], 'scope': previous['plan'], 'snapshot': previous['source'],
                          'runId': run['id'] if run else None} if previous else None,
                   confirmedClaims=claims[:8],
                   openQuestions=(model.get('unresolvedQuestions', []) + report.get('missing', []) + result.get('missing', []))[:8],
                   rejectedPlans=((run or {}).get('input', {}).get('replanning') or {}).get('rejectedPlans', [])[-10:],
                   evidenceLimited=True)
    if report.get('status') == 'partial' and report.get('stopReason'):
        context['openQuestions'] = [report['stopReason'], *context['openQuestions']][:8]
    if run:
        rejected = [{'planId': p['id'], 'reason': p['approval']['comment']} for p in run.get('plans', [])
                    if (p.get('approval') or {}).get('decision') == 'rejected']
        context['rejectedPlans'] = (context['rejectedPlans'] + rejected)[-10:]
    # Apply limits after enrichment as well, rather than letting workflow text bypass them.
    for key in ('confirmedClaims', 'openQuestions', 'rejectedPlans'):
        while context[key] and len(json.dumps(context[key], ensure_ascii=False)) > 8000:
            context[key].pop()
    return context


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


def grounded_answer(context):
    previous = context.get('previous')
    if not previous:
        return '你想解决哪个问题？请先描述问题，或发起一次分析后继续追问。'
    result = previous.get('result') or {}
    if previous['status'] == 'failed':
        return '上一轮分析失败，尚未生成可靠结论。' + (previous.get('error') or '') + ' 请先排查该错误，再重新发起分析。'
    parts = [result.get('summary', '上一轮尚未生成可用的分析结论。')]
    calculation = result.get('calculation') or {}
    for item in calculation.get('results', [])[:5]:
        if not item.get('computable'):
            continue
        parts.append(f"{item['factory']} 厂 {item['material']}：期初可用量 {item['opening']}；按时间依次加入到线供给、扣除需求，余额最小值为负时，其绝对值形成最大缺口 {item['shortage']}。")
        if item.get('firstShortageAt'):
            parts.append('首次缺料时间：' + item['firstShortageAt'])
    if result.get('capturedAt'):
        parts.append('依据结果时间：' + result['capturedAt'])
    if result.get('missing'):
        parts.append('优先补齐或核对：' + '；'.join(result['missing']))
    if result.get('recommendations'):
        parts.append('建议：' + '；'.join(result['recommendations']))
    parts.append('以上依据上一轮保存的结果，未重新查询。现有证据不足以确定的根因或具体处置数量，需要进一步核实；调拨、下单等操作仍需走业务审批。')
    return '\n\n'.join(parts)
