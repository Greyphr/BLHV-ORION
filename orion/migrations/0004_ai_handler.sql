-- AI ticket handler: per-ticket state, knowledge base, activity log.
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS ai_state    TEXT NOT NULL DEFAULT 'off';   -- off | active | escalated | done
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS ai_replies  INT  NOT NULL DEFAULT 0;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS ai_category TEXT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS ai_summary  TEXT;

CREATE TABLE IF NOT EXISTS ai_knowledge (
    id         SERIAL PRIMARY KEY,
    title      TEXT NOT NULL,
    content    TEXT NOT NULL,
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    created_by BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_logs (
    id         BIGSERIAL PRIMARY KEY,
    ticket_id  INT,                            -- not a foreign key: logs outlive deleted tickets
    kind       TEXT NOT NULL,                  -- reply | escalate | close | error | skip | handoff
    confidence REAL,
    category   TEXT,
    reason     TEXT,
    summary    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ai_logs_ticket_idx ON ai_logs (ticket_id, id DESC);
CREATE INDEX IF NOT EXISTS ai_logs_recent_idx ON ai_logs (id DESC);
