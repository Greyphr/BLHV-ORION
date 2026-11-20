"""AI store (real PostgreSQL) + member reopen / read-only-after-close rules (fakes)."""
import asyncio
import os
from types import SimpleNamespace as NS

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from orion.cogs.ticket import Tickets
from orion.db import Database

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run the PostgreSQL tests")


def run_db(fn):
    async def go():
        db = Database(DSN)
        await db.connect()
        try:
            await db.pool.execute("DELETE FROM ai_logs"); await db.pool.execute("DELETE FROM ai_knowledge")
            await fn(db)
        finally:
            await db.close()
    asyncio.run(go())


@needs_db
def test_knowledge_base_crud():
    async def t(db):
        a = await db.kb_add("Rules", "Be kind.", 1)
        b = await db.kb_add("Events", "Weekly scrims.", 1)
        assert [r["title"] for r in await db.kb_list()] == ["Rules", "Events"]
        assert await db.kb_set_enabled(b, False) and [r["id"] for r in await db.kb_list(True)] == [a]
        assert await db.kb_update(a, "House rules", "Be very kind.")
        assert (await db.kb_get(a))["title"] == "House rules"
        assert await db.kb_remove(b) and not await db.kb_remove(b) and not await db.kb_set_enabled(b, True)
    run_db(t)


@needs_db
def test_ticket_ai_state_counts_logs_and_reopen_resets_to_off():
    async def t(db):
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc)
        res = await db.ticket_open(owner_id=424, type_id=None, type_name="General Support", answers=[{"q": "q", "a": "a"}],
                                   max_open=5, now=now)
        tid = res if isinstance(res, int) else (res[0] if isinstance(res, tuple) else res["id"])
        row = await db.ticket_get(tid)
        assert row["ai_state"] == "off" and row["ai_replies"] == 0
        await db.ai_set_state(tid, "active")
        assert await db.ai_count_reply(tid, "General Support") == 1
        assert await db.ai_count_reply(tid, None) == 2                         # category kept when not given
        await db.ai_save_summary(tid, "Wants a tournament.", None)
        row = await db.ticket_get(tid)
        assert (row["ai_replies"], row["ai_category"], row["ai_summary"]) == (2, "General Support", "Wants a tournament.")
        await db.ai_log(tid, "reply", confidence=0.9, category="General Support", reason="answered", summary="x")
        await db.ai_log(tid, "escalate", reason="r" * 900)
        logs = await db.ai_logs(tid)
        assert [l["kind"] for l in logs] == ["escalate", "reply"] and len(logs[0]["reason"]) == 300
        assert await db.ai_stats(7) == {"reply": 1, "escalate": 1}
        await db.ticket_close(tid, 1, "done", now)
        assert await db.ticket_reopen(tid, now) and (await db.ticket_get(tid))["ai_state"] == "off"
        with pytest.raises(AssertionError):
            await db.ai_set_state(tid, "bogus")
        await db.ticket_discard(tid)
    run_db(t)


# ------------------------------------------------------- reopen + read-only after close (fakes)
_MEMBERS = {}


def Member(uid):
    """A stand-in that passes isinstance(x, discord.Member); same id -> same object."""
    from unittest.mock import MagicMock
    if uid not in _MEMBERS:
        m = MagicMock(spec=discord.Member)
        m.id = uid
        _MEMBERS[uid] = m
    return _MEMBERS[uid]


def make_cog(handlers=(50,), blacklisted=(), open_count=0, reopen_ok=True):
    cog = object.__new__(Tickets)
    me = Member(999)
    calls = {"said": [], "perms": []}

    class Ch:
        guild = NS(me=me, get_member=lambda uid: Member(uid))
        overwrites = {me: 1, Member(5): 1, Member(6): 1, Member(50): 1}
        id = 100

        async def set_permissions(self, target, overwrite=None, reason=None):
            calls["perms"].append((target.id, overwrite))
    ch = Ch()

    async def bl_get(uid): return {"user_id": uid} if uid in blacklisted else None
    async def ticket_open_count(uid): return open_count
    async def ticket_reopen(tid, now): return reopen_ok
    async def ticket_get(tid): return {"id": tid, "channel_id": 100, "owner_id": 5, "warned_at": None,
                                       "first_staff_reply_at": None, "type_id": 1, "ai_state": "off"}
    async def say(cid, text, **kw): calls["said"].append(text)
    cog.bot = NS(get_channel=lambda cid: ch,
                 db=NS(bl_get=bl_get, ticket_open_count=ticket_open_count, ticket_reopen=ticket_reopen,
                       ticket_get=ticket_get, get=lambda k, d=None: d))
    cog.open = {}
    cog.is_handler = lambda m, type_id: m.id in handlers
    cog._say = say
    return cog, ch, calls


T = {"id": 7, "channel_id": 100, "owner_id": 5, "type_id": 1}


def test_owner_can_reopen_their_ticket():
    cog, ch, calls = make_cog()
    out = asyncio.run(cog.reopen(T, Member(5)))
    assert "reopened" in out and calls["said"]
    target, ow = calls["perms"][0]                                # the owner can write again
    assert target == 5 and ow.send_messages is True


def test_other_member_cannot_reopen():
    cog, ch, calls = make_cog()
    assert "Only the person who opened" in asyncio.run(cog.reopen(T, Member(6)))
    assert calls["said"] == []


def test_staff_can_reopen_anyones_ticket():
    cog, ch, calls = make_cog()
    assert "reopened" in asyncio.run(cog.reopen(T, Member(50)))


def test_blacklisted_owner_and_full_owner_cannot_reopen():
    cog, *_ = make_cog(blacklisted=(5,))
    assert "can't reopen" in asyncio.run(cog.reopen(T, Member(5)))
    cog, *_ = make_cog(open_count=1)
    from orion.tickets import SETTINGS
    assert "maximum number" in asyncio.run(cog.reopen(T, Member(5)))


def test_owner_keeps_read_only_access_after_close_everyone_else_loses_it():
    cog, ch, calls = make_cog()
    asyncio.run(cog._lock_after_close(ch, T))
    perms = dict(calls["perms"])
    assert set(perms) == {5, 6}                                   # the bot (999) and staff (50) are untouched
    ow = perms[5]
    assert ow.view_channel is True and ow.read_message_history is True and ow.send_messages is False
    assert perms[6] is None                                       # added participant: access removed
