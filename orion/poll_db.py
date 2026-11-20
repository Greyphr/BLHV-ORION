"""Poll tables + queries (mixed into Database).

  * poll_votes has PRIMARY KEY (poll_id, user_id): one vote per member per poll, enforced by the database, so double
    clicks and two devices can never count twice.
  * Private (anonymous) polls store the voter exactly like public ones. "Private" is a DISPLAY rule: the channel
    message and the member-facing buttons never show names; only the staff-only /poll manage panel does.
"""
from datetime import datetime
from typing import NamedTuple, Optional

POLL_SCHEMA = """
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
"""


class VoteResult(NamedTuple):
    state: str                      # new | changed | same | locked | ended
    previous: Optional[int] = None


class PollStore:
    async def poll_create(self, *, question, options, anonymous, allow_change, role_ids, channel_id, created_by,
                          ends_at: Optional[datetime], banner_id: Optional[int] = None) -> int:
        return await self.pool.fetchval(
            "INSERT INTO polls(question, options, anonymous, allow_change, eligible_role_ids, channel_id, created_by, ends_at, banner_id)"
            " VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id",
            question, options, anonymous, allow_change, role_ids, channel_id, created_by, ends_at, banner_id)

    async def poll_set_message(self, poll_id: int, message_id: int):
        await self.pool.execute("UPDATE polls SET message_id=$2 WHERE id=$1", poll_id, message_id)

    async def poll_set_banner(self, poll_id: int, banner_id: Optional[int]):
        await self.pool.execute("UPDATE polls SET banner_id=$2 WHERE id=$1", poll_id, banner_id)

    async def poll_get(self, poll_id: int):
        return await self.pool.fetchrow("SELECT * FROM polls WHERE id=$1", poll_id)

    async def poll_by_message(self, message_id: int):
        return await self.pool.fetchrow("SELECT * FROM polls WHERE message_id=$1", message_id)

    async def poll_list(self, limit: int = 25):
        return await self.pool.fetch("SELECT * FROM polls ORDER BY id DESC LIMIT $1", limit)

    async def poll_active(self):
        return await self.pool.fetch("SELECT * FROM polls WHERE status='active' AND message_id IS NOT NULL")

    async def poll_due(self, now: datetime):
        return await self.pool.fetch(
            "SELECT * FROM polls WHERE status='active' AND ends_at IS NOT NULL AND ends_at <= $1", now)

    async def poll_end(self, poll_id: int) -> bool:
        """True only for the caller that actually closed it (so results are posted once)."""
        return bool(await self.pool.fetchval(
            "UPDATE polls SET status='ended' WHERE id=$1 AND status='active' RETURNING 1", poll_id))

    async def poll_delete(self, poll_id: int):
        await self.pool.execute("DELETE FROM polls WHERE id=$1", poll_id)

    async def poll_vote(self, poll_id: int, user_id: int, idx: int, now: datetime) -> VoteResult:
        async with self.pool.acquire() as c, c.transaction():
            p = await c.fetchrow("SELECT status, ends_at, allow_change, options FROM polls WHERE id=$1 FOR SHARE", poll_id)
            if p is None or p["status"] != "active" or (p["ends_at"] and p["ends_at"] <= now) \
                    or not 0 <= idx < len(p["options"]):
                return VoteResult("ended")
            row = await c.fetchrow(
                "SELECT option_idx FROM poll_votes WHERE poll_id=$1 AND user_id=$2 FOR UPDATE", poll_id, user_id)
            if row is None:
                if await c.fetchval("INSERT INTO poll_votes(poll_id, user_id, option_idx, voted_at) VALUES($1,$2,$3,$4)"
                                    " ON CONFLICT DO NOTHING RETURNING 1", poll_id, user_id, idx, now):
                    return VoteResult("new")
                row = await c.fetchrow(          # a double click won the race: treat as an existing vote
                    "SELECT option_idx FROM poll_votes WHERE poll_id=$1 AND user_id=$2 FOR UPDATE", poll_id, user_id)
            prev = row["option_idx"]
            if prev == idx:
                return VoteResult("same", prev)
            if not p["allow_change"]:
                return VoteResult("locked", prev)
            await c.execute("UPDATE poll_votes SET option_idx=$3, changed_at=$4 WHERE poll_id=$1 AND user_id=$2",
                            poll_id, user_id, idx, now)
            return VoteResult("changed", prev)

    async def poll_counts(self, poll_id: int) -> dict[int, int]:
        rows = await self.pool.fetch(
            "SELECT option_idx, COUNT(*) AS n FROM poll_votes WHERE poll_id=$1 GROUP BY option_idx", poll_id)
        return {r["option_idx"]: r["n"] for r in rows}

    async def poll_voters(self, poll_id: int):
        return await self.pool.fetch(
            "SELECT user_id, option_idx, voted_at, changed_at FROM poll_votes WHERE poll_id=$1 ORDER BY voted_at", poll_id)
