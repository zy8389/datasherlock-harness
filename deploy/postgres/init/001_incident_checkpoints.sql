CREATE TABLE IF NOT EXISTS incident_checkpoints (
    incident_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    saved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    state JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_audit_events (
    event_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incident_checkpoints (incident_id),
    event_type TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    occurred_at TIMESTAMPTZ NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS incident_audit_events_incident_order_idx
    ON incident_audit_events (incident_id, revision, occurred_at, event_id);
