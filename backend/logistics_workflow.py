"""T11宿主：单运行图、持久化节点记录及实时授权/领取边界。"""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import uuid

from fastapi import APIRouter, Request
from langgraph.checkpoint.sqlite import SqliteSaver

from project.workflows.logistics import build_graph
from project.agents.logistics_protocol import ProtocolError, next_step
from project.agents.logistics_coverage import goal_evidence_status, goal_followup
from project.tools.logistics import TOOL_SPECS, validate_arguments
from project.agents.logistics_policy import STOP_REASONS, call_key, evidence_fingerprint, revision_triggers, plan_diff, digest
from .common import APIError, iso
from .logistics_snapshots import canonical, content_hash


SCHEMA = '''CREATE TABLE IF NOT EXISTS logistics_graph_operations (
 run_id TEXT NOT NULL REFERENCES logistics_runs(run_id), operation_key TEXT NOT NULL,
 node TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT, result_hash TEXT,
 created_at TEXT NOT NULL, PRIMARY KEY(run_id,operation_key)
);
CREATE TABLE IF NOT EXISTS logistics_plan_constraints (
 constraint_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES logistics_runs(run_id),
 version INTEGER NOT NULL, instruction TEXT NOT NULL, creator_id INTEGER NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(run_id,version)
);
CREATE TABLE IF NOT EXISTS logistics_plan_revisions (
 run_id TEXT NOT NULL REFERENCES logistics_runs(run_id), version INTEGER NOT NULL,
 record_json TEXT NOT NULL, record_hash TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(run_id,version)
);
CREATE TABLE IF NOT EXISTS logistics_observation_progress (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, family_id TEXT NOT NULL, run_id TEXT NOT NULL,
 turn INTEGER NOT NULL, fingerprint TEXT NOT NULL, effective INTEGER NOT NULL, no_progress INTEGER NOT NULL,
 UNIQUE(run_id,turn)
);'''
REQUIRED = ('get_snapshot_summary', 'get_pending_tasks', 'get_quality_summary')


class LogisticsWorkflow:
    def __init__(self, model, reports):
        self.model, self.tools, self.runs, self.data, self.reports = model, model.tools, model.runs, model.data, reports
        with self.data.lock:
            self.data.db.executescript(SCHEMA)
        # 与业务SQLite配套备份；独立连接避免HTTP事务与检查点提交互相干扰。
        path = Path(self.data.db.execute('PRAGMA database_list').fetchone()[2]).with_name('logistics-graph.sqlite')
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.saver = SqliteSaver(self.connection)
        self.saver.setup()
        self.graph = build_graph(self, self.saver)
        self.actor = self.claim = None

    def config(self, run_id):
        return {'configurable': {'thread_id': 'logistics:'+run_id}, 'recursion_limit': 100}

    def guard(self, run_id):
        with self.data.lock:
            row = self.runs.owned(run_id, self.claim)
            if not row:
                raise APIError(409, '调查领取凭证已失效。')
            self.model.context(self.actor, run_id)
            return row

    def advance(self, row):
        self.actor = {'id': row['creator_id'], 'account': row['creator_account']}
        self.claim = row['claim_token']
        self.guard(row['run_id'])
        config = self.config(row['run_id'])
        saved = self.graph.get_state(config)
        initial = {'run_id': row['run_id'], 'question': row['question'], 'plans': [], 'step_states': {},
                   'evidence_ids': [], 'trace': [], 'observations': [], 'turn': 0, 'revisions': 0,
                   'revision_history': [], 'handled_triggers': [], 'constraint_version': 0}
        try:
            self.runs.budgets.check(row['run_id'], delivery=saved.next == ('finish',) and bool(saved.values.get('report')))
        except ProtocolError as exc:
            state = dict(saved.values) or initial
            state['stop_reason'] = exc.code
            result = self.finish(state)
            self.guard(row['run_id'])
            self.graph.update_state(config, {**state, **result}, as_node='finish')
            saved = self.graph.get_state(config)
        if not saved.values or saved.next:
            self.graph.invoke(None if saved.values else initial, config, durability='sync')
            saved = self.graph.get_state(config)
        self.guard(row['run_id'])
        if saved.next:
            return 'running', 'graph:'+saved.next[0], None
        state = saved.values
        if state.get('stop_reason'):
            return 'waiting', 'graph:stopped', {'kind': 'model-investigation', 'thread_id': row['thread_id'],
                'status': 'partial', 'stop_reason': state['stop_reason'], 'manual_required': True,
                'report': state.get('report'), 'evidence_ids': state['evidence_ids'],
                'unfinished_steps': state.get('unfinished_steps', [])}
        return 'completed', 'graph:done', {'kind': 'model-investigation', 'thread_id': row['thread_id'],
            'status': state['report']['value']['status'], 'report': state['report'],
            'template_report_id': state.get('template_report_id')}

    def node(self, name, state):
        run_id = state['run_id']
        self.guard(run_id)
        # graph checkpoint可能落后于已经提交的节点；同一节点记录复用避免重复副作用。
        key = f"{state['turn']}:{state['revisions']}:{name}"
        if name == 'plan' and state.get('plan'):
            legacy = self.data.one('SELECT * FROM logistics_graph_operations WHERE run_id=? AND operation_key=?', (run_id, key))
            expected = state['plan']['version']+1
            completed = json.loads(legacy['result_json']) if legacy and legacy['status'] == 'done' else {}
            if legacy and legacy['status'] == 'done' and content_hash(completed) != legacy['result_hash']:
                raise APIError(409, '调查节点记录校验失败。')
            # T11旧版已完成/未知操作仍复用；新修订用目标版本和约束版本防止首工具前键碰撞。
            if not (legacy and (legacy['status'] == 'started' or completed.get('plan', {}).get('version') == expected)):
                constraint = self.latest_constraint(run_id)
                key += f":v{expected}:c{constraint['version'] if constraint else 0}"
        with self.runs.transaction():
            saved = self.data.one('SELECT * FROM logistics_graph_operations WHERE run_id=? AND operation_key=?', (run_id, key))
            if saved and saved['status'] == 'done':
                result = json.loads(saved['result_json'])
                if content_hash(result) != saved['result_hash']:
                    raise APIError(409, '调查节点记录校验失败。')
                return result
            if saved:
                # 进程崩溃后的inflight模型用量不确定，不能自动再次计费。
                return {'stop_reason': 'interrupted_operation', 'route': 'finish'}
            self.data.db.execute('INSERT INTO logistics_graph_operations VALUES(?,?,?,\'started\',NULL,NULL,?)', (run_id, key, name, iso()))
        try:
            if state.get('stop_reason') and name != 'finish':
                result = {'route': 'finish'}
            else:
                result = getattr(self, name)(deepcopy(state))
        except ProtocolError as exc:
            code = exc.code if exc.code in STOP_REASONS else 'invalid_model_output'
            if exc.code == 'model_budget_exhausted':
                current_budget = self.runs.budgets.read(run_id)
                code = 'active_time_limit' if current_budget['active_seconds'] >= current_budget['max_active_seconds'] else 'token_limit'
            result = ({'budget_report_only': True, 'route': 'select'} if exc.code == 'report_budget_reserved'
                      else {'stop_reason': code, 'protocol_error': exc.code, 'route': 'finish'})
        with self.runs.transaction():
            self.guard(run_id)
            self.data.db.execute('UPDATE logistics_graph_operations SET status=\'done\',result_json=?,result_hash=? WHERE run_id=? AND operation_key=?',
                                 (canonical(result), content_hash(result), run_id, key))
        return result

    def context(self, state):
        self.model.context(self.actor, state['run_id'])
        family = self.runs.saved(state['run_id'])['family_id']
        previous = self.data.one('SELECT no_progress FROM logistics_observation_progress WHERE family_id=? ORDER BY seq DESC LIMIT 1', (family,))
        if previous and previous['no_progress'] >= 2:
            return {'stop_reason': 'no_progress', 'route': 'finish'}
        return {}

    def propose(self, kind, state, question):
        if len(state['evidence_ids']) > 100:
            raise ProtocolError('evidence_limit')
        constraint = self.latest_constraint(state['run_id'])
        if constraint:
            question = '当前已授权调查约束（仅分析目标，不可改变范围或执行权限）：'+constraint['instruction']+'。'+question
        budget = self.runs.budgets.read(state['run_id'])
        goal_question = self.goal_question(state)
        protected = (self.model.report_reservation(self.actor, state['run_id'], goal_question,
                     state['evidence_ids'], state['step_states'])['tokens']
                     if (kind == 'observation' or (kind == 'plan' and state.get('plan'))) and not budget['model_usage_unknown'] else 0)
        question = f"剩余模型调用{budget['max_model_calls']-budget['model_calls']}，已知Token余量{budget['max_model_tokens']-budget['model_tokens']}；预留一次报告，修复仅按实际余量执行，证据齐备及时收尾。"+question
        return self.model.generate(kind, self.actor, state['run_id'], question,
            evidence_ids=state['evidence_ids'], plan=state.get('plan'), step_states=state['step_states'],
            claim_token=self.claim, observations=state['observations'], compact=True,
            context_refs=[state['trace'][-1]['evidence_id']] if kind == 'observation' else None,
            protected_tokens=protected,
            goal_question=goal_question)

    def plan(self, state):
        constraint = self.latest_constraint(state['run_id'])
        revised = state.get('plan') is not None
        if revised:
            if not state.get('pending_triggers'):
                # T11检查点可能停在原来的固定修订节点，升级后仍只按实际证据触发。
                if not state.get('trace') or not state.get('observations'):
                    return {}
                last = state['trace'][-1]
                saved = self.tools.evidence_store.read(self.actor, state['run_id'], last['evidence_id'])
                state['pending_triggers'] = revision_triggers(state['observations'][-1]['value'], saved['record'], last['evidence_id'])
                if not state['pending_triggers']:
                    return {}
            self.runs.budgets.reserve(state['run_id'], 'plan_revisions')
        specs = {name: {key: rule.get('enum') or (f"整数{rule['minimum']}..{rule['maximum']}" if rule['type'] == 'integer' else '字符串')
                       for key, rule in properties.items()} for name, (_, properties) in TOOL_SPECS.items()}
        question = state['question']+'。可用业务工具及参数约束：'+json.dumps(specs, ensure_ascii=False, separators=(',', ':'))+'。未指定参数取默认，禁止覆盖范围。优先用最多3个步骤解决目标，避免重复分组；缺少现场事实不能无限扩展查询。'
        if state.get('plan'):
            question += f"。根据实际工具结果及最新观察修订下一步；本次输出version必须为{state['plan']['version']+1}；保留已执行步骤并照抄step_states中的状态。"
            question += '。实际修订触发：'+json.dumps(state['pending_triggers'], ensure_ascii=False)
        proposal = self.propose('plan', state, question)
        history = list(state.get('revision_history', []))
        if revised:
            record = {'old_plan': state['plan'], 'new_plan': proposal['value'], 'diff': plan_diff(state['plan'], proposal['value']),
                      'triggers': state['pending_triggers'], 'reason': proposal['value']['revision_reason'], 'created_at': iso()}
            with self.runs.transaction():
                self.guard(state['run_id'])
                self.data.db.execute('INSERT INTO logistics_plan_revisions VALUES(?,?,?,?,?)',
                    (state['run_id'], proposal['value']['version'], canonical(record), content_hash(record), record['created_at']))
            history.append(record)
        ineffective = revised and not any(s['status'] == 'pending' and not any(
            t['tool'] == s['tool'] and t.get('parameters') == s['parameters'] for t in state['trace'])
            for s in proposal['value']['steps'])
        return {'plan': proposal['value'], 'plans': state['plans']+[proposal], 'revisions': state['revisions']+int(revised),
                'revision_history': history, 'pending_triggers': [],
                **({'budget_report_only': True} if ineffective and self.goal_status(state)['supported'] else {}),
                'constraint_version': constraint['version'] if constraint else 0}

    def select(self, state):
        if state.get('stop_reason'):
            return {'route': 'finish'}
        if triggers := self.constraint_trigger(state):
            return {'route': 'plan', 'pending_triggers': triggers}
        reserve_report = state.get('budget_report_only', False) or self.report_reserve(state)
        goal = self.goal_status(state)
        goal_ready = goal['ready']
        step = None if reserve_report or goal_ready else next_step(state['plan'], state['step_states'])
        origin = 'model'
        binding = None
        if goal.get('workload_required') and not goal_ready:
            focus = goal['coverage']['focus_courier_ids']
            focus_step = step and step['tool'] == 'get_courier_workload' and (
                any(word in step['question']+step['completion_condition'] for word in ('重点', '最高', '最多', '领先')) or
                step['parameters'].get('courier_id', '').startswith('$') or
                not step['parameters'].get('courier_id'))
            if focus_step and focus and not reserve_report:
                binding = {'reason': '重点人员由已保存逾期计数证据绑定',
                    'evidence_ids': goal['coverage']['focus_evidence_ids'],
                    'original_parameters': deepcopy(step['parameters']), 'focus_courier_ids': focus}
                step['parameters'] = {**step['parameters'], 'courier_id': focus[0], 'page': 1}
            else:
                # 每个补取签名最多尝试一次，失败不循环；所有调用仍经过正式执行器。
                args = ({'courier_id': goal['missing_workload_ids'][0]} if goal['missing_workload_ids'] else
                        {'dimension': 'courier_id', 'sort': 'overdue_desc', 'page_size': 100})
                tool = 'get_courier_workload' if goal['missing_workload_ids'] else 'get_risk_distribution'
                attempted = any(t['tool'] == tool and t.get('origin') == 'host_goal_requirement' and
                    all(t.get('parameters', {}).get(k) == v for k, v in args.items()) for t in state['trace'])
                if not attempted and (not reserve_report or not any(t.get('origin') == 'host_goal_requirement'
                        for t in state['trace'])) and (goal['missing_workload_ids'] or
                    (not focus and (focus_step or step is None))) and self.followup_fits(
                        state, {'tool': tool, 'parameters': validate_arguments(tool, args)}):
                    origin = 'host_goal_requirement'
                    step = {'step_id': 'goal_'+str(state['turn'])+'_'+tool, 'question': '补取用户目标缺失证据',
                        'tool': tool, 'parameters': validate_arguments(tool, args), 'depends_on': [],
                        'completion_condition': '保存目标证据；不可用时保留缺口', 'status': 'pending'}
                    state['plan']['steps'].append(step)
                    binding = {'reason': '宿主目标补取', 'evidence_ids': goal['coverage']['focus_evidence_ids'],
                        'original_parameters': None, 'focus_courier_ids': focus}
        if step is None and not goal_ready and goal['supported'] and not any(
                t.get('origin') == 'host_goal_requirement' for t in state['trace']):
            scope, evidence = self.model.context(self.actor, state['run_id'], state['evidence_ids'])
            candidate = goal_followup(self.goal_question(state), evidence, scope, state['trace'])
            if candidate and self.followup_fits(state, candidate):
                origin = 'host_goal_requirement'
                step = {'step_id': 'goal_'+str(state['turn'])+'_get_pending_tasks',
                    'question': candidate['reason'], 'tool': candidate['tool'],
                    'parameters': validate_arguments(candidate['tool'], candidate['parameters']), 'depends_on': [],
                    'completion_condition': '补齐逾期归属；失败保留缺口', 'status': 'pending'}
                state['plan']['steps'].append(step)
                binding = {'reason': '宿主目标补取', 'evidence_ids': candidate['evidence_ids'],
                           'original_parameters': None}
        if step is None:
            if not reserve_report and not goal_ready and any(state['step_states'].get(s['step_id'], s['status']) in ('pending', 'running') for s in state['plan']['steps']):
                return {'stop_reason': 'blocked_dependencies', 'route': 'finish'}
            obtained = {c['tool'] for c in state['trace'] if c['status'] != 'error'
                        and self.tools.evidence_store.read(self.actor, state['run_id'], c['evidence_id'])['record'].get('data') is not None}
            needed = next((tool for tool in REQUIRED if tool not in obtained), None)
            if needed:
                origin = 'report_requirement'
                # 首工具由模型选择；报告前宿主保证权威指标/任务/质量证据齐备。
                step = {'step_id': 'required_'+needed, 'question': '报告所需已验证证据', 'tool': needed,
                        'parameters': validate_arguments(needed, {'page_size': 20} if needed == 'get_pending_tasks' else {}),
                        'depends_on': [], 'completion_condition': '保存报告必需证据', 'status': 'pending'}
                state['plan']['steps'].append(step)
            else:
                return {'route': 'report'}
        validate_arguments(step['tool'], step['parameters'])
        return {'selected': step, 'selected_origin': origin, 'selected_binding': binding,
                'plan': state['plan'], 'route': 'execute'}

    def execute(self, state):
        if triggers := self.constraint_trigger(state):
            return {'route': 'plan', 'pending_triggers': triggers}
        run_id = state['run_id']; step = state['selected']
        with self.data.lock:
            self.guard(run_id)
            meta, _ = self.model.context(self.actor, run_id)
            signature = call_key(meta['fingerprint'], step['tool'], step['parameters'])
            for old in state['trace']:
                if not old.get('call_key'):
                    old_step = next((s for s in state['plan']['steps'] if s['step_id'] == old['step_id']), None)
                    if old_step:
                        old['call_key'] = call_key(meta['fingerprint'], old['tool'], old_step['parameters'])
            previous = next((t for t in state['trace'] if t.get('call_key') == signature), None)
            if previous:
                saved = self.tools.evidence_store.read(self.actor, run_id, previous['evidence_id'])
                result = {**saved['record'], 'result_evidence_id': previous['evidence_id'], 'call_id': previous['call_id']}
            else:
                self.runs.budgets.check(run_id, 'tool_calls')
                result = self.tools.call(self.actor, run_id, step['tool'], step['parameters'])
            self.guard(run_id)
        refs = [result['result_evidence_id']]
        if step['tool'] == 'get_pending_tasks':
            refs.extend(r['evidence_id'] for r in result['rows'])
        trace = {'plan_version': state['plan']['version'], 'step_id': step['step_id'], 'tool': step['tool'],
                 'call_id': result['call_id'], 'evidence_id': result['result_evidence_id'], 'status': result['status'], 'origin': state['selected_origin'],
                 'parameters': step['parameters'], 'call_key': signature, 'reused': bool(previous),
                 'evidence_fingerprint': evidence_fingerprint(result)}
        if state.get('selected_binding'):
            trace['parameter_binding'] = state['selected_binding']
        status = 'failed' if result['status'] == 'error' else 'completed'
        return {'step_states': {**state['step_states'], step['step_id']: status},
                'evidence_ids': list(dict.fromkeys(state['evidence_ids']+refs)), 'trace': state['trace']+[trace],
                'turn': state['turn']+1, 'route': 'observe',
                **({'stop_reason': 'required_evidence_unavailable'} if state['selected_origin'] == 'report_requirement'
                   and (result['status'] == 'error' or result.get('data') is None) else {})}

    def observe(self, state):
        selected = state['selected']
        trace = state['trace'][-1]
        reused = trace.get('reused', False)
        reserve_report = self.report_reserve(state)
        goal_ready = self.goal_status(state)['ready']
        budget_fallback = False
        if state['selected_origin'] in ('report_requirement', 'host_goal_requirement') or reused or trace['status'] == 'error' or reserve_report or goal_ready:
            trace = state['trace'][-1]
            # 报告前的必需取数由程序核验，仅记录实际状态，不伪造模型分析或根因。
            value = {'plan_version': state['plan']['version'], 'step_id': selected['step_id'],
                'new_facts': [{'statement': '报告必需工具执行状态：'+trace['status'], 'evidence_ids': [trace['evidence_id']]}],
                'hypothesis_assessments': [], 'missing': [], 'counterevidence': [], 'unresolved_questions': [], 'next_steps': []}
            if reused:
                value['new_facts'] = []
            proposal = {'source': 'program_duplicate_check' if reused else 'program_requirement_check', 'value': value}
            if state['selected_origin'] == 'host_goal_requirement':
                proposal['source'] = 'program_goal_requirement_check'
                value['missing'] = self.goal_status(state)['missing']
            if reserve_report and state['selected_origin'] == 'model' and not reused:
                value['missing'] = ['剩余预算预留报告，当前步骤未进行模型观察；未完成目标须在报告保留。']
                proposal['source'] = 'program_budget_check'
            elif goal_ready and state['selected_origin'] == 'model' and not reused:
                proposal['source'] = 'program_goal_check'
        else:
            try:
                proposal = self.propose('observation', state, state['question']+'。仅评估刚执行完成的步骤 '+selected['step_id'])
            except ProtocolError as error:
                if error.code != 'report_budget_reserved':
                    raise
                budget_fallback = True
                proposal = {'source': 'program_budget_check', 'value': {
                    'plan_version': state['plan']['version'], 'step_id': selected['step_id'],
                    'new_facts': [], 'hypothesis_assessments': [], 'missing': ['预算预检保留报告及修复余量，本步骤未调用模型观察。'],
                    'counterevidence': [], 'unresolved_questions': [], 'next_steps': []}}
        result = {'observations': state['observations']+[proposal]}
        if budget_fallback:
            result['budget_report_only'] = True
        if state['selected_origin'] == 'model':
            value = proposal['value']
            assessments = [a for a in value['hypothesis_assessments'] if a['verdict'] != 'unknown']
            refs = {r for f in value['new_facts']+value['counterevidence']+assessments for r in f['evidence_ids']}
            actual = self.tools.evidence_store.read(self.actor, state['run_id'], trace['evidence_id'])['record']
            cited = trace['evidence_id'] in refs or bool(refs.intersection(r.get('evidence_id') for r in actual.get('rows', [])))
            result['no_progress'] = self.record_progress(state, trace.get('evidence_fingerprint') or evidence_fingerprint(actual),
                cited and not reused and trace['status'] in ('ok', 'empty'))
        return result

    def decide(self, state):
        self.guard(state['run_id'])
        if state.get('stop_reason'):
            return {'route': 'finish'}
        if self.constraint_trigger(state):
            return {'route': 'plan', 'pending_triggers': self.constraint_trigger(state)}
        if self.goal_status(state)['ready']:
            return {'route': 'select'}
        if state.get('no_progress', 0) >= 2:
            return {'stop_reason': 'no_progress', 'route': 'finish'}
        if state.get('budget_report_only') or self.report_reserve(state):
            return {'route': 'select'}
        trace = state['trace'][-1]
        record = self.tools.evidence_store.read(self.actor, state['run_id'], trace['evidence_id'])['record']
        constraint = self.latest_constraint(state['run_id'])
        changed = constraint if constraint and constraint['version'] > state.get('constraint_version', 0) else None
        triggers = revision_triggers(state['observations'][-1]['value'], record, trace['evidence_id'], constraint=changed)
        handled = list(state.get('handled_triggers', []))
        fresh = []
        for trigger in triggers:
            fingerprint = evidence_fingerprint(record)
            if trigger['kind'] == 'evidence_not_supported':
                fingerprint = digest(sorted(evidence_fingerprint(self.tools.evidence_store.read(self.actor, state['run_id'], ref)['record'])
                                            for ref in trigger['evidence_ids']))
            key = digest([trigger['kind'], fingerprint, trigger.get('constraint_id')])
            if key not in handled:
                handled.append(key); fresh.append(trigger)
        # 报告必需补取的缺失最终写入限制；不据此重新扩展已完成的调查目标。
        if fresh and (state['selected_origin'] == 'model' or changed):
            return {'route': 'plan', 'pending_triggers': fresh, 'handled_triggers': handled}
        return {'route': 'select'}

    def report(self, state):
        if triggers := self.constraint_trigger(state):
            return {'route': 'plan', 'pending_triggers': triggers}
        proposal = self.propose('report', state, state['question']+'。以保存证据回答用户目标；目标外未确认原因写limitations，不列为未解目标。目标证据不足才用partial并明确unresolved_questions；complete的unresolved_questions必须为空。')
        if triggers := self.constraint_trigger(state):
            return {'route': 'plan', 'pending_triggers': triggers}
        failed_evidence = any(t['status'] == 'error' or (t['status'] == 'unavailable' and
            self.tools.evidence_store.read(self.actor, state['run_id'], t['evidence_id'])['record'].get('data') is None)
            for t in state['trace'])
        if failed_evidence and proposal['value']['status'] != 'partial':
            raise ProtocolError('invalid_model_output')
        return {'report': proposal, 'route': 'finish'}

    def report_reserve(self, state):
        budget = self.runs.budgets.read(state['run_id'])
        # 未知用量不估算token余量；调用计数仍准确，保留原未知用量停止语义。
        if budget['model_usage_unknown']:
            return False
        reservation = self.model.report_reservation(self.actor, state['run_id'], self.goal_question(state),
                                                    state['evidence_ids'], state['step_states'])
        return (budget['max_model_calls']-budget['model_calls'] <= reservation['calls'] or
                budget['max_model_tokens']-budget['model_tokens'] <= reservation['tokens'])

    def goal_question(self, state):
        constraint = self.latest_constraint(state['run_id'])
        return state['question'] + ('。'+constraint['instruction'] if constraint else '')

    def goal_status(self, state):
        scope, evidence = self.model.context(self.actor, state['run_id'], state['evidence_ids'])
        return goal_evidence_status(self.goal_question(state), evidence, scope)

    def followup_fits(self, state, candidate):
        budget = self.runs.budgets.check(state['run_id'], 'tool_calls')
        if budget['model_usage_unknown'] or budget['model_transport_failed']:
            return False
        quote = self.model.report_reservation(self.actor, state['run_id'], self.goal_question(state),
                    state['evidence_ids'], state['step_states'], followup=candidate)
        return (budget['max_model_calls']-budget['model_calls'] >= quote['calls'] and
                budget['max_model_tokens']-budget['model_tokens'] >= quote['tokens'])

    def finish(self, state):
        if state.get('stop_reason'):
            return self.partial_report(state)
        # 模型报告与T08固定模板分别保存/展示，权威数字仍来自工具证据。
        results = {}
        for tool in REQUIRED:
            # 补页失败不能覆盖此前可用的基础证据；失败仍留在模型partial及完整轨迹中。
            trace = next(t for t in reversed(state['trace']) if t['tool'] == tool and t['status'] != 'error' and
                         self.tools.evidence_store.read(self.actor, state['run_id'], t['evidence_id'])['record'].get('data') is not None)
            item = self.tools.evidence_store.read(self.actor, state['run_id'], trace['evidence_id'])
            results[tool] = {**item['record'], 'result_evidence_id': trace['evidence_id']}
        report = self.reports.create(self.actor, state['run_id'], claim_token=self.claim, results=results)
        return {'template_report_id': report['report_id']}

    def record_progress(self, state, fingerprint, candidate):
        with self.runs.transaction():
            self.guard(state['run_id'])
            previous = self.data.one('SELECT * FROM logistics_observation_progress WHERE run_id=? AND turn=?', (state['run_id'], state['turn']))
            if previous:
                return previous['no_progress']
            family = self.runs.saved(state['run_id'])['family_id']
            seen = self.data.one('SELECT seq FROM logistics_observation_progress WHERE family_id=? AND fingerprint=? AND effective=1', (family, fingerprint))
            last = self.data.one('SELECT no_progress FROM logistics_observation_progress WHERE family_id=? ORDER BY seq DESC LIMIT 1', (family,))
            effective = bool(candidate and not seen)
            count = 0 if effective else (last['no_progress'] if last else 0)+1
            self.data.db.execute('INSERT INTO logistics_observation_progress(family_id,run_id,turn,fingerprint,effective,no_progress) VALUES(?,?,?,?,?,?)',
                                 (family, state['run_id'], state['turn'], fingerprint, int(effective), count))
            return count

    def latest_constraint(self, run_id):
        with self.data.lock:
            return self.data.one('SELECT * FROM logistics_plan_constraints WHERE run_id=? ORDER BY version DESC LIMIT 1', (run_id,))

    def constraint_trigger(self, state):
        constraint = self.latest_constraint(state['run_id'])
        if constraint and constraint['version'] > state.get('constraint_version', 0):
            return [{'kind': 'constraint_changed', 'evidence_ids': [], 'constraint_id': constraint['constraint_id'],
                     'reason': '下一步执行前发现已授权调查约束版本变化'}]
        return []

    def constrain(self, user, run_id, body, *, operation_id=None):
        if set(body) != {'instruction'} or type(body['instruction']) is not str or not 0 < len(body['instruction'].strip()) <= 1000:
            raise APIError(400, '仅接受长度1—1000的调查约束说明。')
        with self.runs.transaction():
            row = self.runs.saved(run_id)
            self.runs.authorize(user, row, owner=True)
            self.model.context(user, run_id)
            if row['mode'] != 'model' or row['status'] not in ('queued', 'running'):
                raise APIError(409, '仅能修改尚未停止的模型调查约束；此接口不恢复运行。')
            identifier = 'LCON-'+uuid.uuid4().hex if operation_id is None else 'LCON-'+content_hash([user['id'], run_id, operation_id])[:32]
            saved = self.data.one('SELECT * FROM logistics_plan_constraints WHERE constraint_id=?', (identifier,))
            if saved:
                if saved['instruction'] != body['instruction'].strip():
                    raise APIError(409, '约束操作标识已用于不同内容。')
                return {'constraint_id': identifier, 'version': saved['version']}
            previous = self.latest_constraint(run_id)
            version = previous['version']+1 if previous else 1
            self.data.db.execute('INSERT INTO logistics_plan_constraints VALUES(?,?,?,?,?,?)',
                                 (identifier, run_id, version, body['instruction'].strip(), user['id'], iso()))
            return {'constraint_id': identifier, 'version': version}

    def partial_report(self, state):
        self.guard(state['run_id'])
        reason = state['stop_reason']
        steps = (state.get('plan') or {}).get('steps', [])
        unfinished = [{**s, 'status': state['step_states'].get(s['step_id'], 'pending')} for s in steps
                      if state['step_states'].get(s['step_id'], 'pending') not in ('completed', 'skipped')]
        if not state.get('plan'):
            unfinished = [{'step_id': 'planning', 'question': state['question'], 'status': 'pending'}]
        # 操作日志可能领先图检查点；仍保留全部已经落盘的证据定位，协议只引用必要汇总。
        stored = self.data.all('SELECT evidence_id FROM logistics_evidence WHERE run_id=?', (state['run_id'],))
        retained = list(dict.fromkeys(state['evidence_ids']+[e['evidence_id'] for e in stored]))
        summary_refs = [e['evidence_id'] for e in self.data.all("SELECT evidence_id FROM logistics_evidence WHERE run_id=? AND source_type='tool_result'", (state['run_id'],))]
        scope, evidence = self.model.context(self.actor, state['run_id'], summary_refs)
        claims = []
        summary = next(((ref, e['record']) for ref, e in evidence.items() if e['record'].get('tool') == 'get_snapshot_summary'), None)
        if summary:
            ref, record = summary
            for name, metric in (record.get('data') or {}).get('metrics', {}).items():
                claims.append({'statement': '已保存快照汇总指标：'+name, 'kind': 'computed', 'evidence_ids': [ref],
                    'limitation': '仅引用已保存程序结果；调查因限制停止，未确认成因。',
                    'computed': {'evidence_id': ref, 'metric': name, 'value': metric['value']}})
        refs = list(dict.fromkeys(r for c in claims for r in c['evidence_ids']))
        value = {'status': 'partial', 'claims': claims, 'evidence_ids': refs,
                 'limitations': ['调查停止：'+reason, '仅保存已取得证据，未重新调用模型或工具；不代表零风险或完成处置。'],
                 'unresolved_questions': ['调查未完成，停止原因：'+reason]+[s['question'] for s in unfinished[:7]],
                 'next_steps': ['人工核查已保存证据及未完成项；恢复不能重置任务家族预算。']}
        validated = self.model.validate('report', self.actor, state['run_id'], value, evidence_ids=refs)
        return {'report': {'kind': 'report', 'source': 'program_stop_report', 'scope': scope, 'value': validated},
                'unfinished_steps': unfinished, 'evidence_ids': retained}

    def read(self, user, run_id):
        with self.data.lock:
            row = self.runs.saved(run_id); self.runs.authorize(user, row)
            saved = self.graph.get_state(self.config(run_id))
            state = dict(saved.values)
            for ref in state.get('evidence_ids', []):
                self.tools.evidence_store.read(user, run_id, ref)
            revisions = []
            for revision in self.data.all('SELECT * FROM logistics_plan_revisions WHERE run_id=? ORDER BY version', (run_id,)):
                record = json.loads(revision['record_json'])
                if content_hash(record) != revision['record_hash']:
                    raise APIError(409, '计划修订记录校验失败。')
                revisions.append(record)
            return {'thread_id': row['thread_id'], 'family_budget': self.runs.budgets.read(run_id),
                    'stop_reasons': list(STOP_REASONS), 'next_nodes': list(saved.next), 'state': state, 'revisions': revisions}

    def routes(self):
        router = APIRouter(prefix='/api/logistics/runs')
        @router.get('/{run_id}/investigation')
        def read(request: Request, run_id: str):
            if set(request.query_params)-{'workspaceId'}:
                raise APIError(400, '调查读取不能覆盖范围。')
            return self.read(self.runs.access.user(request), run_id)
        @router.post('/{run_id}/constraints')
        def constrain(request: Request, run_id: str):
            if set(request.query_params)-{'workspaceId'}:
                raise APIError(400, '调查约束不能覆盖范围。')
            return self.constrain(self.runs.access.user(request), run_id, request.state.body)
        return router

    def close(self):
        self.connection.close()
