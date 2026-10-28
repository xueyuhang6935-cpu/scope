"""单次协议生成和有限修复；不执行工具、不访问配置或数据库。"""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import time

from project.agents.logistics_protocol import SCHEMAS, ProtocolError, strict_json, validate_output, PROTOCOL_VERSION, normalize_report, repair_instruction, validate_report_facts, report_fact_options, source_limitations
from project.prompts.loader import load_prompt
from project.agents.logistics_coverage import overdue_coverage, goal_evidence_status

MODES = ('tool_call', 'json_schema', 'json_object', 'json_text')
REPORT_OUTPUT_TOKENS = 2400
REPORT_MIN_OUTPUT_TOKENS = 1200


def estimate_input_tokens(body):
    # ponytail: 无供应商本地tokenizer，按实测JSON输入的UTF-8字节/3加协议余量估算；实际账单仍是硬检查依据。
    return math.ceil(len(json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()) / 3) + 256


@dataclass
class ModelBudget:
    max_calls: int = 12
    max_tokens: int = 24000
    max_seconds: float = 120
    max_repairs: int = 1
    calls: int = 0
    tokens: int = 0
    usage_unknown: bool = False

    def __post_init__(self):
        for key in ('max_calls', 'max_tokens', 'max_repairs', 'calls', 'tokens'):
            if type(getattr(self, key)) is not int or getattr(self, key) < 0:
                raise ValueError('invalid model budget')
        if type(self.max_seconds) not in (int, float) or not math.isfinite(self.max_seconds) or not 0 < self.max_seconds <= 300 or self.max_calls > 24 or self.max_tokens > 60000 or self.max_repairs > 2:
            raise ValueError('invalid model budget')


def proposal_schema(kind):
    schema = deepcopy(SCHEMAS[kind])
    if kind == 'report':
        schema['required'].remove('evidence_ids')
        schema['required'].remove('limitations')
        claim = schema['properties']['claims']['items']
        claim['required'] = [key for key in claim['required'] if key not in ('computed', 'limitation')]
        schema['properties']['overdue_assessment']['required'].remove('evidence_ids')
    return schema


def model_body(kind, messages, mode, max_tokens):
    if kind not in SCHEMAS or mode not in MODES:
        raise ProtocolError('invalid_adapter_mode')
    schema = proposal_schema(kind)
    body = {'messages': deepcopy(messages), 'stream': False, 'max_tokens': max_tokens}
    name = 'submit_'+kind
    if mode == 'tool_call':
        body.update(tools=[{'type': 'function', 'function': {'name': name, 'description': '提交物流调查协议，不执行业务工具', 'parameters': schema}}],
                    tool_choice={'type': 'function', 'function': {'name': name}}, parallel_tool_calls=False)
    elif mode == 'json_schema':
        body['response_format'] = {'type': 'json_schema', 'json_schema': {'name': name, 'schema': schema, 'strict': False}}
    elif mode == 'json_object':
        body['response_format'] = {'type': 'json_object'}
    return body


def model_output(payload, kind, mode):
    if type(payload) is not dict or type(payload.get('choices')) is not list or len(payload['choices']) != 1:
        raise ProtocolError('invalid_response')
    choice = payload['choices'][0]
    if type(choice) is not dict or choice.get('finish_reason') not in ('stop', 'tool_calls'):
        raise ProtocolError('unfinished_response')
    message = choice.get('message')
    if type(message) is not dict or message.get('refusal'):
        raise ProtocolError('model_refusal')
    calls = message.get('tool_calls')
    if mode == 'tool_call':
        if type(calls) is not list or len(calls) != 1 or type(calls[0]) is not dict or calls[0].get('type') != 'function':
            raise ProtocolError('invalid_protocol_call')
        function = calls[0].get('function', {})
        if type(function) is not dict or function.get('name') != 'submit_'+kind:
            raise ProtocolError('invalid_protocol_call')
        return strict_json(function.get('arguments'))
    if calls:
        raise ProtocolError('unexpected_tool_calls')
    return strict_json(message.get('content'))


def report_context(question, evidence, scope, evidence_context=None):
    """报告估算与发送共用视图；完整证据仍留给授权和严格校验。"""
    goal = goal_evidence_status(question, evidence, scope)
    options = report_fact_options(evidence, scope)
    if not goal['supported']:
        return evidence if evidence_context is None else evidence_context, options, goal
    wanted = {'summary'}
    if goal['workload_required']:
        wanted.add('workload')
    if goal['window_required']:
        wanted.add('window')
    if goal['coverage_required']:
        wanted.add('ownership')
    options = [o for o in options if o['fact']['type'] in wanted and
               (o['fact']['type'] != 'summary' or o['fact']['metric'] == 'overdue')]
    context = {}
    for ref, item in evidence.items():
        if item['source_type'] != 'tool_result':
            continue
        record = item['record']
        tool, data = record.get('tool'), record.get('data')
        view = {k: record[k] for k in ('tool', 'status', 'denominator', 'truncated', 'page',
                'page_size', 'total_rows', 'returned_rows') if k in record}
        view['data'] = None if data is None else {}
        if tool == 'get_snapshot_summary' and data:
            view['data'] = {'metrics': {'overdue': data.get('metrics', {}).get('overdue', {})}}
        elif tool == 'get_courier_workload' and goal['workload_required']:
            view['rows'] = [r for r in record.get('rows', [])
                            if r.get('courier_id') in goal['coverage']['focus_courier_ids']]
        elif tool == 'get_pending_tasks' and goal['window_required']:
            rows = [r for r in record.get('rows', []) if r.get('risk_state') == 'overdue']
            view['rows'] = [{k: r[k] for k in ('evidence_id', 'courier_id', 'risk_state',
                'time_window_start_reference', 'time_window_end_reference') if k in r} for r in rows[:3]]
            view['context_truncated'] = len(rows) > 3
        elif tool == 'get_risk_distribution' and data:
            view['data'] = {k: data[k] for k in ('dimension', 'sort') if k in data}
        elif tool not in ('get_quality_summary', 'get_pending_tasks', 'get_courier_workload'):
            # 保留其他工具实际内容；不把未知工具限制压成成功。
            view = (evidence_context or {}).get(ref, item).get('record', record)
        context[ref] = {'source_type': item['source_type'], 'record': view}
    return context, options, goal


def build_messages(kind, question, *, scope, evidence, plan, step_states, mode='json_text',
                   observations=None, evidence_context=None, plan_context=None, goal_question=None):
    prompt, version = load_prompt('logistics_'+kind)
    context = {'question': question, 'server_scope': deepcopy(scope), 'plan': deepcopy(plan if plan_context is None else plan_context),
               'step_states': deepcopy(step_states), 'evidence': deepcopy(evidence if evidence_context is None else evidence_context), 'observations': deepcopy(observations or [])}
    coverage = overdue_coverage(evidence, scope) if kind in ('observation', 'report') else None
    goal_text = question if goal_question is None else goal_question
    concentration_goal = any(word in goal_text for word in ('集中', '过半', '一半'))
    assessment_required = kind == 'report' and concentration_goal and bool(coverage and coverage['couriers'])
    if coverage and coverage['evidence_ids']:
        context['overdue_coverage'] = coverage
        context['overdue_assessment_required'] = assessment_required
    if kind == 'report':
        context['evidence'], context['report_fact_options'], context['goal_evidence'] = report_context(
            goal_text, evidence, scope, evidence_context)
        context['goal_evidence'].pop('coverage')
        context.update(question=goal_text, source_limitations=source_limitations(evidence, scope))
        if context['goal_evidence']['supported']:
            context.update(plan={}, step_states={}, observations=[])
    schema_text = '' if mode in ('tool_call', 'json_schema') else '\n严格输出协议JSON：'+json.dumps(proposal_schema(kind), ensure_ascii=False, separators=(',', ':'))
    messages = [{'role': 'system', 'content': prompt+schema_text},
                {'role': 'user', 'content': json.dumps(context, ensure_ascii=False, allow_nan=False, separators=(',', ':'))}]
    if len(json.dumps(messages, ensure_ascii=False).encode()) > 96000:
        raise ProtocolError('context_budget')
    return messages, coverage, assessment_required, version, sha256(prompt.encode()).hexdigest()


def generate(kind, question, *, scope, evidence, plan, step_states, request, budget,
             mode='json_text', reserve=None, record_usage=None, observations=None, evidence_context=None, plan_context=None,
             protected_tokens=0, goal_question=None):
    """返回校验后的提议；网络错误/用量未知不自动修复或切换供应商模式。"""
    if kind not in SCHEMAS or mode not in MODES or not isinstance(question, str) or not 0 < len(question.strip()) <= 4000:
        raise ProtocolError('invalid_request')
    messages, coverage, assessment_required, version, prompt_hash = build_messages(kind, question,
        scope=scope, evidence=evidence, plan=plan, step_states=step_states, mode=mode,
        observations=observations, evidence_context=evidence_context, plan_context=plan_context, goal_question=goal_question)
    estimates = []
    started = time.monotonic()
    max_repairs = min(budget.max_repairs, 1) if kind == 'report' else budget.max_repairs
    for attempt in range(max_repairs+1):
        if budget.calls >= budget.max_calls or budget.tokens >= budget.max_tokens or time.monotonic()-started >= budget.max_seconds:
            raise ProtocolError('model_budget_exhausted')
        output_limit = REPORT_OUTPUT_TOKENS if kind == 'report' else 1500 if kind == 'plan' else 1024
        body = model_body(kind, messages, mode, output_limit)
        input_estimate = estimate_input_tokens(body)
        available = budget.max_tokens-budget.tokens-protected_tokens
        # 每次只保障当前请求；修复用实际回执后的余量，不提前占两份报告成本。
        usable = available - 512 - input_estimate
        minimum_output = REPORT_MIN_OUTPUT_TOKENS if kind == 'report' else output_limit
        if usable < minimum_output:
            raise ProtocolError('report_budget_reserved' if protected_tokens else 'model_budget_exhausted')
        body['max_tokens'] = min(output_limit, usable)
        estimates.append({'estimated_input_tokens': input_estimate, 'max_output_tokens': body['max_tokens'],
                          'repair_reserved': False, 'repair_attempt': attempt})
        if reserve:
            reserve()
        budget.calls += 1  # 请求发出前计入总调用；失败不返还。
        try:
            payload = request(body, started, budget.max_seconds)
        except Exception:
            budget.usage_unknown = True
            if record_usage:
                record_usage(None)
            raise ProtocolError('model_transport_failed') from None
        usage = payload.get('usage', {}).get('total_tokens') if type(payload) is dict and type(payload.get('usage', {})) is dict else None
        if type(usage) is not int or usage < 0:
            usage = None
            budget.usage_unknown = True
        else:
            budget.tokens += usage
        if record_usage:
            record_usage(usage)
        if budget.tokens > budget.max_tokens or time.monotonic()-started > budget.max_seconds:
            raise ProtocolError('model_budget_exhausted')
        try:
            value = model_output(payload, kind, mode)
            raw_value = deepcopy(value)
            normalization_paths = []
            if kind == 'report':
                value, normalization_paths = normalize_report(value, assessment_refs_optional=True)
            validated = validate_output(kind, value, scope=scope, evidence=evidence, plan=plan,
                                        step_states=step_states, render_limits=kind == 'report')
            if kind == 'report':
                validate_report_facts(validated)
                goal = goal_evidence_status(question if goal_question is None else goal_question, evidence, scope)
                if goal['supported'] and not goal['ready'] and validated['status'] == 'complete':
                    raise ProtocolError('goal_evidence_incomplete')
            if assessment_required:
                assessment = validated.get('overdue_assessment')
                if assessment is None:
                    raise ProtocolError('overdue_assessment_required')
                candidate = next((c for c in coverage['couriers'] if c['courier_id'] == assessment['courier_id']), None)
                if not candidate or any(assessment[k] != candidate[k] for k in ('overdue_count', 'verdict')) or assessment['global_overdue'] != coverage['global_overdue']:
                    raise ProtocolError('overdue_assessment_mismatch')
                if assessment['verdict'] == 'unknown' and validated['status'] == 'complete':
                    raise ProtocolError('overdue_goal_unresolved')
            if kind == 'report' and evidence_context:
                for claim in validated['claims']:
                    if any(evidence_context.get(ref, {}).get('record', {}).get('context_truncated') for ref in claim['evidence_ids']) and not claim['limitation'].strip():
                        raise ProtocolError('limitation_required')
        except (ProtocolError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            error = exc if isinstance(exc, ProtocolError) else ProtocolError('invalid_response')
            if attempt == max_repairs or budget.usage_unknown:
                raise error from None
            # 不将不可信模型文本或值拼回system提示词；只提供受控校验代码。
            messages.append({'role': 'user', 'content': repair_instruction(error)})
            continue
        return {'protocol_version': PROTOCOL_VERSION, 'kind': kind, 'value': validated,
                'scope': deepcopy(scope), 'prompt_version': version, 'prompt_hash': prompt_hash,
                'mode': mode, 'model_calls': budget.calls, 'known_tokens': budget.tokens,
                'usage_unknown': budget.usage_unknown, 'repairs': attempt,
                'request_budget_estimates': estimates,
                **({'raw_value': raw_value, 'normalization_paths': normalization_paths} if normalization_paths or raw_value != validated else {})}
