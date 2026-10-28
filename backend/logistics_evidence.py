"""物流调用审计和不可变证据；读取必须同时给出run并复核快照。"""
import json
import time
import uuid

from fastapi import APIRouter, Request

from .common import APIError, iso, request_id
from .logistics_access import WORKSPACE
from .logistics_snapshots import canonical, content_hash


SCHEMA = '''
CREATE TABLE IF NOT EXISTS logistics_tool_calls (
 call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES logistics_runs(run_id),
 workspace_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, metric_version TEXT NOT NULL,
 tool TEXT NOT NULL, args_summary_json TEXT NOT NULL, status TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, elapsed_ms INTEGER,
 evidence_id TEXT, result_fingerprint TEXT, error_code TEXT
);
CREATE INDEX IF NOT EXISTS logistics_calls_run ON logistics_tool_calls(run_id,started_at);
CREATE TABLE IF NOT EXISTS logistics_evidence (
 evidence_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES logistics_runs(run_id),
 workspace_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, metric_version TEXT NOT NULL,
 snapshot_fingerprint TEXT NOT NULL, call_id TEXT NOT NULL REFERENCES logistics_tool_calls(call_id),
 source_type TEXT NOT NULL, result_fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS logistics_evidence_run ON logistics_evidence(run_id);
'''


class EvidenceStore:
    def __init__(self, runs):
        self.runs, self.data = runs, runs.data
        with self.data.lock:
            self.data.db.executescript(SCHEMA)
            if 'request_id' not in {r['name'] for r in self.data.all('PRAGMA table_info(logistics_tool_calls)')}:
                self.data.db.execute('ALTER TABLE logistics_tool_calls ADD COLUMN request_id TEXT')

    def start(self, run_id, metadata, name, args):
        identifier = 'LCALL-' + uuid.uuid4().hex
        # 参数已经校验；身份过滤仅记指纹，非法参数/秘密配置从不原样入库。
        summary = {key: {'sha256': content_hash(value), 'length': len(value)}
                   if key in ('courier_id', 'aoi_id', 'evidence_id') else value for key, value in args.items()}
        self.data.db.execute('''INSERT INTO logistics_tool_calls
            (call_id,run_id,workspace_id,snapshot_id,metric_version,tool,args_summary_json,status,started_at)
            VALUES(?,?,?,?,?,?,?,'started',?)''',
            (identifier, run_id, WORKSPACE, metadata['snapshot_id'], metadata.get('metric_version') or 'logistics-metrics-v1',
             name, canonical(summary), iso()))
        self.data.db.execute('UPDATE logistics_tool_calls SET request_id=? WHERE call_id=?', (request_id.get(), identifier))
        return identifier, time.monotonic()

    def save(self, identifier, run_id, metadata, metric_version, source_type, payload, call_id):
        fingerprint = content_hash(payload)
        saved = self.data.one('SELECT * FROM logistics_evidence WHERE evidence_id=?', (identifier,))
        if saved:
            if (saved['run_id'] != run_id or saved['snapshot_id'] != metadata['snapshot_id']
                    or saved['result_fingerprint'] != fingerprint):
                raise APIError(409, '证据标识已绑定不同内容。')
            return
        self.data.db.execute('INSERT INTO logistics_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (identifier, run_id, WORKSPACE, metadata['snapshot_id'], metric_version, metadata['snapshot_fingerprint'],
             call_id, source_type, fingerprint, canonical(payload), iso()))

    def finish(self, ticket, run_id, metadata, result):
        call_id, started = ticket
        identifier = 'LEV-' + uuid.uuid4().hex
        fingerprint = content_hash(result)
        with self.runs.transaction():
            self.save(identifier, run_id, metadata, result['metric_version'], 'tool_result', result, call_id)
            for row in result['rows']:
                task_id = row.get('evidence_id')
                if task_id and task_id.startswith('LTASK:'):
                    record = row.get('record', {key: value for key, value in row.items() if key != 'evidence_id'})
                    self.save(task_id, run_id, metadata, result['metric_version'], 'snapshot_task', record, call_id)
            self.data.db.execute('''UPDATE logistics_tool_calls SET status=?,finished_at=?,elapsed_ms=?,
                evidence_id=?,result_fingerprint=?,error_code=? WHERE call_id=?''',
                (result['status'], iso(), round((time.monotonic()-started)*1000), identifier, fingerprint,
                 result.get('error', {}).get('code'), call_id))
        return {**result, 'call_id': call_id, 'result_evidence_id': identifier, 'result_fingerprint': fingerprint}

    def reject(self, ticket, status):
        call_id, started = ticket
        self.data.db.execute('''UPDATE logistics_tool_calls SET status='error',finished_at=?,elapsed_ms=?,
            error_code=? WHERE call_id=?''', (iso(), round((time.monotonic()-started)*1000), f'http_{status}', call_id))

    def interrupt_open(self):
        with self.data.lock:
            self.data.db.execute("UPDATE logistics_tool_calls SET status='interrupted',finished_at=?,error_code='executor_restart' WHERE status='started'", (iso(),))

    def read(self, user, run_id, evidence_id):
        with self.data.lock:
            run = self.runs.saved(run_id)
            metadata = self.runs.authorize(user, run)
            row = self.data.one('SELECT * FROM logistics_evidence WHERE evidence_id=?', (evidence_id,))
            if not row:
                raise APIError(404, '证据不存在。')
            if (row['workspace_id'] != WORKSPACE or row['run_id'] != run_id or row['snapshot_id'] != run['snapshot_id']
                    or row['snapshot_fingerprint'] != metadata['snapshot_fingerprint']):
                raise APIError(403, '证据不属于当前调查快照。')
            payload = json.loads(row['payload_json'])
            if content_hash(payload) != row['result_fingerprint']:
                raise APIError(409, '证据内容校验失败。')
            return {key: value for key, value in row.items() if key != 'payload_json'} | {'record': payload}

    def calls(self, user, run_id, page=1, page_size=20):
        if type(page) is not int or type(page_size) is not int or not 1 <= page <= 100000 or not 1 <= page_size <= 100:
            raise APIError(400, '调用记录分页参数无效。')
        with self.data.lock:
            run = self.runs.saved(run_id)
            self.runs.authorize(user, run)
            total = self.data.one('SELECT count(*) AS n FROM logistics_tool_calls WHERE run_id=? AND workspace_id=? AND snapshot_id=?',
                                  (run_id, WORKSPACE, run['snapshot_id']))['n']
            rows = self.data.all('''SELECT * FROM logistics_tool_calls WHERE run_id=? AND workspace_id=? AND snapshot_id=?
                ORDER BY started_at,call_id LIMIT ? OFFSET ?''', (run_id, WORKSPACE, run['snapshot_id'], page_size, (page-1)*page_size))
            for row in rows:
                row['args_summary'] = json.loads(row.pop('args_summary_json'))
            return {'rows': rows, 'total_rows': total, 'returned_rows': len(rows),
                    'page': page, 'page_size': page_size, 'truncated': (page-1)*page_size+len(rows) < total}

    def routes(self):
        router = APIRouter(prefix='/api/logistics/runs', tags=['物流调查证据'])

        @router.get('/{run_id}/calls')
        def calls(request: Request, run_id: str, page: int = 1, page_size: int = 20):
            if set(request.query_params) - {'page', 'page_size', 'workspaceId'}:
                raise APIError(400, '调用记录不接受额外参数。')
            return self.calls(self.runs.access.user(request), run_id, page, page_size)

        @router.get('/{run_id}/evidence/{evidence_id}')
        def evidence(request: Request, run_id: str, evidence_id: str):
            if set(request.query_params) - {'workspaceId'}:
                raise APIError(400, '证据读取不能覆盖范围。')
            return self.read(self.runs.access.user(request), run_id, evidence_id)

        return router
