"""Progression history + audit log (shared by the bot now, a future API later)."""
import json
from typing import Optional


class AuditStore:
    async def log_progression(self, user_id: int, old_xp: int, new_xp: int, old_level: int, new_level: int,
                              source: str, actor_id: Optional[int] = None):
        await self.pool.execute(
            "INSERT INTO progression_history(user_id, old_xp, new_xp, old_level, new_level, source, actor_id) "
            "VALUES($1,$2,$3,$4,$5,$6,$7)", user_id, old_xp, new_xp, old_level, new_level, source, actor_id)

    async def progression_history(self, user_id: int, limit: int = 15):
        return await self.pool.fetch(
            "SELECT * FROM progression_history WHERE user_id=$1 ORDER BY created_at DESC, id DESC LIMIT $2",
            user_id, limit)

    async def record_audit(self, actor_id: Optional[int], action: str, target_type: str | None = None,
                    target_id=None, **details):
        """Record an administrative action. Never raises: a logging failure must not block the action itself."""
        try:
            await self.pool.execute(
                "INSERT INTO audit_log(actor_id, action, target_type, target_id, details) VALUES($1,$2,$3,$4,$5)",
                actor_id, action, target_type, None if target_id is None else str(target_id), details)
        except Exception:
            import logging
            logging.getLogger("orion.audit").exception("Could not write audit entry %s", action)

    async def recent_audit_log(self, limit: int = 20, actor_id: Optional[int] = None):
        if actor_id:
            return await self.pool.fetch("SELECT * FROM audit_log WHERE actor_id=$1 ORDER BY id DESC LIMIT $2",
                                         actor_id, limit)
        return await self.pool.fetch("SELECT * FROM audit_log ORDER BY id DESC LIMIT $1", limit)
