import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from datetime import datetime, timedelta

import pytest

from orion.events import KINDS, TimeParseError, event_embed, parse_when, split_signups
from orion.timeutil import TZ

NOW = datetime(2026, 11, 6, 8, 0, tzinfo=TZ)


def test_parse_formats():
    want = datetime(2026, 11, 20, 19, 30, tzinfo=TZ)
    for text in ("2026-11-20 19:30", "20.11.2026 19:30", "20/11/2026 19:30", "20-11-2026 19:30",
                 "20.11 19:30", " 2026-11-20   19:30 "):
        assert parse_when(text, NOW) == want, text
    assert parse_when("today 20:00", NOW) == datetime(2026, 11, 6, 20, 0, tzinfo=TZ)
    assert parse_when("Tomorrow 9:05", NOW) == datetime(2026, 11, 7, 9, 5, tzinfo=TZ)


def test_parse_year_rolls_over_when_omitted():
    assert parse_when("01.02 18:00", NOW) == datetime(2027, 2, 1, 18, 0, tzinfo=TZ)


@pytest.mark.parametrize("bad", ["", "tomorrow", "20.11.2026", "2026-13-01 10:00", "2026-02-30 10:00",
                                 "20.11 25:00", "20.11 19:60", "next friday 19:00", "2026-11-20 7pm"])
def test_parse_rejects_garbage(bad):
    with pytest.raises(TimeParseError):
        parse_when(bad, NOW)


def test_parse_rejects_past_and_far_future():
    with pytest.raises(TimeParseError, match="past"):
        parse_when("2026-11-06 07:00", NOW)
    with pytest.raises(TimeParseError, match="past"):
        parse_when("today 08:00", NOW)
    with pytest.raises(TimeParseError, match="year"):
        parse_when("2030-01-01 10:00", NOW)


def test_split_signups():
    assert split_signups([1, 2, 3, 4], 3) == ([1, 2, 3], [4])
    assert split_signups([1, 2], 3) == ([1, 2], [])
    assert split_signups([1, 2, 3], 0) == ([1, 2, 3], [])      # 0 = unlimited


def _ev(**kw):
    base = dict(id=7, kind="scrim", title="Valorant Scrim", description="Bring your own comms.",
                starts_at=NOW + timedelta(days=2), reg_closes_at=None, slots=2, created_by=42,
                extra={"game": "Valorant", "rank": "Gold-Plat"})
    base.update(kw)
    return base


def _fields(e):
    return {f.name: f.value for f in e.fields}


def test_embed_slots_waitlist_and_full():
    e = event_embed(_ev(), [10, 11, 12])
    f = _fields(e)
    assert f["👥 Slots"] == "**2/2** · **FULL**"
    assert "<@10>" in f["✅ Participants (2)"] and "<@12>" not in f["✅ Participants (2)"]
    assert "<@12>" in f["⏳ Waitlist (1)"]
    assert f["🎮 Game"] == "Valorant" and f["📊 Level / rank"] == "Gold-Plat"
    assert e.footer.text.startswith("Scrim #7")


def test_embed_unlimited_and_empty():
    f = _fields(event_embed(_ev(kind="event", slots=0, extra={}), []))
    assert f["👥 Joined"] == "**0**" and "Nobody yet" in f["✅ Participants (0)"]
    assert not any(k.startswith("⏳") for k in f)


def test_embed_tournament_fields_and_registration_deadline():
    ev = _ev(kind="tournament", slots=16, reg_closes_at=NOW + timedelta(days=1),
             extra={"game": "Rocket League", "format": "Swiss", "prize": "€50"})
    f = _fields(event_embed(ev, []))
    assert f["📐 Format"] == "Swiss" and f["💰 Prize"] == "€50" and "🔒 Registration closes" in f


def test_embed_stays_inside_discord_limits_with_huge_lists():
    e = event_embed(_ev(slots=0, description="d" * 1500), list(range(10**17, 10**17 + 500)))
    assert all(len(f.value) <= 1024 for f in e.fields)
    assert len(e) <= 6000
    assert "more" in _fields(e)["✅ Participants (500)"]


def test_kind_defaults():
    assert KINDS["event"]["slots"] == 0 and KINDS["scrim"]["slots"] == 10 and KINDS["tournament"]["slots"] == 16
