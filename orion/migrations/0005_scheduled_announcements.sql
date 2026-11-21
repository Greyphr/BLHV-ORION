-- Announcements published later by the scheduler.
CREATE TABLE IF NOT EXISTS scheduled_announcements (
    id          SERIAL PRIMARY KEY,
    channel_id  BIGINT NOT NULL,
    author_id   BIGINT NOT NULL,
    data        JSONB NOT NULL,                   -- the Draft
    mentions    JSONB NOT NULL DEFAULT '[]',
    ping_mode   TEXT NOT NULL DEFAULT 'none',
    publish_at  TIMESTAMPTZ NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | publishing | published | failed | cancelled
    attempts    INT NOT NULL DEFAULT 0,
    message_id  BIGINT,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS scheduled_announcements_due_idx ON scheduled_announcements (status, publish_at);
