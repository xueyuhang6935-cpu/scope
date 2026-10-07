"""SQLite persistence for independently committed investigation tool audits."""
import json


class InvestigationAuditStore:
    def __init__(self, data, run_id):
        self.data, self.run_id = data, run_id
        with data.lock:
            data.db.execute('''CREATE TABLE IF NOT EXISTS investigation_tool_calls (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool TEXT NOT NULL,
                args_json TEXT NOT NULL, scope_json TEXT, status TEXT NOT NULL,
                result_json TEXT, audit_json TEXT, started_at TEXT NOT NULL,
                finished_at TEXT)''')

    def start(self, identifier, tool, args):
        with self.data.lock:
            self.data.db.execute('''INSERT INTO investigation_tool_calls
                (id,run_id,tool,args_json,status,started_at)
                VALUES(?,?,?,?,'started',strftime('%Y-%m-%dT%H:%M:%fZ','now'))''',
                (identifier, self.run_id, tool, json.dumps(args, ensure_ascii=False)))

    def finish(self, identifier, result, audit):
        with self.data.lock:
            self.data.db.execute('''UPDATE investigation_tool_calls SET status=?,
                scope_json=coalesce(?,scope_json),result_json=?,audit_json=?,
                finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?''',
                ('failed' if 'error' in result else 'completed',
                 json.dumps(audit['scope']) if audit.get('scope') else None, json.dumps(result, ensure_ascii=False),
                 json.dumps(audit, ensure_ascii=False), identifier))

    def set_scope(self, identifier, scope):
        with self.data.lock:
            self.data.db.execute('UPDATE investigation_tool_calls SET scope_json=? WHERE id=?',
                                 (json.dumps(scope), identifier))

    @staticmethod
    def interrupt(data, run_id):
        with data.lock:
            data.db.execute("UPDATE investigation_tool_calls SET status='interrupted', finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE run_id=? AND status='started'", (run_id,))

    def records(self):
        return self.data.all('SELECT * FROM investigation_tool_calls WHERE run_id=? ORDER BY started_at,rowid', (self.run_id,))
