"""Tickets: pure helpers always run; the SQL tests need a real PostgreSQL (TEST_DATABASE_URL), e.g.

    TEST_DATABASE_URL=postgresql://user:pw@localhost/orion_test python -m pytest tests/test_tickets.py
"""
import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from orion.db import Database
from orion.tickets import (DEFAULT_TYPES, MAX_QUESTION_LEN, MAX_QUESTIONS, SETTINGS, TicketError, channel_name,
                            fmt_duration, log_embed, panel_embed, parse_color, parse_questions, parse_setting,
                            render_transcript, render_transcript_text, setting, slug, star_line, stats_embed, ticket_embed)

UTC = timezone.utc
T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run the PostgreSQL tests")


class FakeDB(dict):
    def get(self, k, d=None):
        return super().get(k, d)


# ----------------------------------------------------------------- helpers
@pytest.mark.parametrize("raw,out", [("Cool_Guy 99!", "cool-guy-99"), ("ÄÖÜ", "member"), ("--a--b--", "a-b"),
                                     ("", "member")])
def test_slug(raw, out):
    assert slug(raw) == out


def test_channel_name():
    assert channel_name(42, "Greyphr") == "ticket-0042-greyphr"
    assert channel_name(12345, "x" * 300).startswith("ticket-12345-") and len(channel_name(1, "x" * 300)) <= 100


def test_parse_questions():
    assert parse_questions("  Who?\n\nWhat happened? \n") == ["Who?", "What happened?"]
    assert parse_questions("") == []
    with pytest.raises(TicketError):
        parse_questions("\n".join(f"Q{n}" for n in range(MAX_QUESTIONS + 1)))
    with pytest.raises(TicketError):
        parse_questions("x" * (MAX_QUESTION_LEN + 1))


def test_default_types_fit_discord_limits():
    for t in DEFAULT_TYPES:
        assert len(t["questions"]) <= MAX_QUESTIONS and all(len(q) <= MAX_QUESTION_LEN for q in t["questions"])
        assert len(t["description"]) <= 100


def test_settings_parse_and_fallback():
    assert parse_setting("ticket_max_open", " 3 ") == 3
    for bad in ("0", "11", "abc", ""):
        with pytest.raises(TicketError):
            parse_setting("ticket_max_open", bad)
    db = FakeDB(ticket_max_open=99, ticket_autoclose_hours=0)
    assert setting(db, "ticket_max_open") == SETTINGS["ticket_max_open"][1]    # out of range -> default
    assert setting(db, "ticket_autoclose_hours") == 0                          # 0 = auto-close off
    assert setting(FakeDB(), "ticket_delete_hours") == 24


def test_parse_color():
    assert parse_color("#7C3AED") == 0x7C3AED and parse_color("default") is None and parse_color("") is None
    for bad in ("purple", "#1234567"):
        with pytest.raises(TicketError):
            parse_color(bad)


def test_fmt_duration_and_stars():
    assert fmt_duration(None) == "—" and fmt_duration(45) == "45s" and fmt_duration(125) == "2m"
    assert fmt_duration(3 * 3600 + 5 * 60) == "3h 5m" and fmt_duration(2 * 86400 + 3600) == "2d 1h"
    assert star_line(3) == "⭐⭐⭐" and star_line(None) == "" and star_line(9) == "⭐" * 5


def ticket(**kw):
    base = {"id": 7, "type_name": "Report a player", "owner_id": 5, "claimed_by": None, "priority": "normal",
            "answers": [{"q": "Who?", "a": "Bob"}], "created_at": T0, "first_staff_reply_at": T0 + timedelta(minutes=4)}
    return {**base, **kw}


def test_embeds():
    types = [{"id": 1, "name": "Support", "emoji": "🛠️", "description": "Help"}]
    e = panel_embed(FakeDB(), types, "attachment://b.png")
    assert "Support" in e.fields[0].value and e.image.url == "attachment://b.png"
    assert "closed right now" in panel_embed(FakeDB(), [], None).fields[0].value
    te = ticket_embed(ticket(priority="urgent", claimed_by=9), "<@5>")
    assert te.title == "🎫 Ticket #0007 · Report a player"
    assert [f.name for f in te.fields] == ["Who?", "Handled by", "Priority"] and "Urgent" in te.fields[2].value
    le = log_embed(ticket(), closed_by=None, reason=None, messages=1, notes=2, now=T0 + timedelta(hours=2))
    assert "auto-close" in le.fields[2].value and "4m" in le.fields[4].value and "2 staff notes" in le.footer.text


def test_stats_embed():
    totals = {"opened": 3, "closed": 2, "still_open": 1, "avg_response": 600, "avg_rating": 4.5, "ratings": 2}
    e = stats_embed(totals, [{"uid": 9, "handled": 2, "closed": 1, "avg_response": 300, "avg_rating": 5, "ratings": 1}],
                    30, lambda uid: f"user{uid}")
    assert "10m" in e.description and "4.5/5" in e.description and "user9" in e.fields[0].value


def test_transcript_escapes_everything():
    msgs = [{"author": "<script>alert(1)</script>", "avatar": "https://x/a.png\" onerror=\"x", "bot": False,
             "created_at": T0, "content": "<b>hi</b> & bye", "attachments": [("a<.png", "https://cdn/a.png")],
             "embeds": [("T<i>", "D")]}]
    page = render_transcript(ticket(answers=[{"q": "<q>", "a": "<a>"}]), msgs, guild_name="Orion<>",
                             owner_name="o&o", closed_by="staff", reason="<done>", now=T0)
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "&lt;b&gt;hi&lt;/b&gt; &amp; bye" in page and "a&lt;.png" in page and "&lt;done&gt;" in page
    assert 'onerror="x' not in page                       # the quote in the avatar URL is escaped too
    assert "Ticket #0007" in page and "1 message." in page


def test_text_transcript_is_plain_and_readable():
    msgs = [{"author": "Bob", "bot": False, "created_at": T0, "content": "hello\nneed help",
             "attachments": [("a.png", "https://cdn/a.png")], "embeds": []},
            {"author": "Orion", "bot": True, "created_at": T0, "content": "", "attachments": [],
             "embeds": [("Closed", "bye")]}]
    text = render_transcript_text(ticket(answers=[{"q": "Who?", "a": "Bob"}]), msgs, guild_name="Orion",
                                  owner_name="Bob", closed_by="staff", reason=None, now=T0)
    assert "<" not in text.split("CONVERSATION")[1].replace("<t:", "")    # no HTML in it
    assert "Bob: hello" in text and "[file] a.png" in text and "Orion [BOT]:" in text and "[embed] Closed - bye" in text
    assert "Reason:     none given" in text and "Who?" in text and "2 messages." in text


# --------------------------------------------------------------------- SQL
def run(coro_fn):
    async def go():
        db = Database(DSN)
        await db.connect()
        await db.pool.execute("TRUNCATE tickets, ticket_types, ticket_notes, ticket_blacklist RESTART IDENTITY CASCADE")
        try:
            return await coro_fn(db)
        finally:
            await db.close()
    return asyncio.run(go())


async def new_ticket(db, owner=5, max_open=1, now=T0):
    return await db.ticket_open(owner_id=owner, type_id=None, type_name="Support", answers=[], max_open=max_open, now=now)


@needs_db
def test_types_crud():
    async def t(db):
        a = await db.tt_create(name="Support", questions=["Q?"])
        b = await db.tt_create(name="Report")
        await db.tt_update(b, enabled=False, staff_role_ids=[1, 2])
        assert [r["name"] for r in await db.tt_list()] == ["Support", "Report"]
        assert [r["id"] for r in await db.tt_list(enabled_only=True)] == [a]
        assert (await db.tt_get(b))["staff_role_ids"] == [1, 2]
        with pytest.raises(ValueError):
            await db.tt_update(a, bogus=1)
        await db.tt_delete(a)
        assert await db.tt_count() == 1
    run(t)


@needs_db
def test_open_limit_survives_double_clicks():
    async def t(db):
        ids = await asyncio.gather(*[new_ticket(db, max_open=1) for _ in range(8)])
        assert sum(1 for x in ids if x) == 1
        assert await db.ticket_open_count(5) == 1
        assert await new_ticket(db, owner=6) is not None             # someone else is not affected
    run(t)


@needs_db
def test_close_reopen_delete_cycle():
    async def t(db):
        tid = await new_ticket(db)
        await db.ticket_set_channel(tid, 100)
        assert await db.ticket_close(tid, 9, "done", T0) is True
        assert await db.ticket_close(tid, 9, "again", T0) is False   # closes exactly once
        assert await db.ticket_open_count(5) == 0 and await new_ticket(db) is not None
        assert await db.ticket_reopen(tid, T0) is True and await db.ticket_reopen(tid, T0) is False
        await db.ticket_close(tid, 9, None, T0)
        assert [r["id"] for r in await db.ticket_delete_due(T0 + timedelta(hours=25), 24)] == [tid]
        assert await db.ticket_delete_due(T0 + timedelta(hours=1), 24) == []
        await db.ticket_mark_deleted(tid, T0)
        assert (await db.ticket_get(tid))["status"] == "deleted"
    run(t)


@needs_db
def test_deleted_open_channel_frees_the_slot():
    async def t(db):
        tid = await new_ticket(db)
        await db.ticket_mark_deleted(tid, T0)
        row = await db.ticket_get(tid)
        assert row["status"] == "deleted" and row["close_reason"] == "Channel deleted"
        assert await db.ticket_open_count(5) == 0
    run(t)


@needs_db
def test_claim_and_transfer():
    async def t(db):
        tid = await new_ticket(db)
        assert await db.ticket_claim(tid, 9) is True
        assert await db.ticket_claim(tid, 8) is False                 # someone else holds it
        assert await db.ticket_claim(tid, 8, force=True) is True      # transfer
        assert (await db.ticket_get(tid))["claimed_by"] == 8
    run(t)


@needs_db
def test_auto_close_flow():
    async def t(db):
        tid = await new_ticket(db)
        await db.ticket_set_channel(tid, 100)
        assert await db.ticket_idle(T0 + timedelta(hours=10), 72) == []
        assert [r["id"] for r in await db.ticket_idle(T0 + timedelta(hours=73), 72)] == [tid]
        await db.ticket_set_warned(tid, T0 + timedelta(hours=73))
        assert await db.ticket_idle(T0 + timedelta(hours=80), 72) == []               # warned once only
        assert await db.ticket_warn_expired(T0 + timedelta(hours=80), 24) == []
        await db.ticket_activity(100, T0 + timedelta(hours=81), staff_reply=False)   # the member answered
        assert await db.ticket_warn_expired(T0 + timedelta(hours=200), 24) == []
        await db.ticket_set_warned(tid, T0 + timedelta(hours=160))
        assert [r["id"] for r in await db.ticket_warn_expired(T0 + timedelta(hours=185), 24)] == [tid]
    run(t)


@needs_db
def test_first_staff_reply_and_stats():
    async def t(db):
        tid = await new_ticket(db)
        await db.ticket_set_channel(tid, 100)
        await db.ticket_claim(tid, 9)
        await db.ticket_activity(100, T0 + timedelta(minutes=10), staff_reply=True)
        await db.ticket_activity(100, T0 + timedelta(minutes=30), staff_reply=True)   # only the first one counts
        await db.ticket_close(tid, 9, None, T0 + timedelta(hours=1))
        assert await db.ticket_rate(tid, 6, 5, T0) is False                         # not the owner
        assert await db.ticket_rate(tid, 5, 4, T0) is True
        assert await db.ticket_rate(tid, 5, 1, T0) is False                         # rated once
        assert await db.ticket_rate_comment(tid, 5, "great") is True
        totals, staff = await db.ticket_stats(T0 - timedelta(days=1))
        assert totals["opened"] == 1 and totals["closed"] == 1 and float(totals["avg_rating"]) == 4.0
        assert int(totals["avg_response"]) == 600
        assert [(r["uid"], r["handled"], r["closed"]) for r in staff] == [(9, 1, 1)]
    run(t)


@needs_db
def test_notes_and_blacklist():
    async def t(db):
        tid = await new_ticket(db)
        await db.note_add(tid, 9, "secret")
        assert [r["text"] for r in await db.note_list(tid)] == ["secret"] and await db.note_count(tid) == 1
        await db.bl_add(5, "spam", 9)
        await db.bl_add(5, "spam again", 9)                                         # updates, no error
        assert (await db.bl_get(5))["reason"] == "spam again" and len(await db.bl_list()) == 1
        assert await db.bl_remove(5) is True and await db.bl_remove(5) is False
    run(t)


# ------------------------------------------------------------ custom emoji
class _E:
    def __init__(self, name, id_):
        self.name, self.id = name, id_

    def __str__(self):
        return f"<:{self.name}:{self.id}>"


class _G:
    emojis = [_E("rflogopurple", 123), _E("other", 5)]


def test_resolve_emoji():
    from orion.tickets import resolve_emoji, is_emoji_name
    assert resolve_emoji("rflogopurple", _G()) == "<:rflogopurple:123>"
    assert resolve_emoji(":RFLogoPurple:", _G()) == "<:rflogopurple:123>"      # colons and case don't matter
    assert resolve_emoji("🛠️", _G()) == "🛠️"
    assert resolve_emoji("<:x:9>", _G()) == "<:x:9>"
    assert resolve_emoji("missing", _G()) == "🎫"                              # unknown name never breaks the panel
    assert resolve_emoji("rflogopurple", None) == "🎫"
    assert is_emoji_name("rflogopurple") and not is_emoji_name("🛠️") and not is_emoji_name("<:x:9>")


def test_default_types_are_the_six_blue_heaven_categories():
    assert [t["name"] for t in DEFAULT_TYPES] == [
        "General Support", "Member Reports", "Staff Applications", "Partnerships",
        "Esports and Team Enquiries", "Other"]


# -------------------------------------------------------------- ping roles
def test_ping_roles():
    from orion.tickets import MAX_PING_ROLES, ping_role_ids, server_ping_role_ids
    assert server_ping_role_ids(FakeDB()) == []
    assert server_ping_role_ids(FakeDB(ticket_ping_role_id=7)) == [7]                  # old single setting still works
    assert server_ping_role_ids(FakeDB(ticket_ping_role_ids=[1, 2, 2], ticket_ping_role_id=7)) == [1, 2]
    db = FakeDB(ticket_ping_role_ids=[1, 2])
    assert ping_role_ids(db, {"ping_role_ids": [5, 6, 7]}) == [5, 6, 7]                # the type's own roles win
    assert ping_role_ids(db, {"ping_role_ids": []}) == [1, 2]                          # none of its own -> server-wide
    assert len(ping_role_ids(db, {"ping_role_ids": list(range(1, 40))})) == MAX_PING_ROLES
