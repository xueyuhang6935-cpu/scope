"""T10宿主适配：复用模型HTTP传输、实时授权和run持久化调用预算。"""
import json
from hashlib import sha256
import time
from urllib.parse import urlsplit

from project.agents.logistics_model import ModelBudget, generate, MODES, build_messages, model_body, estimate_input_tokens, REPORT_OUTPUT_TOKENS, REPORT_MIN_OUTPUT_TOKENS
from project.agents.logistics_protocol import ProtocolError, validate_output
from project.prompts.loader import load_prompt
from project.agents.logistics_coverage import overdue_coverage
from .logistics_metrics import METRIC_VERSION
from .common import APIError, iso
from .investigation_model import model_session


def configured(env):
    url = urlsplit(env.get('SUPPLY_MODEL_BASE_URL', 'https://api.deepseek.com'))
    return bool(env.get('SUPPLY_MODEL') and (env.get('SUPPLY_MODEL_API_KEY') or env.get('DEEPSEEK_API_KEY'))
                and url.scheme == 'https' and url.hostname and not url.username and not url.password
                and not url.query and not url.fragment)


def output_mode(env):
    # 仅对已完成真实报告协议探测的供应商/模型启用默认工具调用；显式配置仍优先。
    verified = (urlsplit(env.get('SUPPLY_MODEL_BASE_URL', 'https://api.deepseek.com')).hostname == 'api.deepseek.com'
                and env.get('SUPPLY_MODEL') == 'deepseek-chat')
    return env.get('LOGISTICS_MODEL_OUTPUT_MODE', 'tool_call' if verified else 'json_text')


def compact_evidence(evidence, context_refs=None, scope=None):
    evidence_context = {}
    coverage = overdue_coverage(evidence, scope) if scope else None
    focus = {row['courier_id'] for row in (coverage or {}).get('couriers', [])[:1]}
    for ref, item in evidence.items():
        if context_refs is not None and ref not in context_refs:
            continue
        # 任务明细已在工具结果中；保留完整授权校验，不重复发送每条快照任务。
        if item['source_type'] == 'snapshot_task':
            continue
        record = item['record']
        # ponytail: T11仅发送最多3行的带截断证据视图；完整内容留存、验证和按需读取。完整ContextBuilder留T14。
        view = {key: record[key] for key in ('tool', 'status', 'data', 'rows', 'denominator', 'page', 'page_size', 'total_rows', 'returned_rows', 'truncated', 'limitations',
                                           'order_id', 'risk_state', 'current_wait_minutes', 'current_overdue_minutes') if key in record}
        view = json.loads(json.dumps(view))
        view['limitations'] = list(dict.fromkeys(view.get('limitations', [])))
        def metrics(values):
            return {name: {key: metric[key] for key in ('value', 'status', 'denominator', 'share', 'known_lower_bound') if key in metric}
                    for name, metric in values.items()}
        if (view.get('data') or {}).get('metrics'):
            view['data']['metrics'] = metrics(view['data']['metrics'])
        if view.get('rows'):
            view['rows'] = [{key: row[key] for key in ('order_id', 'courier_id', 'aoi_id', 'risk_state', 'evidence_id',
                'current_wait_minutes', 'current_overdue_minutes', 'remaining_minutes', 'metrics', 'missing_window_count',
                'time_window_start_reference', 'time_window_end_reference',
                'visible_tasks', 'completed_tasks', 'status', 'source_type', 'record') if key in row} for row in view['rows']]
            for row in view['rows']:
                if row.get('metrics'): row['metrics'] = metrics(row['metrics'])
        if len(view.get('rows', [])) > 3:
            if record.get('tool') == 'get_courier_workload':
                view['rows'] = sorted(view['rows'], key=lambda row: row.get('courier_id') not in focus)
            view['rows'] = view['rows'][:3]
            view['context_truncated'] = True
            view['limitations'] = view.get('limitations', [])+['模型上下文仅展示前3行，完整工具结果已保存；不得编造未展示人员的具体任务和负荷，已有明细与全局汇总支持的结论可以判断。']
        view['query_truncated'] = bool(record.get('truncated'))
        view.setdefault('context_truncated', False)
        evidence_context[ref] = {'source_type': item['source_type'], 'record': view}
    return evidence_context


class LogisticsModelService:
    """仅显式调用generate；不挂网络API，不改变T09确定性执行器。"""
    def __init__(self, tools, env):
        self.tools, self.runs, self.data, self.env = tools, tools.runs, tools.data, env

    def context(self, user, run_id, evidence_ids=()):
        if not isinstance(evidence_ids, (list, tuple)) or len(evidence_ids) > 100 or any(type(v) is not str or not 0 < len(v) <= 200 for v in evidence_ids):
            raise APIError(400, '证据列表无效。')
        with self.data.lock:
            run = self.runs.saved(run_id)
            meta = self.runs.authorize(user, run, owner=True)
            self.runs.access.require(user, 'investigate')
            if not set(meta['region_ids']).issubset(self.runs.access.allowed_regions(user, 'investigate')):
                raise APIError(403, '模型协议缺少调查范围权限。')
            if run['status'] in ('failed', 'cancelled'):
                raise APIError(409, '失败或取消的运行不能调用模型。')
            scope = {key: meta[key] for key in ('snapshot_id', 'business_as_of', 'city', 'region_ids', 'metric_version')}
            scope.update(run_id=run_id, workspace_id=run['workspace_id'], fingerprint=meta['snapshot_fingerprint'])
            scope['metric_version'] = scope['metric_version'] or METRIC_VERSION
            records = {ref: self.tools.evidence_store.read(user, run_id, ref) for ref in evidence_ids}
            return scope, records

    def validate(self, kind, user, run_id, value, *, evidence_ids=(), plan=None, step_states=None):
        scope, evidence = self.context(user, run_id, evidence_ids)
        if kind not in ('intent', 'plan', 'observation', 'report'):
            raise ProtocolError('unknown_protocol')
        return validate_output(kind, value, scope=scope, evidence=evidence, plan=plan, step_states=step_states)

    def report_reservation(self, user, run_id, question, evidence_ids, step_states, followup=None):
        scope, evidence = self.context(user, run_id, evidence_ids)
        mode = output_mode(self.env)
        messages, *_ = build_messages('report', question, scope=scope, evidence=evidence,
            plan={}, step_states=step_states, mode=mode, evidence_context=compact_evidence(evidence, scope=scope))
        estimated_input = estimate_input_tokens(model_body('report', messages, mode, REPORT_OUTPUT_TOKENS))
        if followup:
            # 只估算新增视图的空间，不创建假证据、不查库。最多3行窗口或1行负荷，标识按REF上限估算。
            rows = min(followup['parameters'].get('page_size', 1), 3)
            allowance = {'tool': followup['tool'], 'reference': 'x'*200,
                         'rows': [{'evidence_id': 'x'*200, 'courier_id': 'x'*200,
                                   'window_start': '2000-07-02 10:00:00', 'window_end': '2000-07-02 12:00:00',
                                   'metrics': dict.fromkeys(('visible', 'completed', 'backlog', 'overdue',
                                                            'due_soon', 'accepted', 'picked_up'), 999999999999)}]*rows}
            estimated_input += estimate_input_tokens(allowance)+512
        return {'estimated_input_tokens': estimated_input, 'output_tokens': REPORT_MIN_OUTPUT_TOKENS,
                'tokens': estimated_input+REPORT_MIN_OUTPUT_TOKENS+512, 'calls': 1}

    def generate(self, kind, user, run_id, question, *, evidence_ids=(), plan=None, step_states=None, claim_token=None, observations=None, compact=False, context_refs=None, protected_tokens=0, goal_question=None):
        with self.data.lock:
            run = self.runs.saved(run_id)
            self.runs.authorize(user, run, owner=True)
            if run['mode'] == 'model' and run['status'] not in ('queued', 'running'):
                raise ProtocolError('investigation_stopped')
        if not configured(self.env):
            raise ProtocolError('model_not_configured')
        mode = output_mode(self.env)
        if mode not in MODES:
            raise ProtocolError('invalid_adapter_mode')
        scope, evidence = self.context(user, run_id, evidence_ids)
        if kind not in ('intent', 'plan', 'observation', 'report'):
            raise ProtocolError('unknown_protocol')
        prompt, prompt_version = load_prompt('logistics_'+kind)
        persisted = self.runs.budgets.check(run_id, 'model_calls')
        remaining = persisted['max_active_seconds']-persisted['active_seconds']
        if remaining <= 0:
            raise ProtocolError('model_budget_exhausted')
        budget = ModelBudget(max_calls=persisted['max_model_calls'], calls=persisted['model_calls'],
                             max_tokens=persisted['max_model_tokens'], tokens=persisted.get('model_tokens', 0),
                             usage_unknown=persisted.get('model_usage_unknown', False), max_seconds=remaining)
        if budget.calls >= budget.max_calls or budget.tokens >= budget.max_tokens:
            raise ProtocolError('model_budget_exhausted')
        call_started = None
        evidence_context = None
        plan_context = None
        if compact:
            evidence_context = compact_evidence(evidence, context_refs, scope)
            observations = [] if kind == 'observation' else [o['value'] for o in (observations or [])[-1:]]
            if plan and kind == 'observation':
                selected = next((s for s in plan['steps'] if s['step_id'] == question.split('步骤 ')[-1]), None)
                plan_context = {'version': plan['version'], 'hypotheses': plan['hypotheses'],
                                'steps': [selected] if selected else plan['steps'][-1:]}
            elif kind == 'report':
                plan_context = {}; observations = []

        usage_detail = {}
        request_attempt = 0

        def reserve():
            nonlocal call_started
            with self.data.lock:
                self.context(user, run_id, evidence_ids)
                row = self.runs.saved(run_id)
                if claim_token and not self.runs.owned(run_id, claim_token):
                    raise APIError(409, '调查领取凭证已失效。')
                self.runs.budgets.reserve(run_id, 'model_calls')
                with self.runs.transaction():
                    current = json.loads(self.runs.saved(run_id)['budget_json'])
                    current.setdefault('model_prompt_versions', {})[kind] = {'version': prompt_version, 'sha256': sha256(prompt.encode()).hexdigest()}
                    self.data.db.execute('UPDATE logistics_runs SET budget_json=?,updated_at=? WHERE run_id=?', (json.dumps(current), iso(), run_id))
                call_started = time.monotonic()

        def record_usage(tokens):
            self.runs.budgets.usage(run_id, tokens, max(0, time.monotonic()-call_started), standalone=not claim_token)
            with self.runs.transaction():
                current = json.loads(self.runs.saved(run_id)['budget_json'])
                current.setdefault('model_usage_records', []).append({'kind': kind, 'total_tokens': tokens, **usage_detail})
                self.data.db.execute('UPDATE logistics_runs SET budget_json=? WHERE run_id=?', (json.dumps(current), run_id))
            self.context(user, run_id, evidence_ids)
            if claim_token and not self.runs.owned(run_id, claim_token):
                raise APIError(409, '调查领取凭证已失效。')

        def record_failure(diagnostic):
            usage_detail['transport_failure'] = dict(diagnostic)

        with model_session(self.env, on_failure=record_failure) as request:
            def adapted(body, started, seconds):
                nonlocal request_attempt
                usage_detail.clear()
                usage_detail['request_attempt'] = request_attempt
                usage_detail['protected_report_tokens'] = protected_tokens
                before = self.runs.budgets.read(run_id)
                usage_detail['known_tokens_before'] = before['model_tokens']
                usage_detail['usage_unknown_before'] = before['model_usage_unknown']
                request_attempt += 1
                usage_detail['request_bytes'] = len(json.dumps(body, ensure_ascii=False).encode())
                usage_detail['estimated_input_tokens'] = estimate_input_tokens(body)
                usage_detail['max_output_tokens'] = body['max_tokens']
                usage_detail['output_mode'] = mode
                if urlsplit(self.env.get('SUPPLY_MODEL_BASE_URL', 'https://api.deepseek.com')).hostname == 'api.deepseek.com':
                    body = {**body, 'thinking': {'type': 'disabled'}}
                response = request(body, started, seconds)
                receipt = response.get('usage', {}) if isinstance(response, dict) else {}
                if not isinstance(receipt, dict): receipt = {}
                for key in ('prompt_tokens', 'completion_tokens'):
                    value = receipt.get(key)
                    usage_detail[key] = value if type(value) is int and value >= 0 else None
                return response
            try:
                result = generate(kind, question, scope=scope, evidence=evidence, plan=plan, step_states=step_states,
                                  request=adapted, budget=budget, mode=mode, reserve=reserve, record_usage=record_usage, observations=observations,
                                  evidence_context=evidence_context, plan_context=plan_context, protected_tokens=protected_tokens,
                                  goal_question=goal_question)
            except ProtocolError as exc:
                if exc.code == 'model_transport_failed':
                    with self.runs.transaction():
                        current = json.loads(self.runs.saved(run_id)['budget_json']); current['model_transport_failed'] = True
                        self.data.db.execute('UPDATE logistics_runs SET budget_json=? WHERE run_id=?', (json.dumps(current), run_id))
                raise
        if claim_token and not self.runs.owned(run_id, claim_token):
            raise APIError(409, '调查领取凭证已失效。')
        self.context(user, run_id, evidence_ids)
        self.runs.budgets.check(run_id, delivery=True)
        return result
