-- Moderation: numbered cases (the case id IS the case number) and member reports.
CREATE TABLE IF NOT EXISTS mod_cases (
    id               SERIAL PRIMARY KEY,
    action           TEXT NOT NULL CHECK (action IN ('warn','note','timeout','untimeout','kick','ban','unban')),
    target_id        BIGINT NOT NULL,
    moderator_id     BIGINT,                      -- NULL = automatic (automod / system)
    reason           TEXT NOT NULL DEFAULT '',
    source           TEXT NOT NULL DEFAULT 'manual',   -- manual | automod | escalation | report | system
    duration_seconds INT,                         -- timeouts and temporary bans
    expires_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at       TIMESTAMPTZ,                 -- pardoned warn / lifted ban or timeout
    revoked_by       BIGINT,
    revoke_reason    TEXT,
    report_id        INT,
    log_message_id   BIGINT
);
CREATE INDEX IF NOT EXISTS mod_cases_target_idx ON mod_cases (target_id, id DESC);
CREATE INDEX IF NOT EXISTS mod_cases_expiry_idx ON mod_cases (expires_at) WHERE action = 'ban' AND revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS mod_reports (
    id             SERIAL PRIMARY KEY,
    reporter_id    BIGINT NOT NULL,
    target_id      BIGINT NOT NULL,
    reason         TEXT NOT NULL,
    message_link   TEXT,
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','reviewing','closed','dismissed')),
    handled_by     BIGINT,
    case_id        INT,
    log_message_id BIGINT UNIQUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS mod_reports_status_idx ON mod_reports (status, id DESC);
