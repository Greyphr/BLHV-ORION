-- Build harness (/build): features the AI wrote for the bot, with every version kept so a bad one can be rolled back.
-- Named builder_* on purpose: generated features may only create tables called gen_<slug>[_...], so the two never collide.
CREATE TABLE IF NOT EXISTS builder_features (
    slug            TEXT PRIMARY KEY,                  -- also the module name: gen_<slug>
    title           TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT FALSE,    -- loaded at startup when TRUE
    active_build_id INT,                               -- the version that runs when enabled
    created_by      BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS builder_builds (
    id          SERIAL PRIMARY KEY,
    slug        TEXT NOT NULL,
    version     INT  NOT NULL,
    title       TEXT NOT NULL,
    request     TEXT NOT NULL,                         -- what the Patriarch asked for (or the change request)
    summary     TEXT,
    notes       TEXT,                                  -- the AI's assumptions for the reviewer
    code        TEXT NOT NULL,
    schema_sql  TEXT,                                  -- additive CREATE ... IF NOT EXISTS for the feature's own gen_ tables
    status      TEXT NOT NULL DEFAULT 'pending',       -- pending | active | superseded | rejected | failed
    error       TEXT,
    model       TEXT,
    created_by  BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at  TIMESTAMPTZ,
    UNIQUE (slug, version)
);
CREATE INDEX IF NOT EXISTS builder_builds_slug_idx ON builder_builds (slug, version DESC);
CREATE INDEX IF NOT EXISTS builder_builds_pending_idx ON builder_builds (status) WHERE status = 'pending';
