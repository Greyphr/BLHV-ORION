-- Baseline: every table inherited from the Menace codebase. Idempotent.

-- ---- core ----
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id        BIGINT PRIMARY KEY,
    xp             BIGINT NOT NULL DEFAULT 0,
    messages       BIGINT NOT NULL DEFAULT 0,
    voice_minutes  BIGINT NOT NULL DEFAULT 0,
    last_msg_xp    TIMESTAMPTZ,
    last_daily     DATE,
    voice_day      DATE,
    voice_xp_today INT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS users_xp_idx ON users (xp DESC);
CREATE TABLE IF NOT EXISTS xp_period (
    period_type TEXT NOT NULL,
    period_key  TEXT NOT NULL,
    user_id     BIGINT NOT NULL,
    xp          BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (period_type, period_key, user_id)
);
CREATE INDEX IF NOT EXISTS xp_period_rank_idx ON xp_period (period_type, period_key, xp DESC);
CREATE TABLE IF NOT EXISTS xp_exclusions (
    kind      TEXT NOT NULL,      -- 'channel' (also categories) or 'role'
    target_id BIGINT NOT NULL,
    PRIMARY KEY (kind, target_id)
);
CREATE TABLE IF NOT EXISTS announcement_images (
    id         SERIAL PRIMARY KEY,
    data       BYTEA NOT NULL,
    ext        TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS announcement_templates (
    name       TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS twitch_streamers (
    login             TEXT PRIMARY KEY,           -- lower-case twitch login
    display_name      TEXT NOT NULL,
    profile_image_url TEXT NOT NULL DEFAULT '',
    added_by          BIGINT NOT NULL,
    is_live           BOOLEAN NOT NULL DEFAULT FALSE,
    stream_id         TEXT,                       -- id of the stream we announced
    missed_checks     INT NOT NULL DEFAULT 0,
    added_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS announcements (
    id           SERIAL PRIMARY KEY,
    channel_id   BIGINT NOT NULL,
    message_id   BIGINT NOT NULL UNIQUE,
    author_id    BIGINT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    data         JSONB NOT NULL,
    mentions     JSONB NOT NULL DEFAULT '[]',
    ping_mode    TEXT NOT NULL DEFAULT 'none',
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    edited_at    TIMESTAMPTZ
);

-- ---- giveaways ----
CREATE TABLE IF NOT EXISTS giveaways (
    id                  SERIAL PRIMARY KEY,
    title               TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    extra_message       TEXT NOT NULL DEFAULT '',
    prize_pool          TEXT NOT NULL DEFAULT '',
    body                TEXT NOT NULL DEFAULT '',    -- the big text box (markdown + {placeholders})
    banner_id           INT,                         -- top banner (announcement_images.id)
    bottom_banner_id    INT,
    thumb_id            INT,                         -- small side image
    accent              INT,                         -- card colour, NULL = default
    buttons             JSONB NOT NULL DEFAULT '{}', -- custom button text: {"enter": "🔥 Join", ...}
    winners_count       INT NOT NULL,
    channel_id          BIGINT NOT NULL,
    message_id          BIGINT UNIQUE,               -- NULL until the public message has been posted
    results_message_id  BIGINT,
    interval_seconds    INT NOT NULL,
    starts_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    ends_at             TIMESTAMPTZ NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',      -- active | ended | cancelled
    eligible_roles      JSONB NOT NULL DEFAULT '[]',         -- role NAMES; having any one is enough
    allow_multiple_wins BOOLEAN NOT NULL DEFAULT FALSE,
    claim_hours         INT,                                 -- NULL = no claim deadline
    auto_reassign       BOOLEAN NOT NULL DEFAULT FALSE,
    results_posted      BOOLEAN NOT NULL DEFAULT FALSE,
    announce_attempts   INT NOT NULL DEFAULT 0,
    created_by          BIGINT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at            TIMESTAMPTZ
);
-- upgrades from the first version of this feature
ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS body TEXT NOT NULL DEFAULT '';
ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS bottom_banner_id INT;
ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS thumb_id INT;
ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS accent INT;
ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS buttons JSONB NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS giveaways_status_idx ON giveaways (status, ends_at);
CREATE TABLE IF NOT EXISTS giveaway_rewards (
    id                SERIAL PRIMARY KEY,
    giveaway_id       INT NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
    position          INT NOT NULL,
    name              TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    value             TEXT NOT NULL DEFAULT '',
    reward_type       TEXT NOT NULL DEFAULT 'custom',
    secret_enc        BYTEA,                               -- encrypted code / delivery instructions
    winner_id         BIGINT,
    status            TEXT NOT NULL DEFAULT 'open',        -- open | assigned | claimed | delivered
    claim_deadline_at TIMESTAMPTZ,
    claimed_at        TIMESTAMPTZ,
    delivered_at      TIMESTAMPTZ,
    delivered_by      BIGINT,
    role_assigned     BOOLEAN NOT NULL DEFAULT FALSE,
    role_attempts     INT NOT NULL DEFAULT 0,
    reassign_count    INT NOT NULL DEFAULT 0,
    deadline_flagged  BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (giveaway_id, position)
);
CREATE INDEX IF NOT EXISTS giveaway_rewards_winner_idx ON giveaway_rewards (winner_id);
CREATE TABLE IF NOT EXISTS giveaway_participants (
    giveaway_id   INT NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
    user_id       BIGINT NOT NULL,
    entries       INT NOT NULL DEFAULT 0,
    joined_at     TIMESTAMPTZ NOT NULL,
    last_entry_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
);
CREATE TABLE IF NOT EXISTS giveaway_entries (
    id          BIGSERIAL PRIMARY KEY,
    giveaway_id INT NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
    user_id     BIGINT NOT NULL,
    kind        TEXT NOT NULL,                             -- join | checkin
    created_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS giveaway_entries_idx ON giveaway_entries (giveaway_id, user_id, created_at);
CREATE TABLE IF NOT EXISTS giveaway_draws (
    id          SERIAL PRIMARY KEY,
    giveaway_id INT NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                             -- draw | reroll | reassign
    record      JSONB NOT NULL,                            -- method, weights snapshot, who was excluded
    picks       JSONB NOT NULL,                            -- [{"reward_id":..,"user_id":..}]
    created_by  BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS giveaway_audit (
    id          BIGSERIAL PRIMARY KEY,
    giveaway_id INT,                                       -- no FK: the trail survives even if a giveaway is removed
    actor_id    BIGINT,                                    -- NULL = the bot itself
    action      TEXT NOT NULL,
    details     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS giveaway_audit_idx ON giveaway_audit (giveaway_id, id DESC);

-- ---- polls ----
CREATE TABLE IF NOT EXISTS polls (
    id                SERIAL PRIMARY KEY,
    question          TEXT NOT NULL,
    options           JSONB NOT NULL,
    anonymous         BOOLEAN NOT NULL DEFAULT FALSE,
    allow_change      BOOLEAN NOT NULL DEFAULT TRUE,
    eligible_role_ids JSONB NOT NULL DEFAULT '[]',
    channel_id        BIGINT,
    message_id        BIGINT UNIQUE,
    created_by        BIGINT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    ends_at           TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'active'      -- active | ended
);
ALTER TABLE polls ADD COLUMN IF NOT EXISTS banner_id INT;     -- announcement_images.id, shown as the poll's banner
CREATE INDEX IF NOT EXISTS polls_status_idx ON polls (status, ends_at);
CREATE TABLE IF NOT EXISTS poll_votes (
    poll_id    INT NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    user_id    BIGINT NOT NULL,
    option_idx INT NOT NULL,
    voted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_at TIMESTAMPTZ,
    PRIMARY KEY (poll_id, user_id)
);

-- ---- tickets ----
CREATE TABLE IF NOT EXISTS ticket_types (
    id             SERIAL PRIMARY KEY,
    name           TEXT NOT NULL,
    emoji          TEXT NOT NULL DEFAULT '🎫',
    description    TEXT NOT NULL DEFAULT '',
    category_id    BIGINT,                          -- NULL = the shared Tickets category
    staff_role_ids JSONB NOT NULL DEFAULT '[]',     -- empty = Sect Admin + the approved staff roles
    ping_role_id   BIGINT,                          -- legacy single role: migrated into ping_role_ids below
    ping_role_ids  JSONB NOT NULL DEFAULT '[]',     -- empty = the server-wide ticket ping roles
    questions      JSONB NOT NULL DEFAULT '[]',     -- intake form, up to 5 questions
    position       INT NOT NULL DEFAULT 0,
    enabled        BOOLEAN NOT NULL DEFAULT TRUE
);
ALTER TABLE ticket_types ADD COLUMN IF NOT EXISTS ping_role_ids JSONB NOT NULL DEFAULT '[]';
UPDATE ticket_types SET ping_role_ids = jsonb_build_array(ping_role_id), ping_role_id = NULL
    WHERE ping_role_id IS NOT NULL;                 -- one-off carry-over of the old single ping role
CREATE TABLE IF NOT EXISTS tickets (
    id                   SERIAL PRIMARY KEY,
    type_id              INT REFERENCES ticket_types(id) ON DELETE SET NULL,
    type_name            TEXT NOT NULL DEFAULT 'Support',
    owner_id             BIGINT NOT NULL,
    channel_id           BIGINT UNIQUE,
    control_message_id   BIGINT,                            -- the message with Close / Claim / Staff tools
    status               TEXT NOT NULL DEFAULT 'open',      -- open | closed | deleted
    priority             TEXT NOT NULL DEFAULT 'normal',
    claimed_by           BIGINT,
    answers              JSONB NOT NULL DEFAULT '[]',       -- [{"q": ..., "a": ...}]
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity_at     TIMESTAMPTZ,
    first_staff_reply_at TIMESTAMPTZ,
    warned_at            TIMESTAMPTZ,
    closed_at            TIMESTAMPTZ,
    closed_by            BIGINT,
    close_reason         TEXT,
    transcript_url       TEXT,
    deleted_at           TIMESTAMPTZ,
    rating               INT,
    rating_comment       TEXT,
    rated_at             TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS tickets_owner_idx ON tickets (owner_id, status);
CREATE INDEX IF NOT EXISTS tickets_status_idx ON tickets (status);
CREATE TABLE IF NOT EXISTS ticket_notes (
    id         SERIAL PRIMARY KEY,
    ticket_id  INT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    author_id  BIGINT NOT NULL,
    text       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ticket_blacklist (
    user_id  BIGINT PRIMARY KEY,
    reason   TEXT NOT NULL DEFAULT '',
    added_by BIGINT NOT NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---- keys ----
CREATE TABLE IF NOT EXISTS beta_key_claims (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    amount     INT NOT NULL CHECK (amount > 0),
    kind       TEXT NOT NULL,                     -- self (/keys) | grant (/givekeys); later maybe 'giveaway'
    granted_by BIGINT,                            -- the admin, for grants
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS beta_key_self_once ON beta_key_claims (user_id) WHERE kind = 'self';
CREATE INDEX IF NOT EXISTS beta_key_claims_user_idx ON beta_key_claims (user_id);
CREATE TABLE IF NOT EXISTS beta_keys (
    id          BIGSERIAL PRIMARY KEY,
    code_hash   TEXT NOT NULL UNIQUE,             -- keyed fingerprint of the code (never the code itself)
    code_enc    BYTEA NOT NULL,                   -- the code, encrypted
    claim_id    BIGINT REFERENCES beta_key_claims(id),
    owner_id    BIGINT,                           -- NULL = still in stock
    assigned_at TIMESTAMPTZ,
    imported_by BIGINT,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((owner_id IS NULL) = (claim_id IS NULL))
);
CREATE INDEX IF NOT EXISTS beta_keys_free_idx ON beta_keys (id) WHERE owner_id IS NULL;
-- two separate stocks: 'regular' (testers) and 'creator' (content creators). Older databases get the column added,
-- and everything already loaded stays 'regular'.
ALTER TABLE beta_keys ADD COLUMN IF NOT EXISTS pool TEXT NOT NULL DEFAULT 'regular';
ALTER TABLE beta_key_claims ADD COLUMN IF NOT EXISTS pool TEXT NOT NULL DEFAULT 'regular';
CREATE INDEX IF NOT EXISTS beta_keys_free_pool_idx ON beta_keys (pool, id) WHERE owner_id IS NULL;
CREATE INDEX IF NOT EXISTS beta_keys_owner_idx ON beta_keys (owner_id) WHERE owner_id IS NOT NULL;
