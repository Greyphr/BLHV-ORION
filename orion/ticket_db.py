"""Ticket tables + queries (mixed into Database).

  * One row per ticket. `id` is the ticket number shown to people (ticket-0042-name).
  * The open-ticket limit is enforced inside one transaction holding an advisory lock on the member, so a double
    click (or two devices) can never open two tickets past the limit.
  * Staff notes live only here: they are never posted in the ticket channel and never appear in transcripts.
  * Settings (log channel, panel ids, banners, limits, auto-close hours) live in the shared `config` table.
"""
from datetime import datetime, timedelta
from typing import Optional

TICKET_SCHEMA = """
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
"""

TYPE_FIELDS = {"name", "emoji", "description", "category_id", "staff_role_ids", "ping_role_ids", "questions",
               "position", "enabled"}
TICKET_FIELDS = {"priority", "type_id", "type_name", "claimed_by", "control_message_id"}


class TicketStore:
    # ------------------------------------------------------------- types
    async def tt_list(self, enabled_only: bool = False):
        q = "SELECT * FROM ticket_types" + (" WHERE enabled" if enabled_only else "") + " ORDER BY position, id"
        return await self.pool.fetch(q)

    async def tt_get(self, type_id: int):
        return await self.pool.fetchrow("SELECT * FROM ticket_types WHERE id=$1", type_id)

    async def tt_create(self, *, name: str, emoji: str = "🎫", description: str = "", questions=(),
                        staff_role_ids=(), category_id: Optional[int] = None, ping_role_ids=()) -> int:
        return await self.pool.fetchval(
            "INSERT INTO ticket_types(name, emoji, description, questions, staff_role_ids, category_id, ping_role_ids, position)"
            " VALUES($1,$2,$3,$4,$5,$6,$7,(SELECT COALESCE(MAX(position), 0) + 1 FROM ticket_types)) RETURNING id",
            name, emoji, description, list(questions), list(staff_role_ids), category_id, list(ping_role_ids))

    async def tt_update(self, type_id: int, **fields):
        bad = set(fields) - TYPE_FIELDS
        if bad:
            raise ValueError(f"unknown ticket type fields: {bad}")
        if not fields:
            return
        cols = list(fields)
        sets = ", ".join(f"{c}=${n}" for n, c in enumerate(cols, start=2))
        await self.pool.execute(f"UPDATE ticket_types SET {sets} WHERE id=$1", type_id, *[fields[c] for c in cols])

    async def tt_delete(self, type_id: int):
        await self.pool.execute("DELETE FROM ticket_types WHERE id=$1", type_id)

    async def tt_count(self) -> int:
        return await self.pool.fetchval("SELECT COUNT(*) FROM ticket_types")

    # ------------------------------------------------------------ opening
    async def ticket_open(self, *, owner_id: int, type_id: Optional[int], type_name: str, answers: list,
                          max_open: int, now: datetime) -> Optional[int]:
        """Create the ticket row, or return None if the member already has `max_open` open tickets.
        The advisory lock serialises every open by the same member, so double clicks can't slip past the limit."""
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(7342, ($1::bigint % 2147483647)::int)", owner_id)
            n = await c.fetchval("SELECT COUNT(*) FROM tickets WHERE owner_id=$1 AND status='open'", owner_id)
            if n >= max_open:
                return None
            return await c.fetchval(
                "INSERT INTO tickets(owner_id, type_id, type_name, answers, created_at, last_activity_at)"
                " VALUES($1,$2,$3,$4,$5,$5) RETURNING id", owner_id, type_id, type_name, answers, now)

    async def ticket_set_channel(self, ticket_id: int, channel_id: int):
        await self.pool.execute("UPDATE tickets SET channel_id=$2 WHERE id=$1", ticket_id, channel_id)

    async def ticket_sweep_orphans(self, older_than: datetime) -> int:
        """Open rows that never got a channel (the bot died, or the channel-id write failed) would count against the
        member's open-ticket limit forever. Forget the ones older than `older_than`. Returns how many."""
        res = await self.pool.execute(
            "DELETE FROM tickets WHERE status='open' AND channel_id IS NULL AND created_at <= $1", older_than)
        return int(res.rsplit(" ", 1)[-1]) if res.startswith("DELETE") else 0

    async def ticket_discard(self, ticket_id: int):
        """The channel could not be created: forget the row so it doesn't count against the member."""
        await self.pool.execute("DELETE FROM tickets WHERE id=$1", ticket_id)

    # -------------------------------------------------------------- reads
    async def ticket_get(self, ticket_id: int):
        return await self.pool.fetchrow("SELECT * FROM tickets WHERE id=$1", ticket_id)

    async def ticket_by_channel(self, channel_id: int):
        return await self.pool.fetchrow("SELECT * FROM tickets WHERE channel_id=$1", channel_id)

    async def ticket_open_count(self, owner_id: int) -> int:
        return await self.pool.fetchval("SELECT COUNT(*) FROM tickets WHERE owner_id=$1 AND status='open'", owner_id)

    async def ticket_open_list(self, limit: int = 25):
        return await self.pool.fetch(
            "SELECT * FROM tickets WHERE status='open' AND channel_id IS NOT NULL ORDER BY id DESC LIMIT $1", limit)

    async def ticket_history(self, owner_id: int, limit: int = 15):
        return await self.pool.fetch("SELECT * FROM tickets WHERE owner_id=$1 ORDER BY id DESC LIMIT $2", owner_id, limit)

    # ------------------------------------------------------------- writes
    async def ticket_update(self, ticket_id: int, **fields):
        bad = set(fields) - TICKET_FIELDS
        if bad:
            raise ValueError(f"unknown ticket fields: {bad}")
        cols = list(fields)
        sets = ", ".join(f"{c}=${n}" for n, c in enumerate(cols, start=2))
        await self.pool.execute(f"UPDATE tickets SET {sets} WHERE id=$1", ticket_id, *[fields[c] for c in cols])

    async def ticket_claim(self, ticket_id: int, staff_id: int, force: bool = False) -> bool:
        """True if `staff_id` now holds the ticket. Without force, someone else's claim is left alone."""
        return bool(await self.pool.fetchval(
            "UPDATE tickets SET claimed_by=$2 WHERE id=$1 AND status='open' "
            "AND (claimed_by IS NULL OR claimed_by=$2 OR $3) RETURNING 1", ticket_id, staff_id, force))

    async def ticket_unclaim(self, ticket_id: int):
        await self.pool.execute("UPDATE tickets SET claimed_by=NULL WHERE id=$1", ticket_id)

    async def ticket_close(self, ticket_id: int, by: int, reason: Optional[str], now: datetime) -> bool:
        """True only for the caller that actually closed it (so the transcript is made once)."""
        return bool(await self.pool.fetchval(
            "UPDATE tickets SET status='closed', closed_at=$3, closed_by=$2, close_reason=$4, warned_at=NULL "
            "WHERE id=$1 AND status='open' RETURNING 1", ticket_id, by, now, reason))

    async def ticket_reopen(self, ticket_id: int, now: datetime) -> bool:
        return bool(await self.pool.fetchval(
            "UPDATE tickets SET status='open', closed_at=NULL, closed_by=NULL, close_reason=NULL, warned_at=NULL, "
            "ai_state='off', last_activity_at=$2 WHERE id=$1 AND status='closed' RETURNING 1", ticket_id, now))

    async def ticket_set_transcript(self, ticket_id: int, url: str):
        await self.pool.execute("UPDATE tickets SET transcript_url=$2 WHERE id=$1", ticket_id, url)

    async def ticket_mark_deleted(self, ticket_id: int, now: datetime):
        """The channel is gone. An open ticket counts as closed from here on."""
        await self.pool.execute(
            "UPDATE tickets SET status='deleted', deleted_at=$2, closed_at=COALESCE(closed_at, $2), "
            "close_reason=COALESCE(close_reason, 'Channel deleted') WHERE id=$1 AND status<>'deleted'", ticket_id, now)

    async def ticket_activity(self, channel_id: int, now: datetime, staff_reply: bool):
        """A real person wrote in the ticket: push back auto-close and remember the first staff reply."""
        await self.pool.execute(
            "UPDATE tickets SET last_activity_at=$2, warned_at=NULL, "
            "first_staff_reply_at = CASE WHEN $3 AND first_staff_reply_at IS NULL THEN $2 ELSE first_staff_reply_at END "
            "WHERE channel_id=$1 AND status='open'", channel_id, now, staff_reply)

    # ---------------------------------------------------------- auto-close
    async def ticket_idle(self, now: datetime, hours: float):
        """Open tickets that went quiet for `hours` and haven't been warned yet."""
        return await self.pool.fetch(
            "SELECT * FROM tickets WHERE status='open' AND channel_id IS NOT NULL AND warned_at IS NULL "
            "AND COALESCE(last_activity_at, created_at) <= $1", now - timedelta(hours=hours))

    async def ticket_set_warned(self, ticket_id: int, now: datetime):
        await self.pool.execute("UPDATE tickets SET warned_at=$2 WHERE id=$1 AND status='open'", ticket_id, now)

    async def ticket_warn_expired(self, now: datetime, hours: float):
        """Warned tickets nobody answered within `hours`: these get closed."""
        return await self.pool.fetch(
            "SELECT * FROM tickets WHERE status='open' AND warned_at IS NOT NULL AND warned_at <= $1",
            now - timedelta(hours=hours))

    async def ticket_delete_due(self, now: datetime, hours: float):
        """Closed tickets whose reopen window is over: their channel can be deleted."""
        return await self.pool.fetch(
            "SELECT * FROM tickets WHERE status='closed' AND channel_id IS NOT NULL AND closed_at <= $1",
            now - timedelta(hours=hours))

    # -------------------------------------------------------------- rating
    async def ticket_rate(self, ticket_id: int, owner_id: int, stars: int, now: datetime) -> bool:
        """Only the owner, only after closing, and only once."""
        return bool(await self.pool.fetchval(
            "UPDATE tickets SET rating=$3, rated_at=$4 WHERE id=$1 AND owner_id=$2 AND status<>'open' "
            "AND rating IS NULL RETURNING 1", ticket_id, owner_id, stars, now))

    async def ticket_rate_comment(self, ticket_id: int, owner_id: int, comment: str) -> bool:
        return bool(await self.pool.fetchval(
            "UPDATE tickets SET rating_comment=$3 WHERE id=$1 AND owner_id=$2 AND rating IS NOT NULL RETURNING 1",
            ticket_id, owner_id, comment))

    # --------------------------------------------------------------- stats
    async def ticket_stats(self, since: datetime):
        """(totals row, per-staff rows). Per staff: tickets they closed, tickets they handled (claimed),
        their average first-response time and their average rating."""
        totals = await self.pool.fetchrow(
            "SELECT COUNT(*) AS opened, "
            "COUNT(*) FILTER (WHERE status='open') AS still_open, "
            "COUNT(*) FILTER (WHERE status<>'open') AS closed, "
            "AVG(EXTRACT(EPOCH FROM first_staff_reply_at - created_at)) AS avg_response, "
            "AVG(rating) AS avg_rating, COUNT(rating) AS ratings "
            "FROM tickets WHERE created_at >= $1", since)
        staff = await self.pool.fetch(
            "WITH handled AS ("
            "  SELECT claimed_by AS uid, COUNT(*) AS handled, "
            "         AVG(EXTRACT(EPOCH FROM first_staff_reply_at - created_at)) AS avg_response, "
            "         AVG(rating) AS avg_rating, COUNT(rating) AS ratings "
            "  FROM tickets WHERE created_at >= $1 AND claimed_by IS NOT NULL GROUP BY claimed_by), "
            "closed AS (SELECT closed_by AS uid, COUNT(*) AS closed FROM tickets "
            "  WHERE created_at >= $1 AND closed_by IS NOT NULL AND closed_by <> owner_id GROUP BY closed_by) "
            "SELECT COALESCE(h.uid, c.uid) AS uid, COALESCE(h.handled, 0) AS handled, COALESCE(c.closed, 0) AS closed, "
            "h.avg_response, h.avg_rating, COALESCE(h.ratings, 0) AS ratings "
            "FROM handled h FULL OUTER JOIN closed c ON h.uid = c.uid "
            "ORDER BY COALESCE(h.handled, 0) + COALESCE(c.closed, 0) DESC LIMIT 15", since)
        return totals, staff

    # --------------------------------------------------------------- notes
    async def note_add(self, ticket_id: int, author_id: int, text: str) -> int:
        return await self.pool.fetchval(
            "INSERT INTO ticket_notes(ticket_id, author_id, text) VALUES($1,$2,$3) RETURNING id", ticket_id, author_id, text)

    async def note_list(self, ticket_id: int):
        return await self.pool.fetch("SELECT * FROM ticket_notes WHERE ticket_id=$1 ORDER BY id", ticket_id)

    async def note_count(self, ticket_id: int) -> int:
        return await self.pool.fetchval("SELECT COUNT(*) FROM ticket_notes WHERE ticket_id=$1", ticket_id)

    # ----------------------------------------------------------- blacklist
    async def bl_add(self, user_id: int, reason: str, added_by: int):
        await self.pool.execute(
            "INSERT INTO ticket_blacklist(user_id, reason, added_by) VALUES($1,$2,$3) "
            "ON CONFLICT (user_id) DO UPDATE SET reason=EXCLUDED.reason, added_by=EXCLUDED.added_by, added_at=now()",
            user_id, reason, added_by)

    async def bl_remove(self, user_id: int) -> bool:
        return (await self.pool.execute("DELETE FROM ticket_blacklist WHERE user_id=$1", user_id)) == "DELETE 1"

    async def bl_get(self, user_id: int):
        return await self.pool.fetchrow("SELECT * FROM ticket_blacklist WHERE user_id=$1", user_id)

    async def bl_list(self, limit: int = 50):
        return await self.pool.fetch("SELECT * FROM ticket_blacklist ORDER BY added_at DESC LIMIT $1", limit)
