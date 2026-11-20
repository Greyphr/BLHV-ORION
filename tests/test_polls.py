"""Polls: pure helpers always run; the SQL tests need a real PostgreSQL (TEST_DATABASE_URL), e.g.

    TEST_DATABASE_URL=postgresql://user:pw@localhost/orion_test python -m pytest tests/test_polls.py
"""
import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from orion.db import Database
from orion.polls import PollError, build_embed, parse_options

UTC = timezone.utc
T0 = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run the PostgreSQL tests")


# ----------------------------------------------------------------- helpers
def test_parse_options():
    assert parse_options("Yes | No |  Maybe ") == ["Yes", "No", "Maybe"]
    assert parse_options("A\nB") == ["A", "B"]


@pytest.mark.parametrize("bad", ["", "Only one", "A | a", "|".join(str(n) for n in range(11)), "A | " + "x" * 81])
def test_parse_options_rejects(bad):
    with pytest.raises(PollError):
        parse_options(bad)


def poll(**kw):
    base = {"id": 3, "question": "Which day?", "options": ["Fri", "Sat"], "anonymous": False, "allow_change": True,
            "eligible_role_ids": [], "ends_at": T0 + timedelta(hours=1), "status": "active"}
    return {**base, **kw}


def test_embed_live_and_final():
    e = build_embed(poll(), {0: 3, 1: 1})
    assert "**3** (75%)" in e.description and "🏆" not in e.description
    assert "Public poll" in e.footer.text
    final = build_embed(poll(anonymous=True), {0: 2, 1: 2}, ended=True)
    assert final.description.count("🏆") == 2                      # a tie crowns both
    assert "Private poll" in final.footer.text and "Final results" in final.fields[0].value
    assert "🏆" not in build_embed(poll(), {}, ended=True).description   # no votes, no winner


# --------------------------------------------------------------------- SQL
def run(coro_fn):
    async def go():
        db = Database(DSN)
        await db.connect()
        await db.pool.execute("TRUNCATE polls RESTART IDENTITY CASCADE")
        try:
            return await coro_fn(db)
        finally:
            await db.close()
    return asyncio.run(go())


async def new_poll(db, **kw):
    args = dict(question="Q?", options=["A", "B", "C"], anonymous=False, allow_change=True, role_ids=[], channel_id=10,
                created_by=1, ends_at=T0 + timedelta(hours=1))
    return await db.poll_create(**{**args, **kw})


@needs_db
def test_vote_new_same_changed():
    async def t(db):
        pid = await new_poll(db)
        assert (await db.poll_vote(pid, 5, 0, T0)).state == "new"
        assert (await db.poll_vote(pid, 5, 0, T0)).state == "same"
        r = await db.poll_vote(pid, 5, 2, T0)
        assert (r.state, r.previous) == ("changed", 0)
        await db.poll_vote(pid, 6, 2, T0)
        assert await db.poll_counts(pid) == {2: 2}
    run(t)


@needs_db
def test_votes_final_locks():
    async def t(db):
        pid = await new_poll(db, allow_change=False)
        await db.poll_vote(pid, 5, 0, T0)
        r = await db.poll_vote(pid, 5, 1, T0)
        assert (r.state, r.previous) == ("locked", 0)
        assert await db.poll_counts(pid) == {0: 1}
    run(t)


@needs_db
def test_double_click_counts_once():
    async def t(db):
        pid = await new_poll(db)
        res = await asyncio.gather(*[db.poll_vote(pid, 5, 1, T0) for _ in range(8)])
        assert sorted(r.state for r in res).count("new") == 1
        assert await db.poll_counts(pid) == {1: 1}
    run(t)


@needs_db
def test_ended_and_expired_reject_votes():
    async def t(db):
        pid = await new_poll(db)
        assert (await db.poll_vote(pid, 5, 0, T0 + timedelta(hours=2))).state == "ended"     # past ends_at
        assert (await db.poll_vote(pid, 5, 9, T0)).state == "ended"                         # bad option index
        assert await db.poll_end(pid) is True and await db.poll_end(pid) is False           # closes exactly once
        assert (await db.poll_vote(pid, 5, 0, T0)).state == "ended"
    run(t)


@needs_db
def test_due_lookup_and_voters():
    async def t(db):
        soon = await new_poll(db, ends_at=T0 + timedelta(minutes=5))
        await new_poll(db, ends_at=None)
        assert [r["id"] for r in await db.poll_due(T0 + timedelta(minutes=6))] == [soon]
        await db.poll_vote(soon, 7, 1, T0)
        v = await db.poll_voters(soon)
        assert [(r["user_id"], r["option_idx"]) for r in v] == [(7, 1)]       # staff view has the voter even if "private"
        await db.poll_delete(soon)
        assert await db.poll_voters(soon) == []
    run(t)


# ------------------------------------------------------------- cog smoke
def test_view_layout_and_ids():
    from unittest.mock import MagicMock
    from orion.cogs.poll import PollView
    v = PollView(MagicMock(), [f"o{n}" for n in range(10)], voters_button=True)
    ids = [c.custom_id for c in v.children]
    assert len(ids) == len(set(ids)) == 11 and ids[-1] == "poll:voters"
    assert {c.row for c in v.children} == {0, 1, 2}
    assert [c.custom_id for c in PollView(MagicMock(), ["a", "b"], voters_button=False).children] == ["poll:o:0", "poll:o:1"]


@needs_db
def test_vote_flow_private_vs_public():
    from unittest.mock import AsyncMock, MagicMock
    import discord
    from orion.cogs.poll import Polls

    async def t(db):
        bot = MagicMock(); bot.db = db
        cog = Polls(bot)
        soon = discord.utils.utcnow() + timedelta(hours=1)      # the cog checks the real clock, so don't use the fixed T0
        pub, priv = await new_poll(db, ends_at=soon), await new_poll(db, anonymous=True, role_ids=[42], ends_at=soon)
        await db.poll_set_message(pub, 900); await db.poll_set_message(priv, 901)

        def click(uid, mid, roles=()):
            i = MagicMock(); i.response.defer = AsyncMock(); i.followup.send = AsyncMock()
            i.message.id = mid
            i.user = MagicMock(spec=discord.Member); i.user.id = uid
            i.user.roles = [MagicMock(id=r) for r in roles]
            return i

        i = click(5, 900); await cog.on_vote(i, 1)
        assert "Vote recorded" in i.followup.send.call_args.args[0] and "public" in i.followup.send.call_args.args[0]
        assert 900 and pub in cog._dirty

        i = click(6, 901); await cog.on_vote(i, 0)                       # no role -> refused, nothing stored
        assert "Only members with" in i.followup.send.call_args.args[0] and await db.poll_counts(priv) == {}
        i = click(6, 901, roles=[42]); await cog.on_vote(i, 0)
        assert "private" in i.followup.send.call_args.args[0] and await db.poll_counts(priv) == {0: 1}

        i = click(8, 901, roles=[42]); await cog.on_voters(i)            # members can't list voters of a private poll
        assert "private poll" in i.followup.send.call_args.args[0]
        i = click(8, 900); await cog.on_voters(i)
        assert i.followup.send.call_args.kwargs["embed"].fields[1].value.strip() == "<@5>"

        e = await cog.staff_embed(await db.poll_get(priv))               # staff panel DOES show the private voter
        assert "<@6>" in e.fields[0].value
    run(t)


# ------------------------------------------------------------------ banner
def test_embed_banner():
    assert build_embed(poll(), {}).image.url is None
    assert build_embed(poll(), {}, banner_url="attachment://poll-banner.png").image.url == "attachment://poll-banner.png"


def test_commands_register():
    from unittest.mock import MagicMock
    from orion.cogs.poll import Polls
    assert {c.name for c in Polls(MagicMock()).app_command.commands} == {"create", "manage", "banner"}


@needs_db
def test_banner_set_replace_remove_and_refresh():
    from unittest.mock import AsyncMock, MagicMock
    from orion.cogs.poll import Polls
    from orion.cogs.announce import store_image

    import io
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (4, 4), (124, 58, 237)).save(buf, "PNG"); PNG = buf.getvalue()

    async def t(db):
        pid = await new_poll(db)
        assert (await db.poll_get(pid))["banner_id"] is None
        img = await store_image(db, PNG)
        await db.poll_set_banner(pid, img)
        await db.poll_set_message(pid, 900)

        bot = MagicMock(); bot.db = db
        msg = MagicMock(); msg.edit = AsyncMock()
        ch = MagicMock(); ch.get_partial_message = lambda mid: msg
        bot.get_channel = lambda cid: ch
        cog = Polls(bot)

        url, files = await cog._banner(await db.poll_get(pid))
        assert url == "attachment://poll-banner.png" and files[0].filename == "poll-banner.png"

        assert await cog.refresh(pid) is True                               # normal refresh: keeps the existing attachment
        kw = msg.edit.call_args.kwargs
        assert "attachments" not in kw and kw["embed"].image.url == url

        assert await cog.refresh(pid, reupload=True) is True                # banner changed: re-send the file
        kw = msg.edit.call_args.kwargs
        assert kw["attachments"][0].filename == "poll-banner.png" and kw["embed"].image.url == url

        await db.poll_set_banner(pid, None)                                 # removed: file list emptied, no image
        await cog.refresh(pid, reupload=True)
        kw = msg.edit.call_args.kwargs
        assert kw["attachments"] == [] and kw["embed"].image.url is None
    run(t)


# --------------------------------------------- /admin panel + button builder
def _inter(uid=1, guild=None, channel_id=10):
    from unittest.mock import AsyncMock, MagicMock
    i = MagicMock()
    i.user.id, i.channel_id, i.guild = uid, channel_id, guild or MagicMock()
    i.response.defer = AsyncMock(); i.response.send_modal = AsyncMock(); i.response.send_message = AsyncMock()
    i.edit_original_response = AsyncMock(); i.followup.send = AsyncMock()
    return i


def test_dashboard_has_new_sections_and_buttons():
    from unittest.mock import MagicMock
    from orion.cogs.dashboard import PAGES, Dashboard
    assert {"giveaways", "polls", "announce"} <= set(PAGES)
    bot = MagicMock()
    labels = {}
    for page in ("giveaways", "polls", "announce"):
        d = Dashboard(bot, _inter(), page)
        d._build()
        labels[page] = [c.label for c in d.children if getattr(c, "label", None)]
    assert labels["giveaways"] == ["New giveaway", "Manage giveaways"]
    assert labels["polls"] == ["New poll", "Manage polls"]
    assert labels["announce"] == ["New announcement", "History", "Templates"]


@needs_db
def test_dashboard_pages_render_with_live_data():
    from unittest.mock import MagicMock
    from orion.cogs.dashboard import Dashboard

    async def t(db):
        pid = await new_poll(db, question="Which day?", anonymous=True)
        await db.poll_set_message(pid, 900)
        bot = MagicMock(); bot.db = db
        db.cfg = {}
        for page in ("overview", "giveaways", "polls", "announce"):
            i = _inter()
            d = Dashboard(bot, i, page)
            await d.render()
            emb = i.edit_original_response.call_args.kwargs["embed"]
            text = (emb.description or "") + " ".join(f.value for f in emb.fields)
            if page == "polls":
                assert "Which day?" in text and "🔒" in text
            if page == "overview":
                assert "1 open poll" in text and "0 giveaways" in text
            if page == "giveaways":
                assert "No giveaway is running" in text
    run(t)


def test_buttons_open_a_new_private_message():
    from unittest.mock import AsyncMock, MagicMock
    from orion.cogs.dashboard import Dashboard

    async def go():
        for method, cog_name, opener in (("_gw_new", "giveaway", "open_builder"), ("_gw_manage", "giveaway", "open_manage"),
                                         ("_poll_new", "poll", "open_builder"), ("_poll_manage", "poll", "open_manage")):
            bot, cog = MagicMock(), MagicMock()
            setattr(cog, opener, AsyncMock())
            bot.get_cog = lambda n, c=cog, want=cog_name: c if n == want else None
            i = _inter()
            await getattr(Dashboard(bot, i), method)(i)
            i.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)   # a NEW message, not an edit of the panel
            getattr(cog, opener).assert_awaited_once_with(i)
    asyncio.run(go())


@needs_db
def test_button_poll_builder_end_to_end():
    from unittest.mock import AsyncMock, MagicMock
    import discord
    from orion.cogs.poll import Polls
    from orion.poll_ui import PollBuilder, TextModal

    async def t(db):
        bot = MagicMock(); bot.db = db
        cog = Polls(bot)
        sent = MagicMock(id=555, jump_url="https://discord.test/m")
        chan = MagicMock(id=10, mention="<#10>"); chan.send = AsyncMock(return_value=sent)
        chan.permissions_for = lambda me: MagicMock(view_channel=True, send_messages=True, embed_links=True, attach_files=True)
        guild = MagicMock(); guild.get_channel = lambda cid: chan
        origin = _inter(uid=1, guild=guild)
        b = PollBuilder(cog, origin)

        await b.b_publish.callback(_inter())                               # nothing filled in yet
        assert "add the question and options" in origin.edit_original_response.call_args.kwargs["content"]
        assert chan.send.await_count == 0

        m = TextModal(b); m.q._value, m.o._value, m.t._value = "Which day?", "Fri\nSat\nSun", "2h"
        await m.on_submit(_inter())
        assert b.draft.options == ["Fri", "Sat", "Sun"] and b.draft.duration == 7200
        m = TextModal(b); m.q._value, m.o._value, m.t._value = "Which day?", "only one", ""
        await m.on_submit(_inter())                                         # bad input keeps the good draft + shows why
        assert b.draft.options == ["Fri", "Sat", "Sun"] and "at least 2 options" in origin.edit_original_response.call_args.kwargs["content"]

        await b.b_private.callback(_inter()); await b.b_final.callback(_inter())
        b.draft.role_ids = [42]
        await b.b_publish.callback(_inter())
        row = (await db.poll_list(1))[0]
        assert (row["question"], row["anonymous"], row["allow_change"], row["eligible_role_ids"], row["message_id"]) == \
               ("Which day?", True, False, [42], 555)
        assert row["ends_at"] is not None and "is live" in origin.edit_original_response.call_args.kwargs["content"]
        assert chan.send.call_args.kwargs["embed"].footer.text.startswith("🔒 Private poll")

        other = _inter(uid=2)                                               # someone else can't use my builder
        assert await b.interaction_check(other) is False
    run(t)
