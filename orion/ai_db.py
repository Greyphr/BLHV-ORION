"""AI ticket handler storage: per-ticket state, knowledge base entries, activity log."""
from typing import Optional

STATES = ("off", "active", "escalated", "done")


class AiStore:
    # ---- per-ticket state
    async def ai_set_state(self, ticket_id: int, state: str):
        assert state in STATES
        await self.pool.execute("UPDATE tickets SET ai_state=$2 WHERE id=$1", ticket_id, state)

    async def ai_count_reply(self, ticket_id: int, category: Optional[str] = None) -> int:
        return await self.pool.fetchval(
            "UPDATE tickets SET ai_replies = ai_replies + 1, ai_category = COALESCE($2, ai_category) "
            "WHERE id=$1 RETURNING ai_replies", ticket_id, category)

    async def ai_save_summary(self, ticket_id: int, summary: Optional[str], category: Optional[str]):
        await self.pool.execute(
            "UPDATE tickets SET ai_summary = COALESCE($2, ai_summary), ai_category = COALESCE($3, ai_category) "
            "WHERE id=$1", ticket_id, summary, category)

    # ---- knowledge base
    async def kb_add(self, title: str, content: str, created_by: Optional[int]) -> int:
        return await self.pool.fetchval(
            "INSERT INTO ai_knowledge(title, content, created_by) VALUES($1,$2,$3) RETURNING id",
            title, content, created_by)

    async def kb_list(self, enabled_only: bool = False):
        return await self.pool.fetch(
            "SELECT * FROM ai_knowledge WHERE ($1 = FALSE OR enabled) ORDER BY id", enabled_only)

    async def kb_get(self, entry_id: int):
        return await self.pool.fetchrow("SELECT * FROM ai_knowledge WHERE id=$1", entry_id)

    async def kb_update(self, entry_id: int, title: str, content: str) -> bool:
        res = await self.pool.execute("UPDATE ai_knowledge SET title=$2, content=$3, updated_at=now() WHERE id=$1",
                                      entry_id, title, content)
        return res.endswith("1")

    async def kb_set_enabled(self, entry_id: int, enabled: bool) -> bool:
        return (await self.pool.execute("UPDATE ai_knowledge SET enabled=$2 WHERE id=$1", entry_id, enabled)).endswith("1")

    async def kb_remove(self, entry_id: int) -> bool:
        return (await self.pool.execute("DELETE FROM ai_knowledge WHERE id=$1", entry_id)).endswith("1")

    # ---- activity log
    async def ai_log(self, ticket_id: Optional[int], kind: str, *, confidence: Optional[float] = None,
                     category: Optional[str] = None, reason: Optional[str] = None, summary: Optional[str] = None):
        await self.pool.execute(
            "INSERT INTO ai_logs(ticket_id, kind, confidence, category, reason, summary) VALUES($1,$2,$3,$4,$5,$6)",
            ticket_id, kind, confidence, category, (reason or "")[:300] or None, (summary or "")[:1000] or None)

    async def ai_logs(self, ticket_id: Optional[int] = None, limit: int = 15):
        if ticket_id is not None:
            return await self.pool.fetch("SELECT * FROM ai_logs WHERE ticket_id=$1 ORDER BY id DESC LIMIT $2",
                                         ticket_id, limit)
        return await self.pool.fetch("SELECT * FROM ai_logs ORDER BY id DESC LIMIT $1", limit)

    async def ai_stats(self, days: int = 7) -> dict:
        rows = await self.pool.fetch(
            "SELECT kind, COUNT(*) AS n FROM ai_logs WHERE created_at > now() - make_interval(days => $1) GROUP BY kind",
            days)
        return {r["kind"]: r["n"] for r in rows}
