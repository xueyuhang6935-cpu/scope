"""Single LangGraph orchestrator, durable approvals and scoped workspace membership."""
import json
import logging
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.sqlite import SqliteSaver
from psycopg.types.json import Jsonb

from .common import APIError, iso
from .database_connection import connection
from .investigation_agent import investigate
from .investigation_sql import InvestigationSQL
from .investigation_harness import InvestigationHarness
from .investigation_report import plan_blockers
from .shortage import fingerprint, plain, timestamp, TABLES
from .supply_planning import planning_snapshot, build_candidates


class State(TypedDict, total=False):
    run_id: str
    bundle: dict
    evidence: list
    report: dict
    plan_ids: list
    decision: dict
    blocked: bool
    query_context: dict


class InvestigationService:
    def __init__(self, auth, data, monitor, env):
        self.auth, self.data, self.monitor, self.env = auth, data, monitor, env
        with data.lock:
            data.db.executescript('''CREATE TABLE IF NOT EXISTS workflow_members (
                workspace_id TEXT NOT NULL, account TEXT NOT NULL, role TEXT NOT NULL,
                factories TEXT NOT NULL, approval_limit TEXT NOT NULL,
                updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(workspace_id,account));
                CREATE TABLE IF NOT EXISTS workflow_permission_history (
                id INTEGER PRIMARY KEY,workspace_id TEXT NOT NULL,account TEXT NOT NULL,
                actor TEXT NOT NULL,details TEXT NOT NULL,created_at TEXT NOT NULL);''')
        db_path = data.one('PRAGMA database_list')['file']
        checkpoint_path = Path(db_path).with_name('supply-graph.sqlite')
        self.checkpoint_db = sqlite3.connect(checkpoint_path, check_same_thread=False)
        self.checkpointer = SqliteSaver(self.checkpoint_db)
        self.lock = threading.RLock()
        self.ready = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='supply-graph')
        data.db.execute('CREATE TABLE IF NOT EXISTS investigation_stages (run_id TEXT PRIMARY KEY, stage TEXT NOT NULL, updated_at TEXT NOT NULL)')
        InvestigationHarness(data, '', lambda: None)
        graph = StateGraph(State)
        for name, fn in [('collect', self.collect), ('investigate', self.investigate), ('plans', self.plans), ('approval', self.approval), ('finish', self.finish)]:
            graph.add_node(name, fn)
        graph.add_edge(START, 'collect')
        graph.add_edge('collect', 'investigate')
        graph.add_edge('investigate', 'plans')
        graph.add_conditional_edges('plans', lambda s: END if s['blocked'] else 'approval')
        graph.add_edge('approval', 'finish')
        graph.add_edge('finish', END)
        self.graph = graph.compile(checkpointer=self.checkpointer)

    def ensure(self):
        if self.ready:
            return
        with self.lock:
            if self.ready:
                return
            self.monitor.ensure_schema()
            with connection(self.env, False) as db:
                db.execute("SELECT pg_advisory_xact_lock(hashtext('scope-workflow-schema-v2'))")
                db.execute(Path(__file__).with_name('workflow_schema.sql').read_text(encoding='utf-8'))
            self.ready = True

    def access(self, user, workspace, role=None, factories=()):
        grant = self.permissions.require(user, workspace, role or 'view', factories)
        if role in (None, 'view'):
            grants = self.permissions.grants(user,workspace)
            grant = {**grant, 'factories': sorted({f for g in grants for f in g['factories']})}
        return grant

    def user_by_account(self, account):
        user = self.auth.query('SELECT id,account FROM users WHERE account=?', (account,))
        if not user:
            raise APIError(403, '业务账号已不可用。')
        return user

    def config_for(self, workspace):
        config = self.data.one('SELECT * FROM risk_monitor_configs WHERE workspace_id=?', (workspace,))
        if not config:
            raise APIError(404, '场景配置不存在。')
        return config

    def run_record(self, db, run_id):
        row = db.execute('SELECT * FROM sc.investigations WHERE id=%s', (run_id,)).fetchone()
        if not row:
            raise APIError(404, '调查不存在。')
        return row

    def event(self, db, event_id):
        row = db.execute('SELECT * FROM sc.risk_events WHERE id=%s', (event_id,)).fetchone()
        if not row:
            raise APIError(404, '风险事件不存在。')
        return row

    def collect(self, state):
        self.record_stage(state, 'collect')
        with connection(self.env) as db:
            run = self.run_record(db, state['run_id'])
            event = self.event(db, run['event_id'])
            self.access(self.user_by_account(run['requested_by']), run['workspace_id'], 'plan', run['input']['factories'])
            config = self.config_for(run['workspace_id'])
            if config['scenario_id'] != event['scenario_id']:
                raise APIError(409, '调查场景已变化，请重新发起调查。')
            if fingerprint({k: config[k] for k in ('as_of','horizon_days')}) != run['input']['configHash']:
                raise APIError(409, '调查范围已变化，请重新调查。')
            snapshot = planning_snapshot(db, self.monitor, config)
            if run['input'].get('sourceSnapshotHash') and not run['input'].get('replanning'):
                source_snapshot = {k: v for k, v in snapshot.items() if k != 'localPolicies'}
                source_snapshot['tables'] = {k: v for k, v in snapshot['tables'].items() if k in TABLES}
                if fingerprint(source_snapshot) != run['input']['sourceSnapshotHash']:
                    raise APIError(409, '排队期间数据已变化，请重新计算后调查。')
            bundle = build_candidates(snapshot, event, run['input'].get('windowDays', config['horizon_days']), run['input']['factories'])
            bundle['question'] = run['input'].get('question', '')
            feedback = run['input'].get('replanning')
            if feedback:
                bundle['replanning'] = feedback
                for candidate in bundle['candidates']:
                    if fingerprint(candidate) in feedback.get('rejectedHashes', []):
                        candidate['feasible'] = False
                        candidate['reasons'].append('相同快照下的该方案此前已被驳回，请选择其他方案或补充业务数据。')
            evidence = bundle.get('evidence', bundle['baseline']['evidence'])
        return {'bundle': bundle, 'evidence': evidence,
                'query_context': {'scenario': config['scenario_id'], 'workspace': run['workspace_id']}}

    def authorize_investigation(self, state, run):
        user = self.user_by_account(run['requested_by'])
        self.access(user, run['workspace_id'], 'plan', run['input']['factories'])
        config = self.config_for(run['workspace_id'])
        if state.get('query_context') != {'scenario': config['scenario_id'], 'workspace': run['workspace_id']}:
            raise APIError(409, '调查场景或工作空间已变化，请重新调查。')
        if fingerprint({k: config[k] for k in ('as_of', 'horizon_days')}) != run['input']['configHash']:
            raise APIError(409, '调查范围已变化。')
        from .business_access import DOMAINS
        domains = {domain: [factory for factory in run['input']['factories']
                           if self.permissions.can(user, run['workspace_id'], 'data.read', [factory], domain)]
                   for domain in sorted(DOMAINS)}
        domains['reference'] = sorted({f for factories in domains.values() for f in factories})
        return {'workspace': run['workspace_id'], 'scenario': config['scenario_id'],
                'asOf': config['as_of'], 'domains': domains, 'factories': run['input']['factories']}

    def investigate(self, state):
        self.record_stage(state, 'investigate')
        with connection(self.env, False) as db:
            run = self.run_record(db, state['run_id'])
            # Persist reservation BEFORE any external model request. Checkpoint
            # replay can never silently repeat a possibly billed investigation.
            allowed = not run['model_attempted']
            if self.env.get('SUPPLY_INVESTIGATOR') == 'model':
                db.execute('UPDATE sc.investigations SET model_attempted=true WHERE id=%s', (run['id'],))
        authorize = lambda: self.authorize_investigation(state, run)
        harness = InvestigationHarness(self.data, run['id'], authorize)
        query_tools = InvestigationSQL(self.env, authorize)
        bundle = dict(state['bundle'])
        bundle['evidenceIndex'] = [{'id': e['id'], 'table': e.get('table')} for e in state['evidence'][:50]]
        parent_id = run['input'].get('replanning', {}).get('parentRunId')
        if parent_id and self.env.get('SUPPLY_INVESTIGATOR') == 'model':
            parent = self.read_run(self.user_by_account(run['requested_by']), parent_id, run['workspace_id'])
            saved = parent.get('report') or {}
            if parent['event_id'] == run['event_id'] and saved.get('snapshotHash') == bundle.get('snapshotHash'):
                bundle['evidenceIndex'] += [{'id': e['id'], 'table': e.get('table'), 'historical': True}
                                            for e in saved.get('evidence', []) if e['id'].startswith('sql:')][:20]
        report = investigate(bundle, state['evidence'], self.env, allowed,
                             query_tools=query_tools, harness=harness,
                             evidence_reader=lambda identifier: self.read_evidence(state, run, identifier))
        report['snapshotHash'] = bundle.get('snapshotHash')
        return {'report': report}

    def read_evidence(self, state, run, identifier):
        """Only current evidence or this run's explicit parent, never arbitrary runs."""
        harness = InvestigationHarness(self.data, run['id'], lambda: self.authorize_investigation(state, run))
        harness.check()
        with connection(self.env) as db:
            config = self.config_for(run['workspace_id'])
            if fingerprint(planning_snapshot(db, self.monitor, config)) != state['bundle']['snapshotHash']:
                raise ValueError('数据快照已变化，请刷新调查；不能将历史证据当作当前证据。')
            evidence = state['evidence']
            item = next((e for e in evidence if e['id'] == identifier), None)
            if item is None:
                parent_id = run['input'].get('replanning', {}).get('parentRunId')
                if not parent_id:
                    raise ValueError('证据不在当前或关联调查中。')
                parent = self.run_record(db, parent_id)
                if parent['workspace_id'] != run['workspace_id'] or parent['event_id'] != run['event_id']:
                    raise PermissionError('关联调查范围不匹配。')
                user = self.user_by_account(run['requested_by'])
                self.access(user, parent['workspace_id'], 'view', parent['input']['factories'])
                for domain in ('inventory', 'purchase', 'production', 'quality', 'rules'):
                    self.permissions.require(user, parent['workspace_id'], 'data.read', parent['input']['factories'], domain)
                saved = parent.get('report') or {}
                if saved.get('snapshotHash') != state['bundle']['snapshotHash']:
                    raise ValueError('历史证据快照不同或缺少快照标识，请重新取证。')
                item = next((e for e in saved.get('evidence', []) if e['id'] == identifier), None)
        if item is None:
            raise ValueError('证据ID不存在，不能读取无关任务。')
        harness.check([item])
        if len(json.dumps(item, ensure_ascii=False)) > 20000:
            raise ValueError('证据过大，请使用SQL缩小范围后读取。')
        provenance = item.get('provenance', {})
        if item.get('table') == 'supplier_commitments':
            provenance = {**provenance, 'sourceType': 'supplier_statement', 'verified': False}
        return {**item, 'provenance': provenance}

    def plans(self, state):
        self.record_stage(state, 'plans')
        blockers = plan_blockers(state['report'], state['bundle'])
        blocked = bool(blockers)
        state = {**state, 'report': {**state['report'], 'planBlockers': blockers}}
        if blocked:
            state['report'].update(manualRequired=True, stopReason='；'.join(blockers))
        # Permission/config writes share this lock; keep the final check and
        # PostgreSQL commit within it in the supported single-process deployment.
        with self.data.lock, connection(self.env, False) as db:
            run = self.run_record(db, state['run_id'])
            harness = InvestigationHarness(self.data, run['id'], lambda: self.authorize_investigation(state, run))
            try:
                harness.check(state['report'].get('evidence', []), state['bundle']['candidates'])
            except (APIError, PermissionError):
                db.execute("UPDATE sc.investigations SET status='manual_required',error=%s,updated_at=now() WHERE id=%s",
                           ('调查授权或范围已变化，报告未交付，请人工核查。', run['id']))
                return {'plan_ids': [], 'blocked': True}
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (run['event_id'],))
            identifiers = []
            for index, content in enumerate(state['bundle']['candidates']):
                old = db.execute('SELECT id FROM sc.candidate_plans WHERE run_id=%s AND candidate_index=%s', (run['id'], index)).fetchone()
                if old:
                    identifiers.append(old['id'])
                    continue
                version = db.execute('SELECT coalesce(max(version),0)+1 AS v FROM sc.candidate_plans WHERE event_id=%s', (run['event_id'],)).fetchone()['v']
                identifier = 'PLAN-' + uuid.uuid4().hex
                db.execute('''INSERT INTO sc.candidate_plans(id,event_id,run_id,version,candidate_index,content,content_hash,status,expires_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''', (identifier, run['event_id'], run['id'], version, index,
                    Jsonb(content), fingerprint(content), 'candidate' if content['feasible'] else 'infeasible', datetime.now(timezone.utc)+timedelta(hours=24)))
                identifiers.append(identifier)
            db.execute('UPDATE sc.investigations SET report=%s,status=%s,updated_at=now() WHERE id=%s',
                       (Jsonb(state['report']), 'manual_required' if blocked else 'awaiting_approval', run['id']))
        return {'plan_ids': identifiers, 'blocked': blocked, 'report': state['report']}

    def approval(self, state):
        self.record_stage(state, 'approval')
        decision = interrupt({'runId': state['run_id'], 'planIds': state['plan_ids'], 'reason': '等待有权限的另一位用户审批'})
        return {'decision': decision}

    def finish(self, state):
        self.record_stage(state, 'finish')
        # Never trust a resume payload as authority. Decisions come from committed approvals.
        with connection(self.env, False) as db:
            decision = db.execute('''SELECT a.decision,p.status FROM sc.plan_approvals a JOIN sc.candidate_plans p ON p.id=a.plan_id
                WHERE p.run_id=%s ORDER BY a.decided_at DESC LIMIT 1''', (state['run_id'],)).fetchone()
            if not decision:
                raise APIError(409, '缺少已持久化审批，不能恢复。')
            status = 'approved' if decision['decision'] == 'approved' and decision['status'] == 'approved' else 'replan_required'
            db.execute('UPDATE sc.investigations SET status=%s,updated_at=now() WHERE id=%s', (status, state['run_id']))
        return {}

    def replan(self, run_id):
        """Commit one linked run atomically; replay never creates another child."""
        with self.data.lock, connection(self.env, False) as db:
            run = self.run_record(db, run_id)
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (run['event_id'],))
            run = self.run_record(db, run_id)
            key = 'replan:' + run_id
            child = db.execute('SELECT id FROM sc.investigations WHERE workspace_id=%s AND request_id=%s',
                               (run['workspace_id'], key)).fetchone()
            if child:
                return child['id']
            if run['status'] != 'replan_required':
                return None
            previous = run['input'].get('replanning', {})
            round_number = previous.get('round', 0) + 1
            try:
                limit = max(0, min(5, int(self.env.get('SUPPLY_MAX_REPLANS', 3))))
            except (TypeError, ValueError):
                limit = 3
            reason = None
            try:
                self.access(self.user_by_account(run['requested_by']), run['workspace_id'], 'plan', run['input']['factories'])
                config = self.config_for(run['workspace_id'])
                event = self.event(db, run['event_id'])
                if config['scenario_id'] != event['scenario_id'] or fingerprint({k: config[k] for k in ('as_of', 'horizon_days')}) != run['input']['configHash']:
                    reason = '调查场景或范围已变化，请人工确认后重新发起调查。'
            except APIError:
                reason = '发起人的调查权限或配置已变化，停止自动重规划。'
            if round_number > limit:
                reason = f'达到自动重规划上限（{limit}轮），请人工核查。'
            if db.execute("SELECT 1 FROM sc.investigations WHERE event_id=%s AND status IN ('queued','running','resuming','awaiting_approval')", (run['event_id'],)).fetchone():
                reason = '已有新的进行中调查，停止旧调查的自动重规划。'
            if reason:
                db.execute("UPDATE sc.investigations SET status='manual_required',error=%s,updated_at=now() WHERE id=%s", (reason, run_id))
                return None
            rejected = db.execute('''SELECT p.id,p.content_hash,a.comment FROM sc.plan_approvals a
                JOIN sc.candidate_plans p ON p.id=a.plan_id WHERE p.run_id=%s AND a.decision='rejected'
                ORDER BY a.decided_at''', (run_id,)).fetchall()
            feedback = {'parentRunId': run_id, 'rootRunId': previous.get('rootRunId', run_id), 'round': round_number,
                        'reason': run['error'] or (rejected[-1]['comment'] if rejected else '方案需要重新核查'),
                        'rejectedPlans': (previous.get('rejectedPlans', []) + [
                            {'planId': r['id'], 'reason': r['comment']} for r in rejected])[-10:],
                        'rejectedHashes': sorted(set(previous.get('rejectedHashes', [])) | {r['content_hash'] for r in rejected})}
            child_id = 'INV-' + uuid.uuid4().hex
            db.execute("UPDATE sc.candidate_plans SET status='superseded' WHERE run_id=%s AND status IN ('candidate','approved')", (run_id,))
            db.execute("UPDATE sc.investigations SET status='replanned',updated_at=now() WHERE id=%s", (run_id,))
            db.execute("INSERT INTO sc.investigations(id,event_id,workspace_id,requested_by,request_id,status,input) VALUES(%s,%s,%s,%s,%s,'queued',%s)",
                       (child_id, run['event_id'], run['workspace_id'], run['requested_by'], key,
                        Jsonb({**run['input'], 'replanning': feedback})))
        return child_id

    def process(self, run_id):
        # ponytail: bounded demo uses one graph worker and a local durable saver;
        # migrate the saver/leases together before deploying multiple app processes.
        with self.lock:
            try:
                with connection(self.env, False) as db:
                    run = self.run_record(db, run_id)
                    if run['status'] not in ('queued','running','resuming','awaiting_approval','replan_required','replanned'):
                        return
                    db.execute("UPDATE sc.investigations SET status=CASE WHEN status='queued' THEN 'running' ELSE status END WHERE id=%s", (run_id,))
                    approval = db.execute('SELECT a.id FROM sc.plan_approvals a JOIN sc.candidate_plans p ON p.id=a.plan_id WHERE p.run_id=%s LIMIT 1', (run_id,)).fetchone()
                if run['status'] in ('replan_required', 'replanned'):
                    child_id = self.replan(run_id)
                    if child_id:
                        self.process(child_id)
                    return
                config = {'configurable': {'thread_id': run_id}}
                saved = self.graph.get_state(config)
                if saved.values:
                    # A crash can occur after the plans checkpoint but before the
                    # approval node actually calls interrupt. Establish it first.
                    if saved.next == ('approval',) and not saved.interrupts:
                        self.graph.invoke(None, config, durability='sync')
                        saved = self.graph.get_state(config)
                    payload = Command(resume={'approvalId': approval['id']}) if approval and saved.next == ('approval',) else None
                    if saved.next == ('approval',) and not approval:
                        return
                else:
                    payload = {'run_id': run_id}
                self.graph.invoke(payload, config, durability='sync')
                child_id = self.replan(run_id)
                if child_id:
                    self.process(child_id)
            except Exception as exc:
                logging.getLogger(__name__).exception('Investigation failed: %s', run_id)
                InvestigationHarness.interrupt(self.data, run_id)
                try:
                    with connection(self.env, False) as db:
                        db.execute("UPDATE sc.investigations SET status='failed',error=%s,updated_at=now() WHERE id=%s",
                            (exc.body['message'] if isinstance(exc, APIError) else '调查中断，请检查数据或重新调查；不会自动重复模型调用。', run_id))
                except Exception:
                    logging.getLogger(__name__).error('Investigation status could not be saved: %s', run_id)

    def recover(self):
        if not self.data.one('SELECT 1 FROM risk_monitor_configs LIMIT 1'):
            return
        try:
            self.ensure()
            spaces = [r['workspace_id'] for r in self.data.all('SELECT workspace_id FROM risk_monitor_configs')]
            with connection(self.env) as db:
                rows = db.execute("SELECT id FROM sc.investigations WHERE workspace_id=ANY(%s) AND status IN ('queued','running','resuming','awaiting_approval','replan_required')", (spaces,)).fetchall()
            for row in rows:
                InvestigationHarness(self.data, row['id'], lambda: None)
                InvestigationHarness.interrupt(self.data, row['id'])
                self.executor.submit(self.process, row['id'])
        except Exception:
            # No source DB must not prevent login/file import startup.
            pass

    def close(self):
        self.executor.shutdown(wait=True)
        self.checkpoint_db.close()

    def validate_plan(self, db, plan, run, user, approving=False):
        content = plan['content']
        factories = sorted({a['factory'] for a in content['actions']} | {a['fromFactory'] for a in content['actions'] if a['kind'] == 'transfer'})
        grant = self.permissions.require(user, run['workspace_id'], 'approve' if approving else 'view', factories, amount=content['totalCost'] if approving else None)
        if fingerprint(content) != plan['content_hash']:
            return '方案内容摘要不一致。'
        config = self.config_for(run['workspace_id'])
        snapshot = planning_snapshot(db, self.monitor, config)
        if (snapshot['asOf'] > timestamp(content['validUntil']) or datetime.now(timezone.utc) > plan['expires_at']):
            return '方案已超过有效期。'
        if fingerprint(snapshot) != content['snapshotHash'] or fingerprint({k: config[k] for k in ('as_of','horizon_days')}) != run['input']['configHash']:
            return '库存、需求、交期、规则或回放范围已变化，需要重新调查及审批。'
        if not content['feasible']:
            return '方案未通过确定性校验。'
        if approving:
            if user['account'] == run['requested_by']:
                raise APIError(403, '发起人与审批人必须不同。')
            if Decimal(content['totalCost']) > Decimal(grant['approval_limit']):
                raise APIError(403, '方案总费用超过审批额度。')
            for action in content['actions']:
                local = next((p for p in snapshot.get('localPolicies',[]) if p['action']==action['kind']),None)
                if local and (Decimal(action['quantity'])>Decimal(local['max_quantity']) or Decimal(action['cost'])>Decimal(local['max_cost'])):
                    raise APIError(403,'超过本工作空间配置的审批规则上限。')
                policies = [p for p in snapshot['tables']['approval_policies'] if p['action_type'] == action['kind'] and p['valid_from'] <= snapshot['asOf'] < p['valid_to']]
                if not policies:
                    raise APIError(403, '缺少适用的业务审批规则。')
                policy = max(policies, key=lambda p: p['version'])
                if policy['approver_role'] != 'manager':
                    raise APIError(403, '当前审批角色不匹配业务规则。')
                if Decimal(action['quantity']) > policy['max_quantity'] or Decimal(action['cost']) > policy['max_cost']:
                    raise APIError(403, '动作数量或费用超过业务审批规则上限。')
        return None

    def record_stage(self, state, stage):
        with self.data.lock:
            self.data.db.execute('INSERT OR REPLACE INTO investigation_stages VALUES(?,?,?)', (state['run_id'], stage, iso()))

    def read_run(self, user, run_id, workspace):
        self.ensure()
        with connection(self.env) as db:
            run = self.run_record(db, run_id)
            if run['workspace_id'] != workspace:
                raise APIError(404, '调查不在当前工作空间。')
            self.access(user, workspace, 'view', run['input']['factories'])
            for domain in ('inventory', 'purchase', 'production', 'quality', 'rules'):
                self.permissions.require(user, workspace, 'data.read', run['input']['factories'], domain)
            run['plans'] = db.execute('SELECT * FROM sc.candidate_plans WHERE run_id=%s ORDER BY version', (run_id,)).fetchall()
            for plan in run['plans']:
                plan['approval'] = db.execute('SELECT decision,comment FROM sc.plan_approvals WHERE plan_id=%s', (plan['id'],)).fetchone()
            run['stage'] = (self.data.one('SELECT stage FROM investigation_stages WHERE run_id=?', (run_id,)) or {}).get('stage', 'queued')
            child = db.execute('SELECT id FROM sc.investigations WHERE workspace_id=%s AND request_id=%s', (workspace, 'replan:' + run_id)).fetchone()
            run['nextRunId'] = child['id'] if child else None
        return plain(run)

    def from_calculation(self, user, workspace, calculation, selected, key, days, question=''):
        """Associate only a saved, revalidated result; never trust client risk IDs."""
        self.ensure()
        self.access(user, workspace, 'plan', [selected['factory']])
        config = self.config_for(workspace)
        if config['scenario_id'] != calculation['scenarioId'] or timestamp(config['as_of']) != timestamp(calculation['asOf']):
            raise APIError(409, '场景或证据截止时点已变化，请先重新计算。')
        with connection(self.env, False) as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (workspace,))
            snapshot = self.monitor.snapshot(db, config)
            from .shortage import calculate
            fresh = calculate(snapshot, days, selected['factory'], selected['material'])
            if fresh['snapshotHash'] != calculation['snapshotHash'] or fresh['windowEnd'] != calculation['windowEnd']:
                raise APIError(409, '原结果的数据或窗口已变化，请先重新计算。')
            item = next((r for r in fresh['results'] if r['revision'] == selected['revision']), None)
            if not item or not item['computable'] or Decimal(item['shortage']) <= 0:
                raise APIError(409, '所选对象没有可计算的缺料风险，请重新查询。')
            event = db.execute("SELECT * FROM sc.risk_events WHERE workspace_id=%s AND scenario_id=%s AND factory=%s AND material=%s AND revision=%s AND status<>'covered'", (workspace, config['scenario_id'], item['factory'], item['material'], item['revision'])).fetchone()
            if not event:
                scan_id = 'SCAN-' + uuid.uuid4().hex
                fresh['results'] = [item]
                db.execute('INSERT INTO sc.risk_scans(id,workspace_id,scenario_id,as_of,trigger,snapshot_hash,result) VALUES(%s,%s,%s,%s,%s,%s,%s)', (scan_id, workspace, config['scenario_id'], fresh['asOf'], 'chat', fresh['snapshotHash'], Jsonb(fresh)))
                self.monitor.reconcile(db, config, scan_id, fresh, 'chat', selected_only=True)
                event = db.execute("SELECT * FROM sc.risk_events WHERE workspace_id=%s AND factory=%s AND material=%s AND revision=%s AND status<>'covered'", (workspace, item['factory'], item['material'], item['revision'])).fetchone()
            active = db.execute("SELECT * FROM sc.investigations WHERE event_id=%s AND status IN ('queued','running','resuming','awaiting_approval','approved') ORDER BY created_at DESC LIMIT 1", (event['id'],)).fetchone()
            if active:
                self.access(user, workspace, 'view', active['input']['factories'])
                if active['input'].get('windowDays', config['horizon_days']) != days or active['input']['configHash'] != fingerprint({k: config[k] for k in ('as_of','horizon_days')}):
                    raise APIError(409, '已有调查使用不同窗口或快照，请先处理该调查。')
                # Reuse only plans whose saved snapshot is still valid.
                candidates = db.execute("SELECT * FROM sc.candidate_plans WHERE run_id=%s AND status IN ('candidate','approved')", (active['id'],)).fetchall()
                if any(self.validate_plan(db, p, active, user) for p in candidates):
                    db.execute("UPDATE sc.candidate_plans SET status='stale' WHERE run_id=%s AND status IN ('candidate','approved')", (active['id'],))
                    db.execute("UPDATE sc.investigations SET status='replan_required',error=%s WHERE id=%s", ('聊天试算发现数据变化或方案失效，重新规划。', active['id']))
                    # Commit before scheduling so the graph sees the transition.
                    db.commit()
                    self.executor.submit(self.process, active['id'])
                    return {'riskId': event['id'], 'runId': active['id'], 'reused': False}
                return {'riskId': event['id'], 'runId': active['id'], 'reused': True}
        run = self.start_investigation(user, event['id'], key, window_days=days, expected_snapshot=calculation['snapshotHash'], question=question)
        return {'riskId': event['id'], 'runId': run['id'], 'reused': False}

    def start_investigation(self, user, event_id, key, window_days=None, expected_snapshot=None, question=''):
        self.ensure()
        if not isinstance(key, str) or not 16 <= len(key) <= 100:
            raise APIError(400, '请提供唯一请求编号。')
        with connection(self.env, False) as db:
            event = self.event(db, event_id)
            grant = self.access(user, event['workspace_id'], 'plan', [event['factory']])
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (event_id,))
            old = db.execute('SELECT * FROM sc.investigations WHERE workspace_id=%s AND request_id=%s', (event['workspace_id'], key)).fetchone()
            if old:
                if old['event_id'] != event_id or old['requested_by'] != user['account']:
                    raise APIError(409, '请求编号已用于不同调查。')
                return plain(old)
            if db.execute("SELECT 1 FROM sc.investigations WHERE event_id=%s AND status IN ('queued','running','resuming','awaiting_approval')", (event_id,)).fetchone():
                raise APIError(409, '已有进行中或等待审批的调查，请先处理或撤回。')
            db.execute("SELECT pg_advisory_xact_lock(hashtext('scope-investigation-capacity'))")
            if db.execute("SELECT count(*) AS n FROM sc.investigations WHERE status IN ('queued','running','resuming')").fetchone()['n'] >= 20:
                raise APIError(429, '调查队列已满，请稍后重试。')
            config = self.config_for(event['workspace_id'])
            if config['scenario_id'] != event['scenario_id']:
                raise APIError(409, '风险场景与当前配置不一致。')
            if window_days is not None and (type(window_days) is not int or not 1 <= window_days <= 14):
                raise APIError(400, '计算窗口须为 1 至 14 天。')
            if expected_snapshot and fingerprint(self.monitor.snapshot(db, config)) != expected_snapshot:
                raise APIError(409, '数据已变化，请重新计算。')
            run_id = 'INV-' + uuid.uuid4().hex
            body = {'factories': grant['factories'], 'configHash': fingerprint({k: config[k] for k in ('as_of','horizon_days')})}
            if window_days is not None:
                body['windowDays'] = window_days
            if expected_snapshot:
                body['sourceSnapshotHash'] = expected_snapshot
            if question:
                if not isinstance(question, str) or len(question) > 2000:
                    raise APIError(400, '调查问题长度无效。')
                body['question'] = question
            # New investigations invalidate prior approvals for this event.
            db.execute("UPDATE sc.candidate_plans SET status='superseded' WHERE event_id=%s AND status IN ('candidate','approved')", (event_id,))
            db.execute("INSERT INTO sc.investigations(id,event_id,workspace_id,requested_by,request_id,status,input) VALUES(%s,%s,%s,%s,%s,'queued',%s)",
                (run_id,event_id,event['workspace_id'],user['account'],key,Jsonb(body)))
        self.executor.submit(self.process, run_id)
        return {'id': run_id, 'status': 'queued'}

    def access_info(self, user, eventId=''):
        if eventId:
            self.ensure()
            with connection(self.env) as db:
                event = self.event(db, eventId)
            workspace = event['workspace_id']
            grant = self.access(user, workspace, 'view', [event['factory']])
        else:
            workspace = self.monitor.config(user['id'])['workspace_id']
            grant = self.access(user, workspace, 'view')
        can_admin = self.permissions.can(user,workspace,'admin',['A','B'])
        members = []
        if can_admin:
            for m in self.data.all('SELECT account FROM business_members WHERE workspace_id=?',(workspace,)):
                member=self.permissions.member(workspace,m['account'])
                for g in member['roles']:
                    members.append({**g,'account':m['account'],'factories':json.dumps(g['factories'])})
        return {'workspaceId':workspace,'role':'admin' if can_admin else 'planner' if self.permissions.can(user,workspace,'plan') else grant['role'],
                'factories':grant['factories'],'approvalLimit':grant['approval_limit'],'members':members,
                'canPlan':self.permissions.can(user,workspace,'plan',[event['factory']] if eventId else [])}

    def set_member(self, user, b):
        workspace=b.get('workspaceId')
        self.permissions.require(user,workspace,'admin',['A','B'])
        old=self.permissions.member(workspace,b.get('account',''))
        if b.get('role') not in ('viewer','planner','approver','revoked'):
            raise APIError(400,'业务角色无效。')
        # Compatibility for existing clients, but never silently replace multiple roles.
        if old and len(old['roles'])>1:
            raise APIError(409,'该账号具有多个角色，请在系统设置的角色与权限页面修改。')
        self.permissions.save(user,workspace,{'account':b.get('account'),'version':old['version'] if old else 0,
            'enabled':b['role']!='revoked','roles':[] if b['role']=='revoked' else [{'role':b['role'],'factories':b.get('factories'),
            'domains':['inventory','purchase','production','quality','rules'],'approvalLimit':b.get('approvalLimit','0')}]})
        return {'saved':True}

    def runs(self, user, event_id):
        self.ensure()
        with connection(self.env) as db:
            event = self.event(db,event_id)
            self.access(user,event['workspace_id'],'view',[event['factory']])
            rows = db.execute('SELECT * FROM sc.investigations WHERE event_id=%s ORDER BY created_at DESC', (event_id,)).fetchall()
            # Reports may contain donor evidence. Require the captured scope.
            rows = [r for r in rows if set(r['input']['factories']).issubset(self.access(user,event['workspace_id'],'view')['factories'])]
            rows = [r for r in rows if all(self.permissions.can(user, r['workspace_id'], 'data.read',
                    r['input']['factories'], domain) for domain in ('inventory', 'purchase', 'production', 'quality', 'rules'))]
            for row in rows:
                row['plans'] = db.execute('SELECT * FROM sc.candidate_plans WHERE run_id=%s ORDER BY version', (row['id'],)).fetchall()
        return plain(rows)

    def tool_calls(self, user, run_id):
        self.ensure()
        with connection(self.env) as db:
            run = self.run_record(db, run_id)
        self.access(user, run['workspace_id'], 'view', run['input']['factories'])
        records = self.data.all('SELECT * FROM investigation_tool_calls WHERE run_id=? ORDER BY started_at,rowid', (run_id,))
        for record in records:
            scope = json.loads(record['scope_json'] or 'null')
            if scope:
                for domain, factories in scope['domains'].items():
                    if domain == 'reference':
                        continue
                    for factory in factories:
                        self.permissions.require(user, run['workspace_id'], 'data.read', [factory], domain)
            for field in ('args_json', 'scope_json', 'result_json', 'audit_json'):
                record[field.removesuffix('_json')] = json.loads(record.pop(field) or 'null')
        return records

    def cancel(self, user, run_id):
        self.ensure()
        # Serialize with graph transitions; cancellation never authorizes a resume.
        if not self.lock.acquire(blocking=False):
            raise APIError(409, '调查步骤运行中，请稍后撤回。')
        try:
          with connection(self.env, False) as db:
            run = self.run_record(db,run_id)
            self.access(user,run['workspace_id'],'plan',run['input']['factories'])
            if run['status'] not in ('queued','running','awaiting_approval','replan_required','failed','manual_required'):
                raise APIError(409, '该调查状态不能撤回。')
            db.execute("UPDATE sc.investigations SET status='cancelled',updated_at=now() WHERE id=%s", (run_id,))
            db.execute("UPDATE sc.candidate_plans SET status='superseded' WHERE run_id=%s AND status='candidate'", (run_id,))
        finally:
            self.lock.release()
        return {'status':'cancelled'}

    def approval_list(self, user):
        self.ensure()
        spaces = {r['workspace_id'] for r in self.data.all('SELECT workspace_id FROM risk_monitor_configs WHERE user_id=?', (user['id'],))}
        spaces.update(r['workspace_id'] for r in self.data.all("SELECT workspace_id FROM business_members WHERE account=? AND enabled=1", (user['account'],)))
        with connection(self.env) as db:
            rows = db.execute('''SELECT p.*,r.workspace_id,r.requested_by,r.input,e.factory,e.material FROM sc.candidate_plans p
                JOIN sc.investigations r ON r.id=p.run_id JOIN sc.risk_events e ON e.id=p.event_id
                WHERE r.workspace_id=ANY(%s) ORDER BY p.created_at DESC''', (list(spaces),)).fetchall()
        result=[]
        for row in rows:
            required = {a['factory'] for a in row['content']['actions']} | {a['fromFactory'] for a in row['content']['actions'] if a['kind']=='transfer'}
            try:
                self.access(user,row['workspace_id'],'view',required | {row['factory']} | set(row['input']['factories']))
                result.append(row)
            except APIError:
                continue
        return plain(result)

    def shared_events(self, user):
        self.ensure()
        grants = self.data.all("SELECT workspace_id FROM business_members WHERE account=? AND enabled=1", (user['account'],))
        result = []
        with connection(self.env) as db:
            for member in grants:
                grant = self.access(user, member['workspace_id'], 'view')
                result.extend(db.execute('SELECT id,factory,material,status FROM sc.risk_events WHERE workspace_id=%s AND factory=ANY(%s) ORDER BY updated_at DESC',
                                         (member['workspace_id'],grant['factories'])).fetchall())
        return plain(result)

    def plan_detail(self, user, plan_id):
        self.ensure()
        needs_replan = False
        with self.lock, connection(self.env, False) as db:
            plan = db.execute('SELECT * FROM sc.candidate_plans WHERE id=%s', (plan_id,)).fetchone()
            if not plan:
                raise APIError(404, '方案不存在。')
            run = self.run_record(db,plan['run_id'])
            event = self.event(db,run['event_id'])
            self.access(user,run['workspace_id'],'view',[event['factory']])
            self.access(user,run['workspace_id'],'view',run['input']['factories'])
            for domain in ('inventory', 'purchase', 'production', 'quality', 'rules'):
                self.permissions.require(user, run['workspace_id'], 'data.read', run['input']['factories'], domain)
            error = self.validate_plan(db,plan,run,user)
            if error and plan['status'] in ('candidate','approved'):
                db.execute("UPDATE sc.candidate_plans SET status='stale' WHERE run_id=%s AND status IN ('candidate','approved')", (run['id'],))
                db.execute("UPDATE sc.investigations SET status='replan_required',error=%s WHERE id=%s", (error,run['id']))
                plan['status']='stale'
                needs_replan = True
            approval = db.execute('SELECT * FROM sc.plan_approvals WHERE plan_id=%s', (plan_id,)).fetchone()
            grant=self.access(user,run['workspace_id'],'view')
        if needs_replan:
            self.executor.submit(self.process, run['id'])
        return plain({**plan,'requestedBy':run['requested_by'],'validationError':error,'approval':approval,
                      'canApprove':self.permissions.can(user,run['workspace_id'],'approve',run['input']['factories'],amount=plan['content']['totalCost']) and user['account']!=run['requested_by'], 'report':run['report']})

    def decide(self, user, plan_id, b):
        self.ensure()
        if b.get('decision') not in ('approved','rejected') or not isinstance(b.get('comment'),str) or not 1<=len(b['comment'].strip())<=1000:
            raise APIError(400,'请选择批准或驳回，并填写审批意见。')
        failure=None
        with self.lock, connection(self.env,False) as db:
            plan=db.execute('SELECT * FROM sc.candidate_plans WHERE id=%s FOR UPDATE',(plan_id,)).fetchone()
            if not plan:
                raise APIError(404,'方案不存在。')
            run=self.run_record(db,plan['run_id'])
            self.access(user,run['workspace_id'],'approve',run['input']['factories'])
            if user['account']==run['requested_by']:
                raise APIError(403,'发起人与审批人必须不同。')
            if b.get('version')!=plan['version'] or b.get('contentHash')!=plan['content_hash']:
                raise APIError(409,'审批版本或摘要不匹配，请重新核对。')
            old=db.execute('SELECT * FROM sc.plan_approvals WHERE plan_id=%s',(plan_id,)).fetchone()
            if old:
                if old['approver']==user['account'] and old['decision']==b['decision'] and old['comment']==b['comment'].strip():
                    return plain(old)
                raise APIError(409,'该方案已有审批决定。')
            if run['status']!='awaiting_approval' or plan['status']!='candidate':
                raise APIError(409,'方案不在可审批状态。')
            if b['decision']=='approved':
                failure=self.validate_plan(db,plan,run,user,True)
            if failure:
                db.execute("UPDATE sc.candidate_plans SET status='stale' WHERE run_id=%s AND status='candidate'",(run['id'],))
                db.execute("UPDATE sc.investigations SET status='replan_required',error=%s WHERE id=%s",(failure,run['id']))
            else:
                approval_id='APR-'+uuid.uuid4().hex
                db.execute('INSERT INTO sc.plan_approvals(id,plan_id,plan_version,content_hash,approver,decision,comment,scope) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                    (approval_id,plan_id,plan['version'],plan['content_hash'],user['account'],b['decision'],b['comment'].strip(),Jsonb({'factories':run['input']['factories'],'totalCost':plan['content']['totalCost']})))
                db.execute('UPDATE sc.candidate_plans SET status=%s WHERE id=%s',(b['decision'],plan_id))
                db.execute("UPDATE sc.candidate_plans SET status='superseded' WHERE run_id=%s AND id<>%s AND status='candidate'",(run['id'],plan_id))
                db.execute("UPDATE sc.investigations SET status='resuming' WHERE id=%s",(run['id'],))
        if failure:
            self.executor.submit(self.process,run['id'])
            raise APIError(409,failure)
        self.executor.submit(self.process,run['id'])
        return {'id':approval_id,'decision':b['decision']}

    def routes(self):
        """Compatibility entry point; HTTP routes are defined separately."""
        from .investigation_routes import routes
        return routes(self)
