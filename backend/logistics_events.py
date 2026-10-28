"""事务内事件日志；SSE只读取持久化记录，不承担执行。"""
import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .common import APIError


SCHEMA = '''
CREATE TABLE IF NOT EXISTS logistics_events (
 run_id TEXT NOT NULL REFERENCES logistics_runs(run_id), seq INTEGER NOT NULL,
 kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(run_id,seq)
);
CREATE TRIGGER IF NOT EXISTS logistics_event_create AFTER INSERT ON logistics_runs BEGIN
 INSERT INTO logistics_events VALUES(NEW.run_id,1,'run_created',json_object('status',NEW.status,'node',NEW.current_node),NEW.created_at);
END;
CREATE TRIGGER IF NOT EXISTS logistics_event_stage AFTER UPDATE OF status,current_node ON logistics_runs
 WHEN OLD.status!=NEW.status OR OLD.current_node!=NEW.current_node BEGIN
 INSERT INTO logistics_events SELECT NEW.run_id,coalesce(max(seq),0)+1,
 CASE NEW.status WHEN 'failed' THEN 'run_failed' WHEN 'cancelled' THEN 'run_cancelled' ELSE 'stage_changed' END,
 json_object('status',NEW.status,'node',NEW.current_node,'error_code',NEW.error_code),NEW.updated_at
 FROM logistics_events WHERE run_id=NEW.run_id;
END;
CREATE TRIGGER IF NOT EXISTS logistics_event_tool_start AFTER INSERT ON logistics_tool_calls BEGIN
 INSERT INTO logistics_events SELECT NEW.run_id,coalesce(max(seq),0)+1,'tool_started',
 json_object('call_id',NEW.call_id,'tool',NEW.tool,'status',NEW.status),NEW.started_at
 FROM logistics_events WHERE run_id=NEW.run_id;
END;
CREATE TRIGGER IF NOT EXISTS logistics_event_tool_finish AFTER UPDATE OF status ON logistics_tool_calls
 WHEN OLD.status!=NEW.status BEGIN
 INSERT INTO logistics_events SELECT NEW.run_id,coalesce(max(seq),0)+1,'tool_finished',
 json_object('call_id',NEW.call_id,'tool',NEW.tool,'status',NEW.status,'evidence_id',NEW.evidence_id,'error_code',NEW.error_code),NEW.finished_at
 FROM logistics_events WHERE run_id=NEW.run_id;
END;
CREATE TRIGGER IF NOT EXISTS logistics_event_report AFTER INSERT ON logistics_reports BEGIN
 INSERT INTO logistics_events SELECT NEW.run_id,coalesce(max(seq),0)+1,'report_ready',
 json_object('report_id',NEW.report_id),NEW.created_at FROM logistics_events WHERE run_id=NEW.run_id;
END;
'''


class RunEvents:
    def __init__(self, runs):
        self.runs, self.data = runs, runs.data
        with self.data.lock:
            self.data.db.executescript(SCHEMA)

    def read(self, user, run_id, after=0):
        if type(after) is not int or not 0 <= after <= 2147483647:
            raise APIError(400, '事件游标无效。')
        with self.data.lock:
            run = self.runs.saved(run_id)
            self.runs.authorize(user, run)
            rows = self.data.all('SELECT * FROM logistics_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 100', (run_id, after))
            return {'rows': [{**r, 'payload': json.loads(r['payload_json'])} for r in rows],
                    'next_seq': rows[-1]['seq'] if rows else after, 'truncated': len(rows) == 100,
                    'report_ready': bool(self.data.one('SELECT report_id FROM logistics_reports WHERE run_id=?', (run_id,)))}

    def routes(self):
        router = APIRouter(prefix='/api/logistics/runs')

        @router.get('/{run_id}/events')
        async def events(request: Request, run_id: str, after: int = 0):
            if set(request.query_params) - {'after', 'workspaceId', 'stream'}:
                raise APIError(400, '事件读取不能覆盖范围。')
            actor = self.runs.access.user(request)
            first = self.read(actor, run_id, after)
            if request.query_params.get('stream') != '1':
                return first
            cursor = request.headers.get('last-event-id')
            if cursor is not None:
                try:
                    after = int(cursor)
                except ValueError:
                    raise APIError(400, '事件游标无效。') from None
                self.read(actor, run_id, after)
            async def stream():
                current = after
                while not await request.is_disconnected():
                    try:
                        # 每轮同时重验会话和实时权限；撤权即终止流。
                        batch = self.read(self.runs.access.user(request), run_id, current)
                    except APIError:
                        yield 'event: access_invalid\ndata: {}\n\n'
                        return
                    for row in batch['rows']:
                        yield f"id: {row['seq']}\nevent: run_event\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                        current = row['seq']
                    if not batch['rows']:
                        yield ': heartbeat\n\n'
                    await asyncio.sleep(.5)
            return StreamingResponse(stream(), media_type='text/event-stream',
                                     headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
        return router
