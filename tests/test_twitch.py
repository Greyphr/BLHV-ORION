import asyncio
import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from orion import config
from orion.cogs import twitch as tw
from orion.cogs.twitch import Twitch, build_live_embed, decide, normalize_twitch_username, ping_parts
from orion.twitch_api import TwitchRateLimited


# ------------------------------------------------------------------ helpers
def test_normalize_username():
    assert normalize_twitch_username("  @Some_Name ") == "some_name"
    assert normalize_twitch_username("https://www.twitch.tv/Some_Name/videos?x=1") == "some_name"
    assert normalize_twitch_username("twitch.tv/abc") == "abc"
    assert normalize_twitch_username("bad name!") is None
    assert normalize_twitch_username("a" * 26) is None
    assert normalize_twitch_username("") is None


def test_decide():
    assert decide(False, None, False, "1") == (True, False)        # normal go-live
    assert decide(True, "1", False, "1") == (False, False)         # bot restarted mid-stream: same stream, stay quiet
    assert decide(True, "1", False, "2") == (True, False)          # bot was off, a NEW stream started
    assert decide(True, None, False, "9") == (False, True)         # old record without an id: just remember it
    assert decide(True, "1", True, "2") == (False, False)          # already announced this run
    assert decide(False, None, True, "1") == (False, False)        # announced but DB write failed: no double post


def test_ping_parts_role(monkeypatch):
    monkeypatch.setattr(config, "TWITCH_PING_ROLE_ID", 123)
    text, allowed = ping_parts("role")
    assert text == " <@&123>"
    assert not allowed.everyone and allowed.roles[0].id == 123


def test_ping_parts_no_role_chosen_pings_nobody(monkeypatch):
    monkeypatch.setattr(config, "TWITCH_PING_ROLE_ID", 0)
    text, allowed = ping_parts("role")
    assert text == "" and not allowed.everyone and not allowed.roles


def test_ping_parts_custom_role():
    text, allowed = ping_parts("role", 555)
    assert text == " <@&555>" and allowed.roles[0].id == 555


def test_ping_parts():
    assert ping_parts("everyone")[0] == " @everyone" and ping_parts("everyone")[1].everyone
    assert ping_parts("here")[0] == " @here"
    text, allowed = ping_parts("none")
    assert text == "" and not allowed.everyone


def test_live_embed():
    info = {"display_name": "Sam", "profile_image_url": "https://x/p.png"}
    e = build_live_embed("sam", info, {"title": "Hi", "game_name": "Game",
                                       "thumbnail_url": "https://x/{width}x{height}.jpg"})
    assert e.title == "Hi" and e.url == "https://twitch.tv/sam" and "Game" in e.description
    assert e.color.value == config.COLOR_ACCENT and "440x248" in e.image.url
    assert "is now live" in e.author.name and e.thumbnail.url == "https://x/p.png"
    bare = build_live_embed("sam", {"display_name": "Sam", "profile_image_url": ""}, {})
    assert bare.title == "Live on Twitch!" and bare.image.url is None


def _cog(db, api=None):
    me = Twitch.__new__(Twitch)
    me.bot = NS(db=db, get_guild=lambda _id: GUILD)
    me.api = api
    me._announced, me._rate_limited_until = set(), 0.0
    return me


# ------------------------------------------------------------- polling loop
class FakeDB:
    def __init__(self, rows, cfg=None):
        self.rows = {r["login"]: {"is_live": False, "stream_id": None, "missed_checks": 0,
                                  "display_name": r["login"].title(), "profile_image_url": "", **r} for r in rows}
        self.cfg = {"twitch_channel_id": 5, **(cfg or {})}

    def get(self, k, d=None): return self.cfg.get(k, d)
    async def twitch_list(self): return [dict(r) for r in self.rows.values()]

    async def twitch_set_live(self, login, live, sid=None):
        r = self.rows[login]; r["is_live"] = live
        r["stream_id"] = (sid or r["stream_id"]) if live else None
        if not live: r["missed_checks"] = 0

    async def twitch_bump_missed(self, login):
        self.rows[login]["missed_checks"] += 1; return self.rows[login]["missed_checks"]

    async def twitch_reset_missed(self, login): self.rows[login]["missed_checks"] = 0


CHANNEL = MagicMock(spec=discord.TextChannel); CHANNEL.send = AsyncMock()
CHANNEL.__class__ = discord.TextChannel
GUILD = MagicMock(); GUILD.get_channel_or_thread.return_value = CHANNEL


def _run(me, live):
    me.api = NS(get_live_streams=AsyncMock(return_value=live))
    asyncio.run(me.run_once())


def _stream(sid="100"):
    return {"id": sid, "user_name": "Sam", "title": "T", "game_name": "G", "thumbnail_url": ""}


def setup_function(_):
    CHANNEL.send.reset_mock(side_effect=True)


def test_announces_once_per_stream_then_goes_offline_after_grace():
    db = FakeDB([{"login": "sam"}]); me = _cog(db)
    _run(me, {"sam": _stream()})
    assert CHANNEL.send.await_count == 1
    kw = CHANNEL.send.call_args.kwargs
    assert kw["content"] == "🔴 **Sam** is now live! https://twitch.tv/sam"   # no ping role configured yet
    assert kw["embed"].url
    assert db.rows["sam"]["is_live"] and db.rows["sam"]["stream_id"] == "100"

    _run(me, {"sam": _stream()})                                  # still live: no second post
    assert CHANNEL.send.await_count == 1

    for _ in range(config.TWITCH_OFFLINE_GRACE - 1):              # blips don't end the stream
        _run(me, {})
    assert db.rows["sam"]["is_live"]
    _run(me, {})                                                  # grace used up
    assert not db.rows["sam"]["is_live"] and "sam" not in me._announced

    _run(me, {"sam": _stream("101")})                             # next stream posts again
    assert CHANNEL.send.await_count == 2


def test_restart_mid_stream_stays_quiet_but_new_stream_posts():
    db = FakeDB([{"login": "sam", "is_live": True, "stream_id": "100"}]); me = _cog(db)
    _run(me, {"sam": _stream("100")})
    assert not CHANNEL.send.called
    _run(me, {"sam": _stream("200")})
    assert CHANNEL.send.await_count == 1


def test_failed_send_retries_next_cycle():
    db = FakeDB([{"login": "sam"}]); me = _cog(db)
    CHANNEL.send.side_effect = discord.HTTPException(MagicMock(status=500, reason="x"), "boom")
    import orion.alerts as alerts
    alerts.alert_staff = AsyncMock()
    _run(me, {"sam": _stream()})
    assert not db.rows["sam"]["is_live"] and "sam" not in me._announced
    CHANNEL.send.side_effect = None
    _run(me, {"sam": _stream()})
    assert CHANNEL.send.await_count == 2 and db.rows["sam"]["is_live"]


def test_ping_none_and_no_channel_and_rate_limit():
    db = FakeDB([{"login": "sam"}], {"twitch_ping": "none"}); me = _cog(db)
    _run(me, {"sam": _stream()})
    assert "@everyone" not in CHANNEL.send.call_args.kwargs["content"]

    db = FakeDB([{"login": "sam"}]); db.cfg.pop("twitch_channel_id"); me = _cog(db)   # no channel: nothing happens
    _run(me, {"sam": _stream()})
    assert not db.rows["sam"]["is_live"]

    db = FakeDB([{"login": "sam"}]); me = _cog(db)
    me.api = NS(get_live_streams=AsyncMock(side_effect=TwitchRateLimited(None)))
    CHANNEL.send.reset_mock()
    asyncio.run(me.run_once())
    assert me._rate_limited_until > 0 and not CHANNEL.send.called
