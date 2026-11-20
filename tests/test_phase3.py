"""Phase 3: events store, achievements, member profiles (real PostgreSQL; skipped without TEST_DATABASE_URL)."""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from orion.db import Database
from orion.profile_ui import profile_embed
from orion.services import profiles

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run the PostgreSQL tests")
SOON = datetime.now(timezone.utc) + timedelta(days=3)


def run_db(coro_fn):
    async def go():
        db = Database(DSN)
        await db.connect()
        try:
            for t in ("event_signups", "events", "member_achievements", "achievements", "progression_history"):
                await db.pool.execute(f"DELETE FROM {t}")
            await db.pool.execute("DELETE FROM xp_period WHERE user_id BETWEEN 9000 AND 9100")
            await db.pool.execute("DELETE FROM users WHERE user_id BETWEEN 9000 AND 9100")
            await coro_fn(db)
        finally:
            await db.close()
    asyncio.run(go())


async def new_event(db, slots=2, kind="scrim"):
    return await db.create_event(kind, "Night Scrim", "desc", SOON, None, slots, 5, None, {"game": "Valorant"}, 7)


@needs_db
def test_event_create_join_leave_and_waitlist_promotion():
    async def t(db):
        eid = await new_event(db, slots=2)
        assert await db.join_event(eid, 1) == ("joined", 1)
        assert await db.join_event(eid, 2) == ("joined", 2)
        assert await db.join_event(eid, 3) == ("waitlisted", 3)
        assert await db.join_event(eid, 4) == ("waitlisted", 4)
        assert await db.join_event(eid, 1) == ("already", 1)
        assert await db.event_signups(eid) == [1, 2, 3, 4]
        assert await db.leave_event(eid, 4) == (True, None)           # leaving the waitlist promotes nobody
        assert await db.leave_event(eid, 1) == (True, 3)              # a confirmed member leaves: #3 moves in
        assert await db.leave_event(eid, 1) == (False, None)
        assert await db.event_signups(eid) == [2, 3]
        assert await db.join_event(9999, 1) == ("missing", 0)
    run_db(t)


@needs_db
def test_unlimited_event_never_waitlists_and_lookup_by_message():
    async def t(db):
        eid = await new_event(db, slots=0, kind="event")
        for uid in range(1, 30):
            assert (await db.join_event(eid, uid))[0] == "joined"
        await db.set_event_message(eid, 555)
        ev = await db.get_event_by_message(555)
        assert ev["id"] == eid and ev["status"] == "open" and ev["extra"] == {"game": "Valorant"}
        await db.delete_event(eid)
        assert await db.get_event(eid) is None and await db.event_signups(eid) == []
    run_db(t)


@needs_db
def test_concurrent_joins_never_overfill():
    async def t(db):
        eid = await new_event(db, slots=3)
        res = await asyncio.gather(*[db.join_event(eid, u) for u in range(100, 110)])
        assert sorted(p for s, p in res if s == "joined") == [1, 2, 3]
        assert sorted(p for s, p in res if s == "waitlisted") == list(range(4, 11))
    run_db(t)


@needs_db
def test_event_history_stats_and_attendance():
    async def t(db):
        e1, e2 = await new_event(db, slots=1), await new_event(db, slots=1)
        await db.join_event(e1, 9001)
        await db.join_event(e2, 9002)
        await db.join_event(e2, 9001)                                 # waitlisted on e2
        assert await db.mark_attendance(e1, 9001, True) and not await db.mark_attendance(e1, 9999, True)
        stats = await db.member_event_stats(9001)
        assert stats == {"registered": 2, "attended": 1}
        hist = await db.member_event_history(9001, 5)
        assert len(hist) == 2 and {bool(h["pos"] > h["slots"]) for h in hist} == {False, True}
        await db.set_event_status(e2, "cancelled")
        assert (await db.member_event_stats(9001))["registered"] == 1  # cancelled events don't count
    run_db(t)


@needs_db
def test_achievements_lifecycle():
    async def t(db):
        assert await db.create_achievement("first-blood", "First Blood", "d", "🩸", "events", "rare", 1)
        assert await db.create_achievement("first-blood", "dup", "", "x", "x", "common", 1) is None
        a = await db.grant_achievement(9010, "first-blood", granted_by=1)
        assert a["name"] == "First Blood"
        assert await db.grant_achievement(9010, "first-blood") is None          # already held
        assert await db.grant_achievement(9010, "nope") is None
        assert [x["key"] for x in await db.member_achievements(9010)] == ["first-blood"]
        assert (await db.list_achievements())[0]["holders"] == 1
        await db.set_achievement_active("first-blood", False)
        assert await db.grant_achievement(9011, "first-blood") is None          # disabled
        assert await db.revoke_achievement(9010, "first-blood") and not await db.revoke_achievement(9010, "first-blood")
        with pytest.raises(Exception):                                          # rarity is constrained
            await db.create_achievement("bad", "Bad", "", "x", "x", "mythic", 1)
    run_db(t)


@needs_db
def test_profile_data_for_new_and_active_members():
    async def t(db):
        joined = datetime(2026, 11, 1, tzinfo=timezone.utc)
        empty = await profiles.get_profile(db, 9020, joined_at=joined)         # never earned XP: still a valid profile
        assert empty["xp"] == 0 and empty["rank"] == "Earth King 1" and empty["position"] is None
        assert empty["joined_at"] == joined and empty["achievements"] == [] and empty["events"]["registered"] == 0

        await db.adjust_xp(9021, "add", 250)
        await db.log_progression(9021, 0, 250, 0, 2, "admin", 7)
        await db.create_achievement("pioneer", "Pioneer", "", "🌟", "community", "epic", 1)
        await db.grant_achievement(9021, "pioneer")
        eid = await new_event(db, slots=5)
        await db.join_event(eid, 9021)
        assert await db.record_join_dates([(9021, joined)]) == 1
        assert await db.record_join_dates([(9021, joined)]) == 0               # unchanged: nothing rewritten

        d = await profiles.get_profile(db, 9021, include_staff=True)
        assert d["xp"] == 250 and d["rank"] == "Earth King 3" and d["joined_at"] == joined
        assert d["activity"]["week_xp"] == 250 and d["activity"]["month_xp"] == 250
        assert d["achievements"][0]["key"] == "pioneer" and d["events"]["registered"] == 1
        assert d["staff"]["progression"][0]["source"] == "admin"
        assert "staff" not in await profiles.get_profile(db, 9021)             # members never get the staff section
    run_db(t)


def _fake_member():
    return NS(display_name="Lin", display_avatar=NS(url="https://example.com/a.png"))


def _data(**kw):
    base = {"member_id": 1, "xp": 250, "level": 2, "realm": "Earth King", "rank": "Earth King 3", "into": 20, "need": 80,
            "percent": 25, "position": 4, "joined_at": datetime(2026, 11, 1, tzinfo=timezone.utc),
            "first_seen": None, "activity": {"messages": 10, "voice_hours": 1.5, "week_xp": 5, "month_xp": 9},
            "achievements": [], "events": {"registered": 0, "attended": 0, "recent": []}}
    base.update(kw)
    return base


def test_profile_embed_basics_and_limits():
    e = profile_embed(_data(), _fake_member())
    names = [f.name for f in e.fields]
    assert names[:3] == ["Realm", "Rank", "Sect position"] and "Achievements" in names and "Events" in names
    many = [{"key": f"k{i}", "name": f"Achievement {i}", "icon": "🏅", "rarity": "common", "category": "x",
             "granted_at": None} for i in range(40)]
    e = profile_embed(_data(achievements=many), _fake_member())
    field = next(f for f in e.fields if f.name.startswith("Achievements"))
    assert "and 28 more" in field.value and len(field.value) <= 1024 and len(e) <= 6000


def test_profile_embed_staff_section_only_when_asked():
    staff = {"progression": [{"old_level": 8, "new_level": 9, "source": "admin", "actor_id": 7,
                              "at": datetime(2026, 11, 2, tzinfo=timezone.utc), "new_xp": 5}]}
    d = _data(staff=staff)
    assert not any("Staff" in f.name for f in profile_embed(d, _fake_member()).fields)
    e = profile_embed(d, _fake_member(), staff_view=True)
    f = next(f for f in e.fields if "Staff" in f.name)
    assert "Earth King 9" in f.value and "Planetary 1" in f.value and "<@7>" in f.value
    assert e.footer.text == "User ID 1"


def test_profile_embed_max_rank():
    e = profile_embed(_data(need=0, into=0, percent=100, level=89, rank="Eternal True God 9"), _fake_member())
    assert any("highest rank" in f.value for f in e.fields)
