-- Orion: progression history (every rank change) and the administrative audit log.
CREATE TABLE IF NOT EXISTS progression_history (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    old_xp     BIGINT NOT NULL,
    new_xp     BIGINT NOT NULL,
    old_level  INT NOT NULL,          -- rank index 0..89 (0 = Earth King 1)
    new_level  INT NOT NULL,
    source     TEXT NOT NULL,         -- activity | admin | event | achievement | system
    actor_id   BIGINT,                -- who caused it (NULL = automatic)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS progression_user_idx ON progression_history (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    actor_id    BIGINT,               -- Discord user ID (NULL = system)
    action      TEXT NOT NULL,        -- e.g. xp.add, config.set, ticket.close
    target_type TEXT,
    target_id   TEXT,
    details     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_created_idx ON audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS audit_actor_idx ON audit_log (actor_id, created_at DESC);
