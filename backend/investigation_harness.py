"""Live investigation authorization and independently committed tool audit."""
import json
import time
import uuid

from .investigation_sql import TABLES


class InvestigationHarness:
    def __init__(self, data, run_id, authorize):
        self.data, self.run_id, self.authorize = data, run_id, authorize
        with data.lock:
            data.db.execute('''CREATE TABLE IF NOT EXISTS investigation_tool_calls (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool TEXT NOT NULL,
                args_json TEXT NOT NULL, scope_json TEXT, status TEXT NOT NULL,
                result_json TEXT, audit_json TEXT, started_at TEXT NOT NULL,
                finished_at TEXT)''')

    def check(self, evidence=(), candidates=()):
        scope = self.authorize()
        for item in evidence:
            if item.get('table') == 'sql_query':
                previous = item['record']['scope']
                if any(previous.get(k) != scope.get(k) for k in ('workspace', 'scenario', 'asOf')):
                    raise PermissionError('调查范围已变化。')
                for domain, factories in previous['domains'].items():
                    if not set(factories).issubset(scope['domains'].get(domain, [])):
                        raise PermissionError('证据权限已缩小。')
            elif item.get('table') in TABLES:
                domain = TABLES[item['table']][0]
                # Cached evidence can have indirect ownership; require the entire
                # captured factory scope rather than guessing ownership from a row.
                if not set(scope['factories']).issubset(scope['domains'].get(domain, [])):
                    raise PermissionError('缓存证据的数据域权限不足。')
        for plan in candidates:
            factories = {a['factory'] for a in plan.get('actions', [])}
            factories.update(a['fromFactory'] for a in plan.get('actions', []) if a['kind'] == 'transfer')
            for domain in ('inventory', 'purchase', 'production', 'quality', 'rules'):
                if not factories.issubset(scope['domains'].get(domain, [])):
                    raise PermissionError('方案来源工厂或数据域权限不足。')
        return scope

    def start(self, tool, args):
        identifier = uuid.uuid4().hex
        with self.data.lock:
            self.data.db.execute('''INSERT INTO investigation_tool_calls
                (id,run_id,tool,args_json,status,started_at)
                VALUES(?,?,?,?,'started',strftime('%Y-%m-%dT%H:%M:%fZ','now'))''',
                (identifier, self.run_id, tool, json.dumps(args, ensure_ascii=False)))
        return identifier, time.monotonic()

    def finish(self, ticket, result, audit):
        identifier, started = ticket
        audit['callId'] = identifier
        audit['elapsedMs'] = round((time.monotonic() - started) * 1000)
        with self.data.lock:
            self.data.db.execute('''UPDATE investigation_tool_calls SET status=?,
                scope_json=coalesce(?,scope_json),result_json=?,audit_json=?,
                finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?''',
                ('failed' if 'error' in result else 'completed',
                 json.dumps(audit['scope']) if audit.get('scope') else None, json.dumps(result, ensure_ascii=False),
                 json.dumps(audit, ensure_ascii=False), identifier))

    def set_scope(self, ticket, scope):
        with self.data.lock:
            self.data.db.execute('UPDATE investigation_tool_calls SET scope_json=? WHERE id=?',
                                 (json.dumps(scope), ticket[0]))

    @staticmethod
    def interrupt(data, run_id):
        with data.lock:
            data.db.execute("UPDATE investigation_tool_calls SET status='interrupted', finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE run_id=? AND status='started'", (run_id,))

    def records(self):
        return self.data.all('SELECT * FROM investigation_tool_calls WHERE run_id=? ORDER BY started_at,rowid', (self.run_id,))
