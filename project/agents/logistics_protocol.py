"""T10纯协议：模型只能提议结构；授权、状态与证据由宿主提供。"""
from copy import deepcopy
import json
import math
import re

from project.tools.logistics import validate_arguments
from project.agents.logistics_coverage import overdue_coverage

PROTOCOL_VERSION = 'logistics-protocol-v1'
ID = {'type': 'string', 'minLength': 1, 'maxLength': 64, 'pattern': r'^[A-Za-z][A-Za-z0-9_-]*$'}
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 1000}
SHORT = {'type': 'string', 'maxLength': 1000}
REF = {'type': 'string', 'minLength': 1, 'maxLength': 200}


def array(items, maximum=16, minimum=0):
    return {'type': 'array', 'items': items, 'minItems': minimum, 'maxItems': maximum}


def obj(**properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def enum(*values):
    return {'type': 'string', 'enum': list(values)}


REFS = {**array(REF, 10, 1), 'uniqueItems': True}
FACT = obj(statement=TEXT, evidence_ids=REFS)
STEP = obj(step_id=ID, question=TEXT, tool={'type': 'string'},
           parameters={'type': 'object'}, depends_on={**array(ID), 'uniqueItems': True},
           completion_condition=TEXT, status=enum('pending', 'running', 'completed', 'failed', 'skipped'))
INTENT_SCHEMA = obj(goal=TEXT, questions=array(TEXT, 8), clarification=SHORT,
                    action=enum('investigate', 'clarify', 'unsupported'))
PLAN_SCHEMA = obj(goal=TEXT, version={'type': 'integer', 'minimum': 1},
                  hypotheses=array(obj(hypothesis_id=ID, statement=TEXT), 8), steps=array(STEP, 16, 1),
                  revision_reason=SHORT)
OBSERVATION_SCHEMA = obj(plan_version={'type': 'integer', 'minimum': 1}, step_id=ID,
    new_facts=array(FACT, 16), hypothesis_assessments=array(obj(hypothesis_id=ID,
        verdict=enum('supported', 'not_supported', 'unknown'), evidence_ids={**array(REF, 10), 'uniqueItems': True}, reason=TEXT), 8),
    missing=array(TEXT, 8), counterevidence=array(FACT, 8), unresolved_questions=array(TEXT, 8), next_steps=array(TEXT, 8))
METRIC_NAMES = ('backlog', 'overdue', 'due_soon', 'recent_accepted', 'recent_completed')
COMPUTED = obj(evidence_id=REF, metric=enum(*METRIC_NAMES), value={'type': ['integer', 'null'], 'minimum': 0})
REPORT_SCHEMA = obj(status=enum('complete', 'partial'), claims=array(obj(
    statement=TEXT, kind=enum('computed', 'observed', 'reported', 'inferred'), evidence_ids=REFS,
    limitation=SHORT, computed={'anyOf': [COMPUTED, {'type': 'null'}]}), 16),
    evidence_ids={**array(REF, 100), 'uniqueItems': True}, limitations=array(TEXT, 16),
    unresolved_questions=array(TEXT, 8), next_steps=array(TEXT, 8))
# 新字段仅用于新模型报告；旧报告及程序停止报告保持可读。
REPORT_SCHEMA['properties']['overdue_assessment'] = obj(
    claim_index={'type': 'integer', 'minimum': 0},
    courier_id=REF, overdue_count={'type': 'integer', 'minimum': 0},
    global_overdue={'type': ['integer', 'null'], 'minimum': 0},
    verdict=enum('over_half', 'not_over_half', 'unknown', 'no_overdue'),
    missing_evidence=array(TEXT, 8), evidence_ids={**array(REF, 100, 1), 'uniqueItems': True})
# 可选的新事实绑定；旧保存报告不要求新增字段。
REPORT_SCHEMA['properties']['claims']['items']['properties']['fact'] = {'anyOf': [
    obj(type=enum('summary'), evidence_id=REF, metric=enum(*METRIC_NAMES, 'visible_tasks', 'completed_tasks')),
    obj(type=enum('workload'), evidence_id=REF, courier_id=REF),
    obj(type=enum('ownership')),
    obj(type=enum('window'), courier_ids={**array(REF, 100, 2), 'uniqueItems': True},
        coverage=enum('observed_subset', 'all_overdue'))]}
SCHEMAS = {'intent': INTENT_SCHEMA, 'plan': PLAN_SCHEMA, 'observation': OBSERVATION_SCHEMA, 'report': REPORT_SCHEMA}
METRIC_LABELS = {'backlog': '积压', 'overdue': '逾期未完成', 'due_soon': '临期未完成',
                 'recent_accepted': '近期接单', 'recent_completed': '近期完成',
                 'visible_tasks': '可见任务', 'completed_tasks': '已观察完成任务'}


class ProtocolError(ValueError):
    """错误仅携带固定代码和schema路径，不回显不可信输出。"""
    def __init__(self, code, path='$'):
        self.code, self.path = code, path
        super().__init__(f'{code}: {path}')


def strict_json(text):
    if not isinstance(text, str) or len(text.encode('utf-8')) > 96000:
        raise ProtocolError('invalid_json')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProtocolError('duplicate_key')
            result[key] = value
        return result
    try:
        def number(raw):
            value = float(raw)
            if not math.isfinite(value):
                raise ProtocolError('non_finite_number')
            return value
        value = json.loads(text, object_pairs_hook=pairs,
                           parse_float=number,
                           parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError('non_finite_number')))
        if not isinstance(value, dict):
            raise ProtocolError('object_required')
        return value
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError('invalid_json') from None


def check(value, schema, path='$'):
    """仅实现本文件使用的JSON Schema子集，供应商格式也必须经过同一校验。"""
    if 'anyOf' in schema:
        for choice in schema['anyOf']:
            try:
                check(value, choice, path)
                return
            except ProtocolError:
                pass
        raise ProtocolError('invalid_type', path)
    kind = schema.get('type')
    kinds = kind if isinstance(kind, list) else [kind]
    valid = any((k == 'object' and type(value) is dict) or (k == 'array' and type(value) is list)
        or (k == 'string' and type(value) is str) or (k == 'integer' and type(value) is int)
        or (k == 'null' and value is None) for k in kinds)
    if not valid:
        raise ProtocolError('invalid_type', path)
    if 'enum' in schema and value not in schema['enum']:
        raise ProtocolError('invalid_enum', path)
    if isinstance(value, dict):
        props = schema.get('properties', {})
        missing = next((key for key in schema.get('required', []) if key not in value), None)
        if missing is not None:
            raise ProtocolError('missing_field', path+'.'+missing)
        if schema.get('additionalProperties') is False and set(value) - props.keys():
            raise ProtocolError('extra_field', path)
        for key in props.keys() & value.keys():
            check(value[key], props[key], path+'.'+key)
    elif isinstance(value, list):
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 100):
            raise ProtocolError('invalid_length', path)
        if schema.get('uniqueItems') and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            raise ProtocolError('duplicate_item', path)
        for i, item in enumerate(value):
            check(item, schema['items'], f'{path}[{i}]')
    elif isinstance(value, str):
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 1000) or (schema.get('minLength') and not value.strip()):
            raise ProtocolError('invalid_length', path)
        if 'pattern' in schema and not re.fullmatch(schema['pattern'], value):
            raise ProtocolError('invalid_identifier', path)
    elif type(value) is int and value < schema.get('minimum', value):
        raise ProtocolError('invalid_number', path)


def evidence_record(identifier, evidence, scope):
    item = evidence.get(identifier)
    if not item or any(item.get(key) != scope[key] for key in ('run_id', 'workspace_id', 'snapshot_id')):
        raise ProtocolError('evidence_scope')
    if item.get('source_type') not in ('tool_result', 'snapshot_task', 'manual_statement'):
        raise ProtocolError('evidence_source')
    record = item.get('record', {})
    if item['source_type'] == 'tool_result' and any(record.get(key) != scope[key] for key in ('run_id', 'snapshot_id', 'fingerprint', 'business_as_of', 'metric_version')):
        raise ProtocolError('evidence_scope')
    return item


def validate_plan(value, *, previous=None, step_states=None, evidence=None):
    check(value, PLAN_SCHEMA)
    result = deepcopy(value)
    if value['version'] != (previous['version']+1 if previous else 1) or (previous and not value['revision_reason'].strip()):
        raise ProtocolError('plan_version')
    hypothesis_ids = [h['hypothesis_id'] for h in value['hypotheses']]
    ids = [s['step_id'] for s in value['steps']]
    if len(ids) != len(set(ids)) or len(hypothesis_ids) != len(set(hypothesis_ids)):
        raise ProtocolError('duplicate_identifier')
    states = step_states or {}
    old = {s['step_id']: s for s in previous['steps']} if previous else {}
    for index, step in enumerate(result['steps']):
        expected = states.get(step['step_id'], 'pending') if step['step_id'] in old else 'pending'
        if step['status'] != expected:
            raise ProtocolError('forged_step_state')
        if any(dep not in ids or dep == step['step_id'] for dep in step['depends_on']):
            raise ProtocolError('missing_dependency')
        try:
            step['parameters'] = validate_arguments(step['tool'], step['parameters'])
        except ValueError:
            raise ProtocolError('invalid_tool_parameters', f'$.steps[{index}].parameters') from None
        if step['tool'] == 'get_evidence' and step['parameters']['evidence_id'] not in (evidence or {}):
            raise ProtocolError('unread_evidence')
        if step['step_id'] in old and states.get(step['step_id'], 'pending') != 'pending':
            before = deepcopy(old[step['step_id']]); before['status'] = step['status']
            if step != before:
                raise ProtocolError('changed_executed_step')
    if any(states.get(key, 'pending') != 'pending' and key not in ids for key in old):
        raise ProtocolError('removed_executed_step')
    pending = {s['step_id']: set(s['depends_on']) for s in result['steps']}
    done = set()
    while pending:
        ready = {key for key, deps in pending.items() if deps <= done}
        if not ready:
            raise ProtocolError('cyclic_dependency')
        done.update(ready)
        pending = {key: deps for key, deps in pending.items() if key not in ready}
    return result


def normalize_report(value, *, assessment_refs_optional=False):
    """补齐无歧义默认字段并派生总引用；保留原对象及修改路径。"""
    result = deepcopy(value)
    paths = []
    if type(result) is dict and type(result.get('claims')) is list:
        for index, claim in enumerate(result['claims']):
            if type(claim) is dict and claim.get('kind') in ('observed', 'reported', 'inferred') and 'computed' not in claim:
                claim['computed'] = None
                paths.append(f'$.claims[{index}].computed')
            if type(claim) is dict and claim.get('kind') in ('computed', 'observed') and 'limitation' not in claim:
                claim['limitation'] = ''
                paths.append(f'$.claims[{index}].limitation')
        sources = list(result['claims'])
        if 'overdue_assessment' in result and not (assessment_refs_optional and
                type(result['overdue_assessment']) is dict and 'evidence_ids' not in result['overdue_assessment']):
            sources.append(result['overdue_assessment'])
        # 缺少或错误的逐条引用留给严格校验，不通过总清单补造依据。
        if all(type(source) is dict and type(source.get('evidence_ids')) is list
               and all(type(ref) is str for ref in source['evidence_ids']) for source in sources):
            refs = list(dict.fromkeys(ref for source in sources for ref in source['evidence_ids']))
            if 'evidence_ids' not in result or result['evidence_ids'] != refs:
                result['evidence_ids'] = refs
                paths.append('$.evidence_ids')
    return result, paths


def repair_instruction(error):
    """只输出受控路径和修复规则，不回传不可信字段值或证据文本。"""
    path = error.path if re.fullmatch(r'\$(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\])*', error.path) else '$'
    if error.code == 'missing_field':
        rule = ('该结论为computed类型，必须提供真实的evidence_id、metric和value，不能猜测指标。'
                if re.fullmatch(r'\$\.claims\[[0-9]+\]\.computed', path)
                else '补齐此必填字段，使用协议要求的类型；事实或证据未知时明确保留未解问题，不能编造。')
    else:
        rule = {'computed_mismatch': '指标必须等于所引用快照汇总的原值，不能改写程序数字。',
                'limitation_required': '填写该结论的实际证据限制；不得把缺失解释成零风险。',
                'evidence_scope': '只能引用当前已授权运行提供的证据，不得猜测编号。',
                'overdue_assessment_mismatch': '对应正文结论的evidence_ids必须足以核验人员计数、全局分母和判定，并与宿主覆盖摘要一致；不能靠仅列在判断中的引用补足正文依据。',
                'overdue_assessment_required': '补充overdue_assessment及对应observed结论，使用宿主覆盖摘要。',
                'overdue_assessment_claim': 'claim_index必须指向有充分正文引用支持集中判断的observed结论；overdue_assessment.evidence_ids可省略，核验通过后由程序从正文引用派生。不要把判断绑定到computed结论。',
                'invalid_json': '输出必须为完整JSON对象，避免重复内容、未闭合字符串或遗漏括号；保留必要数字、证据和限制，缩短表述。',
                'goal_evidence_incomplete': '用户目标所需证据尚未齐备，使用partial并具体列出未解目标。',
                'incomplete_report': 'complete须覆盖目标且无未解问题；仍有未解目标时使用partial。',
                'report_fact_required': '数字指标、人员负荷或共同窗口结论须从report_fact_options选择fact及引用；集中程度只用overdue_assessment。',
                'report_fact_mismatch': '从report_fact_options复制fact及引用。summary只能引用get_snapshot_summary；workload只能引用get_courier_workload及对应人员；window必须引用逾期明细，全部范围必须完整覆盖。',
                'unsupported_cause': 'GPS不可用不能证明人员未到场；原因只能写为有依据、待核实的推断。'
                }.get(error.code, '按当前协议修正此项，保留真实证据和未解问题。')
    return f'校验错误{error.code}，位置{path}。{rule}只修正相关问题，重新提交完整合法JSON；不执行工具。'


def validate_output(kind, value, *, scope, evidence=None, plan=None, step_states=None, render_limits=False):
    if kind not in SCHEMAS:
        raise ProtocolError('unknown_protocol')
    evidence = evidence or {}
    schema = SCHEMAS[kind]
    if kind == 'report':
        value, _ = normalize_report(value, assessment_refs_optional=render_limits)
        if render_limits and 'limitations' not in value:
            value['limitations'] = []
        if render_limits:
            schema = deepcopy(schema)
            schema['properties']['overdue_assessment']['required'].remove('evidence_ids')
    check(value, schema)
    if kind == 'intent':
        if value['action'] != 'investigate' and not value['clarification'].strip():
            raise ProtocolError('clarification_required')
    elif kind == 'plan':
        result = validate_plan(value, previous=plan, step_states=step_states, evidence=evidence)
        for step in result['steps']:
            if step['tool'] == 'get_evidence':
                evidence_record(step['parameters']['evidence_id'], evidence, scope)
        return result
    elif kind == 'observation':
        if not plan or value['plan_version'] != plan['version'] or value['step_id'] not in {s['step_id'] for s in plan['steps']}:
            raise ProtocolError('observation_plan')
        if (step_states or {}).get(value['step_id']) != 'completed':
            raise ProtocolError('observation_before_execution')
        refs = [r for fact in value['new_facts']+value['counterevidence'] for r in fact['evidence_ids']]
        seen = set()
        for assessment in value['hypothesis_assessments']:
            identifier = assessment['hypothesis_id']
            if identifier in seen or identifier not in {h['hypothesis_id'] for h in plan['hypotheses']}:
                raise ProtocolError('unknown_hypothesis')
            seen.add(identifier)
            if assessment['verdict'] != 'unknown' and not assessment['evidence_ids']:
                raise ProtocolError('assessment_without_evidence')
            refs.extend(assessment['evidence_ids'])
        for ref in refs:
            evidence_record(ref, evidence, scope)
    elif kind == 'report':
        claims = value['claims']
        if value['status'] == 'complete' and (not claims or value['unresolved_questions']):
            raise ProtocolError('incomplete_report')
        if value['status'] == 'partial' and not value['unresolved_questions']:
            raise ProtocolError('partial_reason_required')
        refs = {ref for claim in claims for ref in claim['evidence_ids']}
        refs.update(value.get('overdue_assessment', {}).get('evidence_ids', []))
        if refs != set(value['evidence_ids']):
            raise ProtocolError('report_evidence_list')
        for index, claim in enumerate(claims):
            claim_path = f'$.claims[{index}]'
            items = [evidence_record(ref, evidence, scope) for ref in claim['evidence_ids']]
            if claim['kind'] == 'reported' and any(e['source_type'] != 'manual_statement' for e in items):
                raise ProtocolError('reported_source_required')
            if claim['kind'] == 'observed' and any(e['source_type'] == 'manual_statement' for e in items):
                raise ProtocolError('observed_source_required')
            assessment = value.get('overdue_assessment', {})
            bound = claim['kind'] == 'computed' or (claim['kind'] == 'observed' and
                    ('fact' in claim or assessment.get('claim_index') == index))
            if render_limits and bound:
                # 仅渲染绑定事实；引用、数值与绑定仍由下方原严格校验验证。
                claim['limitation'] = bound_limitation(claim, evidence, scope,
                                                       assessment.get('claim_index') == index)
            limited = any(e.get('record', {}).get('truncated') for e in items)
            if (claim['kind'] in ('reported', 'inferred') or limited) and not claim['limitation'].strip():
                raise ProtocolError('limitation_required', claim_path+'.limitation')
            computed = claim['computed']
            if claim['kind'] != 'computed' and computed is not None:
                raise ProtocolError('unexpected_computed')
            if claim['kind'] == 'computed':
                if computed is None or computed['evidence_id'] not in claim['evidence_ids']:
                    raise ProtocolError('computed_reference')
                item = evidence_record(computed['evidence_id'], evidence, scope)
                if item['source_type'] != 'tool_result':
                    raise ProtocolError('computed_source_required')
                record = item['record']
                metric = (record.get('data') or {}).get('metrics', {}).get(computed['metric'])
                if record.get('tool') != 'get_snapshot_summary' or not metric or metric.get('value') != computed['value']:
                    raise ProtocolError('computed_mismatch', claim_path+'.computed')
                if computed['value'] is None and not claim['limitation'].strip():
                    raise ProtocolError('limitation_required')
                claim['statement'] = summary_statement(computed['metric'], computed['value'])
            if 'fact' in claim:
                if claim['kind'] == 'computed' and claim['fact'] == {
                    'type': 'summary', 'evidence_id': computed['evidence_id'], 'metric': computed['metric']}:
                    claim.pop('fact')  # 与computed完全相同的重复绑定可无歧义移除。
                    continue
                if claim['kind'] != 'observed' or claim['computed'] is not None:
                    raise ProtocolError('report_fact_mismatch', claim_path+'.fact')
                try:
                    claim['statement'] = fact_statement(claim['fact'], claim['evidence_ids'], evidence, scope)
                except ProtocolError as exc:
                    raise ProtocolError(exc.code, claim_path+'.fact') from None
        if 'overdue_assessment' in value:
            assessment = value['overdue_assessment']
            index = assessment['claim_index']
            if index >= len(claims) or claims[index]['kind'] != 'observed':
                raise ProtocolError('overdue_assessment_claim')
            # 新报告只用正文依据核验；模型额外给出的判断引用仍须通过授权检查。
            for ref in assessment.get('evidence_ids', []):
                evidence_record(ref, evidence, scope)
            proof_refs = claims[index]['evidence_ids'] if render_limits else assessment['evidence_ids']
            subset = {ref: evidence_record(ref, evidence, scope) for ref in proof_refs}
            coverage = overdue_coverage(subset, scope)
            candidate = next((c for c in coverage['couriers'] if c['courier_id'] == assessment['courier_id']), None)
            if not candidate or any(assessment[k] != candidate[k] for k in ('overdue_count', 'verdict')) or assessment['global_overdue'] != coverage['global_overdue']:
                raise ProtocolError('overdue_assessment_mismatch')
            if assessment['verdict'] == 'unknown' and not assessment['missing_evidence']:
                raise ProtocolError('overdue_missing_reason_required')
            if assessment['verdict'] != 'unknown' and assessment['missing_evidence']:
                raise ProtocolError('overdue_unnecessary_uncertainty')
            if not render_limits and set(claims[index]['evidence_ids']) != set(assessment['evidence_ids']):
                raise ProtocolError('overdue_assessment_claim')
            if render_limits:
                # 先核验充分性，再派生判断引用和最终总引用，不补造正文依据。
                assessment['evidence_ids'] = list(proof_refs)
                value, _ = normalize_report(value)
            # 这条数值关系由程序表述，避免结构化字段正确而正文反向解释。
            value = deepcopy(value)
            prefix = f"人员{assessment['courier_id']}已知逾期{assessment['overdue_count']}条"
            if assessment['verdict'] in ('over_half', 'not_over_half'):
                ratio = assessment['overdue_count'] / assessment['global_overdue'] * 100
                conclusion = '超过一半' if assessment['verdict'] == 'over_half' else '未超过一半'
                statement = f"{prefix}，全局逾期{assessment['global_overdue']}条，占比{ratio:.1f}%，{conclusion}。"
            elif assessment['verdict'] == 'no_overdue':
                statement = '同口径全局逾期为0条，无逾期集中程度可判断。'
            else:
                statement = prefix+'；无法确认是否超过全局一半：'+'；'.join(assessment['missing_evidence'])+'。'
            value['claims'][index]['statement'] = statement
        if render_limits:
            limits = source_limitations(evidence, scope)
            limits.extend(c['limitation'] for c in value['claims'] if c['limitation'])
            value['limitations'] = list(dict.fromkeys(limits))
            check(value, REPORT_SCHEMA)
    return deepcopy(value)


def source_limitations(evidence, scope):
    """源证据的真实边界始终保留；局部字段缺失不扩大为工具失效。"""
    limits = []
    for ref, item in evidence.items():
        item = evidence_record(ref, evidence, scope)
        record = item.get('record', {})
        limits.extend(record.get('limitations', []))
        if item['source_type'] != 'tool_result':
            continue
        data = record.get('data')
        if record.get('status') == 'error' or (record.get('status') == 'unavailable' and data is None):
            limits.append(str(record.get('tool'))+'未取得可用结果，不能据此认定零风险。')
        for field, quality in (data or {}).get('quality', {}).items():
            if quality.get('unavailable', 0):
                limits.append(field+'字段截至快照时点部分不可用；GPS缺失不能证明人员未到场。'
                              if 'gps' in field.lower() else field+'字段截至快照时点部分不可用。')
            if quality.get('not_applicable', 0):
                limits.append(field+'包含不适用记录，不能当作缺失或零风险。')
    return list(dict.fromkeys(limits))


def bound_limitation(claim, evidence, scope, assessment=False):
    subset = {ref: evidence_record(ref, evidence, scope) for ref in claim['evidence_ids']}
    limits = ['当前历史样本快照，不代表生产全量风险。']
    fact = claim.get('fact', {})
    if assessment or fact.get('type') == 'ownership':
        coverage = overdue_coverage(subset, scope)
        if coverage['ownership_coverage'] == 'complete':
            limits.append('当前快照同口径逾期已完整归属。')
        elif coverage['ownership_coverage'] == 'partial':
            missing = coverage['global_overdue']-coverage['covered_overdue']
            limits.append(f'尚缺{missing}条逾期归属；仅明细支持的人员计数为已知下界。')
        else:
            limits.extend(coverage['missing_evidence'])
    if fact.get('type') == 'window':
        limits.append(('仅描述已观察逾期任务子集。' if fact['coverage'] == 'observed_subset' else
                       '当前快照全部逾期明细已核对。')+'共同窗口不证明共同根因。')
    if fact.get('type') == 'workload':
        limits.append('指定人员样本负荷不代表真实容量。')
    for item in subset.values():
        record = item.get('record', {})
        if record.get('truncated'):
            limits.append('引用工具存在查询分页；人员名单展示范围与逾期归属覆盖分别核验。')
        if any(m.get('value') is None or m.get('status') == 'unavailable'
               for m in (record.get('data') or {}).get('metrics', {}).values()):
            limits.append('引用结果存在不可计算指标，不能当作零风险。')
    return '；'.join(dict.fromkeys(limits))


def summary_statement(metric, value):
    return f"当前样本内{METRIC_LABELS[metric]}{'不可计算' if value is None else str(value)+'条'}。"


def fact_statement(fact, refs, evidence, scope):
    """事实句仅从引用证据取值；不采纳模型提供的数字或范围解释。"""
    if fact['type'] == 'ownership':
        subset = {ref: evidence_record(ref, evidence, scope) for ref in refs}
        coverage = overdue_coverage(subset, scope)
        if coverage['ownership_coverage'] == 'unknown':
            raise ProtocolError('report_fact_mismatch')
        people = '、'.join(f"{c['courier_id']}已知{c['overdue_count']}条（{'完整计数' if c['count_is_exact'] else '下界'}）" for c in coverage['couriers'])
        return (f"同口径全局逾期{coverage['global_overdue']}条，已覆盖归属{coverage['covered_overdue']}条；"
                + ('归属完整覆盖。' if coverage['ownership_coverage'] == 'complete' else '归属仍部分覆盖，不能用已知下界否定未展示人员风险。')
                + ('已保存证据的人员计数：'+people+'；各人员占比分母均为同口径全局逾期。' if people else ''))
    if fact['type'] in ('summary', 'workload'):
        if fact['evidence_id'] not in refs:
            raise ProtocolError('report_fact_mismatch')
        item = evidence_record(fact['evidence_id'], evidence, scope)
        record = item['record']
        if item['source_type'] != 'tool_result' or record.get('status') not in ('ok', 'empty'):
            raise ProtocolError('report_fact_mismatch')
        if fact['type'] == 'summary':
            if record.get('tool') != 'get_snapshot_summary':
                raise ProtocolError('report_fact_mismatch')
            data = record.get('data') or {}
            metric = fact['metric']
            value = data.get(metric) if metric in ('visible_tasks', 'completed_tasks') else data.get('metrics', {}).get(metric, {}).get('value')
            if value is not None and (type(value) is not int or value < 0):
                raise ProtocolError('report_fact_mismatch')
            if metric not in data and metric not in data.get('metrics', {}):
                raise ProtocolError('report_fact_mismatch')
            return summary_statement(metric, value)
        if record.get('tool') != 'get_courier_workload':
            raise ProtocolError('report_fact_mismatch')
        rows = [row for row in record.get('rows', []) if row.get('courier_id') == fact['courier_id']]
        if not rows or any(row != rows[0] for row in rows):
            raise ProtocolError('report_fact_mismatch')
        row = rows[0]
        values = {k: row.get(k) for k in ('visible_tasks', 'completed_tasks')}
        values.update({k: row.get('metrics', {}).get(k, {}).get('value') for k in METRIC_NAMES})
        if any(type(v) is not int or v < 0 for v in values.values()):
            raise ProtocolError('report_fact_mismatch')
        return '人员'+fact['courier_id']+'样本内'+ '、'.join(METRIC_LABELS[k]+str(v)+'条' for k, v in values.items())+'；样本负荷不代表真实容量。'
    subset = {ref: evidence_record(ref, evidence, scope) for ref in refs}
    tasks = {}
    for item in subset.values():
        record = item['record']
        if item['source_type'] != 'tool_result' or record.get('tool') != 'get_pending_tasks' or record.get('status') not in ('ok', 'empty'):
            continue
        for row in record.get('rows', []):
            if row.get('risk_state') != 'overdue' or not row.get('evidence_id'):
                continue
            previous = tasks.get(row['evidence_id'])
            if previous and any(previous.get(k) != row.get(k) for k in ('courier_id', 'time_window_start_reference', 'time_window_end_reference')):
                raise ProtocolError('report_fact_mismatch')
            tasks[row['evidence_id']] = row
    selected = set(fact['courier_ids'])
    windows = {}
    for row in tasks.values():
        key = (row.get('time_window_start_reference'), row.get('time_window_end_reference'))
        if all(key):
            windows.setdefault(key, set()).add(row.get('courier_id'))
    common = [key for key, couriers in windows.items() if selected <= couriers]
    if not common:
        raise ProtocolError('report_fact_mismatch')
    key = sorted(common)[0]
    if fact['coverage'] == 'all_overdue':
        coverage = overdue_coverage(subset, scope)
        if coverage['ownership_coverage'] != 'complete' or len(tasks) != coverage['global_overdue'] or any(
            row.get('courier_id') not in selected or (row.get('time_window_start_reference'), row.get('time_window_end_reference')) != key
            for row in tasks.values()):
            raise ProtocolError('report_fact_mismatch')
    label = '全部逾期任务' if fact['coverage'] == 'all_overdue' else '已观察逾期任务子集'
    return f"{label}中人员{'、'.join(sorted(selected))}存在相同窗口{key[0]}至{key[1]}；相同窗口不证明共同根因。"


def validate_report_facts(value):
    """仅新生成报告启用；旧报告读取兼容，其他语义仍须逐条审核。"""
    assessment_index = value.get('overdue_assessment', {}).get('claim_index')
    for index, claim in enumerate(value['claims']):
        statement = claim['statement']
        if claim['kind'] in ('observed', 'inferred') and index != assessment_index and 'fact' not in claim and (
            any(word in statement for word in ('共同窗口', '相同窗口', '共同时间窗口', '样本负荷', '人员负荷')) or
            (re.search(r'\d', statement) and any(word in statement for word in ('积压', '逾期', '临期', '可见任务', '接单', '完成', '占比', '分母')))):
            raise ProtocolError('report_fact_required', f'$.claims[{index}].fact')
        if not any(word in statement for word in ('不能证明', '无法证明', '不足以证明', '不证明')) and re.search(r'(GPS|定位).*(证明|因此|导致|说明).*(未到|没有到)', statement, re.I):
            raise ProtocolError('unsupported_cause', f'$.claims[{index}]')
        if claim['kind'] == 'inferred':
            claim['statement'] = '待核实推断：'+statement.removeprefix('待核实推断：')


def report_fact_options(evidence, scope):
    """从已有证据给出可复制的绑定选项；不增加取数，不提供独立评分答案。"""
    options, windows = [], {}
    focus = set(overdue_coverage(evidence, scope)['focus_courier_ids'])
    for ref, item in evidence.items():
        record = item.get('record', {})
        if item.get('source_type') != 'tool_result' or record.get('status') not in ('ok', 'empty'):
            continue
        if record.get('tool') == 'get_snapshot_summary':
            data = record.get('data') or {}
            for metric in (*METRIC_NAMES, 'visible_tasks', 'completed_tasks'):
                if metric in data or metric in data.get('metrics', {}):
                    options.append({'fact': {'type': 'summary', 'evidence_id': ref, 'metric': metric}, 'evidence_ids': [ref]})
        elif record.get('tool') == 'get_courier_workload':
            rows = record.get('rows', [])
            for row in (row for row in rows if row.get('courier_id') in focus) if focus else rows[:3]:
                options.append({'fact': {'type': 'workload', 'evidence_id': ref, 'courier_id': row['courier_id']}, 'evidence_ids': [ref]})
        elif record.get('tool') == 'get_pending_tasks':
            for row in record.get('rows', []):
                key = (row.get('time_window_start_reference'), row.get('time_window_end_reference'))
                if row.get('risk_state') == 'overdue' and row.get('courier_id') and all(key):
                    entry = windows.setdefault(key, {'couriers': set(), 'refs': set()})
                    entry['couriers'].add(row['courier_id']); entry['refs'].add(ref)
    for entry in windows.values():
        if len(entry['couriers']) >= 2:
            for coverage in ('observed_subset', 'all_overdue'):
                options.append({'fact': {'type': 'window', 'courier_ids': sorted(entry['couriers']), 'coverage': coverage},
                                'evidence_ids': sorted(entry['refs'])})
    valid = []
    coverage = overdue_coverage(evidence, scope)
    if coverage['ownership_coverage'] != 'unknown':
        options.append({'fact': {'type': 'ownership'}, 'evidence_ids': coverage['evidence_ids']})
    for option in options:
        try:
            fact_statement(option['fact'], option['evidence_ids'], evidence, scope)
        except ProtocolError:
            continue
        valid.append(option)
    return valid


def next_step(plan, step_states):
    """宿主在执行前调用；输出本身不执行工具，状态不能取自模型。"""
    for step in plan['steps']:
        if step_states.get(step['step_id'], 'pending') == 'pending' and all(step_states.get(dep) == 'completed' for dep in step['depends_on']):
            return deepcopy(step)
    return None
