"""持久化物流会话与串行消息处理；单应用进程，复用既有业务服务。"""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
import logging
import re
import threading
import time
import uuid

from fastapi import APIRouter, Request

from project.agents.logistics_chat import validate, explicit_submit, constraint_parameters
from project.agents.logistics_protocol import ProtocolError
from project.context.logistics_chat import build_messages
from .common import APIError, iso, reply
from .logistics_access import WORKSPACE, REGIONS
from .logistics_chat_model import ChatModel
from .logistics_repository import reference_time
from .logistics_model import compact_evidence
from .logistics_snapshots import canonical, content_hash


SCHEMA = '''
CREATE TABLE IF NOT EXISTS logistics_conversations (
 conversation_id TEXT PRIMARY KEY, creator_id INTEGER NOT NULL, creator_account TEXT NOT NULL,
 title TEXT NOT NULL, context_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS logistics_messages (
 message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES logistics_conversations(conversation_id),
 seq INTEGER NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL,
 request_key TEXT NOT NULL, request_hash TEXT NOT NULL, context_json TEXT NOT NULL,
 result_json TEXT, created_at TEXT NOT NULL, claim_token TEXT, lease_until REAL,
 UNIQUE(conversation_id,seq), UNIQUE(conversation_id,request_key)
);
CREATE TABLE IF NOT EXISTS logistics_chat_operations (
 message_id TEXT PRIMARY KEY REFERENCES logistics_messages(message_id), stage TEXT NOT NULL,
 input_json TEXT NOT NULL, action_json TEXT, result_json TEXT,
 budget_day TEXT, reserved_tokens INTEGER NOT NULL DEFAULT 0, tokens INTEGER,
 elapsed_seconds REAL, error_code TEXT, prompt_version TEXT
);
CREATE TABLE IF NOT EXISTS logistics_conversation_runs (
 conversation_id TEXT NOT NULL REFERENCES logistics_conversations(conversation_id),
 message_id TEXT NOT NULL UNIQUE REFERENCES logistics_messages(message_id),
 run_id TEXT NOT NULL REFERENCES logistics_runs(run_id), snapshot_id TEXT NOT NULL,
 delivered INTEGER NOT NULL DEFAULT 0, event_version INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(conversation_id,run_id)
);
CREATE INDEX IF NOT EXISTS logistics_messages_queue ON logistics_messages(status,lease_until);
'''
ERRORS = {'model_not_configured': '聊天模型尚未配置。可查看已有调查和报告，当前消息未执行。',
          'model_transport_failed': '模型请求失败或结果未知，本次没有自动重发。',
          'interrupted_operation': '模型请求在服务中断时结果未知，没有自动重放；本次操作未执行。',
          'chat_account_budget': '今日对话额度已用完。已有报告仍可直接阅读和下载。',
          'context_budget': '本轮上下文超过处理限额，请缩小讨论对象。'}


def chat_time(value):
    """完整参考日期已有时，分钟精度只补零秒；不推断日期或真实年份。"""
    if type(value) is not str:
        raise APIError(400, '请提供完整参考日期与时点。')
    match = re.fullmatch(r'(2000-\d{2}-\d{2})[ T](\d{2}:\d{2})(:\d{2})?', value)
    if not match:
        raise APIError(400, '请提供2000参考年的完整日期与时点。')
    normalized = match[1]+' '+match[2]+(match[3] or ':00')
    reference_time(normalized, 'business_as_of', business=True)
    return normalized


def chat_regions(values):
    if type(values) is not list or not 1 <= len(values) <= 30:
        raise APIError(400, '请提供具体授权区域。')
    normalized = []
    for value in values:
        match = re.fullmatch(r'(?:区域\s*)?([0-9]+)', value) if type(value) is str else None
        if not match or match[1] not in REGIONS:
            raise APIError(400, '区域无效。')
        normalized.append(match[1])
    return sorted(set(normalized))


class ChatService:
    lease_seconds = 60
    daily_calls = 200
    daily_tokens = 300000

    def __init__(self, collaboration, env):
        self.collaboration, self.runs = collaboration, collaboration.runs
        self.data, self.access, self.snapshots = self.runs.data, self.runs.access, self.runs.snapshots
        self.reports = collaboration.reports
        self.model = ChatModel(env)
        self.knowledge = None
        self.stop, self.wake = threading.Event(), threading.Event()
        self.worker = self.heartbeat = None
        self.active = None
        with self.data.lock:
            self.data.db.executescript(SCHEMA)
            if 'diagnostic_json' not in {r['name'] for r in self.data.all('PRAGMA table_info(logistics_chat_operations)')}:
                self.data.db.execute('ALTER TABLE logistics_chat_operations ADD COLUMN diagnostic_json TEXT')

    def user(self, row):
        user = self.access.auth.query('SELECT * FROM users WHERE id=? AND account=?', (row['creator_id'], row['creator_account']))
        if not user:
            raise APIError(403, '会话账号已失效。')
        self.access.require(user, 'investigate')
        return user

    def owned(self, user, identifier):
        row = self.data.one('SELECT * FROM logistics_conversations WHERE conversation_id=?', (identifier,))
        if not row:
            raise APIError(404, '会话不存在。')
        if row['creator_id'] != user['id'] or row['creator_account'] != user['account']:
            raise APIError(403, '只能访问自己的会话。')
        self.access.require(user, 'view')
        return row

    def authorize_context(self, user, context):
        self.access.require(user, 'view')
        regions = context.get('region_ids', [])
        if regions and not set(regions).issubset(self.access.allowed_regions(user, 'view')):
            raise APIError(403, '会话关联范围已撤权。')
        if context.get('run_id'):
            metadata = self.runs.authorize(user, self.runs.saved(context['run_id']), owner=True)
            for field in ('region_ids', 'business_as_of', 'snapshot_id', 'batch_id'):
                if field in context and context[field] is not None and context[field] != metadata[field]:
                    raise APIError(409, '会话范围与关联调查不一致。')
        elif context.get('snapshot_id'):
            self.snapshots.ready_metadata(user, context['snapshot_id'])
        for identifier in context.get('knowledge_ids', []):
            if not self.knowledge:
                raise APIError(409, '成果引用服务尚未就绪。')
            self.knowledge.read(user, identifier)

    def create(self, user, body):
        if body:
            raise APIError(400, '新会话不接受其他参数。')
        self.access.require(user, 'investigate')
        identifier, now = 'LCONV-'+uuid.uuid4().hex, iso()
        with self.runs.transaction():
            self.data.db.execute('INSERT INTO logistics_conversations VALUES(?,?,?,?,?,0,?,?)',
                (identifier, user['id'], user['account'], '物流对话', canonical({}), now, now))
        return self.read(user, identifier)

    def read(self, user, identifier):
        with self.data.lock:
            row = self.owned(user, identifier)
            context = json.loads(row['context_json'])
            try:
                self.authorize_context(user, context)
                restricted = False
            except APIError:
                context, restricted = {}, True
            return {'conversation_id': identifier, 'title': row['title'], 'context': context,
                    'version': row['version'], 'restricted': restricted, 'created_at': row['created_at'],
                    'updated_at': row['updated_at'], 'status': self.runs.saved(context['run_id'])['status'] if context.get('run_id') else 'new'}

    def conversation_for_run(self, user, run_id):
        run = self.runs.saved(run_id)
        self.runs.authorize(user, run)
        if run['creator_id'] != user['id'] or run['creator_account'] != user['account']:
            return None
        link = self.data.one('''SELECT c.conversation_id FROM logistics_conversation_runs r
            JOIN logistics_conversations c USING(conversation_id) WHERE r.run_id=?
            AND c.creator_id=? AND c.creator_account=? ORDER BY c.updated_at DESC LIMIT 1''',
            (run_id, user['id'], user['account']))
        return link['conversation_id'] if link else None

    def open_run(self, user, run_id, body):
        if body:
            raise APIError(400, '关联调查不接受其他参数。')
        with self.runs.transaction():
            run = self.runs.saved(run_id)
            metadata = self.runs.authorize(user, run, owner=True)
            self.access.require(user, 'investigate', metadata['region_ids'])
            identifier = self.conversation_for_run(user, run_id)
            if not identifier:
                identifier, now = 'LCONV-'+uuid.uuid4().hex, iso()
                context = {key: metadata[key] for key in ('region_ids', 'business_as_of', 'snapshot_id', 'batch_id')}
                context.update(run_id=run_id, mode=run['mode'])
                report = self.saved_report(user, run_id)
                if report and report.get('report_id'):
                    context.update(report_id=report['report_id'], available_task_ids=[task['order_id'] for task in report['focus_tasks']],
                                   task_ids=[task['order_id'] for task in report['focus_tasks'][:2]])
                self.data.db.execute('INSERT INTO logistics_conversations VALUES(?,?,?,?,?,0,?,?)',
                    (identifier, user['id'], user['account'], run['question'] or '既有调查 · 区域 '+ '、'.join(metadata['region_ids']), canonical(context), now, now))
                message = self.append(identifier, 'assistant', '已关联原调查，正在读取已保存结果；不会重新执行。',
                                      'waiting', 'adopt:'+run_id, context, {'run_id': run_id})
                self.data.db.execute('INSERT INTO logistics_conversation_runs(conversation_id,message_id,run_id,snapshot_id) VALUES(?,?,?,?)',
                                     (identifier, message['message_id'], run_id, run['snapshot_id']))
            return {'conversation_id': identifier, 'run_id': run_id, 'path': f'/assistant/{identifier}?run={run_id}'}

    def migrate_pending(self):
        """启动时幂等承接无会话且仍需处理的既有调查，不触发执行。"""
        for run in self.data.all('SELECT * FROM logistics_runs'):
            try:
                user = self.access.auth.query('SELECT * FROM users WHERE id=? AND account=?', (run['creator_id'], run['creator_account']))
                if not user:
                    continue
                state = self.collaboration.state(run['run_id'])
                needs_processing = run['status'] in ('queued', 'running') or state['phase'] in (
                    'changes_requested', 'draft', 'pending_approval', 'approved', 'followup', 'receipt')
                if needs_processing and not self.conversation_for_run(user, run['run_id']):
                    self.open_run(user, run['run_id'], {})
            except APIError:
                # 无权或快照暂不可用的原调查保留，恢复权限后可通过原链接承接。
                continue

    def reference(self, user, identifier, body):
        if type(body) is not dict or set(body) != {'knowledge_id', 'context_version'} or type(body['knowledge_id']) is not str:
            raise APIError(400, '成果引用参数无效。')
        with self.runs.transaction():
            row = self.owned(user, identifier)
            self.access.require(user, 'investigate')
            context = json.loads(row['context_json'])
            self.authorize_context(user, context)
            value = self.knowledge.read(user, body['knowledge_id'])
            refs = context.get('knowledge_ids', [])
            if body['knowledge_id'] in refs:
                return self.read(user, identifier)
            if type(body['context_version']) is not int or body['context_version'] != row['version']:
                raise APIError(409, '会话版本已变化，请刷新后引用。')
            if self.data.one("SELECT message_id FROM logistics_messages WHERE conversation_id=? AND status IN ('saved','processing')", (identifier,)):
                raise APIError(409, '请等待当前消息完成后引用成果。')
            context['knowledge_ids'] = [*refs, body['knowledge_id']]
            self.data.db.execute('UPDATE logistics_conversations SET context_json=?,version=version+1 WHERE conversation_id=?',
                                 (canonical(context), identifier))
            self.append(identifier, 'assistant', '已引用历史成果，结论以原报告参考时点为准，尚未发起新调查。',
                        'replied', 'knowledge:'+body['knowledge_id'], context, {'knowledge_reference': self.knowledge.brief(value)})
            return self.read(user, identifier)

    def message(self, user, row):
        context = json.loads(row['context_json'])
        try:
            self.authorize_context(user, context)
        except APIError:
            return {'message_id': row['message_id'], 'seq': row['seq'], 'role': row['role'],
                    'text': '关联内容当前无权查看。', 'status': 'restricted', 'result': None}
        return {'message_id': row['message_id'], 'seq': row['seq'], 'role': row['role'],
                'text': row['text'], 'status': row['status'], 'created_at': row['created_at'],
                'result': json.loads(row['result_json']) if row['result_json'] else None}

    def messages(self, user, identifier, after=0):
        if type(after) is not int or not 0 <= after <= 1000000:
            raise APIError(400, '消息游标无效。')
        with self.data.lock:
            self.owned(user, identifier)
            rows = self.data.all('SELECT * FROM logistics_messages WHERE conversation_id=? AND seq>? ORDER BY seq LIMIT 100', (identifier, after))
            values = [self.message(user, r) for r in rows]
            return {'rows': values, 'after': rows[-1]['seq'] if rows else after, 'has_more': len(rows) == 100}

    def append(self, identifier, role, text, status, key, context, result=None, request_hash=''):
        existing = self.data.one('SELECT * FROM logistics_messages WHERE conversation_id=? AND request_key=?', (identifier, key))
        if existing:
            return existing
        message_id = 'LMSG-'+uuid.uuid4().hex
        seq = self.data.one('SELECT coalesce(max(seq),0)+1 n FROM logistics_messages WHERE conversation_id=?', (identifier,))['n']
        self.data.db.execute('''INSERT INTO logistics_messages
            (message_id,conversation_id,seq,role,text,status,request_key,request_hash,context_json,result_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (message_id, identifier, seq, role, text, status, key, request_hash, canonical(context), canonical(result) if result else None, iso()))
        self.data.db.execute('UPDATE logistics_conversations SET updated_at=? WHERE conversation_id=?', (iso(), identifier))
        return self.data.one('SELECT * FROM logistics_messages WHERE message_id=?', (message_id,))

    def send(self, user, identifier, body):
        if type(body) is not dict or set(body) - {'text', 'request_id', 'context_version', 'selection', 'command'}:
            raise APIError(400, '消息参数无效。')
        text, key = body.get('text'), body.get('request_id')
        if type(text) is not str or not 0 < len(text.strip()) <= 2000 or type(key) is not str or not 0 < len(key) <= 100:
            raise APIError(400, '消息或请求标识无效。')
        selection = body.get('selection', {})
        if type(selection) is not dict or set(selection) - {'region_ids', 'business_as_of', 'mode', 'task_ids', 'run_id'}:
            raise APIError(400, '选择范围无效。')
        regions = selection.get('region_ids')
        if regions is not None and (type(regions) is not list or not regions or len(regions) > 30 or any(type(r) is not str or r not in REGIONS for r in regions)):
            raise APIError(400, '区域无效。')
        if regions:
            self.access.require(user, 'investigate', regions)
        if selection.get('business_as_of'):
            chat_time(selection['business_as_of'])
        if selection.get('mode', 'deterministic') not in ('deterministic', 'model'):
            raise APIError(400, '调查模式无效。')
        command = body.get('command')
        if command is not None and (type(command) is not dict or set(command) != {'action', 'draft_version', 'draft_hash'} or command['action'] != 'submit' or type(command['draft_version']) is not int or type(command['draft_hash']) is not str):
            raise APIError(400, '提交操作无效。')
        fingerprint = content_hash(body)
        with self.runs.transaction():
            conversation = self.owned(user, identifier)
            self.access.require(user, 'investigate')
            previous = self.data.one('SELECT * FROM logistics_messages WHERE conversation_id=? AND request_key=?', (identifier, 'user:'+key))
            if previous:
                if previous['request_hash'] != fingerprint:
                    raise APIError(409, '请求标识已用于不同消息。')
                return self.message(user, previous)
            if type(body.get('context_version')) is not int or body['context_version'] != conversation['version']:
                raise APIError(409, '会话对象已变化，请刷新核对后发送。')
            context = json.loads(conversation['context_json'])
            self.authorize_context(user, context)
            if selection.get('run_id'):
                run_id = selection['run_id']
                if type(run_id) is not str or len(run_id) > 100 or not self.data.one('SELECT run_id FROM logistics_conversation_runs WHERE conversation_id=? AND run_id=?', (identifier, run_id)):
                    raise APIError(403, '所选报告不属于当前会话。')
                saved = self.data.one("SELECT context_json FROM logistics_messages WHERE conversation_id=? AND json_extract(context_json,'$.run_id')=? ORDER BY seq DESC LIMIT 1", (identifier, run_id))
                context = {**json.loads(saved['context_json']),
                           **({'knowledge_ids': context['knowledge_ids']} if context.get('knowledge_ids') else {})}
                self.authorize_context(user, context)
                report = self.saved_report(user, run_id)
                if not report:
                    raise APIError(409, '所选调查尚无可讨论的报告。')
            if 'task_ids' in selection:
                ids = selection['task_ids']
                if type(ids) is not list or len(ids) > 20 or any(type(t) is not str or t not in context.get('available_task_ids', []) for t in ids) or len(set(ids)) != len(ids):
                    raise APIError(400, '选择的任务不属于当前报告。')
                context['task_ids'] = ids
            queue = self.data.one("SELECT count(*) n FROM logistics_messages WHERE conversation_id=? AND status IN ('saved','processing')", (identifier,))['n']
            if queue >= 20:
                raise APIError(409, '待处理消息过多，请等待当前回复。')
            row = self.append(identifier, 'user', text.strip(), 'saved', 'user:'+key, context, request_hash=fingerprint)
            self.data.db.execute('INSERT INTO logistics_chat_operations(message_id,stage,input_json) VALUES(?,?,?)',
                (row['message_id'], 'pending', canonical({'context': context, 'selection': selection, 'command': command,
                    'context_version': conversation['version']})))
        self.wake.set()
        return self.message(user, row)

    def claim(self):
        with self.runs.transaction():
            row = self.data.one("SELECT * FROM logistics_messages WHERE role='user' AND (status='saved' OR (status='processing' AND lease_until<=?)) ORDER BY created_at,seq LIMIT 1", (time.time(),))
            if not row:
                return None
            token = uuid.uuid4().hex
            self.data.db.execute("UPDATE logistics_messages SET status='processing',claim_token=?,lease_until=? WHERE message_id=?", (token, time.time()+self.lease_seconds, row['message_id']))
            return dict(row, claim_token=token)

    def check_claim(self, row):
        saved = self.data.one('SELECT * FROM logistics_messages WHERE message_id=?', (row['message_id'],))
        if saved['status'] != 'processing' or saved['claim_token'] != row['claim_token'] or saved['lease_until'] <= time.time():
            raise APIError(409, '消息处理凭证已失效。')
        return saved

    def reserve(self, row, user, estimate):
        with self.runs.transaction():
            self.check_claim(row)
            self.user(self.owned(user, row['conversation_id']))
            self.authorize_context(user, json.loads(row['context_json']))
            day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
            used = self.data.one('''SELECT count(*) calls,coalesce(sum(coalesce(o.tokens,o.reserved_tokens)),0) tokens
                FROM logistics_chat_operations o JOIN logistics_messages m USING(message_id)
                JOIN logistics_conversations c USING(conversation_id) WHERE c.creator_id=? AND o.budget_day=?''', (user['id'], day))
            if used['calls'] >= self.daily_calls or used['tokens']+estimate > self.daily_tokens:
                raise ProtocolError('chat_account_budget')
            self.data.db.execute("UPDATE logistics_chat_operations SET stage='model_inflight',budget_day=?,reserved_tokens=? WHERE message_id=?", (day, estimate, row['message_id']))

    def usage(self, row, tokens, seconds, error, diagnostic=None):
        with self.data.lock:
            self.data.db.execute('UPDATE logistics_chat_operations SET tokens=?,elapsed_seconds=?,error_code=?,diagnostic_json=? WHERE message_id=?', (tokens, seconds, error, canonical(diagnostic) if diagnostic else None, row['message_id']))

    def mutate_once(self, user, row, run_id, action, body):
        # 先保存原版本与参数，恢复不能拿最新版本构造第二次写入。
        with self.runs.transaction():
            self.check_claim(row)
            operation = self.data.one('SELECT input_json FROM logistics_chat_operations WHERE message_id=?', (row['message_id'],))
            frozen = json.loads(operation['input_json'])
            saved = frozen.get('business_operation')
            if saved is None:
                saved = {'run_id': run_id, 'action': action, 'body': body}
                frozen['business_operation'] = saved
                self.data.db.execute('UPDATE logistics_chat_operations SET input_json=? WHERE message_id=?', (canonical(frozen), row['message_id']))
            elif saved['run_id'] != run_id or saved['action'] != action:
                raise APIError(409, '消息业务关联已变化。')
        return self.collaboration.mutate(user, run_id, action, saved['body'], operation_id='chat:'+row['message_id'])

    def attach_feedback(self, user, run_id, context):
        """将调查期间保存的上下文反馈绑定到已就绪的方案依据。"""
        for kind, name in [('reported', 'facts'), ('constraint', 'constraints')]:
            for item in context.get(name, []):
                with self.data.lock:
                    saved = self.data.one('SELECT operation_id FROM logistics_collaboration_operations WHERE run_id=? AND actor_id=? AND operation_id=?',
                                          (run_id, user['id'], item['id']))
                if saved:
                    continue
                state = self.collaboration.read(user, run_id)
                self.collaboration.mutate(user, run_id, 'chat_feedback', {'version': state['version'], 'kind': kind,
                    'text': item['text'], 'task_ids': item['task_ids'], 'occurred_at': item['occurred_at'], 'source': item['source'],
                    'constraint_key': item['constraint_key'], 'active': item['active']}, operation_id=item['id'])

    def inputs(self, user, identifier, context):
        self.authorize_context(user, context)
        report, evidence, collaboration = None, {}, None
        run = context.get('run_id')
        if run:
            collaboration = self.collaboration.read(user, run)
            report = self.saved_report(user, run)
            if report and report.get('report_id'):
                for task in report['focus_tasks']:
                    if task['order_id'] in context.get('task_ids', []):
                        evidence[task['evidence_id']] = self.runs.workflow.tools.evidence_store.read(user, run, task['evidence_id'])
                for field in ('summary_evidence_id', 'tasks_evidence_id', 'quality_evidence_id'):
                    if report.get(field):
                        ref = report[field]
                        value = self.runs.workflow.tools.evidence_store.read(user, run, ref)
                        if field == 'tasks_evidence_id':
                            value = deepcopy(value)
                            record = value['record']
                            record['rows'] = record.get('rows', [])[:3]
                            record['context_truncated'] = True
                        evidence[ref] = value
                report = {k: report[k] for k in ('report_id', 'scope', 'summary', 'limitations', 'focus_tasks') if k in report}
                report['focus_tasks'] = report.get('focus_tasks', [])[:20]
            elif report:
                for ref in report.get('value', {}).get('evidence_ids', [])[:20]:
                    evidence[ref] = self.runs.workflow.tools.evidence_store.read(user, run, ref)
                evidence = compact_evidence(evidence, scope=report['scope'])
            if self.runs.saved(run)['mode'] == 'model':
                investigation = self.runs.workflow.read(user, run)['state']
                saved_value = (investigation.get('report') or {}).get('value', {})
                counterevidence = []
                for observation in investigation.get('observations', []):
                    for fact in observation.get('value', {}).get('counterevidence', []):
                        if fact not in counterevidence:
                            counterevidence.append(fact)
                            for ref in fact.get('evidence_ids', []):
                                if ref not in evidence:
                                    record = self.runs.workflow.tools.evidence_store.read(user, run, ref)
                                    evidence.update(compact_evidence({ref: record}))
                if report:
                    report = deepcopy(report)
                    report['investigation_history'] = {'source': 'saved_model_analysis', 'stop_reason': investigation.get('stop_reason'),
                        'limitations': saved_value.get('limitations', []), 'unresolved_questions': saved_value.get('unresolved_questions', []),
                        'counterevidence': counterevidence}
        references = []
        for identifier_ref in context.get('knowledge_ids', []):
            value = self.knowledge.read(user, identifier_ref)
            references.append({**self.knowledge.brief(value), 'model_report': value['model_report'],
                               'summary': (value['report'] or {}).get('summary'), 'unresolved_questions': value['unresolved_questions']})
            for ref in value['evidence_ids'][:20]:
                record = self.runs.workflow.tools.evidence_store.read(user, value['run_id'], ref)
                evidence.update(compact_evidence({ref: record}))
        if references:
            report = {**(report or {}), 'historical_references': references}
        recent = [self.message(user, r) for r in reversed(self.data.all('SELECT * FROM logistics_messages WHERE conversation_id=? ORDER BY seq DESC LIMIT 8', (identifier,)))]
        recent = [{'role': r['role'], 'text': r['text']} for r in recent if r['status'] != 'restricted']
        return recent, report, evidence, collaboration

    def saved_report(self, user, run_id):
        self.runs.authorize(user, self.runs.saved(run_id), owner=True)
        if self.data.one('SELECT report_id FROM logistics_reports WHERE run_id=?', (run_id,)):
            return self.reports.read(user, run_id)
        state = self.runs.workflow.read(user, run_id)['state']
        return state.get('report')

    def process(self, row):
        try:
            conversation = self.data.one('SELECT * FROM logistics_conversations WHERE conversation_id=?', (row['conversation_id'],))
            user = self.user(conversation)
            operation = self.data.one('SELECT * FROM logistics_chat_operations WHERE message_id=?', (row['message_id'],))
            frozen = json.loads(operation['input_json'])
            context = frozen['context']
            self.authorize_context(user, context)
            if operation['stage'] == 'model_inflight':
                raise ProtocolError('interrupted_operation')
            if operation['stage'] == 'pending':
                recent, report, evidence, collaboration = self.inputs(user, row['conversation_id'], context)
                restriction = constraint_parameters(row['text'], {})
                if frozen['command']:
                    candidate = {'action': 'submit', 'params': {'draft_version': frozen['command']['draft_version']}, 'reply': '', 'evidence_ids': []}
                elif explicit_submit(row['text']):
                    candidate = {'action': 'submit', 'params': {'draft_version': context['draft_version']} if context.get('draft_version') else {}, 'reply': '', 'evidence_ids': []}
                elif restriction.get('constraint_key') == 'contact_courier':
                    candidate = {'action': 'constraint', 'params': dict(restriction, note=row['text'], task_ids=context.get('task_ids', [])), 'reply': '', 'evidence_ids': []}
                else:
                    model_context = dict(context, selection=frozen['selection'], allowed_region_ids=self.access.allowed_regions(user, 'investigate'))
                    messages, version = build_messages(row['text'], model_context, recent, report, evidence, collaboration)
                    with self.data.lock:
                        self.data.db.execute('UPDATE logistics_chat_operations SET prompt_version=? WHERE message_id=?', (version, row['message_id']))
                    candidate = self.model.generate(messages, evidence.keys(), lambda estimate: self.reserve(row, user, estimate),
                                                    lambda tokens, seconds, error, diagnostic=None: self.usage(row, tokens, seconds, error, diagnostic))
                validate(candidate, evidence.keys())
                with self.runs.transaction():
                    self.check_claim(row)
                    self.authorize_context(user, context)
                    self.data.db.execute("UPDATE logistics_chat_operations SET stage='action_ready',action_json=? WHERE message_id=?", (canonical(candidate), row['message_id']))
                operation = self.data.one('SELECT * FROM logistics_chat_operations WHERE message_id=?', (row['message_id'],))
            if operation['stage'] == 'action_ready':
                with self.data.lock:
                    self.check_claim(row)
                candidate = json.loads(operation['action_json'])
                result, new_context = self.dispatch(user, row, frozen, candidate)
                with self.runs.transaction():
                    self.check_claim(row)
                    self.authorize_context(user, new_context)
                    self.data.db.execute("UPDATE logistics_chat_operations SET stage='dispatched',result_json=? WHERE message_id=?", (canonical({'result': result, 'context': new_context}), row['message_id']))
                operation = self.data.one('SELECT * FROM logistics_chat_operations WHERE message_id=?', (row['message_id'],))
            output = json.loads(operation['result_json'])
            with self.runs.transaction():
                self.check_claim(row)
                self.user(conversation)
                self.authorize_context(user, output['context'])
                current = self.owned(user, row['conversation_id'])
                # 回复引用固定对象；并发的新范围不能被迟到结果覆盖。
                if current['version'] == frozen['context_version']:
                    self.data.db.execute('UPDATE logistics_conversations SET context_json=?,version=version+1 WHERE conversation_id=?', (canonical(output['context']), row['conversation_id']))
                result = output['result']
                status = 'waiting' if result.get('waiting') else 'clarification' if result.get('clarify') else 'replied'
                self.append(row['conversation_id'], 'assistant', result['text'], status, 'reply:'+row['message_id'], output['context'], result)
                self.data.db.execute('UPDATE logistics_messages SET status=?,context_json=?,claim_token=NULL,lease_until=NULL WHERE message_id=?', (status, canonical(output['context']), row['message_id']))
                self.data.db.execute("UPDATE logistics_chat_operations SET stage='done' WHERE message_id=?", (row['message_id'],))
        except (APIError, ProtocolError) as exc:
            code = exc.code if isinstance(exc, ProtocolError) else 'business_rejected'
            message = ERRORS.get(code, '模型输出未通过校验，本次操作未执行。') if isinstance(exc, ProtocolError) else exc.body.get('message', '操作未执行，请核对范围与版本。')
            self.fail(row, code, message)
        except Exception:
            logging.getLogger(__name__).error('物流消息处理失败，保留操作阶段与已落盘结果。')
            self.fail(row, 'processing_failed', '消息处理失败，已保存的业务结果保留。请查看运行状态后再继续。')

    def fail(self, row, code, message):
        with self.runs.transaction():
            try:
                self.check_claim(row)
            except APIError:
                return
            self.append(row['conversation_id'], 'assistant', message, 'failed', 'reply:'+row['message_id'], json.loads(row['context_json']))
            self.data.db.execute("UPDATE logistics_messages SET status='failed',claim_token=NULL,lease_until=NULL WHERE message_id=?", (row['message_id'],))
            self.data.db.execute("UPDATE logistics_chat_operations SET error_code=? WHERE message_id=?", (code, row['message_id']))

    def dispatch(self, user, row, frozen, candidate):
        context, selection = deepcopy(frozen['context']), frozen['selection']
        action, params = candidate['action'], candidate['params']
        self.authorize_context(user, context)
        self.access.require(user, 'investigate')
        if action not in ('clarify', 'investigate'):
            if (selection.get('region_ids') and chat_regions(selection['region_ids']) != context.get('region_ids')) or (
                    selection.get('business_as_of') and chat_time(selection['business_as_of']) != context.get('business_as_of')):
                return {'text': '明确选择的范围与当前报告不同，请先确认是否发起该范围的新调查。', 'clarify': True}, context
        if action in ('fact', 'constraint', 'draft', 'submit') and not frozen.get('business_operation'):
            current = self.owned(user, row['conversation_id'])
            if current['version'] != frozen['context_version']:
                raise APIError(409, '讨论对象或版本已变化，本次未修改；请核对最新对象后继续。')
        operation_id = 'chat:'+row['message_id']
        if action in ('clarify', 'investigate'):
            params = deepcopy(params)
            selection = deepcopy(selection)
            for values in (params, selection):
                if values.get('region_ids'):
                    values['region_ids'] = chat_regions(values['region_ids'])
                if values.get('business_as_of'):
                    values['business_as_of'] = chat_time(values['business_as_of'])
            regions = params.get('region_ids') or selection.get('region_ids') or context.get('region_ids')
            moment = params.get('business_as_of') or selection.get('business_as_of') or context.get('business_as_of')
            for key in ('region_ids', 'business_as_of'):
                if params.get(key) and selection.get(key) and params[key] != selection[key]:
                    return {'text': '消息与明确选择的范围冲突，请确定区域和完整参考时点。', 'clarify': True}, context
            if not regions or not moment or action == 'clarify':
                pending = {k: v for k, v in [('region_ids', regions), ('business_as_of', moment)] if v}
                if regions:
                    if type(regions) is not list or any(r not in REGIONS for r in regions):
                        raise APIError(400, '区域无效。')
                    self.access.require(user, 'investigate', regions)
                if moment:
                    reference_time(moment, 'business_as_of', business=True)
                if context.get('run_id') and any(context.get(k) != v for k, v in pending.items()):
                    context = {'mode': context.get('mode', 'deterministic'),
                               **({'knowledge_ids': context['knowledge_ids']} if context.get('knowledge_ids') else {})}
                context.update(pending)
                return {'text': candidate['reply'] or '请补充授权区域和完整参考日期、时点。', 'clarify': True}, context
            self.access.require(user, 'investigate', regions)
            reference_time(moment, 'business_as_of', business=True)
            connection = self.data.one('SELECT batch_id FROM database_source_connections WHERE workspace_id=?', (WORKSPACE,))
            if not connection:
                raise APIError(409, '尚未选择物流批次。')
            mode = selection.get('mode', context.get('mode', 'deterministic'))
            same_scope = (context.get('region_ids') == regions and context.get('business_as_of') == moment) or (
                not context.get('run_id') and context.get('region_ids', regions) == regions and context.get('business_as_of', moment) == moment)
            active = [f['text'] for f in context.get('constraints', []) if f.get('active')] if same_scope else []
            question = row['text']+('\n持续分析约束：'+'；'.join(active) if active else '')
            if len(question) > 2000:
                raise APIError(400, '问题与持续约束过长，请缩小范围或解除无效约束。')
            snapshot = self.snapshots.create(user, {'batch_id': connection['batch_id'], 'city': 'Shanghai', 'region_ids': regions,
                                                  'business_as_of': moment}, operation_id=operation_id)
            run = self.runs.create(user, {'snapshot_id': snapshot['snapshot_id'], 'idempotency_key': operation_id,
                                          'mode': mode, 'question': question})
            context = {'region_ids': regions, 'business_as_of': moment, 'batch_id': connection['batch_id'], 'mode': mode,
                       'snapshot_id': snapshot['snapshot_id'], 'run_id': run['run_id'], 'task_ids': [],
                       **({'knowledge_ids': context['knowledge_ids']} if context.get('knowledge_ids') else {}),
                       'facts': context.get('facts', []) if same_scope else [], 'constraints': context.get('constraints', []) if same_scope else []}
            with self.data.lock:
                self.data.db.execute('INSERT OR IGNORE INTO logistics_conversation_runs(conversation_id,message_id,run_id,snapshot_id) VALUES(?,?,?,?)', (row['conversation_id'], row['message_id'], run['run_id'], snapshot['snapshot_id']))
            return {'text': ('实验模型调查' if mode == 'model' else '确定性调查')+'已创建，正在后台处理。', 'run_id': run['run_id'], 'waiting': True}, context
        if action == 'reply':
            if context.get('run_id'):
                self.saved_report(user, context['run_id'])
            text = candidate['reply'] or '请说明你希望解释的内容。'
            if '\n' not in text:
                text = text.replace('\\n', '\n')
            return {'text': text, 'evidence_ids': candidate['evidence_ids'], 'run_id': context.get('run_id'), 'source': 'model_explanation'}, context
        run_id = context.get('run_id')
        if action in ('fact', 'constraint'):
            if action == 'constraint':
                params = constraint_parameters(row['text'], params)
            note = params.get('note') or row['text']
            ids = params.get('task_ids', context.get('task_ids', []))
            if any(t not in context.get('available_task_ids', []) for t in ids):
                raise APIError(400, '反馈任务对象不属于当前范围。')
            occurred = params.get('occurred_at') or None
            if occurred:
                occurred = chat_time(occurred)
            entry = {'id': operation_id, 'text': note, 'task_ids': ids, 'source': params.get('source') or '用户输入',
                     'occurred_at': occurred, 'created_at': row['created_at'], 'verification': 'unverified' if action == 'fact' else 'analysis_constraint',
                     'constraint_key': params.get('constraint_key', 'instruction'), 'active': params.get('active', 'yes') == 'yes'}
            name = 'facts' if action == 'fact' else 'constraints'
            values = context.setdefault(name, [])
            if len(values) >= 50:
                raise APIError(409, '当前范围的事实或约束已达50条。')
            if action == 'constraint':
                for old in values:
                    if old['constraint_key'] == entry['constraint_key'] and old['task_ids'] == ids:
                        old['active'] = False
            values.append(entry)
            if action == 'constraint' and run_id:
                running = self.runs.saved(run_id)
                if running['mode'] == 'model' and running['status'] in ('queued', 'running'):
                    active = [f['text'] for f in values if f.get('active')]
                    instruction = '当前生效的人工约束：'+'；'.join(active) if active else '先前人工限制已解除；仍遵守现有权限、快照和工具边界。'
                    if len(instruction) > 1000:
                        raise APIError(400, '运行中的持续约束过长，请缩小或解除不再适用的约束。')
                    try:
                        self.runs.workflow.constrain(user, run_id, {'instruction': instruction}, operation_id=operation_id)
                    except APIError as exc:
                        if exc.status != 409 or self.runs.saved(run_id)['status'] in ('queued', 'running'):
                            raise
            if run_id and self.runs.saved(run_id)['status'] in ('completed', 'waiting') and (self.saved_report(user, run_id) or {}).get('report_id'):
                state = self.collaboration.read(user, run_id)
                body = {'version': state['version'], 'kind': 'reported' if action == 'fact' else 'constraint', 'text': note,
                        'task_ids': ids, 'occurred_at': occurred, 'source': entry['source'], 'constraint_key': entry['constraint_key'], 'active': entry['active']}
                self.mutate_once(user, row, run_id, 'chat_feedback', body)
            return {'text': '人工陈述已保存，保持未经验证。' if action == 'fact' else '约束已保存，仅适用于当前范围；已提交版本如受影响将失效。'}, context
        if not run_id:
            return {'text': '请先发起调查并确定要讨论的报告。', 'clarify': True}, context
        state = self.collaboration.read(user, run_id)
        if action == 'approval_status':
            return {'text': self.approval_text(state), 'run_id': run_id, 'collaboration': state}, context
        if action == 'followup':
            parent = self.runs.saved(run_id)
            if parent['status'] not in ('completed', 'waiting'):
                raise APIError(409, '请等待当前调查结束；失败或取消的调查不能自动恢复。')
            self.runs.budgets.check(run_id, 'model_calls')
            if parent['mode'] == 'model':
                budget = self.runs.budgets.read(run_id)
                stopped = self.runs.workflow.read(user, run_id)['state'].get('stop_reason')
                if budget['model_usage_unknown'] or stopped in ('no_progress', 'interrupted_operation', 'invalid_model_output', 'required_evidence_unavailable', 'blocked_dependencies'):
                    raise APIError(409, '本次调查已停止或存在未知调用，不能自动补查。可以继续解释已有结果或补充人工信息。')
            if selection.get('mode', context.get('mode')) != 'model':
                raise APIError(409, '补充模型取证需要明确选择实验模型模式。')
            active = [f['text'] for f in context.get('constraints', []) if f.get('active')]
            question = row['text'] + ('\n人工分析约束：'+'；'.join(active) if active else '')
            if len(question) > 2000:
                raise APIError(400, '补查问题与持续约束过长，请缩小讨论范围。')
            run = self.runs.create(user, {'snapshot_id': parent['snapshot_id'], 'parent_run_id': run_id,
                                          'idempotency_key': operation_id, 'mode': 'model', 'question': question})
            context.update(run_id=run['run_id'], report_id=None, task_ids=[], available_task_ids=[], mode='model')
            with self.data.lock:
                self.data.db.execute('INSERT OR IGNORE INTO logistics_conversation_runs(conversation_id,message_id,run_id,snapshot_id) VALUES(?,?,?,?)', (row['conversation_id'], row['message_id'], run['run_id'], run['snapshot_id']))
            return {'text': '已创建关联补查，继续累计原调查家族预算。', 'run_id': run['run_id'], 'waiting': True}, context
        if action == 'draft':
            self.attach_feedback(user, run_id, context)
            state = self.collaboration.read(user, run_id)
            ids = params.get('task_ids') or context.get('task_ids', [])
            drafts = state.get('drafts', [])
            strategy = params.get('strategy') or (drafts[-1]['content']['strategy'] if drafts else 'verify_window')
            for constraint in context.get('constraints', []):
                if constraint.get('active') and constraint['constraint_key'] == 'contact_courier' and strategy == 'contact_courier' and (not constraint['task_ids'] or set(ids) & set(constraint['task_ids'])):
                    raise APIError(409, '当前约束禁止联系快递员，请改用窗口核查。')
            body = {'version': state['version'], 'strategy': strategy, 'note': params.get('note') or row['text'], 'task_ids': ids}
            result = self.mutate_once(user, row, run_id, 'draft', body)
            draft = result['drafts'][-1]
            context.update(draft_version=draft['draft_version'], task_ids=ids)
            return {'text': f'草稿 v{draft["draft_version"]} 已保存，尚未提交审批。'+draft['change_note'], 'run_id': run_id, 'draft': draft}, context
        if action == 'submit':
            if not frozen['command'] and not explicit_submit(row['text']):
                raise APIError(409, '尚未收到明确提交命令，草稿保持未提交。')
            drafts = state.get('drafts', [])
            if not drafts:
                raise APIError(409, '请先生成并核对草稿。')
            # 草稿之后新增的反馈必须先进入业务状态，使旧草稿不能带旧依据提交。
            self.attach_feedback(user, run_id, context)
            state = self.collaboration.read(user, run_id)
            draft = drafts[-1]
            version = params.get('draft_version')
            if version != draft['draft_version'] or (frozen['command'] and frozen['command']['draft_hash'] != draft['draft_hash']):
                raise APIError(409, '提交对象已变化，请核对最新草稿。')
            result = self.mutate_once(user, row, run_id, 'submit', {'version': state['version'],
                'draft_version': version, 'draft_hash': draft['draft_hash']})
            proposal = result['proposals'][-1]
            return {'text': f'方案 v{proposal["proposal_version"]} 已提交，等待另一账号审核。', 'run_id': run_id, 'proposal': proposal}, context
        raise APIError(400, '动作不受支持。')

    @staticmethod
    def approval_text(state):
        if not state['proposals']:
            return '尚未提交审批。'
        proposal = state['proposals'][-1]
        labels = {'pending': '待审核', 'approved': '已批准，尚未执行', 'rejected': '已驳回', 'invalidated': '已失效'}
        text = f'方案 v{proposal["proposal_version"]}：{labels[proposal["status"]]}。'
        if proposal.get('decision'):
            decision = proposal['decision']
            moment = datetime.fromisoformat(decision['created_at'].replace('Z', '+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
            text += f'审核人 {decision["actor"]}，时间 {moment}（北京时间），理由：{decision["reason"]}'
        return text

    def sync(self):
        # ponytail: 按关联逐项复核状态；会话量增长后用持久化事件游标替代全量扫描。
        for link in self.data.all('SELECT * FROM logistics_conversation_runs'):
            try:
                conversation = self.data.one('SELECT * FROM logistics_conversations WHERE conversation_id=?', (link['conversation_id'],))
                user = self.user(conversation)
                run = self.runs.read(user, link['run_id'])
                state = self.collaboration.read(user, link['run_id'])
                if not link['delivered'] and run['status'] in ('completed', 'waiting', 'failed', 'cancelled'):
                    report = self.saved_report(user, link['run_id'])
                    original = self.data.one('SELECT * FROM logistics_messages WHERE message_id=?', (link['message_id'],))
                    context = json.loads(original['context_json'])
                    context.update(run_id=link['run_id'], snapshot_id=link['snapshot_id'])
                    if report and report.get('report_id'):
                        context.update(report_id=report['report_id'], available_task_ids=[t['order_id'] for t in report['focus_tasks']],
                                       task_ids=[t['order_id'] for t in report['focus_tasks'][:2]])
                        metrics = report['summary']['metrics']
                        parts = [f'{m["name"]}：{m["value"] if m["value"] is not None else "不可计算"}' for m in metrics.values()]
                        text = '调查结果已保存。'+'；'.join(parts)+'。逾期状态不能证明现场原因。'
                        result = {'run_id': link['run_id'], 'report': report, 'source': 'program_saved_report'}
                    else:
                        text = '调查'+{'completed': '已完成', 'waiting': '已停止，保留部分结果', 'failed': '失败', 'cancelled': '已取消'}[run['status']]+'。'+(run.get('error_code') or '')
                        result = {'run_id': link['run_id'], 'partial_report': report, 'source': 'program_saved_state'}
                    with self.runs.transaction():
                        self.authorize_context(user, context)
                        self.append(link['conversation_id'], 'assistant', text, 'partial' if run['status'] != 'completed' else 'replied', 'run:'+link['run_id'], context, result)
                        current = json.loads(self.owned(user, link['conversation_id'])['context_json'])
                        if current.get('run_id') == link['run_id']:
                            # 保留调查期间新增的事实和约束，仅补入报告聚焦对象。
                            for field in ('report_id', 'available_task_ids', 'task_ids'):
                                if field in context:
                                    current[field] = context[field]
                            self.data.db.execute('UPDATE logistics_conversations SET context_json=?,version=version+1 WHERE conversation_id=?', (canonical(current), link['conversation_id']))
                        self.data.db.execute('UPDATE logistics_conversation_runs SET delivered=1 WHERE run_id=? AND conversation_id=?', (link['run_id'], link['conversation_id']))
                        self.data.db.execute("UPDATE logistics_messages SET status='replied' WHERE message_id=? AND status='waiting'", (link['message_id'],))
                        self.data.db.execute("UPDATE logistics_messages SET status='replied' WHERE conversation_id=? AND request_key=? AND status='waiting'", (link['conversation_id'], 'reply:'+link['message_id']))
                if state['version'] > link['event_version']:
                    text = self.approval_text(state)
                    if state['followup']:
                        text += '内部跟进状态：'+state['followup']['status']+'；内部单不等于外部派单。'
                    if state['review']:
                        text += '新快照复核已保存，历史完成不能归因于建议或模拟回执。'
                    with self.runs.transaction():
                        self.runs.authorize(user, self.runs.saved(link['run_id']), owner=True)
                        if state['proposals']:
                            self.append(link['conversation_id'], 'assistant', text, 'replied', f'event:{link["run_id"]}:{state["version"]}',
                                        {'run_id': link['run_id']}, {'run_id': link['run_id'], 'collaboration': state})
                        self.data.db.execute('UPDATE logistics_conversation_runs SET event_version=? WHERE run_id=? AND conversation_id=?', (state['version'], link['run_id'], link['conversation_id']))
            except (APIError, ProtocolError):
                continue

    def _work(self):
        while not self.stop.is_set():
            try:
                self.sync()
                row = self.claim()
                if row:
                    self.active = (row['message_id'], row['claim_token'])
                    try:
                        self.process(row)
                    finally:
                        self.active = None
                else:
                    self.wake.wait(.5)
                    self.wake.clear()
            except Exception:
                logging.getLogger(__name__).error('物流会话后台处理暂时失败。')
                self.stop.wait(.5)

    def _beat(self):
        while not self.stop.wait(1):
            with self.data.lock:
                if self.active:
                    self.data.db.execute("UPDATE logistics_messages SET lease_until=? WHERE message_id=? AND claim_token=? AND status='processing'", (time.time()+self.lease_seconds, *self.active))

    def start(self):
        if self.worker:
            return
        self.migrate_pending()
        # ponytail: 单应用一个聊天执行线程；吞吐不足时再整体升级领取、SQLite 与检查点并发契约。
        self.worker = threading.Thread(target=self._work, name='logistics-chat')
        self.heartbeat = threading.Thread(target=self._beat, name='logistics-chat-heartbeat')
        self.worker.start()
        self.heartbeat.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.worker:
            self.worker.join()
            self.heartbeat.join()

    def routes(self):
        router = APIRouter(prefix='/api/logistics/conversations', tags=['物流连续对话'])

        def actor(request):
            if set(request.query_params) - {'workspaceId', 'after', 'page', 'search'}:
                raise APIError(400, '会话读取参数无效。')
            return self.access.user(request)

        @router.post('')
        def create(request: Request):
            return self.create(actor(request), request.state.body)

        @router.get('')
        def listing(request: Request, page: int = 1, search: str = ''):
            user = actor(request)
            self.access.require(user, 'view')
            if not 1 <= page <= 10000 or len(search) > 200:
                raise APIError(400, '会话分页无效。')
            with self.data.lock:
                rows = self.data.all('SELECT conversation_id,title FROM logistics_conversations WHERE creator_id=? AND creator_account=? ORDER BY updated_at DESC,conversation_id', (user['id'], user['account']))
                values = [self.read(user, r['conversation_id']) for r in rows if search.strip() in r['title']]
                return {'rows': values[(page-1)*20:page*20], 'total_rows': len(values)}

        @router.post('/for-run/{run_id}')
        def associate(request: Request, run_id: str):
            return self.open_run(actor(request), run_id, request.state.body)

        @router.get('/{identifier}')
        def read(request: Request, identifier: str):
            return self.read(actor(request), identifier)

        @router.get('/{identifier}/messages')
        def messages(request: Request, identifier: str, after: int = 0):
            return self.messages(actor(request), identifier, after)

        @router.post('/{identifier}/messages')
        def send(request: Request, identifier: str):
            return reply(self.send(actor(request), identifier, request.state.body), 202)

        @router.post('/{identifier}/references')
        def reference(request: Request, identifier: str):
            return self.reference(actor(request), identifier, request.state.body)

        return router
