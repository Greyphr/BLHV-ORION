"""Events, scrims and tournaments: pure helpers (no Discord connection needed, so they are unit-tested).

  parse_when()   staff type a time like "2026-11-20 19:00" or "tomorrow 20:00" (server time)
  split_signups  first `slots` people are in, everyone after is on the waitlist
  event_embed()  the public sign-up message
"""
import re
from datetime import datetime, timedelta
from typing import Mapping, Optional, Sequence

import discord

from . import config
from .timeutil import TZ, now_local

KINDS = {
    "event":      {"label": "Community event", "plural": "Community events", "emoji": "🎉",
                   "color": config.COLOR_ACCENT, "slots": 0},
    "scrim":      {"label": "Scrim", "plural": "Scrims", "emoji": "⚔️", "color": 0x10B981, "slots": 10},
    "tournament": {"label": "Tournament", "plural": "Tournaments", "emoji": "🏆", "color": 0xF59E0B, "slots": 16},
}

WHEN_HELP = ("Use `YYYY-MM-DD HH:MM`, `DD.MM.YYYY HH:MM`, `DD.MM HH:MM`, `today HH:MM` or `tomorrow HH:MM` "
             f"(24-hour clock, {TZ.key} time). Example: `2026-11-20 19:30`.")
MAX_DAYS_AHEAD = 366


class TimeParseError(ValueError):
    pass


def parse_when(text: str, now: Optional[datetime] = None) -> datetime:
    """Parse a staff-typed date/time in the server time zone into an aware datetime. Must be in the future."""
    now = now or now_local()
    parts = text.strip().lower().replace(",", " ").split()
    if len(parts) != 2:
        raise TimeParseError(f"I couldn't read that time. {WHEN_HELP}")
    date_s, time_s = parts
    t = re.fullmatch(r"(\d{1,2}):(\d{2})", time_s)
    if not t or int(t[1]) > 23 or int(t[2]) > 59:
        raise TimeParseError(f"I couldn't read the time part. {WHEN_HELP}")
    hh, mm = int(t[1]), int(t[2])

    year = None
    if date_s == "today":
        d = now.date()
        year, month, day = d.year, d.month, d.day
    elif date_s == "tomorrow":
        d = (now + timedelta(days=1)).date()
        year, month, day = d.year, d.month, d.day
    elif iso := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_s):
        year, month, day = int(iso[1]), int(iso[2]), int(iso[3])
    elif eu := re.fullmatch(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}))?", date_s):
        day, month = int(eu[1]), int(eu[2])
        year = int(eu[3]) if eu[3] else None
    else:
        raise TimeParseError(f"I couldn't read the date part. {WHEN_HELP}")

    def build(y: int) -> datetime:
        return datetime(y, month, day, hh, mm, tzinfo=TZ)

    try:
        result = build(year if year else now.year)
        if year is None and result <= now:           # "20.11 19:00" typed after that date: next year
            result = build(now.year + 1)
    except ValueError:
        raise TimeParseError("That date doesn't exist on the calendar.") from None
    if result <= now:
        raise TimeParseError("That time is in the past.")
    if result - now > timedelta(days=MAX_DAYS_AHEAD):
        raise TimeParseError("That's more than a year away. Check the year.")
    return result


def split_signups(ids: Sequence[int], slots: int) -> tuple[list[int], list[int]]:
    """slots == 0 means unlimited. Returns (confirmed, waitlist), both in sign-up order."""
    ids = list(ids)
    return (ids[:slots], ids[slots:]) if slots else (ids, [])


def _lines(ids: Sequence[int], limit: int = 1000) -> str:
    """Numbered mentions that always fit one embed field (1024 chars)."""
    out, used = [], 0
    for n, uid in enumerate(ids, 1):
        line = f"`{n}.` <@{uid}>"
        if used + len(line) + 1 > limit - 24:
            out.append(f"…and {len(ids) - n + 1} more")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def event_embed(ev: Mapping, signups: Sequence[int]) -> discord.Embed:
    kind = KINDS[ev["kind"]]
    extra = ev["extra"] or {}
    slots = ev["slots"]
    confirmed, waitlist = split_signups(signups, slots)
    ts = int(ev["starts_at"].timestamp())

    e = discord.Embed(title=f"{kind['emoji']} {ev['title']}", description=ev["description"] or None,
                      color=kind["color"])
    e.add_field(name="🗓️ When", value=f"<t:{ts}:F>\n<t:{ts}:R>", inline=True)
    if extra.get("game"):
        e.add_field(name="🎮 Game", value=extra["game"], inline=True)
    if extra.get("format"):
        e.add_field(name="📐 Format", value=extra["format"], inline=True)
    if extra.get("rank"):
        e.add_field(name="📊 Level / rank", value=extra["rank"], inline=True)
    if extra.get("prize"):
        e.add_field(name="💰 Prize", value=extra["prize"], inline=True)
    if ev["reg_closes_at"]:
        e.add_field(name="🔒 Registration closes", value=f"<t:{int(ev['reg_closes_at'].timestamp())}:R>",
                    inline=True)
    e.add_field(name="🧑‍💼 Host", value=f"<@{ev['created_by']}>", inline=True)

    if slots:
        full = " · **FULL**" if len(confirmed) >= slots else ""
        e.add_field(name="👥 Slots", value=f"**{len(confirmed)}/{slots}**{full}", inline=True)
    else:
        e.add_field(name="👥 Joined", value=f"**{len(confirmed)}**", inline=True)

    e.add_field(name=f"✅ Participants ({len(confirmed)})", value=_lines(confirmed) or "*Nobody yet. Be the first!*",
                inline=False)
    if waitlist:
        e.add_field(name=f"⏳ Waitlist ({len(waitlist)})", value=_lines(waitlist), inline=False)
    e.set_footer(text=f"{kind['label']} #{ev['id']} · {config.BRAND}")
    return e
