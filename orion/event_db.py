"""Events / scrims / tournaments storage: sign-ups, waitlist order, history."""
from typing import Optional


class EventStore:
    async def create_event(self, kind, title, description, starts_at, reg_closes_at, slots, channel_id,
                           notify_role_id, extra, created_by) -> int:
        return await self.pool.fetchval(
            "INSERT INTO events(kind, title, description, starts_at, reg_closes_at, slots, channel_id, "
            "notify_role_id, extra, created_by) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id",
            kind, title, description, starts_at, reg_closes_at, slots, channel_id, notify_role_id, extra or {},
            created_by)

    async def get_event(self, event_id: int):
        return await self.pool.fetchrow("SELECT * FROM events WHERE id=$1", event_id)

    async def get_event_by_message(self, message_id: int):
        return await self.pool.fetchrow("SELECT * FROM events WHERE message_id=$1", message_id)

    async def set_event_message(self, event_id: int, message_id: int):
        await self.pool.execute("UPDATE events SET message_id=$2 WHERE id=$1", event_id, message_id)

    async def delete_event(self, event_id: int):
        await self.pool.execute("DELETE FROM events WHERE id=$1", event_id)

    async def set_event_status(self, event_id: int, status: str):
        await self.pool.execute("UPDATE events SET status=$2 WHERE id=$1", event_id, status)

    async def event_signups(self, event_id: int) -> list[int]:
        """User ids in sign-up order (the first `slots` are confirmed, the rest are the waitlist)."""
        rows = await self.pool.fetch("SELECT user_id FROM event_signups WHERE event_id=$1 ORDER BY seq", event_id)
        return [r["user_id"] for r in rows]

    async def join_event(self, event_id: int, user_id: int) -> tuple[str, int]:
        """Returns (state, position): joined | waitlisted | already | missing. Position is 1-based sign-up order."""
        async with self.pool.acquire() as c, c.transaction():
            ev = await c.fetchrow("SELECT slots FROM events WHERE id=$1 FOR UPDATE", event_id)   # serialises joins
            if ev is None:
                return "missing", 0
            ids = [r["user_id"] for r in await c.fetch(
                "SELECT user_id FROM event_signups WHERE event_id=$1 ORDER BY seq", event_id)]
            if user_id in ids:
                return "already", ids.index(user_id) + 1
            await c.execute("INSERT INTO event_signups(event_id, user_id) VALUES($1,$2)", event_id, user_id)
            pos = len(ids) + 1
            return ("joined" if not ev["slots"] or pos <= ev["slots"] else "waitlisted"), pos

    async def leave_event(self, event_id: int, user_id: int) -> tuple[bool, Optional[int]]:
        """Returns (was_signed_up, promoted_user_id). Someone leaves a full event -> the first waitlisted member moves in."""
        async with self.pool.acquire() as c, c.transaction():
            ev = await c.fetchrow("SELECT slots FROM events WHERE id=$1 FOR UPDATE", event_id)
            if ev is None:
                return False, None
            ids = [r["user_id"] for r in await c.fetch(
                "SELECT user_id FROM event_signups WHERE event_id=$1 ORDER BY seq", event_id)]
            if user_id not in ids:
                return False, None
            await c.execute("DELETE FROM event_signups WHERE event_id=$1 AND user_id=$2", event_id, user_id)
            slots = ev["slots"]
            promoted = ids[slots] if slots and ids.index(user_id) < slots and len(ids) > slots else None
            return True, promoted

    async def mark_attendance(self, event_id: int, user_id: int, attended: bool) -> bool:
        res = await self.pool.execute("UPDATE event_signups SET attended=$3 WHERE event_id=$1 AND user_id=$2",
                                      event_id, user_id, attended)
        return res.endswith("1")

    async def member_event_history(self, user_id: int, limit: int = 5):
        """Newest first. `confirmed` is False when the member was still on the waitlist."""
        return await self.pool.fetch(
            "SELECT e.id, e.kind, e.title, e.starts_at, e.status, s.attended, "
            "  (SELECT COUNT(*) FROM event_signups o WHERE o.event_id = e.id AND o.seq <= s.seq) AS pos, e.slots "
            "FROM event_signups s JOIN events e ON e.id = s.event_id WHERE s.user_id=$1 "
            "ORDER BY e.starts_at DESC LIMIT $2", user_id, limit)

    async def member_event_stats(self, user_id: int) -> dict:
        r = await self.pool.fetchrow(
            "SELECT COUNT(*) AS registered, COUNT(*) FILTER (WHERE s.attended) AS attended "
            "FROM event_signups s JOIN events e ON e.id = s.event_id WHERE s.user_id=$1 AND e.status <> 'cancelled'",
            user_id)
        return {"registered": r["registered"], "attended": r["attended"]}
