import sqlite3
import threading


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS import_drafts (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, filename TEXT NOT NULL, domain TEXT NOT NULL,
 factory TEXT NOT NULL, owner TEXT NOT NULL, sheet TEXT NOT NULL, columns_json TEXT NOT NULL,
 rows_json TEXT NOT NULL, mapping_json TEXT, check_json TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_files (
 draft_id TEXT PRIMARY KEY REFERENCES import_drafts(id), content BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS import_batches (
 id TEXT PRIMARY KEY, draft_id TEXT NOT NULL UNIQUE, user_id INTEGER NOT NULL, filename TEXT NOT NULL,
 domain TEXT NOT NULL, factory TEXT NOT NULL, owner TEXT NOT NULL, sheet TEXT NOT NULL,
 mapping_json TEXT NOT NULL, strategy TEXT NOT NULL, business_key TEXT NOT NULL,
 total INTEGER NOT NULL, imported INTEGER NOT NULL, isolated INTEGER NOT NULL,
 created_at TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS source_records (
 id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batches(id), row_number INTEGER NOT NULL,
 raw_json TEXT NOT NULL, material TEXT, quantity TEXT, unit TEXT, normalized_quantity TEXT,
 status TEXT NOT NULL, UNIQUE(batch_id,row_number)
);
CREATE TABLE IF NOT EXISTS import_groups (
 idempotency_key TEXT PRIMARY KEY, user_id INTEGER NOT NULL, payload TEXT NOT NULL,
 result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quality_issues (
 id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batches(id), record_id INTEGER NOT NULL REFERENCES source_records(id),
 rule TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL, original_value TEXT NOT NULL,
 reason TEXT NOT NULL, impact TEXT NOT NULL, assignee TEXT, note TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unit_rules (
 id INTEGER PRIMARY KEY, material TEXT NOT NULL, factory TEXT NOT NULL, from_unit TEXT NOT NULL,
 to_unit TEXT NOT NULL, factor TEXT NOT NULL, evidence TEXT NOT NULL, confirmed_by TEXT NOT NULL,
 valid_from TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(material,factory,from_unit,to_unit,version)
);
CREATE TABLE IF NOT EXISTS material_rules (
 id INTEGER PRIMARY KEY, source_code TEXT NOT NULL, standard_code TEXT NOT NULL, factory TEXT NOT NULL,
 evidence TEXT NOT NULL, confirmed_by TEXT NOT NULL, valid_from TEXT NOT NULL,
 version INTEGER NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(source_code,factory,version)
);
CREATE TABLE IF NOT EXISTS change_events (
 id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, issue_id TEXT, kind TEXT NOT NULL,
 details TEXT NOT NULL, actor_id INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS current_records (
 domain TEXT NOT NULL, factory TEXT NOT NULL, business_key TEXT NOT NULL,
 source_record_id INTEGER NOT NULL REFERENCES source_records(id),
 PRIMARY KEY(domain,factory,business_key)
);
CREATE INDEX IF NOT EXISTS issues_status_factory ON quality_issues(status,batch_id);
"""


class DataStore:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    def one(self, sql, args=()):
        with self.lock:
            row = self.db.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]
