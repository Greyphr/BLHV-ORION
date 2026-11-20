"""Cultivation service: XP changes, rank lookups and progression history, free of Discord objects."""
from dataclasses import dataclass

from .. import levels


@dataclass(frozen=True)
class XpChange:
    member_id: int
    old_xp: int
    new_xp: int
    old_level: int
    new_level: int

    @property
    def old_rank(self) -> str:
        return levels.rank_name(self.old_level)

    @property
    def new_rank(self) -> str:
        return levels.rank_name(self.new_level)

    @property
    def realm_changed(self) -> bool:
        return levels.is_realm_change(self.old_level, self.new_level)


async def adjust_member_xp(db, actor_id: int, member_id: int, mode: str, amount: int) -> XpChange:
    """Apply an administrative XP change (add / remove / set / reset), then record it in the progression history
    and the audit log. Permission checks belong to the caller (the Discord check today, API auth tomorrow)."""
    res = await db.adjust_xp(member_id, mode, amount)
    change = XpChange(member_id, res.old_xp, res.new_xp,
                      levels.level_from_xp(res.old_xp), levels.level_from_xp(res.new_xp))
    await db.log_progression(member_id, res.old_xp, res.new_xp, change.old_level, change.new_level, "admin", actor_id)
    await db.record_audit(actor_id, f"xp.{mode}", "member", member_id, amount=amount,
                          old_xp=res.old_xp, new_xp=res.new_xp)
    return change


async def member_progress(db, member_id: int) -> dict:
    """Everything a profile needs about a member's cultivation, as plain data."""
    row = await db.get_user(member_id)
    xp = row["xp"] if row else 0
    lvl, into, need = levels.progress(xp)
    return {"member_id": member_id, "xp": xp, "level": lvl, "realm": levels.realm_of(lvl),
            "rank": levels.rank_name(lvl), "into": into, "need": need,
            "percent": int(into * 100 / need) if need else 100,
            "position": await db.get_rank(member_id) if row else None}
