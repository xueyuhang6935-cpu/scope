"""Bounded tool investigation; domain numbers and approval decisions never come from the model."""
import json
import time
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .supply_model import configured
from project.prompts.loader import INVESTIGATOR_PROMPT, INVESTIGATOR_PROMPT_VERSION
from project.agents.report_protocol import REPORT_SCHEMA, validate_report
from .investigation_sql import TABLES, validate_sql


def investigate(bundle, evidence, env, model_allowed=True, query_tools=None, harness=None, evidence_reader=None):
    target = bundle['target']
    facts = []
    if target and target['computable']:
        first = datetime.fromisoformat(target['firstShortageAt']).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M（北京时间）') if target['firstShortageAt'] else '无'
        facts.append({'statement': f"{target['factory']}厂 {target['material']} 首次缺料 {first}，最大缺口 {format(Decimal(target['shortage']).normalize(), 'f')} {target['unit']}。",
                      'evidenceIds': target['evidenceIds']})
    hypotheses = [{'statement': str(e['record'].get('feedback', '')), 'evidenceIds': [e['id']],
                   'qualification': '供应商反馈，仅确认其如此报告，原因未经独立验证。'}
                  for e in evidence if e['table'] == 'supplier_commitments' and e['record'].get('feedback')]
    report = {'mode': 'rules', 'facts': facts, 'hypotheses': hypotheses, 'missing': bundle['missing'],
              'trace': [], 'tokens': 0, 'modelCalls': 0, 'stopReason': '确定性工具调查完成', 'modelSummary': None,
              'evidence': list(evidence), 'planRevisions': [], 'question': bundle.get('question', ''),
              'status': 'complete', 'promptVersion': INVESTIGATOR_PROMPT_VERSION}
    tools = {
        'get_inventory': [e for e in evidence if e['table'] in ('inventory_snapshots', 'inventory_reservations', 'quality_holds')],
        'get_production_plan': [e for e in evidence if e['table'] in ('production_plan_lines', 'bom_headers', 'bom_lines')],
        'get_supply_evidence': [e for e in evidence if e['table'] in ('supplier_commitments', 'purchase_order_lines', 'shipment_lines', 'receipt_lines', 'quality_releases', 'line_deliveries', 'transfer_routes', 'expedite_options', 'approval_policies')],
        'get_candidate_plans': [{'id': f'candidate:{i}', 'table': 'candidate_plan', 'record': p,
                                 'provenance': {'sourceType': 'deterministic_calculation'}}
                                for i, p in enumerate(bundle['candidates'])],
    }

    def stop(reason, access_invalid=False):
        # Keep completed evidence only while the original live boundary still holds.
        if harness and not access_invalid:
            try:
                harness.check(report['evidence'], bundle['candidates'])
            except Exception:
                access_invalid = True
        report.update(status='partial', stopReason=reason, manualRequired=True)
        if access_invalid:
            report.update(accessInvalid=True, facts=[], hypotheses=[], evidence=[], modelSummary=None,
                          planRevisions=[], trace=[], question='', missing=[])
        else:
            report['modelSummary'] = {'status': 'partial', 'claims': [], 'evidenceIds': [],
                'summary': '调查未完成；已取得的工具证据和确定性结果保留，未生成新的根因结论。',
                'unresolvedQuestions': [reason], 'nextSteps': ['核对已取得的证据并补充未完成调查。'],
                'qualification': '后端整理的部分结果，不代表模型已完成调查。'}
        return report
    model_env = {**env, 'SUPPLY_PLANNER': 'model'}
    if harness:
        try:
            harness.check(report['evidence'], bundle['candidates'])
        except Exception:
            return stop('调查授权或范围已变化，转人工。', access_invalid=True)
    if env.get('SUPPLY_INVESTIGATOR') != 'model':
        report['trace'] = [{'tool': name, 'status': 'completed', 'items': len(value)} for name, value in tools.items()]
        return report
    report.update(mode='model', tokens=None)
    if not model_allowed or not configured(model_env):
        return stop('模型配置不可用或上次模型调用结果未知，转人工，不自动重复计费。')
    def budget(key, default, minimum, maximum):
        try:
            return max(minimum, min(maximum, int(env.get(key, default))))
        except (ValueError, TypeError):
            return default
    rounds = budget('SUPPLY_AGENT_MAX_ROUNDS', 12 if query_tools else 4, 2, 24)
    seconds = budget('SUPPLY_AGENT_MAX_SECONDS', 120 if query_tools else 60, 15, 300)
    token_limit = budget('SUPPLY_AGENT_MAX_TOKENS', 24000 if query_tools else 12000, 1000, 60000)
    messages = [{'role': 'system', 'content': INVESTIGATOR_PROMPT.format(rounds=rounds)},
                {'role': 'user', 'content': json.dumps({'factory': target['factory'] if target else None,
                    'material': target['material'] if target else None, 'task': '调查缺料风险并比较处置方案',
                    'question': bundle.get('question', ''), 'replanning': bundle.get('replanning'),
                    'evidenceIndex': bundle.get('evidenceIndex', [])}, ensure_ascii=False)}]
    schemas = [{'type': 'function', 'function': {'name': name, 'description': '读取已授权场景快照中的'+name,
                'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}} for name in tools]
    if query_tools:
        # Raw evidence must pass the same live permission boundary as SQL. Keep only
        # deterministic candidate retrieval; data exploration now executes queries.
        schemas = [s for s in schemas if s['function']['name'] == 'get_candidate_plans']
        schemas.extend([
            {'type': 'function', 'function': {'name': 'get_database_schema', 'description': '读取授权表目录及业务口径；指定tables获取最多6张表的字段与关联，编写SQL前先调用。',
                'parameters': {'type': 'object', 'properties': {'tables': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 6}}, 'additionalProperties': False}}},
            {'type': 'function', 'function': {'name': 'execute_sql', 'description': '在授权视图上执行单条只读PostgreSQL查询。返回证据ID、结果、截断标记或可修正错误。',
                'parameters': {'type': 'object', 'properties': {'sql': {'type': 'string', 'maxLength': 12000},
                    'parameters': {'type': 'object', 'description': '与SQL中 %(name)s 对应的具名标量参数；无参数时传空对象。'}},
                    'required': ['sql', 'parameters'], 'additionalProperties': False}}},
        ])
    schemas.append({'type': 'function', 'function': {'name': 'update_plan', 'description': '根据新证据、错误或审批反馈更新调查步骤；不执行业务动作。',
        'parameters': {'type': 'object', 'properties': {'reason': {'type': 'string', 'maxLength': 1000},
            'steps': {'type': 'array', 'items': {'type': 'string', 'maxLength': 500}, 'minItems': 1, 'maxItems': 5}},
            'required': ['reason', 'steps'], 'additionalProperties': False}}})
    if evidence_reader:
        schemas.append({'type': 'function', 'function': {'name': 'get_evidence',
            'description': '按证据ID展开当前或关联历史调查证据，重新核验权限和快照；不可读取任意任务。',
            'parameters': {'type': 'object', 'properties': {'evidenceId': {'type': 'string', 'maxLength': 200}},
                           'required': ['evidenceId'], 'additionalProperties': False}}})
    schemas.append({'type': 'function', 'function': {'name': 'submit_report', 'description': '逐条提交结论与证据；未查清时提交 partial 并列明待查事项。',
        'parameters': REPORT_SCHEMA}})
    submitted = {}
    schema_tables_seen = set()
    plan_created = closing = False
    started, total_tokens, unknown_usage = time.monotonic(), 0, False
    ticket = None
    repeated = {}

    def feedback(call, result, audit):
        nonlocal ticket
        audit['promptVersion'] = INVESTIGATOR_PROMPT_VERSION
        if harness:
            audit['scope'] = harness.check(report['evidence'], bundle['candidates'])
            harness.finish(ticket, result, audit)
            ticket = None
        report['trace'].append(audit)
        messages.extend([{'role': 'assistant', 'content': None, 'tool_calls': [call]},
                         {'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result, ensure_ascii=False)}])

    def repair(call, message):
        error = {'code': 'invalid_tool_input', 'message': message, 'retryable': True}
        feedback(call, {'error': error}, {'tool': call['function']['name'], 'status': 'error', 'error': error})

    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            for turn in range(rounds):
                elapsed = time.monotonic() - started
                context_size = len(json.dumps(messages, ensure_ascii=False))
                if elapsed >= seconds or total_tokens >= token_limit or context_size > 96000:
                    return stop('达到调查时间、Token或上下文预算，保留已取得证据，请人工核查。')
                if not closing and (turn == rounds - 1 or elapsed >= seconds - min(20, seconds / 3)
                                    or total_tokens >= token_limit * .75 or context_size > 80000):
                    closing = True
                    messages.append({'role': 'system', 'content': '调查进入收尾。仅调用 submit_report，依据已有证据提交；未查清或无证据必须提交 partial 并列明未完成事项，不得继续扩展调查。'})
                available = [s for s in schemas if not closing or s['function']['name'] == 'submit_report']
                if not plan_created and not closing:
                    available = [s for s in schemas if s['function']['name'] == 'update_plan']
                body = {'model': env['SUPPLY_MODEL'], 'messages': messages, 'tools': available, 'tool_choice': 'required',
                        'max_tokens': min(1600, max(1, token_limit - total_tokens)), 'stream': False}
                if harness:
                    harness.check(report['evidence'], bundle['candidates'])
                report['modelCalls'] += 1
                with client.stream('POST', env.get('SUPPLY_MODEL_BASE_URL', 'https://api.deepseek.com').rstrip('/') + '/chat/completions',
                    json=body, timeout=min(15, max(1, seconds - (time.monotonic() - started))), headers={'Authorization': 'Bearer ' + (env.get('SUPPLY_MODEL_API_KEY') or env['DEEPSEEK_API_KEY'])}) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 100000 or time.monotonic() - started > seconds:
                            raise ValueError('response budget')
                payload = json.loads(raw)
                usage = payload.get('usage', {}).get('total_tokens')
                if type(usage) is int and usage >= 0:
                    total_tokens += usage
                else:
                    unknown_usage = True
                report['tokens'] = None if unknown_usage else total_tokens
                message = payload['choices'][0]['message']
                calls = message.get('tool_calls', [])
                if len(calls) != 1:
                    raise ValueError('one tool per round')
                call = calls[0]
                name = call['function']['name']
                try:
                    args = json.loads(call['function']['arguments'])
                except (ValueError, TypeError):
                    args = None
                if harness:
                    ticket = harness.start(name, args)
                    scope = harness.check(report['evidence'], bundle['candidates'])
                    harness.set_scope(ticket, scope)
                signature = json.dumps([name, args], sort_keys=True, ensure_ascii=False)
                repeated[signature] = repeated.get(signature, 0) + 1
                if repeated[signature] >= 3:
                    feedback(call, {'error': {'message': '相同调用重复三次，停止自动调查。', 'retryable': False}},
                             {'tool': name, 'status': 'error'})
                    return stop('相同工具与参数重复三次，停止空转，请人工核查。')
                if not isinstance(args, dict):
                    repair(call, '工具参数必须是有效 JSON 对象，请修正参数并继续。')
                    continue
                if name not in {s['function']['name'] for s in available}:
                    repair(call, '当前仅允许提交报告。' if closing else '请先创建调查计划，再使用当前可用工具。')
                    continue
                if name == 'update_plan':
                    steps, reason = args.get('steps'), args.get('reason')
                    if (set(args) != {'steps', 'reason'} or not isinstance(reason, str) or not 1 <= len(reason) <= 1000
                            or not isinstance(steps, list) or not 1 <= len(steps) <= 5
                            or not all(isinstance(step, str) and 1 <= len(step) <= 500 for step in steps)):
                        repair(call, '计划需要1至5个步骤（每步1至500字）及1至1000字的调整原因。')
                        continue
                    revision = {'revision': len(report['planRevisions']) + 1, 'reason': reason, 'steps': steps}
                    feedback(call, revision, {'tool': name, 'status': 'completed', **revision})
                    report['planRevisions'].append(revision)
                    plan_created = True
                    continue
                if name == 'submit_report':
                    try:
                        summary = validate_report(args, submitted)
                    except ValueError as exc:
                        repair(call, str(exc))
                        continue
                    feedback(call, summary, {'tool': name, 'status': 'completed'})
                    report.update(modelSummary=summary, status=summary['status'],
                                  stopReason='模型完成证据引用说明' if summary['status'] == 'complete' else '调查未完成，已保存发现及待查事项。')
                    if not summary['claims']:
                        report['manualRequired'] = True
                    return report
                if name == 'get_evidence':
                    if set(args) != {'evidenceId'} or not isinstance(args['evidenceId'], str) or not 1 <= len(args['evidenceId']) <= 200:
                        repair(call, '需要1至200字符的 evidenceId。')
                        continue
                    try:
                        result = evidence_reader(args['evidenceId'])
                    except ValueError as exc:
                        repair(call, str(exc))
                        continue
                    if harness:
                        harness.check([result], [])
                    feedback(call, result, {'tool': name, 'status': 'completed', 'evidenceId': result['id']})
                    submitted[result['id']] = result
                    if not any(e['id'] == result['id'] for e in report['evidence']):
                        report['evidence'].append(result)
                    continue
                if query_tools and name in ('get_database_schema', 'execute_sql'):
                    if name == 'execute_sql':
                        try:
                            refs = set()
                            validate_sql(args.get('sql'), args.get('parameters'), TABLES, referenced_tables=refs)
                            if refs - schema_tables_seen:
                                raise ValueError('先读取相关表字段：' + '、'.join(sorted(refs - schema_tables_seen)))
                            if not refs:
                                raise ValueError('调查SQL必须读取至少一张授权业务表；常量查询不能作为业务证据。')
                        except ValueError as exc:
                            repair(call, str(exc))
                            continue
                    result, audit = query_tools.call(name, args)
                    if harness:
                        harness.check(report['evidence'] + ([result] if result.get('id') else []), bundle['candidates'])
                    feedback(call, result, audit)
                    if name == 'get_database_schema' and not result.get('error'):
                        schema_tables_seen.update(t for t, detail in result.get('tables', {}).items() if detail.get('columns'))
                    if result.get('id'):
                        submitted[result['id']] = result
                        report['evidence'].append(result)
                    if result.get('error', {}).get('retryable') is False:
                        return stop('调查权限或配置已变化，转人工。', access_invalid=True)
                    continue
                if query_tools and name != 'get_candidate_plans':
                    repair(call, '工具未注册，请按可用工具列表更新计划。')
                    continue
                if name not in tools or args != {}:
                    repair(call, '工具不存在或参数无效；快照工具参数必须为空对象。')
                    continue
                value = tools[name]
                # Bounded excerpts; IDs are usable only after their tool is called.
                shown = [dict(row) for row in value[:20]]
                for row in shown:
                    if row.get('table') == 'supplier_commitments':
                        row['provenance'] = {'sourceType': 'supplier_statement', 'verified': False}
                while shown and len(json.dumps(shown, ensure_ascii=False)) > 8000:
                    shown = shown[:-1]
                for row in shown:
                    if 'id' in row:
                        row['provenance'] = {**row.get('provenance', {}), 'truncated': len(value) > len(shown)}
                        submitted[row['id']] = row
                        existing = next((i for i, e in enumerate(report['evidence']) if e['id'] == row['id']), None)
                        if existing is None:
                            report['evidence'].append(row)
                        else:
                            report['evidence'][existing] = row
                audit = {'tool': name, 'status': 'completed', 'items': len(shown), 'truncated': len(value) > len(shown)}
                feedback(call, {'items': shown, 'truncated': audit['truncated']}, audit)
        return stop(f'达到{rounds}轮工具/模型预算，保留已取得证据，请人工核查。')
    except Exception:
        if harness and ticket:
            error = {'code': 'tool_stopped', 'message': '调用未交付，授权、范围或工具校验失败。', 'retryable': False}
            audit = {'tool': name, 'status': 'error', 'error': error}
            audit['promptVersion'] = INVESTIGATOR_PROMPT_VERSION
            harness.finish(ticket, {'error': error}, audit)
            report['trace'].append(audit)
        return stop('模型调用失败、超时或证据引用无效，转人工；未自动重试。')
