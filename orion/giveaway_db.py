"""Giveaway tables + queries (mixed into Database).

Design rules that keep the system honest:
  * giveaway_entries is an append-only LEDGER: one row per entry earned. The draw weights come from counting the
    ledger, never from the cached giveaway_participants.entries counter.
  * Joining / checking in runs in one transaction with the participant row locked, so double clicks, two devices
    and bot restarts can never grant a second entry inside one interval.
  * Claiming is a single UPDATE ... WHERE status='assigned' RETURNING, so a reward can only be claimed once.
  * Reward secrets arrive here ALREADY ENCRYPTED (see vault.py); this layer never sees a plain-text code and the
    audit log never stores one.
"""
from datetime import datetime
from typing import NamedTuple, Optional

from .giveaways import evaluate_entry

GIVEAWAY_SCHEMA = """
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
"""

GW_EDITABLE = {"title", "description", "extra_message", "prize_pool", "banner_id", "interval_seconds",
               "body", "bottom_banner_id", "thumb_id", "accent", "buttons", "eligible_roles", "allow_multiple_wins", "claim_hours", "auto_reassign"}
REWARD_EDITABLE = {"name", "description", "value", "secret_enc"}


class EntryOutcome(NamedTuple):
    state: str                       # joined | checked_in | cooldown | closed
    entries: int
    next_at: Optional[datetime]


class GiveawayStore:
    """Mixin: expects `self.pool` (asyncpg pool)."""

    # ------------------------------------------------------------ giveaways
    async def gw_create(self, g: dict, rewards: list[dict]) -> int:
        async with self.pool.acquire() as c, c.transaction():
            gid = await c.fetchval(
                "INSERT INTO giveaways(title, description, extra_message, prize_pool, body, banner_id, "
                "bottom_banner_id, thumb_id, accent, buttons, winners_count, channel_id, interval_seconds, starts_at, "
                "ends_at, eligible_roles, allow_multiple_wins, claim_hours, auto_reassign, created_by) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20) RETURNING id",
                g["title"], g.get("description", ""), g.get("extra_message", ""), g["prize_pool"], g.get("body", ""),
                g.get("banner_id"), g.get("bottom_banner_id"), g.get("thumb_id"), g.get("accent"),
                g.get("buttons") or {}, len(rewards), g["channel_id"], g["interval_seconds"], g["starts_at"],
                g["ends_at"], g["eligible_roles"], g["allow_multiple_wins"], g["claim_hours"], g["auto_reassign"],
                g["created_by"])
            for n, r in enumerate(rewards, 1):
                await c.execute(
                    "INSERT INTO giveaway_rewards(giveaway_id, position, name, description, value, reward_type, "
                    "secret_enc) VALUES($1,$2,$3,$4,$5,$6,$7)",
                    gid, n, r["name"], r["description"], r["value"], r["reward_type"], r["secret_enc"])
            return gid

    async def gw_set_message(self, gid: int, message_id: int):
        await self.pool.execute("UPDATE giveaways SET message_id=$2 WHERE id=$1", gid, message_id)

    async def gw_delete(self, gid: int):
        await self.pool.execute("DELETE FROM giveaways WHERE id=$1", gid)

    async def gw_get(self, gid: int):
        return await self.pool.fetchrow("SELECT * FROM giveaways WHERE id=$1", gid)

    async def gw_by_message(self, message_id: int):
        """The giveaway behind either its public message or its results message."""
        return await self.pool.fetchrow(
            "SELECT * FROM giveaways WHERE message_id=$1 OR results_message_id=$1", message_id)

    async def gw_list(self, limit: int = 25):
        return await self.pool.fetch(
            "SELECT * FROM giveaways WHERE message_id IS NOT NULL "
            "ORDER BY (status='active') DESC, created_at DESC LIMIT $1", limit)

    async def gw_active_ids(self) -> list[int]:
        return [r["id"] for r in await self.pool.fetch(
            "SELECT id FROM giveaways WHERE status='active' AND message_id IS NOT NULL")]

    async def gw_due(self, now: datetime):
        return await self.pool.fetch(
            "SELECT * FROM giveaways WHERE status='active' AND message_id IS NOT NULL AND ends_at <= $1", now)

    async def gw_unannounced(self):
        return await self.pool.fetch(
            "SELECT * FROM giveaways WHERE status='ended' AND NOT results_posted AND message_id IS NOT NULL")

    async def gw_update(self, gid: int, **fields):
        bad = set(fields) - GW_EDITABLE
        if bad:
            raise ValueError(f"not editable: {bad}")
        sets = ", ".join(f"{k} = ${n}" for n, k in enumerate(fields, start=2))
        await self.pool.execute(f"UPDATE giveaways SET {sets} WHERE id=$1", gid, *fields.values())

    async def gw_extend(self, gid: int, seconds: int, now: datetime) -> Optional[datetime]:
        return await self.pool.fetchval(
            "UPDATE giveaways SET ends_at = ends_at + make_interval(secs => $2::float8) "
            "WHERE id=$1 AND status='active' AND ends_at > $3 RETURNING ends_at", gid, float(seconds), now)

    async def gw_close_entries(self, gid: int, now: datetime):
        """Entries stop the instant a draw begins (matters for 'end early')."""
        await self.pool.execute(
            "UPDATE giveaways SET ends_at = LEAST(ends_at, $2) WHERE id=$1 AND status='active'", gid, now)

    async def gw_cancel(self, gid: int, now: datetime) -> bool:
        return await self.pool.fetchval(
            "UPDATE giveaways SET status='cancelled', ended_at=$2, results_posted=TRUE "
            "WHERE id=$1 AND status='active' RETURNING id", gid, now) is not None

    async def gw_mark_announced(self, gid: int, results_message_id: Optional[int]):
        await self.pool.execute(
            "UPDATE giveaways SET results_posted=TRUE, results_message_id=COALESCE($2, results_message_id) "
            "WHERE id=$1", gid, results_message_id)

    async def gw_bump_announce(self, gid: int) -> int:
        return await self.pool.fetchval(
            "UPDATE giveaways SET announce_attempts = announce_attempts + 1 WHERE id=$1 "
            "RETURNING announce_attempts", gid)

    # -------------------------------------------------------------- rewards
    async def gw_rewards(self, gid: int):
        return await self.pool.fetch("SELECT * FROM giveaway_rewards WHERE giveaway_id=$1 ORDER BY position", gid)

    async def reward_get(self, rid: int):
        return await self.pool.fetchrow("SELECT * FROM giveaway_rewards WHERE id=$1", rid)

    async def reward_update(self, rid: int, **fields) -> bool:
        """Only slots nobody has claimed can change (a delivered code must stay what it was)."""
        bad = set(fields) - REWARD_EDITABLE
        if bad:
            raise ValueError(f"not editable: {bad}")
        sets = ", ".join(f"{k} = ${n}" for n, k in enumerate(fields, start=2))
        return await self.pool.fetchval(
            f"UPDATE giveaway_rewards SET {sets} WHERE id=$1 AND status IN ('open','assigned') RETURNING id",
            rid, *fields.values()) is not None

    async def reward_add(self, gid: int, r: dict) -> Optional[int]:
        """Add a slot to a LIVE giveaway (raises the winner count by one)."""
        async with self.pool.acquire() as c, c.transaction():
            if await c.fetchval("SELECT status FROM giveaways WHERE id=$1 FOR UPDATE", gid) != "active":
                return None
            pos = await c.fetchval("SELECT COALESCE(MAX(position), 0) + 1 FROM giveaway_rewards WHERE giveaway_id=$1", gid)
            rid = await c.fetchval(
                "INSERT INTO giveaway_rewards(giveaway_id, position, name, description, value, reward_type, secret_enc) "
                "VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id",
                gid, pos, r["name"], r["description"], r["value"], r["reward_type"], r["secret_enc"])
            await c.execute("UPDATE giveaways SET winners_count = winners_count + 1 WHERE id=$1", gid)
            return rid

    async def reward_remove_last(self, gid: int) -> bool:
        async with self.pool.acquire() as c, c.transaction():
            if await c.fetchval("SELECT status FROM giveaways WHERE id=$1 FOR UPDATE", gid) != "active":
                return False
            n = await c.fetchval("SELECT COUNT(*) FROM giveaway_rewards WHERE giveaway_id=$1", gid)
            if n <= 1:
                return False
            await c.execute("DELETE FROM giveaway_rewards WHERE id = (SELECT id FROM giveaway_rewards "
                            "WHERE giveaway_id=$1 ORDER BY position DESC LIMIT 1)", gid)
            await c.execute("UPDATE giveaways SET winners_count = winners_count - 1 WHERE id=$1", gid)
            return True

    async def gw_user_rewards(self, gid: int, user_id: int):
        return await self.pool.fetch(
            "SELECT * FROM giveaway_rewards WHERE giveaway_id=$1 AND winner_id=$2 ORDER BY position", gid, user_id)

    async def reward_claim(self, rid: int, user_id: int, now: datetime):
        """Atomic: succeeds for exactly one caller, only for the winner, only inside the claim window."""
        return await self.pool.fetchrow(
            "UPDATE giveaway_rewards SET status='claimed', claimed_at=$3 "
            "WHERE id=$1 AND winner_id=$2 AND status='assigned' "
            "AND (claim_deadline_at IS NULL OR claim_deadline_at > $3) RETURNING *", rid, user_id, now)

    async def reward_unclaim(self, rid: int):
        """Undo a claim whose delivery message could not be sent, so the winner can try again."""
        await self.pool.execute(
            "UPDATE giveaway_rewards SET status='assigned', claimed_at=NULL WHERE id=$1 AND status='claimed'", rid)

    async def reward_mark_delivered(self, rid: int, admin_id: int, now: datetime):
        return await self.pool.fetchrow(
            "UPDATE giveaway_rewards SET status='delivered', delivered_at=$3, delivered_by=$2 "
            "WHERE id=$1 AND status IN ('assigned','claimed') AND winner_id IS NOT NULL RETURNING *",
            rid, admin_id, now)

    async def reward_set_winner(self, rid: int, user_id: int, now: datetime, claim_hours: Optional[int],
                                auto: bool = False):
        """Give an UNCLAIMED slot to a new winner (reroll / claim-deadline reassignment)."""
        return await self.pool.fetchrow(
            "UPDATE giveaway_rewards SET winner_id=$2, status='assigned', role_assigned=FALSE, role_attempts=0, "
            "deadline_flagged=FALSE, claimed_at=NULL, reassign_count = reassign_count + $5, "
            "claim_deadline_at = $3::timestamptz + make_interval(hours => $4::int) "
            "WHERE id=$1 AND status IN ('open','assigned') RETURNING *",
            rid, user_id, now, claim_hours, 1 if auto else 0)

    async def reward_flag_deadline(self, rid: int):
        await self.pool.execute("UPDATE giveaway_rewards SET deadline_flagged=TRUE WHERE id=$1", rid)

    async def rewards_past_deadline(self, now: datetime):
        return await self.pool.fetch(
            "SELECT r.*, g.auto_reassign, g.claim_hours, g.title AS giveaway_title FROM giveaway_rewards r "
            "JOIN giveaways g ON g.id = r.giveaway_id "
            "WHERE g.status='ended' AND r.status='assigned' AND r.claim_deadline_at IS NOT NULL "
            "AND r.claim_deadline_at <= $1 AND NOT r.deadline_flagged", now)

    async def rewards_needing_role(self, max_attempts: int, gid: Optional[int] = None):
        return await self.pool.fetch(
            "SELECT r.* FROM giveaway_rewards r JOIN giveaways g ON g.id = r.giveaway_id "
            "WHERE g.status='ended' AND r.winner_id IS NOT NULL AND NOT r.role_assigned AND r.role_attempts < $1 "
            "AND ($2::int IS NULL OR r.giveaway_id = $2)", max_attempts, gid)

    async def rewards_role_done(self, ids: list[int]):
        await self.pool.execute("UPDATE giveaway_rewards SET role_assigned=TRUE WHERE id = ANY($1::int[])", ids)

    async def rewards_role_failed(self, ids: list[int], gone: bool = False):
        """gone=True: the winner left the server, stop retrying."""
        await self.pool.execute(
            "UPDATE giveaway_rewards SET role_attempts = CASE WHEN $2 THEN 1000 ELSE role_attempts + 1 END "
            "WHERE id = ANY($1::int[])", ids, gone)

    async def rewards_reset_role_attempts(self, gid: int):
        """Staff fixed the role permissions: let the bot try again from scratch."""
        await self.pool.execute(
            "UPDATE giveaway_rewards SET role_attempts=0 "
            "WHERE giveaway_id=$1 AND winner_id IS NOT NULL AND NOT role_assigned", gid)

    async def image_ext(self, image_id: int) -> Optional[str]:
        return await self.pool.fetchval("SELECT ext FROM announcement_images WHERE id=$1", image_id)

    async def user_has_other_wins(self, user_id: int, except_reward: int) -> bool:
        return await self.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM giveaway_rewards WHERE winner_id=$1 AND id <> $2 "
            "AND status IN ('assigned','claimed','delivered'))", user_id, except_reward)

    # ------------------------------------------------------------- entries
    async def gw_enter(self, gid: int, user_id: int, now: datetime) -> EntryOutcome:
        """Join or check in, atomically. The rule itself lives in giveaways.evaluate_entry (unit-tested)."""
        async with self.pool.acquire() as c, c.transaction():
            gw = await c.fetchrow(
                "SELECT status, ends_at, interval_seconds FROM giveaways WHERE id=$1 FOR SHARE", gid)
            if gw is None or gw["status"] != "active":
                return EntryOutcome("closed", 0, None)
            row = await c.fetchrow(
                "SELECT entries, last_entry_at FROM giveaway_participants "
                "WHERE giveaway_id=$1 AND user_id=$2 FOR UPDATE", gid, user_id)
            if row is None:
                state, nxt = evaluate_entry(None, now, gw["ends_at"], gw["interval_seconds"])
                if state == "closed":
                    return EntryOutcome("closed", 0, None)
                made = await c.fetchval(
                    "INSERT INTO giveaway_participants(giveaway_id, user_id, entries, joined_at, last_entry_at) "
                    "VALUES($1,$2,1,$3,$3) ON CONFLICT DO NOTHING RETURNING 1", gid, user_id, now)
                if made:
                    await c.execute("INSERT INTO giveaway_entries(giveaway_id, user_id, kind, created_at) "
                                    "VALUES($1,$2,'join',$3)", gid, user_id, now)
                    return EntryOutcome("joined", 1, nxt)
                # lost a race with our own second click: fall through and treat it as an existing participant
                row = await c.fetchrow(
                    "SELECT entries, last_entry_at FROM giveaway_participants "
                    "WHERE giveaway_id=$1 AND user_id=$2 FOR UPDATE", gid, user_id)
            state, nxt = evaluate_entry(row["last_entry_at"], now, gw["ends_at"], gw["interval_seconds"])
            if state == "checked_in":
                await c.execute("UPDATE giveaway_participants SET entries = entries + 1, last_entry_at=$3 "
                                "WHERE giveaway_id=$1 AND user_id=$2", gid, user_id, now)
                await c.execute("INSERT INTO giveaway_entries(giveaway_id, user_id, kind, created_at) "
                                "VALUES($1,$2,'checkin',$3)", gid, user_id, now)
                return EntryOutcome("checked_in", row["entries"] + 1, nxt)
            return EntryOutcome(state, row["entries"], nxt)

    async def gw_participant(self, gid: int, user_id: int):
        return await self.pool.fetchrow(
            "SELECT * FROM giveaway_participants WHERE giveaway_id=$1 AND user_id=$2", gid, user_id)

    async def gw_stats(self, gid: int) -> tuple[int, int]:
        r = await self.pool.fetchrow(
            "SELECT COUNT(*)::int AS p, COALESCE(SUM(entries), 0)::int AS e "
            "FROM giveaway_participants WHERE giveaway_id=$1", gid)
        return r["p"], r["e"]

    async def gw_participants(self, gid: int):
        return await self.pool.fetch(
            "SELECT user_id, entries, joined_at, last_entry_at FROM giveaway_participants "
            "WHERE giveaway_id=$1 ORDER BY entries DESC, joined_at, user_id", gid)

    async def gw_weights(self, gid: int) -> dict[int, int]:
        """Entries per user, counted from the LEDGER (the source of truth for the draw)."""
        rows = await self.pool.fetch(
            "SELECT user_id, COUNT(*)::int AS n FROM giveaway_entries WHERE giveaway_id=$1 GROUP BY user_id", gid)
        return {r["user_id"]: r["n"] for r in rows}

    async def gw_ledger(self, gid: int):
        return await self.pool.fetch(
            "SELECT id, user_id, kind, created_at FROM giveaway_entries WHERE giveaway_id=$1 ORDER BY id", gid)

    # --------------------------------------------------------------- draws
    async def gw_finalize(self, gid: int, assignments: list[tuple[int, int]], record: dict, now: datetime,
                          claim_hours: Optional[int], actor_id: Optional[int], kind: str = "draw") -> bool:
        """Close the giveaway and hand the slots to the winners in ONE transaction.
        Returns False if it was no longer active (someone else already ended it)."""
        async with self.pool.acquire() as c, c.transaction():
            done = await c.fetchval(
                "UPDATE giveaways SET status='ended', ended_at=$2 WHERE id=$1 AND status='active' RETURNING id",
                gid, now)
            if done is None:
                return False
            for rid, uid in assignments:
                await c.execute(
                    "UPDATE giveaway_rewards SET winner_id=$2, status='assigned', "
                    "claim_deadline_at = $3::timestamptz + make_interval(hours => $4::int) WHERE id=$1", rid, uid, now, claim_hours)
            await c.execute(
                "INSERT INTO giveaway_draws(giveaway_id, kind, record, picks, created_by) VALUES($1,$2,$3,$4,$5)",
                gid, kind, record, [{"reward_id": r, "user_id": u} for r, u in assignments], actor_id)
            return True

    async def draw_add(self, gid: int, kind: str, record: dict, picks: list[dict], actor_id: Optional[int]):
        await self.pool.execute(
            "INSERT INTO giveaway_draws(giveaway_id, kind, record, picks, created_by) VALUES($1,$2,$3,$4,$5)",
            gid, kind, record, picks, actor_id)

    async def draw_picked_users(self, gid: int) -> set[int]:
        """Everyone who was EVER picked in this giveaway (replaced winners are never re-picked)."""
        rows = await self.pool.fetch(
            "SELECT DISTINCT (p->>'user_id')::bigint AS u FROM giveaway_draws d, jsonb_array_elements(d.picks) p "
            "WHERE d.giveaway_id=$1", gid)
        return {r["u"] for r in rows}

    # --------------------------------------------------------------- audit
    async def audit(self, gid: Optional[int], actor_id: Optional[int], action: str, details: Optional[dict] = None):
        await self.pool.execute(
            "INSERT INTO giveaway_audit(giveaway_id, actor_id, action, details) VALUES($1,$2,$3,$4)",
            gid, actor_id, action, details or {})

    async def audit_list(self, gid: int, limit: int = 15):
        return await self.pool.fetch(
            "SELECT * FROM giveaway_audit WHERE giveaway_id=$1 ORDER BY id DESC LIMIT $2", gid, limit)
