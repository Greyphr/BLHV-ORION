"""Moderation: pure rules (durations, hierarchy, spam/raid detection, settings) and full command flows (real PostgreSQL +
fake Discord objects). The flows are skipped without TEST_DATABASE_URL."""
import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from orion import config
from orion.cogs.moderation import Moderation, ReportView
from orion.db import Database
from orion.mod_ui import case_embed, case_line, dm_text, report_embed
from orion.services import moderation as rules
from orion.services.moderation import (RaidDetector, SpamDetector, escalation_due, format_duration, hierarchy_problem,
                                       load_settings, parse_duration, parse_setting)

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run the PostgreSQL tests")


# ================================================================= pure rules
@pytest.mark.parametrize("text,secs", [("30s", 30), ("10m", 600), ("2h", 7200), ("7d", 604800), ("1w", 604800),
                                       ("1d12h", 129600), (" 1H 30M ", 5400)])
def test_parse_duration(text, secs):
    assert parse_duration(text) == secs


@pytest.mark.parametrize("bad", ["", "abc", "10", "m", "0m", "-5m", "1.5h", "10 minutes"])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_format_duration():
    assert format_duration(90) == "1m 30s" and format_duration(129600) == "1d 12h" and format_duration(0) == "0s"


def hp(**kw):
    base = dict(actor_id=1, actor_top=50, actor_is_admin=False, target_id=2, target_top=10, target_is_owner=False,
                target_is_admin=False, target_is_staff=False, target_is_bot=False, bot_id=99, bot_top=100)
    base.update(kw)
    return hierarchy_problem(**base)


def test_hierarchy_rules():
    assert hp() is None
    assert "yourself" in hp(target_id=1)
    assert "myself" in hp(target_id=99)
    assert "Patriarch" in hp(target_is_owner=True, actor_is_admin=True)
    assert "bot account" in hp(target_is_bot=True)
    assert "Sect Admin" in hp(target_is_admin=True)
    assert "Staff can only" in hp(target_is_staff=True)
    assert hp(target_is_staff=True, actor_is_admin=True) is None                 # admins may act on staff
    assert "equal to or above yours" in hp(target_top=50) and "equal to or above yours" in hp(target_top=70)
    assert hp(actor_is_admin=True, target_top=70) is None                         # admin role bypasses the role-position check
    assert "above mine" in hp(target_top=100, actor_is_admin=True)
    assert hp(target_top=100, actor_is_admin=True, needs_bot_hierarchy=False) is None      # e.g. warnings need no bot power


def test_escalation_due():
    assert not escalation_due(3, 0) and not escalation_due(0, 3)
    assert [n for n in range(1, 10) if escalation_due(n, 3)] == [3, 6, 9]


def test_settings_defaults_and_repair():
    s = load_settings(lambda k, d=None: d)
    assert s.admin_only == frozenset({"kick", "ban", "unban", "pardon"}) and s.spam == "alert" and s.raid == "alert"
    assert s.escalate_every == 0 and s.dm is True
    bad = {"automod_spam": "nuke", "automod_rate_count": "lots", "mod_escalate_every": 9999,
           "mod_admin_only": ["ban", "fly"], "automod_exempt_channels": [1, "2", "x"]}
    s = load_settings(lambda k, d=None: bad.get(k, d))
    assert s.spam == "alert" and s.spam_rate_count == 6 and s.escalate_every == 50
    assert s.admin_only == frozenset({"ban"}) and s.exempt_channels == frozenset({1, 2})


@pytest.mark.parametrize("name,value,expect", [
    ("spam", "ACT", {"automod_spam": "act"}), ("raid", "off", {"automod_raid": "off"}),
    ("escalate-every", "3", {"mod_escalate_every": 3}), ("dm-members", "off", {"mod_dm": False}),
    ("admin-only", "ban, Kick", {"mod_admin_only": ["ban", "kick"]}),
    ("spam-rate", "6/5", {"automod_rate_count": 6, "automod_rate_seconds": 5}),
    ("spam-duplicates", "4 / 30", {"automod_dup_count": 4, "automod_dup_seconds": 30}),
    ("raid-joins", "10/20", {"automod_raid_joins": 10, "automod_raid_seconds": 20}),
    ("spam-mentions", "8", {"automod_mentions": 8})])
def test_parse_setting_ok(name, value, expect):
    assert parse_setting(name, value) == expect


@pytest.mark.parametrize("name,value", [("spam", "nuke"), ("escalate-every", "-1"), ("escalate-every", "99"),
                                        ("dm-members", "maybe"), ("admin-only", "ban,fly"), ("spam-rate", "6"),
                                        ("spam-rate", "1/5"), ("raid-joins", "2/20"), ("nope", "x")])
def test_parse_setting_rejects(name, value):
    with pytest.raises(ValueError):
        parse_setting(name, value)


def S(**kw):
    return rules.ModSettings(**kw)


def test_spam_flood_detected_and_reset():
    d = SpamDetector(S(spam_rate_count=5, spam_rate_seconds=5))
    hits = [d.check(1, f"msg {i}", 0, 10, 100 + i, now=i * 0.5) for i in range(5)]
    assert hits[:4] == [None] * 4 and hits[4][0] == "message flood" and len(hits[4][1]) == 5
    assert d.check(1, "next", 0, 10, 200, now=3) is None                          # window cleared after a hit


def test_spam_normal_chat_is_not_flagged():
    d = SpamDetector(S())
    assert all(d.check(1, f"hello {i}", 1, 10, i, now=i * 3.0) is None for i in range(50))      # one message per 3s
    d2 = SpamDetector(S())
    assert all(d2.check(u, "gm everyone", 0, 10, u, now=u * 0.1) is None for u in range(100))   # same text, different people


def test_spam_duplicates_and_mentions():
    d = SpamDetector(S(spam_dup_count=3, spam_dup_seconds=30))
    assert d.check(1, "BUY   gold now", 0, 10, 1, 0) is None
    assert d.check(1, "buy gold now", 0, 11, 2, 5) is None
    hit = d.check(1, "buy gold NOW", 0, 12, 3, 9)
    assert hit[0] == "repeated message" and {m for _, m in hit[1]} == {1, 2, 3}        # across channels, case/spacing ignored
    assert SpamDetector(S(spam_mentions=6)).check(2, "hi", 6, 10, 5, 0)[0] == "mass mentions"
    assert SpamDetector(S(spam_mentions=6)).check(2, "hi", 5, 10, 5, 0) is None


def test_spam_window_expiry_and_memory_is_bounded():
    d = SpamDetector(S(spam_dup_count=3, spam_dup_seconds=10))
    d.check(1, "x", 0, 1, 1, 0); d.check(1, "x", 0, 1, 2, 5)
    assert d.check(1, "x", 0, 1, 3, 30) is None                                   # the older copies expired
    for u in range(2000):
        d.check(u, "hi", 0, 1, u, now=100 + u * 0.001)
    d.check(5000, "hi", 0, 1, 1, now=10_000)
    for n in range(600):
        d.check(6000 + n, "hi", 0, 1, n, now=20_000)
    assert len(d._msgs) < 700                                                     # idle members were purged


def test_raid_detector_fires_once_per_raid():
    r = RaidDetector(joins=5, seconds=30)
    out = [r.record(u, 1000.0, now=u * 1.0) for u in range(5)]
    assert out[:4] == [None] * 4 and [u for u, _ in out[4]] == [0, 1, 2, 3, 4]
    assert all(r.record(10 + u, 1000.0, now=5 + u) is None for u in range(10))     # same raid: no second alert
    assert r.record(99, 1000.0, now=500) is None                                   # lone join later
    assert RaidDetector(5, 30).record(1, 0, 0) is None


def test_raid_normal_growth_is_not_flagged():
    r = RaidDetector(joins=8, seconds=30)
    assert all(r.record(u, 1000.0, now=u * 10.0) is None for u in range(100))       # one join every 10s


# ============================================================ embeds / messages
CASE = {"id": 12, "action": "timeout", "target_id": 5, "moderator_id": 7, "reason": "Spamming", "source": "manual",
        "duration_seconds": 3600, "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc),
        "created_at": datetime(2026, 11, 30, tzinfo=timezone.utc), "revoked_at": None, "revoked_by": None,
        "revoke_reason": None}


def test_case_embed_and_line_and_dm():
    e = case_embed(CASE)
    assert "Case #12" in e.title and any(f.name == "Duration" and "1h" in f.value for f in e.fields)
    auto = case_embed({**CASE, "moderator_id": None, "source": "automod"})
    assert any("auto-moderation" in f.value for f in auto.fields)
    assert "~~revoked~~" in case_line({**CASE, "revoked_at": datetime.now(timezone.utc)})
    assert "<@7>" in case_line(CASE) and "<@7>" not in case_line(CASE, show_moderator=False)
    dm = dm_text("warn", "Blue Heaven", 12, "Spamming")
    assert "case #12" in dm and "Spamming" in dm and "ticket" in dm
    assert "case" not in dm_text("kick", "Blue Heaven", None, "x") and "ban" not in dm_text("kick", "BH", None, "x").lower().split("**")[1]


def test_report_embed_states():
    r = {"id": 3, "reporter_id": 1, "target_id": 2, "reason": "r", "message_link": None, "status": "open",
         "handled_by": None, "created_at": datetime.now(timezone.utc)}
    assert report_embed(r).title == "🚨 Report #3 · Open"
    assert any(f.name == "Handled by" for f in report_embed({**r, "status": "closed", "handled_by": 9}).fields)


# =============================================================== command flows
STAFF, ADMIN = config.STAFF_ROLE_IDS[0], config.ADMIN_ROLE_ID
LOG_ID = 555


class FakeLog:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, view=None, allowed_mentions=None, **kw):
        self.sent.append({"content": content, "embed": embed, "view": view})
        return NS(id=9000 + len(self.sent))


def mk_member(uid, top=10, roles=(), bot=False, guild=None):
    m = MagicMock(spec=discord.Member)
    m.id, m.bot, m.top_role = uid, bot, NS(position=top)
    m.roles = [NS(id=r) for r in roles]
    m.mention, m.display_name = f"<@{uid}>", f"user{uid}"
    m.guild = guild
    m.send, m.timeout, m.kick = AsyncMock(), AsyncMock(), AsyncMock()
    m.is_timed_out = MagicMock(return_value=False)
    m.created_at = datetime.now(timezone.utc) - timedelta(days=1)
    return m


class FakeGuild:
    def __init__(self):
        self.id, self.name, self.owner_id = config.GUILD_ID, "Blue Heaven", 1
        self.log = FakeLog()
        self.members, self.me = {}, None
        self.default_role = NS(id=0)
        self.ban, self.unban = AsyncMock(), AsyncMock()
        self.fetch_ban = AsyncMock(side_effect=discord.NotFound(NS(status=404, reason="x"), "unknown ban"))
        self.me = mk_member(999, top=100, guild=self)
        self.me.guild_permissions = discord.Permissions.all()

    def add(self, uid, **kw):
        m = mk_member(uid, guild=self, **kw)
        self.members[uid] = m
        return m

    def get_member(self, uid): return self.members.get(uid)
    def get_channel(self, cid): return self.log if cid == LOG_ID else None
    def get_role(self, rid): return NS(id=rid, mention=f"<@&{rid}>")


def mk_i(user, guild):
    i = NS(user=user, guild=guild, guild_id=guild.id)
    i.response = NS(send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock())
    i.followup = NS(send=AsyncMock())
    return i


def said(i) -> str:
    calls = i.response.send_message.call_args_list + i.followup.send.call_args_list
    return " | ".join(str((c.args[0] if c.args else c.kwargs.get("content")) or "") for c in calls)


def flow(fn, **cfg):
    async def go():
        db = Database(DSN)
        await db.connect()
        try:
            for t in ("mod_cases", "mod_reports", "audit_log"):
                await db.pool.execute(f"DELETE FROM {t}")
            db.cfg.clear()
            await db.set("mod_log_channel_id", LOG_ID)
            for k, v in cfg.items():
                await db.set(k, v)
            g = FakeGuild()
            cog = object.__new__(Moderation)
            cog.bot = NS(db=db, get_guild=lambda gid: g, add_view=lambda v: None)
            cog.spam, cog.raid, cog._alerted = SpamDetector(S()), RaidDetector(8, 30), {}
            await fn(cog, db, g)
        finally:
            await db.close()
    asyncio.run(go())


@needs_db
def test_staff_warn_creates_case_dms_logs_and_audits():
    async def t(cog, db, g):
        mod, target = g.add(7, top=50, roles=[STAFF]), g.add(5, top=10)
        i = mk_i(mod, g)
        await Moderation.warn.callback(cog, i, target, "Spamming links")
        c = (await db.mod_cases_for(5))[0]
        assert (c["action"], c["moderator_id"], c["reason"]) == ("warn", 7, "Spamming links")
        assert "Case **#" in said(i) and "1** active" in said(i)
        target.send.assert_awaited_once()
        assert "Spamming links" in target.send.call_args.args[0] and f"#{c['id']}" in target.send.call_args.args[0]
        assert g.log.sent[0]["embed"].title.startswith("⚠️ Case") and c["log_message_id"] == 9001
        assert (await db.recent_audit_log(1))[0]["action"] == "mod.warn"
    flow(t)


@needs_db
def test_dm_failure_is_reported_but_case_still_recorded():
    async def t(cog, db, g):
        mod, target = g.add(7, top=50, roles=[STAFF]), g.add(5)
        target.send.side_effect = discord.Forbidden(NS(status=403, reason="x"), "closed DMs")
        i = mk_i(mod, g)
        await Moderation.warn.callback(cog, i, target, "x")
        assert "couldn't DM" in said(i) and len(await db.mod_cases_for(5)) == 1
    flow(t)


@needs_db
def test_dm_can_be_switched_off():
    async def t(cog, db, g):
        mod, target = g.add(7, top=50, roles=[STAFF]), g.add(5)
        await Moderation.warn.callback(cog, mk_i(mod, g), target, "x")
        target.send.assert_not_awaited()
    flow(t, mod_dm=False)


@needs_db
def test_non_staff_cannot_moderate_and_nothing_is_recorded():
    async def t(cog, db, g):
        nobody, target = g.add(8, top=60), g.add(5)
        i = mk_i(nobody, g)
        await Moderation.warn.callback(cog, i, target, "x")
        await Moderation.timeout.callback(cog, i, target, "10m", "x")
        assert "Only staff" in said(i) and await db.mod_cases_for(5) == []
        target.timeout.assert_not_awaited()
    flow(t)


@needs_db
def test_staff_cannot_ban_or_kick_but_admin_can():
    async def t(cog, db, g):
        staff, admin, target = g.add(7, top=50, roles=[STAFF]), g.add(8, top=60, roles=[ADMIN]), g.add(5)
        i = mk_i(staff, g)
        await Moderation.ban.callback(cog, i, target, "bad", None, 0)
        await Moderation.kick.callback(cog, i, target, "bad")
        assert "needs the" in said(i) and g.ban.await_count == 0 and target.kick.await_count == 0
        i2 = mk_i(admin, g)
        await Moderation.ban.callback(cog, i2, target, "bad", None, 3)
        g.ban.assert_awaited_once()
        assert g.ban.call_args.kwargs["delete_message_seconds"] == 3 * 86400
        c = (await db.mod_cases_for(5))[0]
        assert c["action"] == "ban" and c["expires_at"] is None
    flow(t)


@needs_db
def test_admin_only_list_is_configurable():
    async def t(cog, db, g):
        staff, target = g.add(7, top=50, roles=[STAFF]), g.add(5)
        await Moderation.kick.callback(cog, mk_i(staff, g), target, "x")
        target.kick.assert_awaited_once()                                              # kick opened to staff
        i = mk_i(staff, g)
        await Moderation.warn.callback(cog, i, g.add(6), "x")
        assert "needs the" in said(i)                                                   # warn now admin-only
    flow(t, mod_admin_only=["warn", "ban"])


@needs_db
def test_hierarchy_is_enforced_on_every_action():
    async def t(cog, db, g):
        staff, other_staff, boss = g.add(7, top=50, roles=[STAFF]), g.add(6, top=20, roles=[STAFF]), g.add(8, top=60, roles=[ADMIN])
        higher, owner = g.add(9, top=80), g.add(1, top=90)
        i = mk_i(staff, g)
        for target in (other_staff, boss, higher, staff):
            await Moderation.warn.callback(cog, i, target, "x")
            await Moderation.timeout.callback(cog, i, target, "1h", "x")
        assert (await db.mod_recent_cases(5)) == []
        i2 = mk_i(boss, g)
        await Moderation.kick.callback(cog, i2, owner, "x")                            # the owner is untouchable
        assert "owner" in said(i2) and owner.kick.await_count == 0
        top = g.add(10, top=100)                                                        # at/above the bot's own role
        i3 = mk_i(boss, g)
        await Moderation.timeout.callback(cog, i3, top, "1h", "x")
        assert "above mine" in said(i3) and top.timeout.await_count == 0
    flow(t)


@needs_db
def test_timeout_validation_and_success():
    async def t(cog, db, g):
        staff, target = g.add(7, top=50, roles=[STAFF]), g.add(5)
        for bad, why in (("soon", "duration"), ("29d", "28 days")):
            i = mk_i(staff, g)
            await Moderation.timeout.callback(cog, i, target, bad, "x")
            assert why in said(i)
        assert target.timeout.await_count == 0
        i = mk_i(staff, g)
        await Moderation.timeout.callback(cog, i, target, "2h", "Being rude")
        assert target.timeout.call_args.args[0] == timedelta(hours=2)
        c = (await db.mod_cases_for(5))[0]
        assert c["action"] == "timeout" and c["duration_seconds"] == 7200 and c["expires_at"] is not None
    flow(t)


@needs_db
def test_kick_sends_dm_before_kicking_and_failed_kick_records_nothing():
    async def t(cog, db, g):
        admin, target = g.add(8, top=60, roles=[ADMIN]), g.add(5)
        order = []
        target.send.side_effect = lambda *a, **k: order.append("dm")
        target.kick.side_effect = lambda *a, **k: order.append("kick")
        await Moderation.kick.callback(cog, mk_i(admin, g), target, "Rule 3")
        assert order == ["dm", "kick"] and (await db.mod_cases_for(5))[0]["action"] == "kick"
        t2 = g.add(6)
        t2.kick.side_effect = discord.Forbidden(NS(status=403, reason="x"), "Missing Permissions")
        i = mk_i(admin, g)
        await Moderation.kick.callback(cog, i, t2, "x")
        assert "Discord refused" in said(i) and await db.mod_cases_for(6) == []
    flow(t)


@needs_db
def test_missing_bot_permission_is_explained():
    async def t(cog, db, g):
        admin, target = g.add(8, top=60, roles=[ADMIN]), g.add(5)
        g.me.guild_permissions = discord.Permissions(send_messages=True)
        i = mk_i(admin, g)
        await Moderation.kick.callback(cog, i, target, "x")
        await Moderation.ban.callback(cog, i, target, "x", None, 0)
        await Moderation.timeout.callback(cog, i, target, "1h", "x")
        assert said(i).count("I need the") == 3 and await db.mod_cases_for(5) == []
    flow(t)


@needs_db
def test_temporary_ban_and_auto_unban():
    async def t(cog, db, g):
        admin = g.add(8, top=60, roles=[ADMIN])
        outsider = NS(id=400)                                                           # not in the server
        await Moderation.ban.callback(cog, mk_i(admin, g), outsider, "Trolling", "7d", 0)
        c = (await db.mod_cases_for(400))[0]
        assert c["duration_seconds"] == 7 * 86400 and c["expires_at"] > datetime.now(timezone.utc) + timedelta(days=6)
        assert await db.mod_due_tempbans() == []
        await db.pool.execute("UPDATE mod_cases SET expires_at = now() - interval '1 minute' WHERE id=$1", c["id"])
        await Moderation.tempbans.coro(cog)
        g.unban.assert_awaited_once()
        rows = await db.mod_cases_for(400)
        assert [r["action"] for r in rows] == ["unban", "ban"] and rows[1]["revoked_at"] is not None
        assert rows[0]["moderator_id"] is None and rows[0]["source"] == "system"
        await Moderation.tempbans.coro(cog)
        assert g.unban.await_count == 1                                                  # lifted only once
        i = mk_i(admin, g)
        await Moderation.ban.callback(cog, i, outsider, "x", "400d", 0)
        assert "365 days" in said(i)
    flow(t)


@needs_db
def test_already_banned_and_unban_flow():
    async def t(cog, db, g):
        admin = g.add(8, top=60, roles=[ADMIN])
        g.fetch_ban = AsyncMock(return_value=NS())                                       # the user IS banned
        i = mk_i(admin, g)
        await Moderation.ban.callback(cog, i, NS(id=400), "x", None, 0)
        assert "already banned" in said(i) and g.ban.await_count == 0
        await db.mod_add_case("ban", 400, 8, "old")
        await Moderation.unban.callback(cog, mk_i(admin, g), "400", "appealed")
        g.unban.assert_awaited_once()
        rows = await db.mod_cases_for(400)
        assert rows[0]["action"] == "unban" and rows[1]["revoked_at"] is not None
        g.unban.side_effect = discord.NotFound(NS(status=404, reason="x"), "unknown ban")
        i2 = mk_i(admin, g)
        await Moderation.unban.callback(cog, i2, "401", "x")
        assert "isn't banned" in said(i2)
        i3 = mk_i(admin, g)
        await Moderation.unban.callback(cog, i3, "not-a-number", "x")
        assert "numeric" in said(i3)
    flow(t)


@needs_db
def test_untimeout_revokes_the_timeout_case():
    async def t(cog, db, g):
        staff, target = g.add(7, top=50, roles=[STAFF]), g.add(5)
        await Moderation.timeout.callback(cog, mk_i(staff, g), target, "1h", "x")
        await Moderation.untimeout.callback(cog, mk_i(staff, g), target, "sorry")
        assert target.timeout.call_args.args == (None,) and target.timeout.call_args.kwargs["reason"].endswith(": sorry")
        rows = await db.mod_cases_for(5)
        assert [r["action"] for r in rows] == ["untimeout", "timeout"] and rows[1]["revoked_at"] is not None
    flow(t)


@needs_db
def test_warning_escalation_times_the_member_out_automatically():
    async def t(cog, db, g):
        staff, target = g.add(7, top=50, roles=[STAFF]), g.add(5)
        for n in (1, 2):
            i = mk_i(staff, g)
            await Moderation.warn.callback(cog, i, target, f"warn {n}")
        assert target.timeout.await_count == 1 and "Auto-timeout applied" in said(i)
        rows = await db.mod_cases_for(5)
        esc = rows[0]
        assert esc["action"] == "timeout" and esc["source"] == "escalation" and esc["moderator_id"] is None
        assert esc["duration_seconds"] == 30 * 60
        i = mk_i(staff, g)
        await Moderation.warn.callback(cog, i, target, "warn 3")
        assert target.timeout.await_count == 1                                           # 3rd warning: not a multiple of 2
    flow(t, mod_escalate_every=2, mod_escalate_minutes=30)


@needs_db
def test_escalation_off_by_default_and_old_or_pardoned_warnings_dont_count():
    async def t(cog, db, g):
        staff, boss, target = g.add(7, top=50, roles=[STAFF]), g.add(8, top=60, roles=[ADMIN]), g.add(5)
        for n in range(4):
            await Moderation.warn.callback(cog, mk_i(staff, g), target, f"w{n}")
        assert target.timeout.await_count == 0
        rows = await db.mod_cases_for(5)
        await Moderation.pardon.callback(cog, mk_i(boss, g), rows[0]["id"], "mistake")
        await db.pool.execute("UPDATE mod_cases SET created_at = now() - interval '90 days' WHERE id=$1", rows[1]["id"])
        assert await db.mod_active_warns(5, datetime.now(timezone.utc) - timedelta(days=30)) == 2
    flow(t)


@needs_db
def test_pardon_rules():
    async def t(cog, db, g):
        staff, boss, target = g.add(7, top=50, roles=[STAFF]), g.add(8, top=60, roles=[ADMIN]), g.add(5)
        w = await db.mod_add_case("warn", 5, 7, "x")
        n = await db.mod_add_case("note", 5, 7, "just a note")
        i = mk_i(staff, g)
        await Moderation.pardon.callback(cog, i, w["id"], "x")
        assert "needs the" in said(i)
        i = mk_i(boss, g)
        await Moderation.pardon.callback(cog, i, n["id"], "x")
        assert "isn't a warning" in said(i)
        await Moderation.pardon.callback(cog, mk_i(boss, g), w["id"], "ok")
        i = mk_i(boss, g)
        await Moderation.pardon.callback(cog, i, w["id"], "again")
        assert "already pardoned" in said(i)
    flow(t)


@needs_db
def test_case_views_and_member_privacy():
    async def t(cog, db, g):
        staff, member = g.add(7, top=50, roles=[STAFF]), g.add(5)
        await db.mod_add_case("warn", 5, 7, "secret mod name")
        await db.mod_add_case("note", 5, 7, "private staff note")
        i = mk_i(member, g)
        await Moderation.warnings.callback(cog, i, None)                                  # a member sees their own warnings
        e = i.response.send_message.call_args.kwargs["embed"]
        assert "secret mod name" in e.description and "<@7>" not in e.description and "private staff note" not in e.description
        i = mk_i(member, g)
        await Moderation.warnings.callback(cog, i, NS(id=99))
        assert "your own" in said(i)
        i = mk_i(member, g)
        await Moderation.cases.callback(cog, i, member)
        assert "Only staff" in said(i)
        i = mk_i(staff, g)
        await Moderation.cases.callback(cog, i, member)
        assert "private staff note" in i.response.send_message.call_args.kwargs["embed"].description
        i = mk_i(staff, g)
        await Moderation.case.callback(cog, i, 999)
        assert "No case" in said(i)
    flow(t)


@needs_db
def test_note_is_private_and_case_reason_can_be_fixed():
    async def t(cog, db, g):
        staff, target = g.add(7, top=50, roles=[STAFF]), g.add(5)
        await Moderation.note.callback(cog, mk_i(staff, g), target, "watch this one")
        target.send.assert_not_awaited()
        c = (await db.mod_cases_for(5))[0]
        await Moderation.case_reason.callback(cog, mk_i(staff, g), c["id"], "corrected")
        assert (await db.mod_get_case(c["id"]))["reason"] == "corrected"
    flow(t)


@needs_db
def test_report_flow_limits_and_buttons():
    async def t(cog, db, g):
        reporter, target, staff = g.add(20), g.add(5), g.add(7, top=50, roles=[STAFF])
        i = mk_i(reporter, g)
        await Moderation.report.callback(cog, i, reporter, "I report myself for fun", None)
        assert "yourself" in said(i)
        i = mk_i(reporter, g)
        await Moderation.report.callback(cog, i, target, "They keep insulting people", "https://example.com/x")
        assert "Discord link" in said(i)
        i = mk_i(reporter, g)
        link = f"https://discord.com/channels/{g.id}/1/2"
        await Moderation.report.callback(cog, i, target, "They keep insulting people", link)
        assert "Report **#" in said(i)
        sent = g.log.sent[-1]
        assert sent["embed"].title.startswith("🚨 Report") and sent["view"] is not None and "<@&" in sent["content"]
        i = mk_i(reporter, g)
        await Moderation.report.callback(cog, i, target, "Again the same person", None)
        assert "already reported" in said(i)
        for n in range(4):                                                                 # reach the 5-open limit
            await Moderation.report.callback(cog, mk_i(reporter, g), g.add(100 + n), "Something bad happened here", None)
        i = mk_i(reporter, g)
        await Moderation.report.callback(cog, i, g.add(200), "One report too many please", None)
        assert "5 open reports" in said(i)
        # a staff member presses a button
        rep = (await db.mod_report_list())[-1]
        view = ReportView(cog)
        bi = NS(user=staff, message=NS(id=rep["log_message_id"]), response=NS(send_message=AsyncMock(), edit_message=AsyncMock()))
        await view._set(bi, "reviewing")
        assert (await db.mod_report_get(rep["id"]))["status"] == "reviewing"
        await view._set(bi, "closed")
        r2 = await db.mod_report_get(rep["id"])
        assert r2["status"] == "closed" and r2["handled_by"] == 7 and r2["closed_at"] is not None
        assert bi.response.edit_message.call_args.kwargs["view"] is None                    # buttons go away when closed
        outsider = NS(user=reporter, response=NS(send_message=AsyncMock()))
        assert await view.interaction_check(outsider) is False
        assert await view.interaction_check(NS(user=staff)) is True
    flow(t)


@needs_db
def test_actions_with_no_log_channel_still_work_and_alert():
    async def t(cog, db, g):
        await db.set("mod_log_channel_id", 0)
        staff, target = g.add(7, top=50, roles=[STAFF]), g.add(5)
        await Moderation.warn.callback(cog, mk_i(staff, g), target, "x")
        assert len(await db.mod_cases_for(5)) == 1
    flow(t)


# ------------------------------------------------------------- auto-moderation
def mk_msg(g, author, text="hello", channel_id=10, mid=1, mentions=()):
    ch = NS(id=channel_id, mention=f"<#{channel_id}>")
    deleted = []
    g_channel = NS(id=channel_id, get_partial_message=lambda m: NS(delete=AsyncMock(side_effect=lambda: deleted.append(m))))
    return NS(guild=g, author=author, content=text, channel=ch, id=mid, mentions=list(mentions), role_mentions=[],
              jump_url=f"https://discord.com/channels/{g.id}/{channel_id}/{mid}"), g_channel, deleted


def feed(cog, g, author, texts, ch_obj):
    async def go():
        for n, text in enumerate(texts):
            m, _, _ = mk_msg(g, author, text, mid=n + 1)
            await Moderation.on_message(cog, m)
    return go()


@needs_db
def test_spam_alert_only_by_default_takes_no_action():
    async def t(cog, db, g):
        spammer = g.add(30)
        await feed(cog, g, spammer, [f"buy {i}" for i in range(8)], None)
        assert spammer.timeout.await_count == 0 and await db.mod_cases_for(30) == []
        assert len(g.log.sent) == 1 and "Possible spam" in g.log.sent[0]["embed"].title
        assert "Alert only" in g.log.sent[0]["embed"].description
    flow(t)


@needs_db
def test_spam_act_mode_deletes_times_out_and_records_automod_case():
    async def t(cog, db, g):
        spammer = g.add(30)
        deleted = []
        chan = NS(id=10, get_partial_message=lambda m: NS(delete=AsyncMock(side_effect=lambda: deleted.append(m))))
        g.get_channel = lambda cid: g.log if cid == LOG_ID else chan if cid == 10 else None
        await feed(cog, g, spammer, [f"buy {i}" for i in range(8)], None)
        assert spammer.timeout.await_count == 1 and spammer.timeout.call_args.args[0] == timedelta(minutes=10)
        assert len(deleted) >= 6
        c = (await db.mod_cases_for(30))[0]
        assert (c["action"], c["moderator_id"], c["source"]) == ("timeout", None, "automod")
        spammer.send.assert_awaited()
    flow(t, automod_spam="act")


@needs_db
def test_staff_exempt_channels_and_off_switch_are_never_checked():
    async def t(cog, db, g):
        staff, normal = g.add(7, top=50, roles=[STAFF]), g.add(31)
        await feed(cog, g, staff, ["spam"] * 10, None)
        assert g.log.sent == []
        bot_user = g.add(32, bot=True)
        await feed(cog, g, bot_user, ["spam"] * 10, None)
        assert g.log.sent == []
        for n in range(10):
            m, _, _ = mk_msg(g, normal, "spam", channel_id=77, mid=n)                          # exempt channel
            await Moderation.on_message(cog, m)
        assert g.log.sent == []
        await db.set("automod_spam", "off")
        await feed(cog, g, normal, ["x"] * 10, None)
        assert g.log.sent == []
    flow(t, automod_exempt_channels=[77])


@needs_db
def test_mass_mentions_and_alert_throttle():
    async def t(cog, db, g):
        u = g.add(33)
        pings = [NS(id=1000 + i) for i in range(7)]
        m, _, _ = mk_msg(g, u, "hey all", mentions=pings)
        await Moderation.on_message(cog, m)
        assert "mass mentions" in g.log.sent[0]["embed"].description
        m, _, _ = mk_msg(g, u, "hey again", mid=2, mentions=pings)
        await Moderation.on_message(cog, m)
        assert len(g.log.sent) == 1                                                         # same member: throttled
    flow(t)


@needs_db
def test_raid_alert_only_pings_staff_and_acts_only_on_young_accounts_when_enabled():
    async def t(cog, db, g):
        young = [g.add(500 + n) for n in range(8)]
        old = g.add(600)
        old.created_at = datetime.now(timezone.utc) - timedelta(days=900)
        for m in young[:6] + [old]:                                                         # 7 joins so far
            await Moderation.on_member_join(cog, m)
        assert g.log.sent == []                                                             # threshold is 8
        await Moderation.on_member_join(cog, young[7])
        assert len(g.log.sent) == 1 and "Possible raid" in g.log.sent[0]["embed"].title
        assert "<@&" in g.log.sent[0]["content"] and all(m.timeout.await_count == 0 for m in young)
        assert "Alert only" in g.log.sent[0]["embed"].fields[0].value
    flow(t)


@needs_db
def test_raid_act_mode_times_out_young_accounts_only():
    async def t(cog, db, g):
        young = [g.add(500 + n) for n in range(7)]
        old = g.add(600)
        old.created_at = datetime.now(timezone.utc) - timedelta(days=900)
        for m in young + [old]:
            await Moderation.on_member_join(cog, m)
        assert all(m.timeout.await_count == 1 for m in young) and old.timeout.await_count == 0
        assert len([c for c in await db.mod_recent_cases(20) if c["source"] == "automod"]) == 7
    flow(t, automod_raid="act", automod_raid_joins=8)


@needs_db
def test_raid_ignores_bots_and_off_switch():
    async def t(cog, db, g):
        for n in range(20):
            await Moderation.on_member_join(cog, g.add(700 + n, bot=True))
        assert g.log.sent == []
        await db.set("automod_raid", "off")
        for n in range(20):
            await Moderation.on_member_join(cog, g.add(800 + n))
        assert g.log.sent == []
    flow(t)


# ---------------------------------------------------------------- DB specifics
@needs_db
def test_case_numbers_summary_and_profile_integration():
    async def t(cog, db, g):
        a = await db.mod_add_case("warn", 5, 7, "a")
        b = await db.mod_add_case("warn", 5, 7, "b")
        assert b["id"] == a["id"] + 1
        await db.mod_add_case("timeout", 5, None, "auto", source="automod", duration_seconds=60)
        await db.mod_revoke_case(a["id"], 8, "mistake")
        s = await db.mod_summary(5)
        assert s["total"] == 3 and s["active_warns"] == 1 and s["by_action"]["warn"] == {"total": 2, "active": 1}
        assert (await db.mod_summary(404))["total"] == 0
        from orion.services import profiles
        d = await profiles.get_profile(db, 5, include_staff=True)
        assert d["staff"]["moderation"]["active_warns"] == 1
        assert "staff" not in await profiles.get_profile(db, 5)                               # members never see it
        from orion.profile_ui import profile_embed
        e = profile_embed(d, NS(display_name="x", display_avatar=NS(url="https://a.b/c.png")), staff_view=True)
        assert any("moderation record" in f.name for f in e.fields)
        assert not await db.mod_revoke_case(a["id"], 8, "twice")
        note = await db.mod_add_case("note", 5, 7, "n")
        assert not await db.mod_revoke_case(note["id"], 8, "notes can't be revoked")
    flow(t)
