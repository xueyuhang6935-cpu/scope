-- Version 1: runtime data is separate from immutable supply_demo fixtures.
CREATE SCHEMA IF NOT EXISTS sc;
CREATE TABLE IF NOT EXISTS sc.schema_versions (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS sc.risk_scans (
 id text PRIMARY KEY, workspace_id text NOT NULL, scenario_id text NOT NULL,
 as_of timestamptz NOT NULL, trigger text NOT NULL, snapshot_hash text NOT NULL,
 result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS risk_scans_workspace ON sc.risk_scans(workspace_id,created_at DESC);
CREATE TABLE IF NOT EXISTS sc.risk_events (
 id text PRIMARY KEY, workspace_id text NOT NULL, scenario_id text NOT NULL,
 factory text NOT NULL, material text NOT NULL, revision text NOT NULL,
 status text NOT NULL CHECK(status IN ('open','acknowledged','data_required','covered')),
 owner text NOT NULL, version integer NOT NULL DEFAULT 1, recurrence_of text REFERENCES sc.risk_events(id),
 scan_id text NOT NULL REFERENCES sc.risk_scans(id), result jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS risk_active_key ON sc.risk_events(workspace_id,factory,material,revision) WHERE status<>'covered';
CREATE TABLE IF NOT EXISTS sc.risk_history (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, event_id text NOT NULL REFERENCES sc.risk_events(id),
 version integer NOT NULL, kind text NOT NULL, details jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(event_id,version)
);
CREATE TABLE IF NOT EXISTS sc.commitment_changes (
 id text PRIMARY KEY, workspace_id text NOT NULL, scenario_id text NOT NULL,
 request_id text NOT NULL, purchase_line_id text NOT NULL, installment text NOT NULL,
 available_at timestamptz NOT NULL, known_at timestamptz NOT NULL, note text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(workspace_id,request_id)
);
INSERT INTO sc.schema_versions(version) VALUES(1) ON CONFLICT DO NOTHING;
