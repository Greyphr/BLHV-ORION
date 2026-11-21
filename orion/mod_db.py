"""Moderation storage: cases, warnings, temporary bans, reports."""
from datetime import datetime
from typing import Optional


class ModStore:
    # ------------------------------------------------------------------ cases
    async def mod_add_case(self, action: str, target_id: int, moderator_id: Optional[int], reason: str, *,
                           source: str = "manual", duration_seconds: Optional[int] = None,
                           expires_at: Optional[datetime] = None, report_id: Optional[int] = None):
        return await self.pool.fetchrow(
            "INSERT INTO mod_cases(action, target_id, moderator_id, reason, source, duration_seconds, expires_at, report_id) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *",
            action, target_id, moderator_id, reason[:1000], source, duration_seconds, expires_at, report_id)

    async def mod_delete_case(self, case_id: int):
        """Only for a case whose Discord action failed straight after it was created."""
        await self.pool.execute("DELETE FROM mod_cases WHERE id=$1", case_id)

    async def mod_get_case(self, case_id: int):
        return await self.pool.fetchrow("SELECT * FROM mod_cases WHERE id=$1", case_id)

    async def mod_set_log_message(self, case_id: int, message_id: int):
        await self.pool.execute("UPDATE mod_cases SET log_message_id=$2 WHERE id=$1", case_id, message_id)

    async def mod_set_reason(self, case_id: int, reason: str) -> bool:
        return (await self.pool.execute("UPDATE mod_cases SET reason=$2 WHERE id=$1", case_id, reason[:1000])).endswith("1")

    async def mod_cases_for(self, target_id: int, limit: int = 15):
        return await self.pool.fetch("SELECT * FROM mod_cases WHERE target_id=$1 ORDER BY id DESC LIMIT $2",
                                     target_id, limit)

    async def mod_recent_cases(self, limit: int = 15, moderator_id: Optional[int] = None):
        if moderator_id:
            return await self.pool.fetch("SELECT * FROM mod_cases WHERE moderator_id=$1 ORDER BY id DESC LIMIT $2",
                                         moderator_id, limit)
        return await self.pool.fetch("SELECT * FROM mod_cases ORDER BY id DESC LIMIT $1", limit)

    async def mod_active_warns(self, target_id: int, since: datetime) -> int:
        return await self.pool.fetchval(
            "SELECT COUNT(*) FROM mod_cases WHERE target_id=$1 AND action='warn' AND revoked_at IS NULL "
            "AND created_at >= $2", target_id, since)

    async def mod_revoke_case(self, case_id: int, by: Optional[int], reason: str) -> bool:
        """Pardon a warning or lift a ban/timeout record. False when missing or already revoked."""
        return (await self.pool.execute(
            "UPDATE mod_cases SET revoked_at=now(), revoked_by=$2, revoke_reason=$3 "
            "WHERE id=$1 AND revoked_at IS NULL AND action IN ('warn','ban','timeout')",
            case_id, by, reason[:300])).endswith("1")

    async def mod_revoke_active(self, target_id: int, action: str, by: Optional[int], reason: str) -> int:
        res = await self.pool.execute(
            "UPDATE mod_cases SET revoked_at=now(), revoked_by=$3, revoke_reason=$4 "
            "WHERE target_id=$1 AND action=$2 AND revoked_at IS NULL", target_id, action, by, reason[:300])
        return int(res.split()[-1])

    async def mod_due_tempbans(self, limit: int = 10):
        return await self.pool.fetch(
            "SELECT * FROM mod_cases WHERE action='ban' AND revoked_at IS NULL AND expires_at IS NOT NULL "
            "AND expires_at <= now() ORDER BY expires_at LIMIT $1", limit)

    async def mod_summary(self, target_id: int) -> dict:
        rows = await self.pool.fetch(
            "SELECT action, COUNT(*) AS n, COUNT(*) FILTER (WHERE revoked_at IS NULL) AS active "
            "FROM mod_cases WHERE target_id=$1 GROUP BY action", target_id)
        by = {r["action"]: {"total": r["n"], "active": r["active"]} for r in rows}
        last = await self.pool.fetchval("SELECT MAX(created_at) FROM mod_cases WHERE target_id=$1", target_id)
        return {"by_action": by, "last_at": last, "total": sum(v["total"] for v in by.values()),
                "active_warns": by.get("warn", {}).get("active", 0)}

    # ---------------------------------------------------------------- reports
    async def mod_report_add(self, reporter_id: int, target_id: int, reason: str, message_link: Optional[str]):
        return await self.pool.fetchrow(
            "INSERT INTO mod_reports(reporter_id, target_id, reason, message_link) VALUES($1,$2,$3,$4) RETURNING *",
            reporter_id, target_id, reason[:1000], message_link)

    async def mod_report_get(self, report_id: int):
        return await self.pool.fetchrow("SELECT * FROM mod_reports WHERE id=$1", report_id)

    async def mod_report_by_message(self, message_id: int):
        return await self.pool.fetchrow("SELECT * FROM mod_reports WHERE log_message_id=$1", message_id)

    async def mod_report_set_message(self, report_id: int, message_id: int):
        await self.pool.execute("UPDATE mod_reports SET log_message_id=$2 WHERE id=$1", report_id, message_id)

    async def mod_report_update(self, report_id: int, status: str, handled_by: int, case_id: Optional[int] = None) -> bool:
        closing = status in ("closed", "dismissed")
        return (await self.pool.execute(
            "UPDATE mod_reports SET status=$2, handled_by=$3, case_id=COALESCE($4, case_id), "
            "closed_at = CASE WHEN $5 THEN now() ELSE NULL END WHERE id=$1", report_id, status, handled_by,
            case_id, closing)).endswith("1")

    async def mod_report_list(self, statuses=("open", "reviewing"), limit: int = 15):
        return await self.pool.fetch("SELECT * FROM mod_reports WHERE status = ANY($1::text[]) ORDER BY id DESC LIMIT $2",
                                     list(statuses), limit)

    async def mod_report_open_count(self, reporter_id: int) -> int:
        return await self.pool.fetchval(
            "SELECT COUNT(*) FROM mod_reports WHERE reporter_id=$1 AND status IN ('open','reviewing')", reporter_id)

    async def mod_report_recent_duplicate(self, reporter_id: int, target_id: int, since: datetime) -> bool:
        return bool(await self.pool.fetchval(
            "SELECT 1 FROM mod_reports WHERE reporter_id=$1 AND target_id=$2 AND created_at >= $3 LIMIT 1",
            reporter_id, target_id, since))
