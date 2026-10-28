"""单进程、单运行的持久化执行器；T06 确定性流程，不调用模型。"""
import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager

from fastapi import APIRouter, Request

from .common import APIError, iso, reply, request_id
from .logistics_access import WORKSPACE
from .logistics_budget import BudgetService


SCHEMA = '''
CREATE TABLE IF NOT EXISTS logistics_runs (
 run_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
 snapshot_id TEXT NOT NULL REFERENCES logistics_snapshots(snapshot_id),
 creator_id INTEGER NOT NULL, creator_account TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, parent_run_id TEXT REFERENCES logistics_runs(run_id),
 status TEXT NOT NULL, current_node TEXT NOT NULL, budget_json TEXT NOT NULL,
 active_seconds REAL NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 lease_until REAL, heartbeat_at REAL, claim_token TEXT, accounted_at REAL,
 result_json TEXT, error_code TEXT,
 UNIQUE(workspace_id, creator_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS logistics_runs_queue ON logistics_runs(status, lease_until, created_at);
'''
BUDGET = {'model_calls': 0, 'tool_calls': 0, 'plan_revisions': 0,
          'max_model_calls': 12, 'max_tool_calls': 16,
          'max_plan_revisions': 3, 'max_active_seconds': 120}


class RunService:
    lease_seconds = 10
    heartbeat_seconds = 1

    def __init__(self, snapshots):
        self.snapshots, self.data, self.access = snapshots, snapshots.data, snapshots.access
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.active = None
        self.worker = self.heartbeat = None
        self.report_builder = None
        self.workflow = None
        with self.data.lock:
            self.data.db.executescript(SCHEMA)
            if 'request_id' not in {r['name'] for r in self.data.all('PRAGMA table_info(logistics_runs)')}:
                self.data.db.execute('ALTER TABLE logistics_runs ADD COLUMN request_id TEXT')
            columns = {r['name'] for r in self.data.all('PRAGMA table_info(logistics_runs)')}
            for name, definition in [('mode', "TEXT NOT NULL DEFAULT 'deterministic'"), ('question', "TEXT NOT NULL DEFAULT ''"), ('thread_id', 'TEXT'), ('family_id', 'TEXT'), ('family_key', 'TEXT')]:
                if name not in columns:
                    self.data.db.execute(f'ALTER TABLE logistics_runs ADD COLUMN {name} {definition}')
            self.data.db.execute("UPDATE logistics_runs SET thread_id='logistics:'||run_id WHERE thread_id IS NULL")
            # 兼容旧运行和关联链；同一业务快照换ID也不能重置家族预算。
            rows = self.data.all('SELECT r.*,s.metadata_json FROM logistics_runs r JOIN logistics_snapshots s USING(snapshot_id) ORDER BY r.created_at,r.run_id')
            roots = {r['run_id']: r['run_id'] for r in rows}
            def root(key):
                while roots[key] != key:
                    key = roots[key]
                return key
            seen = {}
            for row in rows:
                key = json.loads(row['metadata_json']).get('snapshot_fingerprint', row['snapshot_id'])
                grouping = (row['workspace_id'], row['creator_id'], key)
                for linked in (row['parent_run_id'], row['family_id'], seen.get(grouping)):
                    if linked in roots:
                        roots[root(row['run_id'])] = root(linked)
                seen[grouping] = row['run_id']
                self.data.db.execute('UPDATE logistics_runs SET family_key=? WHERE run_id=?', (key, row['run_id']))
            for row in rows:
                self.data.db.execute('UPDATE logistics_runs SET family_id=? WHERE run_id=?', (root(row['run_id']), row['run_id']))
            if self.data.one("SELECT name FROM sqlite_master WHERE type='table' AND name='logistics_tool_calls'"):
                for row in rows:
                    value = json.loads(row['budget_json'])
                    audited = self.data.one('SELECT count(*) AS n FROM logistics_tool_calls WHERE run_id=?', (row['run_id'],))['n']
                    value['tool_calls'] = max(value.get('tool_calls', 0), audited)
                    if 'standalone_seconds' not in value:
                        value['standalone_seconds'] = max(0, value.get('model_seconds', 0)-row['active_seconds'])
                    self.data.db.execute('UPDATE logistics_runs SET budget_json=? WHERE run_id=?', (json.dumps(value), row['run_id']))
            self.data.db.execute('CREATE INDEX IF NOT EXISTS logistics_runs_family ON logistics_runs(family_id)')
            self.data.db.execute('CREATE INDEX IF NOT EXISTS logistics_runs_family_key ON logistics_runs(workspace_id,creator_id,family_key)')
        self.budgets = BudgetService(self)

    def saved(self, run_id):
        row = self.data.one('SELECT * FROM logistics_runs WHERE run_id=?', (run_id,))
        if not row:
            raise APIError(404, '运行不存在。')
        if row['workspace_id'] != WORKSPACE:
            raise APIError(403, '运行不属于当前空间。')
        return row

    @contextmanager
    def transaction(self):
        with self.data.lock:
            self.data.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.data.db.commit()
            except Exception:
                self.data.db.rollback()
                raise

    def authorize(self, user, row, *, owner=False):
        metadata = self.snapshots.ready_metadata(user, row['snapshot_id'])
        if owner and (row['creator_id'] != user['id'] or row['creator_account'] != user['account']):
            raise APIError(403, '只能操作自己创建的运行。')
        return metadata

    def public(self, row):
        return {**{k: row[k] for k in ('run_id', 'workspace_id', 'snapshot_id', 'parent_run_id',
                'status', 'current_node', 'active_seconds', 'created_at', 'updated_at',
                'lease_until', 'heartbeat_at', 'error_code', 'request_id', 'mode', 'question', 'thread_id', 'family_id')},
                'cancel_requested': bool(row['cancel_requested']),
                'budget': {**json.loads(row['budget_json']), **self.budgets.read(row['run_id'])},
                'result': json.loads(row['result_json']) if row['result_json'] else None}

    def create(self, user, body):
        if set(body) - {'snapshot_id', 'idempotency_key', 'parent_run_id', 'mode', 'question'}:
            raise APIError(400, '运行参数包含不允许的字段。')
        snapshot, key, parent = (body.get(k) for k in ('snapshot_id', 'idempotency_key', 'parent_run_id'))
        mode, question = body.get('mode', 'deterministic'), body.get('question', '')
        if mode not in ('deterministic', 'model') or type(question) is not str or len(question) > 2000 or (mode == 'model' and not question.strip()):
            raise APIError(400, '调查模式或问题无效。')
        if mode == 'model':
            from .logistics_model import configured
            if not self.workflow or not configured(self.workflow.model.env):
                raise APIError(503, '模型调查配置不可用。')
        if not isinstance(snapshot, str) or not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise APIError(400, '必须提供快照和长度不超过128的幂等键。')
        if parent is not None and (type(parent) is not str or not parent):
            raise APIError(400, '关联运行标识无效。')
        if parent is not None and (not isinstance(parent, str) or not parent):
            raise APIError(400, '父运行参数无效。')
        with self.data.lock:
            metadata = self.snapshots.ready_metadata(user, snapshot)
            self.access.require(user, 'investigate')
            regions = json.loads(self.data.one('SELECT regions_json FROM logistics_snapshots WHERE snapshot_id=?', (snapshot,))['regions_json'])
            if not set(regions).issubset(self.access.allowed_regions(user, 'investigate')):
                raise APIError(403, '调查包含未授权区域。')
            if parent:
                self.authorize(user, self.saved(parent), owner=True)
            self.data.db.execute('BEGIN IMMEDIATE')
            try:
                row = self.data.one('SELECT * FROM logistics_runs WHERE workspace_id=? AND creator_id=? AND idempotency_key=?',
                                    (WORKSPACE, user['id'], key))
                if row and (row['snapshot_id'] != snapshot or row['parent_run_id'] != parent or row['mode'] != mode or row['question'] != question):
                    raise APIError(409, '幂等键已用于不同的运行请求。')
                if not row:
                    identifier = 'LRUN-' + uuid.uuid4().hex
                    family_key = metadata['snapshot_fingerprint']
                    sibling = self.data.one('SELECT family_id FROM logistics_runs WHERE workspace_id=? AND creator_id=? AND family_key=? ORDER BY created_at,run_id LIMIT 1',
                                            (WORKSPACE, user['id'], family_key))
                    family = self.saved(parent)['family_id'] if parent else sibling['family_id'] if sibling else identifier
                    if parent and sibling and family != sibling['family_id']:
                        raise APIError(409, '快照已绑定另一任务家族，不能合并或重置预算。')
                    self.data.db.execute('''INSERT INTO logistics_runs
                        (run_id,workspace_id,snapshot_id,creator_id,creator_account,idempotency_key,
                         parent_run_id,status,current_node,budget_json,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,'queued','validate',?,?,?)''',
                        (identifier, WORKSPACE, snapshot, user['id'], user['account'], key, parent,
                         json.dumps(BUDGET), iso(), iso()))
                    row = self.saved(identifier)
                    self.data.db.execute('UPDATE logistics_runs SET request_id=? WHERE run_id=?', (request_id.get(), identifier))
                    self.data.db.execute('UPDATE logistics_runs SET mode=?,question=?,thread_id=?,family_id=?,family_key=? WHERE run_id=?',
                                         (mode, question, 'logistics:'+identifier, family, family_key, identifier))
                    row = self.saved(identifier)
                self.data.db.commit()
            except Exception:
                self.data.db.rollback()
                raise
        self.wake.set()
        return self.public(row)

    def read(self, user, run_id):
        with self.data.lock:
            row = self.saved(run_id)
            metadata = self.authorize(user, row)
            return {**self.public(row), 'business_as_of': metadata['business_as_of'], 'region_ids': metadata['region_ids'],
                    'can_cancel': row['creator_id'] == user['id'],
                    'can_report': row['creator_id'] == user['id'] and
                    set(self.authorize(user, row)['region_ids']).issubset(self.access.allowed_regions(user, 'investigate'))}

    def cancel(self, user, run_id):
        with self.transaction():
            row = self.saved(run_id)
            self.authorize(user, row, owner=True)
            if row['status'] in ('queued', 'running', 'waiting'):
                self._account(row, time.time())
                self.data.db.execute('''UPDATE logistics_runs SET cancel_requested=1,status='cancelled',
                    claim_token=NULL,lease_until=NULL,updated_at=? WHERE run_id=?''', (iso(), run_id))
            return self.public(self.saved(run_id))

    def _account(self, row, now):
        # 心跳增量落盘；失联段最多计到租约终点，等待/排队不计时。
        elapsed = max(0, min(now, row['lease_until']) - row['accounted_at']) if row['status'] == 'running' else 0
        self.data.db.execute('UPDATE logistics_runs SET active_seconds=active_seconds+?,accounted_at=? WHERE run_id=?',
                             (elapsed, now, row['run_id']))

    def claim(self):
        with self.data.lock:
            now = time.time()
            self.data.db.execute('BEGIN IMMEDIATE')
            try:
                row = self.data.one('''SELECT * FROM logistics_runs WHERE workspace_id=? AND cancel_requested=0
                    AND (status='queued' OR (status='running' AND lease_until<=?))
                    ORDER BY created_at,run_id LIMIT 1''', (WORKSPACE, now))
                if row:
                    self._account(row, now)
                    token = uuid.uuid4().hex
                    self.data.db.execute('''UPDATE logistics_runs SET status='running',claim_token=?,
                        lease_until=?,heartbeat_at=?,accounted_at=?,updated_at=? WHERE run_id=?''',
                        (token, now+self.lease_seconds, now, now, iso(), row['run_id']))
                    row = self.saved(row['run_id'])
                self.data.db.commit()
                return row
            except Exception:
                self.data.db.rollback()
                raise

    def owned(self, run_id, token):
        row = self.saved(run_id)
        return row if (row['status'] == 'running' and row['claim_token'] == token
                       and not row['cancel_requested'] and row['lease_until'] > time.time()) else None

    def renew(self, run_id, token):
        with self.transaction():
            row = self.owned(run_id, token)
            if not row:
                return False
            now = time.time()
            self._account(row, now)
            current = self.saved(run_id)
            if self.budgets.read(run_id)['active_seconds'] >= self.budgets.read(run_id)['max_active_seconds'] and current['mode'] != 'model':
                self.data.db.execute('''UPDATE logistics_runs SET status='failed',error_code='active_budget_exhausted',
                    claim_token=NULL,lease_until=NULL,updated_at=? WHERE run_id=?''', (iso(), run_id))
                return False
            self.data.db.execute('UPDATE logistics_runs SET lease_until=?,heartbeat_at=?,updated_at=? WHERE run_id=?',
                                 (now+self.lease_seconds, now, iso(), run_id))
            return True

    def commit(self, run_id, token, status, node, result=None, error=None):
        if status not in ('running', 'queued', 'waiting', 'completed', 'failed'):
            raise ValueError('invalid transition')
        with self.transaction():
            row = self.owned(run_id, token)
            if not row:
                return False
            self._account(row, time.time())
            current = self.saved(run_id)
            if self.budgets.read(run_id)['active_seconds'] >= self.budgets.read(run_id)['max_active_seconds'] and current['mode'] != 'model':
                status, node, result, error = 'failed', row['current_node'], None, 'active_budget_exhausted'
            self.data.db.execute('''UPDATE logistics_runs SET status=?,current_node=?,result_json=?,error_code=?,
                updated_at=?,claim_token=?,lease_until=? WHERE run_id=?''',
                (status, node, json.dumps(result) if result is not None else None, error, iso(),
                 token if status == 'running' else None, row['lease_until'] if status == 'running' else None, run_id))
            return True

    def step(self, row, metadata):
        if row['mode'] == 'model':
            return self.workflow.advance(row)
        if row['current_node'] == 'validate':
            return 'running', 'finish', None
        if row['current_node'] == 'finish':
            if self.report_builder is not None:
                report = self.report_builder(row)
                return 'completed', 'done', {'kind': 'deterministic-report', 'report_id': report['report_id'],
                    'message': '确定性工具、证据和模板报告已保存；尚未执行模型调查。'}
            return 'completed', 'done', {'kind': 'deterministic-runtime-check',
                'snapshot_id': row['snapshot_id'], 'visible_content_hash': metadata['visible_content_hash'],
                'message': '快照授权与持久化执行检查完成；尚未执行模型调查。'}
        raise ValueError('unknown saved node')

    def process(self, row):
        run_id, token = row['run_id'], row['claim_token']
        request_token = request_id.set(row.get('request_id') or uuid.uuid4().hex)
        try:
            while not self.stop.is_set():
                with self.data.lock:
                    row = self.owned(run_id, token)
                    if not row:
                        return
                    user = {'id': row['creator_id'], 'account': row['creator_account']}
                    metadata = self.authorize(user, row, owner=True)
                    self.access.require(user, 'investigate')
                    saved = self.data.one('SELECT regions_json FROM logistics_snapshots WHERE snapshot_id=?', (row['snapshot_id'],))
                    if not set(json.loads(saved['regions_json'])).issubset(self.access.allowed_regions(user, 'investigate')):
                        raise APIError(403, '调查权限已撤销。')
                    budget = self.budgets.read(run_id)
                    if budget['active_seconds'] >= budget['max_active_seconds'] and row['mode'] != 'model':
                        self.commit(run_id, token, 'failed', row['current_node'], error='active_budget_exhausted')
                        return
                status, node, result = self.step(row, metadata)
                with self.data.lock:
                    # 结果提交再次检查实时权限，撤权不能留下新结果。
                    self.authorize(user, row, owner=True)
                    self.access.require(user, 'investigate')
                    if not set(json.loads(saved['regions_json'])).issubset(self.access.allowed_regions(user, 'investigate')):
                        raise APIError(403, '调查权限已撤销。')
                    current = self.owned(run_id, token)
                    budget = self.budgets.read(run_id)
                    if current and budget['active_seconds'] >= budget['max_active_seconds'] and row['mode'] != 'model':
                        status, node, result = 'failed', row['current_node'], None
                        self.commit(run_id, token, status, node, error='active_budget_exhausted')
                        return
                    if not self.commit(run_id, token, status, node, result) or status != 'running':
                        return
        except Exception as exc:
            # 不将异常原文、连接信息或源字段写入运行或日志。
            logging.getLogger(__name__).warning('Logistics run failed: %s (%s)', run_id, type(exc).__name__)
            self.commit(run_id, token, 'failed', row['current_node'],
                        error='authorization_changed' if isinstance(exc, APIError) else 'execution_failed')
        finally:
            request_id.reset(request_token)
            if self.stop.is_set():
                self.commit(run_id, token, 'queued', self.saved(run_id)['current_node'])

    def _work(self):
        while not self.stop.is_set():
            row = self.claim()
            if row:
                with self.data.lock:
                    self.active = (row['run_id'], row['claim_token'])
                try:
                    self.process(row)
                finally:
                    with self.data.lock:
                        self.active = None
            else:
                self.wake.wait(.2)
                self.wake.clear()

    def _beat(self):
        while not self.stop.wait(self.heartbeat_seconds):
            with self.data.lock:
                if self.active:
                    self.renew(*self.active)

    def start(self):
        if self.worker is not None:
            return
        # ponytail: 单进程一个执行线程，SQLite 排队无进程内无界 Future 队列；扩容时整体迁移领取与存储。
        self.worker = threading.Thread(target=self._work, name='logistics-runs')
        self.heartbeat = threading.Thread(target=self._beat, name='logistics-heartbeat')
        self.worker.start()
        self.heartbeat.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.worker:
            self.worker.join()
            self.heartbeat.join()

    def routes(self):
        router = APIRouter(prefix='/api/logistics/runs')

        @router.get('')
        def listing(request: Request, page: int = 1, page_size: int = 20, status: str = ''):
            user = self.access.user(request)
            if set(request.query_params) - {'page', 'page_size', 'status', 'workspaceId'} or status not in ('', 'queued','running','waiting','completed','failed','cancelled'):
                raise APIError(400, '运行筛选参数无效。')
            if not 1 <= page <= 100000 or not 1 <= page_size <= 100:
                raise APIError(400, '运行分页无效。')
            with self.data.lock:
                self.access.require(user, 'view')
                values = []
                # ponytail: 首版扫描授权后分页；运行量大时迁移为授权JOIN与索引查询。
                for row in self.data.all('SELECT * FROM logistics_runs WHERE workspace_id=? ORDER BY created_at DESC,run_id DESC', (WORKSPACE,)):
                    if status and row['status'] != status:
                        continue
                    try:
                        metadata = self.authorize(user, row)
                    except APIError:
                        continue
                    values.append({**self.public(row), 'business_as_of': metadata['business_as_of'],
                                   'region_ids': metadata['region_ids'], 'can_cancel': row['creator_id']==user['id']})
                offset=(page-1)*page_size
                return {'rows':values[offset:offset+page_size], 'total_rows':len(values),
                        'page':page,'page_size':page_size}

        @router.post('')
        def create(request: Request):
            return reply(self.create(self.access.user(request), request.state.body), 202)

        @router.get('/{run_id}')
        def read(request: Request, run_id: str):
            return self.read(self.access.user(request), run_id)

        @router.post('/{run_id}/cancel')
        def cancel(request: Request, run_id: str):
            if request.state.body:
                raise APIError(400, '取消请求不接受额外参数。')
            return self.cancel(self.access.user(request), run_id)

        return router
