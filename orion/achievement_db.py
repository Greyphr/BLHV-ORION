"""Achievement definitions and which members hold them."""
from typing import Optional

RARITIES = ("common", "uncommon", "rare", "epic", "legendary")


class AchievementStore:
    async def create_achievement(self, key: str, name: str, description: str, icon: str, category: str,
                                 rarity: str, created_by: Optional[int]) -> Optional[int]:
        """Returns the new id, or None when the key already exists."""
        return await self.pool.fetchval(
            "INSERT INTO achievements(key, name, description, icon, category, rarity, created_by) "
            "VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (key) DO NOTHING RETURNING id",
            key, name, description, icon, category, rarity, created_by)

    async def get_achievement(self, key: str):
        return await self.pool.fetchrow("SELECT * FROM achievements WHERE key=$1", key)

    async def list_achievements(self, active_only: bool = True):
        return await self.pool.fetch(
            "SELECT a.*, (SELECT COUNT(*) FROM member_achievements m WHERE m.achievement_id = a.id) AS holders "
            "FROM achievements a WHERE ($1 = FALSE OR a.active) ORDER BY a.category, a.name", active_only)

    async def set_achievement_active(self, key: str, active: bool) -> bool:
        return (await self.pool.execute("UPDATE achievements SET active=$2 WHERE key=$1", key, active)).endswith("1")

    async def grant_achievement(self, user_id: int, key: str, granted_by: Optional[int] = None):
        """Returns the achievement row if newly granted, None if unknown/inactive or already held."""
        async with self.pool.acquire() as c, c.transaction():
            a = await c.fetchrow("SELECT * FROM achievements WHERE key=$1 AND active", key)
            if a is None:
                return None
            ins = await c.fetchval(
                "INSERT INTO member_achievements(user_id, achievement_id, granted_by) VALUES($1,$2,$3) "
                "ON CONFLICT DO NOTHING RETURNING 1", user_id, a["id"], granted_by)
            return a if ins else None

    async def revoke_achievement(self, user_id: int, key: str) -> bool:
        res = await self.pool.execute(
            "DELETE FROM member_achievements WHERE user_id=$1 AND achievement_id = "
            "(SELECT id FROM achievements WHERE key=$2)", user_id, key)
        return res.endswith("1")

    async def member_achievements(self, user_id: int):
        return await self.pool.fetch(
            "SELECT a.*, m.granted_at FROM member_achievements m JOIN achievements a ON a.id = m.achievement_id "
            "WHERE m.user_id=$1 ORDER BY m.granted_at DESC", user_id)
