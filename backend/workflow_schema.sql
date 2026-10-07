CREATE TABLE IF NOT EXISTS sc.investigations (
 id text PRIMARY KEY, event_id text NOT NULL REFERENCES sc.risk_events(id), workspace_id text NOT NULL,
 requested_by text NOT NULL, request_id text NOT NULL, status text NOT NULL,
 input jsonb NOT NULL, report jsonb, error text, model_attempted boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(workspace_id,request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS investigation_active_event ON sc.investigations(event_id)
 WHERE status IN ('queued','running','awaiting_approval','resuming');
CREATE TABLE IF NOT EXISTS sc.candidate_plans (
 id text PRIMARY KEY, event_id text NOT NULL REFERENCES sc.risk_events(id),
 run_id text NOT NULL REFERENCES sc.investigations(id), version integer NOT NULL,
 candidate_index integer NOT NULL, content jsonb NOT NULL, content_hash text NOT NULL,
 status text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 UNIQUE(event_id,version), UNIQUE(run_id,candidate_index)
);
CREATE TABLE IF NOT EXISTS sc.plan_approvals (
 id text PRIMARY KEY, plan_id text NOT NULL UNIQUE REFERENCES sc.candidate_plans(id),
 plan_version integer NOT NULL, content_hash text NOT NULL, approver text NOT NULL,
 decision text NOT NULL, comment text NOT NULL, scope jsonb NOT NULL,
 decided_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO sc.schema_versions(version) VALUES(2) ON CONFLICT DO NOTHING;
