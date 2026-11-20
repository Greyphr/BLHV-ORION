"""Member profile service: one plain-data view of a member (no Discord objects, so an API can serve it too)."""
from datetime import datetime
from typing import Optional

from . import cultivation

RARITY_ORDER = {"legendary": 0, "epic": 1, "rare": 2, "uncommon": 3, "common": 4}


async def get_profile(db, member_id: int, *, joined_at: Optional[datetime] = None,
                      include_staff: bool = False) -> dict:
    """Everything the profile shows. `joined_at` is Discord's join date when the caller has it (it wins over the stored copy).
    `include_staff` adds the progression history and member ID details meant only for staff."""
    prog = await cultivation.member_progress(db, member_id)
    row = await db.get_user(member_id)
    period = await db.period_xp(member_id)
    ach = sorted(await db.member_achievements(member_id), key=lambda a: (RARITY_ORDER[a["rarity"]], a["name"]))
    ev_stats = await db.member_event_stats(member_id)
    ev_recent = await db.member_event_history(member_id, 3)
    data = {
        **prog,
        "joined_at": joined_at or (row["joined_server_at"] if row else None),
        "first_seen": row["created_at"] if row else None,
        "activity": {"messages": row["messages"] if row else 0,
                     "voice_hours": round((row["voice_minutes"] if row else 0) / 60, 1),
                     "week_xp": period["week"], "month_xp": period["month"]},
        "achievements": [{"key": a["key"], "name": a["name"], "icon": a["icon"], "rarity": a["rarity"],
                          "category": a["category"], "granted_at": a["granted_at"]} for a in ach],
        "events": {**ev_stats,
                   "recent": [{"id": e["id"], "kind": e["kind"], "title": e["title"], "starts_at": e["starts_at"],
                               "attended": e["attended"], "status": e["status"],
                               "waitlisted": bool(e["slots"]) and e["pos"] > e["slots"]} for e in ev_recent]},
    }
    if include_staff:
        hist = await db.progression_history(member_id, 5)
        data["staff"] = {"progression": [{"old_level": h["old_level"], "new_level": h["new_level"],
                                          "source": h["source"], "actor_id": h["actor_id"],
                                          "at": h["created_at"], "new_xp": h["new_xp"]} for h in hist]}
    return data
