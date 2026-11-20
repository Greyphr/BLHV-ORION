import asyncio
import io
import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord
from PIL import Image

from orion.cogs.leveling import Leveling


def _png():
    b = io.BytesIO(); Image.new("RGB", (64, 64), (90, 60, 160)).save(b, "PNG"); return b.getvalue()


def _setup(avatar_ok=True, enabled=True):
    cfg = {"levelup_channel_id": 5, "levelup_announce_enabled": enabled}
    db = MagicMock(); db.get = lambda k, d=None: cfg.get(k, d)
    db.get_user = AsyncMock(return_value={"xp": 614, "messages": 3, "voice_minutes": 0})
    db.get_rank = AsyncMock(return_value=2)
    ch = MagicMock(); ch.send = AsyncMock()
    avatar = MagicMock(); avatar.url = "https://x/a.png"
    if avatar_ok:
        avatar.replace.return_value.read = AsyncMock(return_value=_png())
    else:
        avatar.replace.return_value.read = AsyncMock(side_effect=RuntimeError("cdn down"))
    member = MagicMock(mention="<@9>", display_name="Sam", display_avatar=avatar)
    member.guild.get_channel.return_value = ch
    return NS(bot=NS(db=db)), member, ch


def run(avatar_ok=True, enabled=True, new_rank="Planetary", level=9):
    me, member, ch = _setup(avatar_ok, enabled)
    asyncio.run(Leveling.announce(me, member, level, new_rank))
    return ch


def test_levelup_posts_the_rank_card():
    ch = run()
    kw = ch.send.call_args.kwargs
    assert kw["content"] == "<@9>"                                  # still pings the member
    assert kw["file"].filename == "levelup.png" and kw["embed"].image.url == "attachment://levelup.png"
    assert "Planetary" in kw["embed"].description                      # rank change is called out
    assert Image.open(kw["file"].fp).size == (2170, 725)            # a realm change uses the same banner size


def test_non_milestone_level_uses_the_generic_banner():
    kw = run(level=10, new_rank=None).send.call_args.kwargs
    assert Image.open(kw["file"].fp).size == (2170, 725)


def test_milestone_art_is_optional():
    from orion.images import milestone_template
    assert milestone_template(10) is None          # no per-realm artwork shipped; the generic banner is used


def test_levelup_without_rank_change_has_no_rank_line():
    kw = run(new_rank=None).send.call_args.kwargs
    assert kw["embed"].description is None and kw["file"].filename == "levelup.png"


def test_levelup_falls_back_to_text_if_card_fails():
    ch = run(avatar_ok=False)
    kw = ch.send.call_args.kwargs
    assert "file" not in kw and "advanced to **Planetary 1**" in kw["embed"].description
    assert ch.send.await_count == 1


def test_levelup_respects_the_off_switch():
    assert not run(enabled=False).send.called
