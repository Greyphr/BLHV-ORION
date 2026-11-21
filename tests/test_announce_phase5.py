"""Phase 5: built-in templates, link buttons, author line, scheduling and the scheduler (DB parts need PostgreSQL)."""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from orion.announce_templates import BUILTIN_TEMPLATES
from orion.cogs import announce
from orion.cogs.announce import Announce, Draft, build_embed, link_view, mention_parts, parse_links
from orion.db import Database

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run the PostgreSQL tests")


# ----------------------------------------------------------------- templates
def test_eight_builtin_templates_exist_and_are_valid():
    assert set(BUILTIN_TEMPLATES) == {"general-sect", "community-update", "esports", "tournament-results", "recruitment",
                                      "community-event", "realm-milestone", "maintenance"}
    for name, data in BUILTIN_TEMPLATES.items():
        d = Draft.from_dict(data)
        assert d.title and d.description and len(build_embed(d)) < 6000, name
        assert len(d.fields) <= 25 and all(len(f["name"]) <= 256 and len(f["value"]) <= 1024 for f in d.fields)
        assert d.buttons == [] and 0 <= d.color <= 0xFFFFFF


def test_template_data_does_not_leak_between_drafts():
    a = Draft.from_dict(BUILTIN_TEMPLATES["esports"])
    a.fields.append({"name": "x", "value": "y", "inline": False})
    a.buttons.append({"label": "l", "url": "https://x.y"})
    assert len(BUILTIN_TEMPLATES["esports"]["fields"]) == 2 and BUILTIN_TEMPLATES["esports"]["buttons"] == []


# ------------------------------------------------------------- link buttons
def test_parse_links_ok_and_blank():
    assert parse_links("") == [] and parse_links("  \n ") == []
    got = parse_links("Join | https://discord.gg/abc\n\nRules|https://example.com/rules?a=b|c")
    assert got == [{"label": "Join", "url": "https://discord.gg/abc"},
                   {"label": "Rules", "url": "https://example.com/rules?a=b|c"}]


@pytest.mark.parametrize("bad", ["no separator", "| https://x.y", "Label |", "L | javascript:alert(1)", "L | ftp://x.y",
                                 "L | https://", "x" * 81 + " | https://a.b", "L | https://a.b/" + "x" * 520,
                                 "\n".join(f"b{i} | https://a.b" for i in range(6))])
def test_parse_links_rejects(bad):
    with pytest.raises(ValueError):
        parse_links(bad)


def test_link_view_makes_link_buttons_only():
    assert link_view(Draft()) is None
    v = link_view(Draft(buttons=[{"label": "A", "url": "https://a.b"}, {"label": "B", "url": "https://c.d"}]))
    assert [(b.label, b.url, b.style) for b in v.children] == [
        ("A", "https://a.b", discord.ButtonStyle.link), ("B", "https://c.d", discord.ButtonStyle.link)]
    assert v.timeout is None


def test_draft_roundtrip_keeps_author_and_buttons_and_ignores_unknown_keys():
    d = Draft(author="Sect Elder", buttons=[{"label": "A", "url": "https://a.b"}])
    d2 = Draft.from_dict({**d.to_dict(), "future_field": 1})
    assert d2.author == "Sect Elder" and d2.buttons == d.buttons
    assert Draft.from_dict({"title": "old draft without the new keys"}).buttons == []
    assert build_embed(d).author.name == "Sect Elder" and build_embed(Draft()).author.name is None


def test_mention_parts():
    text, allowed = mention_parts([{"t": "role", "id": 5}, {"t": "user", "id": 6}], "here")
    assert text == "@here <@&5> <@6>" and allowed.everyone
    assert mention_parts([], "none")[0] is None


# ---------------------------------------------------------------- DB parts
def run_db(fn):
    async def go():
        db = Database(DSN)
        await db.connect()
        try:
            await db.pool.execute("DELETE FROM scheduled_announcements")
            await db.pool.execute("DELETE FROM announcement_templates WHERE name IN ('esports','my-own')")
            await fn(db)
        finally:
            await db.close()
    asyncio.run(go())


@needs_db
def test_templates_builtin_fallback_override_and_delete():
    async def t(db):
        assert "esports" in await db.list_templates() and (await db.get_template("ESPORTS"))["title"].startswith("🎮")
        assert await db.get_template("nope") is None
        await db.save_template("esports", {"title": "My esports"}, 1)
        await db.save_template("my-own", {"title": "Mine"}, 1)
        assert (await db.get_template("esports"))["title"] == "My esports"          # saved one overrides the built-in
        assert "my-own" in await db.list_templates() and (await db.list_templates()).count("esports") == 1
        assert await db.delete_template("esports")                                   # deleting the override...
        assert (await db.get_template("esports"))["title"].startswith("🎮")          # ...restores the built-in
        assert not await db.delete_template("esports") and not await db.delete_template("general-sect")
        assert await db.delete_template("my-own")
    run_db(t)


@needs_db
def test_schedule_lifecycle():
    async def t(db):
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        due = await db.announce_schedule_add(5, 1, {"title": "Due"}, [], "none", past)
        later = await db.announce_schedule_add(5, 1, {"title": "Later"}, [], "none", future)
        claimed = await db.announce_claim_due(10)
        assert [r["id"] for r in claimed] == [due] and claimed[0]["attempts"] == 1 and claimed[0]["data"]["title"] == "Due"
        assert await db.announce_claim_due(10) == []                                   # nothing is taken twice
        await db.announce_finish(due, "pending", error="Discord error 500", retry_at=past)   # retry path
        again = await db.announce_claim_due(10)
        assert again[0]["id"] == due and again[0]["attempts"] == 2
        assert await db.announce_release_stuck() == 1                                   # restart: back to pending
        assert (await db.announce_claim_due(10))[0]["attempts"] == 3
        await db.announce_finish(due, "published", message_id=77)
        assert [r["id"] for r in await db.announce_scheduled_list()] == [later]
        assert await db.announce_cancel_scheduled(later) and not await db.announce_cancel_scheduled(later)
        assert not await db.announce_cancel_scheduled(due)                              # already published
        assert await db.announce_scheduled_list() == []
    run_db(t)


@needs_db
def test_delete_announcement_record():
    async def t(db):
        await db.pool.execute("DELETE FROM announcements WHERE message_id=987654321012345678")
        await db.save_announcement(1, 987654321012345678, 2, "T", {"title": "T"}, [], "none")
        assert await db.delete_announcement_record(987654321012345678)
        assert not await db.delete_announcement_record(987654321012345678)
    run_db(t)


# ---------------------------------------------------- the scheduler's publishing
class FakeResp:
    status, reason = 500, "x"


def http_error(status, text="boom"):
    return discord.HTTPException(NS(status=status, reason="r"), {"message": text, "code": 0})


class FakeDB:
    def __init__(self):
        self.finished, self.saved, self.audits = [], [], []

    async def get_image(self, iid): return None
    async def announce_finish(self, sid, status, **kw): self.finished.append((sid, status, kw))
    async def save_announcement(self, *a): self.saved.append(a)
    async def record_audit(self, *a, **kw): self.audits.append(a)


def make(send, channel_exists=True):
    db = FakeDB()
    sent = []

    class Ch:
        id, mention = 5, "<#5>"
        async def send(self, **kw):
            sent.append(kw)
            return await send(kw)
    guild = NS(get_channel=lambda cid: Ch() if channel_exists else None)
    cog = object.__new__(Announce)
    cog.bot = NS(db=db, get_guild=lambda gid: guild)
    return cog, db, sent


ROW = {"id": 3, "channel_id": 5, "author_id": 1, "attempts": 1, "mentions": [{"t": "role", "id": 8}], "ping_mode": "none",
       "data": Draft(title="Hello", buttons=[{"label": "A", "url": "https://a.b"}]).to_dict()}


def test_scheduler_publishes_and_records(monkeypatch):
    async def ok(kw): return NS(id=900)
    cog, db, sent = make(ok)
    asyncio.run(cog._publish_scheduled(ROW))
    assert db.finished == [(3, "published", {"message_id": 900})] and db.saved[0][1] == 900
    kw = sent[0]
    assert kw["content"] == "<@&8>" and kw["embeds"][0].title == "Hello" and kw["view"].children[0].label == "A"
    assert kw["allowed_mentions"].everyone is False


def test_scheduler_channel_gone_fails_cleanly():
    cog, db, sent = make(None, channel_exists=False)
    asyncio.run(cog._publish_scheduled(ROW))
    assert db.finished[0][:2] == (3, "failed") and sent == []


def test_scheduler_retries_server_errors_then_gives_up(monkeypatch):
    async def boom(kw): raise http_error(502)
    cog, db, _ = make(boom)
    asyncio.run(cog._publish_scheduled({**ROW, "attempts": 1}))
    sid, status, kw = db.finished[0]
    assert status == "pending" and kw["retry_at"] is not None                     # tries again in a couple of minutes
    alerts = []

    async def fake_alert(bot, guild, text, key=None): alerts.append(text)
    monkeypatch.setattr(announce.alerts, "alert_staff", fake_alert)
    cog, db, _ = make(boom)
    asyncio.run(cog._publish_scheduled({**ROW, "attempts": 3}))
    assert db.finished[0][1] == "failed" and alerts


def test_scheduler_permission_error_fails_at_once_and_alerts_staff(monkeypatch):
    async def forbidden(kw): raise http_error(403, "Missing Permissions")
    alerts = []

    async def fake_alert(bot, guild, text, key=None): alerts.append(text)
    monkeypatch.setattr(announce.alerts, "alert_staff", fake_alert)
    cog, db, _ = make(forbidden)
    asyncio.run(cog._publish_scheduled({**ROW, "attempts": 1}))
    assert db.finished[0][1] == "failed" and "Missing Permissions" in db.finished[0][2]["error"]
    assert alerts and "#3" in alerts[0]


def test_scheduler_rate_limit_is_retried_not_failed():
    async def limited(kw): raise http_error(429, "slow down")
    cog, db, _ = make(limited)
    asyncio.run(cog._publish_scheduled({**ROW, "attempts": 1}))
    assert db.finished[0][1] == "pending"
