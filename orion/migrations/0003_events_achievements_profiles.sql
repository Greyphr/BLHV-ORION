-- Events (the store the Menace events cog expected but never shipped), achievements, and profile fields.
CREATE TABLE IF NOT EXISTS events (
    id             SERIAL PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('event', 'scrim', 'tournament')),
    title          TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    starts_at      TIMESTAMPTZ NOT NULL,
    reg_closes_at  TIMESTAMPTZ,
    slots          INT NOT NULL DEFAULT 0,          -- 0 = unlimited
    channel_id     BIGINT NOT NULL,
    notify_role_id BIGINT,
    extra          JSONB NOT NULL DEFAULT '{}',     -- game, format, prize, rank
    created_by     BIGINT NOT NULL,
    message_id     BIGINT UNIQUE,
    status         TEXT NOT NULL DEFAULT 'open',    -- open | cancelled | finished
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_start_idx ON events (starts_at DESC);

CREATE TABLE IF NOT EXISTS event_signups (
    event_id  INT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id   BIGINT NOT NULL,
    seq       BIGSERIAL,                            -- strict sign-up order (decides the waitlist)
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attended  BOOLEAN,                              -- NULL = not recorded yet
    PRIMARY KEY (event_id, user_id)
);
CREATE INDEX IF NOT EXISTS event_signups_user_idx ON event_signups (user_id, joined_at DESC);

CREATE TABLE IF NOT EXISTS achievements (
    id          SERIAL PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,               -- stable identifier, e.g. first-breakthrough
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    icon        TEXT NOT NULL DEFAULT '🏅',
    category    TEXT NOT NULL DEFAULT 'general',
    rarity      TEXT NOT NULL DEFAULT 'common' CHECK (rarity IN ('common', 'uncommon', 'rare', 'epic', 'legendary')),
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_by  BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS member_achievements (
    user_id        BIGINT NOT NULL,
    achievement_id INT NOT NULL REFERENCES achievements(id) ON DELETE CASCADE,
    granted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by     BIGINT,                          -- NULL = automatic
    PRIMARY KEY (user_id, achievement_id)
);
CREATE INDEX IF NOT EXISTS member_achievements_user_idx ON member_achievements (user_id, granted_at DESC);

-- The date a member joined the Discord server (copied from Discord so a future API doesn't need Discord to show it).
ALTER TABLE users ADD COLUMN IF NOT EXISTS joined_server_at TIMESTAMPTZ;
