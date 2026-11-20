"""Ticket helpers that need no database or Discord connection: names, settings, embeds and the HTML transcript."""
import html
import re
from datetime import datetime
from typing import Iterable, Mapping, Optional, Sequence

import discord

from . import config

MAX_TYPES = 10                 # the panel dropdown and the /admin type picker stay readable
MAX_PING_ROLES = 10           # roles pinged when a ticket opens (per type, and server-wide)
MAX_QUESTIONS = 5              # a Discord form holds at most 5 inputs
MAX_QUESTION_LEN = 45          # a form label holds at most 45 characters
MAX_TRANSCRIPT_MESSAGES = 5000

CATEGORY_NAME = "🎫・Tickets"
PANEL_CHANNEL_NAME = "📩・open-a-ticket"
LOG_CHANNEL_NAME = "📜・ticket-logs"

PRIORITIES = {"low": "🟢 Low", "normal": "🔵 Normal", "high": "🟠 High", "urgent": "🔴 Urgent"}
STARS = "⭐"

# key: (label, default, min, max). Stored in the config table as-is.
SETTINGS = {
    "ticket_max_open": ("Open tickets allowed per member", 1, 1, 10),
    "ticket_autoclose_hours": ("Hours of silence before the warning (0 = off)", 72, 0, 720),
    "ticket_warn_hours": ("Hours after the warning before it closes", 24, 1, 168),
    "ticket_delete_hours": ("Hours a closed ticket can be reopened", 24, 0, 720),
}

DEFAULT_PANEL_TITLE = "🎫 Need help? Open a ticket"
DEFAULT_PANEL_TEXT = ("Pick what your ticket is about below. A private channel opens that only you and the staff "
                      "team can see.\n\nPlease open **one** ticket per issue and give as much detail as you can.")
DEFAULT_BUTTON = "Open a ticket"

from .ticket_ai import AI_HELPER_KEY          # re-exported: the global AI switch (off by default)
_EMOJI_NAME = re.compile(r"^:?([A-Za-z0-9_]{2,32}):?$")

DEFAULT_TYPES = [
    {"name": "General Support", "emoji": "🛠️", "description": "Questions or problems with the server",
     "questions": ["What do you need help with?"]},
    {"name": "Member Reports", "emoji": "🚨", "description": "Report someone breaking the rules",
     "questions": ["Who are you reporting? (name or ID)", "What happened?", "Proof (links to screenshots/clips)"]},
    {"name": "Staff Applications", "emoji": "📜", "description": "Apply to join the Sect staff",
     "questions": ["Why do you want to join the staff?", "Any past moderation experience?",
                   "How many hours a week can you give?"]},
    {"name": "Partnerships", "emoji": "🤝", "description": "Partner, sponsor or collaborate with Blue Heaven",
     "questions": ["Who do you represent?", "What do you have in mind?"]},
    {"name": "Esports and Team Enquiries", "emoji": "🏆", "description": "Teams, tryouts, tournaments and esports",
     "questions": ["What is your enquiry about?", "Which game or team does it concern?"]},
    {"name": "Other", "emoji": "❓", "description": "Anything that doesn't fit above",
     "questions": ["How can we help?"]},
]


class TicketError(ValueError):
    """A problem phrased for the person who caused it."""


# ------------------------------------------------------------------ names
def slug(name: str, fallback: str = "member") -> str:
    """'Ꮇr. Cool_Guy 99!' -> 'r-cool-guy-99'. Discord channel names: lower case letters, digits and dashes."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:40].strip("-") or fallback


def channel_name(number: int, username: str) -> str:
    return f"ticket-{number:04d}-{slug(username)}"[:100]


def resolve_emoji(raw: str, guild=None, fallback: str = "🎫") -> str:
    """What to show for a ticket type's emoji.

    A real emoji (🛠️) or a pasted custom emoji (<:rflogopurple:123>) is used as it is. A plain name, with or without
    colons ('rflogopurple' or ':rflogopurple:'), is looked up among the server's custom emojis, so staff never need the
    long <:name:id> form. A name the server doesn't have falls back to `fallback` instead of breaking the panel."""
    raw = (raw or "").strip()
    if not raw:
        return fallback
    m = _EMOJI_NAME.match(raw)
    if not m:
        return raw
    if guild is not None:
        want = m.group(1).lower()
        for e in guild.emojis:
            if e.name.lower() == want:
                return str(e)
    return fallback


def is_emoji_name(raw: str) -> bool:
    return bool(_EMOJI_NAME.match((raw or "").strip()))


def parse_questions(text: str) -> list[str]:
    """One question per line. Raises TicketError when there are too many or one is too long."""
    out = [q.strip() for q in (text or "").splitlines() if q.strip()]
    if len(out) > MAX_QUESTIONS:
        raise TicketError(f"A ticket form can ask at most {MAX_QUESTIONS} questions (you wrote {len(out)}).")
    for q in out:
        if len(q) > MAX_QUESTION_LEN:
            raise TicketError(f"Each question can be at most {MAX_QUESTION_LEN} characters (“{q[:25]}…” is {len(q)}).")
    return out


def setting(db, key: str) -> int:
    label, default, lo, hi = SETTINGS[key]
    v = db.get(key, default)
    return v if isinstance(v, int) and lo <= v <= hi else default


def _role_ids(raw) -> list[int]:
    out = []
    for r in raw or []:
        if isinstance(r, int) and not isinstance(r, bool) and r > 0 and r not in out:
            out.append(r)
    return out[:MAX_PING_ROLES]


def server_ping_role_ids(db) -> list[int]:
    """Roles pinged by every ticket type that has none of its own. Reads the old single-role setting too."""
    ids = _role_ids(db.get("ticket_ping_role_ids"))
    if not ids and isinstance(db.get("ticket_ping_role_id"), int):
        ids = _role_ids([db.get("ticket_ping_role_id")])
    return ids


def ping_role_ids(db, ttype: Optional[Mapping]) -> list[int]:
    """Roles to ping for a new ticket of `ttype`: the type's own roles, else the server-wide ones."""
    return _role_ids((ttype or {}).get("ping_role_ids")) or server_ping_role_ids(db)


def parse_setting(key: str, raw: str) -> int:
    label, _, lo, hi = SETTINGS[key]
    try:
        v = int((raw or "").replace(",", "").strip())
    except ValueError:
        raise TicketError(f"**{label}** must be a whole number from {lo} to {hi}.")
    if not lo <= v <= hi:
        raise TicketError(f"**{label}** must be from {lo} to {hi}.")
    return v


def parse_color(raw: str) -> Optional[int]:
    """'#7C3AED' -> int, '' / 'default' -> None. Raises TicketError for anything else."""
    raw = (raw or "").strip()
    if raw.lower() in ("", "default", "reset"):
        return None
    try:
        v = int(raw.lstrip("#"), 16)
    except ValueError:
        raise TicketError("That colour isn't valid hex (example: #7C3AED).")
    if not 0 <= v <= 0xFFFFFF:
        raise TicketError("That colour isn't valid hex (example: #7C3AED).")
    return v


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    s = int(max(0, seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m" if m else f"{s}s"


def star_line(n: Optional[int]) -> str:
    return STARS * max(0, min(5, n or 0))


def stars(n: Optional[float]) -> str:
    if n is None:
        return "no ratings"
    return f"{STARS} {float(n):.1f}/5"


# ----------------------------------------------------------------- embeds
def panel_color(db) -> int:
    c = db.get("ticket_color")
    return c if isinstance(c, int) else config.COLOR_MAIN


def panel_embed(db, types: Sequence[Mapping], banner_url: Optional[str] = None) -> discord.Embed:
    e = discord.Embed(color=panel_color(db), title=(db.get("ticket_panel_title") or DEFAULT_PANEL_TITLE)[:256],
                      description=(db.get("ticket_panel_text") or DEFAULT_PANEL_TEXT)[:3500])
    lines = [f"{t['emoji']} **{t['name']}**" + (f" · {t['description']}" if t["description"] else "") for t in types]
    if lines:
        e.add_field(name="Ticket types", value="\n".join(lines)[:1024], inline=False)
    else:
        e.add_field(name="​", value="*Tickets are closed right now.*", inline=False)
    if banner_url:
        e.set_image(url=banner_url)
    e.set_footer(text=config.BRAND)
    return e


def ticket_embed(ticket: Mapping, owner_mention: str, *, banner_url: Optional[str] = None,
                 color: int = config.COLOR_MAIN) -> discord.Embed:
    """The message at the top of every ticket channel. Redrawn when it is claimed or its priority changes."""
    e = discord.Embed(color=color, title=f"🎫 Ticket #{ticket['id']:04d} · {ticket['type_name']}"[:256],
                      description=f"Hi {owner_mention}, thanks for reaching out! Staff will be with you soon.\n"
                                  "Tell us everything we need in the meantime. Press **Close** when you're done.")
    for qa in ticket.get("answers") or []:
        e.add_field(name=str(qa.get("q", "?"))[:256], value=(str(qa.get("a", "")).strip() or "*no answer*")[:1024],
                    inline=False)
    claimed = f"<@{ticket['claimed_by']}>" if ticket.get("claimed_by") else "*nobody yet*"
    e.add_field(name="Handled by", value=claimed, inline=True)
    e.add_field(name="Priority", value=PRIORITIES.get(ticket.get("priority") or "normal", "🔵 Normal"), inline=True)
    if banner_url:
        e.set_image(url=banner_url)
    e.set_footer(text=f"{config.BRAND} · Ticket #{ticket['id']}")
    return e


def log_embed(ticket: Mapping, *, closed_by: Optional[int], reason: Optional[str], messages: int, notes: int,
              now: datetime) -> discord.Embed:
    e = discord.Embed(color=config.COLOR_MAIN, title=f"🔒 Ticket #{ticket['id']:04d} closed · {ticket['type_name']}"[:256],
                      timestamp=now)
    e.add_field(name="Opened by", value=f"<@{ticket['owner_id']}>", inline=True)
    e.add_field(name="Handled by", value=f"<@{ticket['claimed_by']}>" if ticket.get("claimed_by") else "*unclaimed*",
                inline=True)
    e.add_field(name="Closed by", value=f"<@{closed_by}>" if closed_by else "auto-close", inline=True)
    e.add_field(name="Open for", value=fmt_duration((now - ticket["created_at"]).total_seconds()), inline=True)
    first = ticket.get("first_staff_reply_at")
    e.add_field(name="First staff reply",
                value=fmt_duration((first - ticket["created_at"]).total_seconds()) if first else "never", inline=True)
    e.add_field(name="Priority", value=PRIORITIES.get(ticket.get("priority") or "normal", "🔵 Normal"), inline=True)
    e.add_field(name="Reason", value=(reason or "*none given*")[:1024], inline=False)
    e.set_footer(text=f"{messages} message{'s' if messages != 1 else ''} in the transcript · "
                      f"{notes} staff note{'s' if notes != 1 else ''} (not in the transcript)")
    return e


def stats_embed(totals: Mapping, staff: Iterable[Mapping], days: int, name_of) -> discord.Embed:
    e = discord.Embed(color=config.COLOR_MAIN, title=f"📈 Ticket stats · last {days} day{'s' if days != 1 else ''}")
    e.description = (f"**{totals['opened'] or 0}** opened · **{totals['closed'] or 0}** closed · "
                     f"**{totals['still_open'] or 0}** still open\n"
                     f"Average first staff reply: **{fmt_duration(totals['avg_response'])}**\n"
                     f"Average rating: **{stars(totals['avg_rating'])}** ({totals['ratings'] or 0} rating"
                     f"{'s' if totals['ratings'] != 1 else ''})")
    lines = [f"**{name_of(r['uid'])}** · handled {r['handled']} · closed {r['closed']} · "
             f"reply {fmt_duration(r['avg_response'])} · {stars(r['avg_rating'])}" for r in staff]
    e.add_field(name="Staff", value="\n".join(lines)[:1024] if lines else "*No staff activity yet.*", inline=False)
    return e


# ------------------------------------------------------------- transcript
_CSS = """
body{background:#1e1f22;color:#dbdee1;font:16px/1.5 'gg sans','Segoe UI',Arial,sans-serif;margin:0}
header{background:#4C1D95;color:#fff;padding:18px 24px}
header h1{margin:0 0 4px;font-size:20px}header p{margin:2px 0;opacity:.85;font-size:13px}
main{padding:12px 24px}.qa{background:#2b2d31;border-left:4px solid #7C3AED;padding:10px 14px;margin:10px 0;border-radius:4px}
.qa b{display:block;color:#fff}.m{display:flex;gap:12px;padding:8px 0;border-bottom:1px solid #2b2d31}
.m img.av{width:40px;height:40px;border-radius:50%;flex:none}.who{font-weight:600;color:#fff}
.bot{background:#5865f2;color:#fff;font-size:10px;padding:1px 4px;border-radius:3px;margin-left:4px}
.t{color:#949ba4;font-size:12px;margin-left:6px}.c{white-space:pre-wrap;word-wrap:break-word}
.em{border-left:4px solid #7C3AED;background:#2b2d31;padding:8px 12px;margin-top:6px;border-radius:4px}
.att a{color:#00a8fc}.note{color:#949ba4;font-size:12px;padding:16px 0}
.how{background:#2b2d31;border-radius:6px;padding:10px 14px;margin:12px 0;color:#b5bac1;font-size:13px}
"""


def render_transcript_text(ticket: Mapping, messages: Sequence[Mapping], *, guild_name: str, owner_name: str,
                           closed_by: str, reason: Optional[str], now: datetime) -> str:
    """The same conversation as plain text: reads fine in Discord's own file preview, on a phone and in Notepad.

    Looks like:
        [14:05] Mugiwara: hello, I need help
        [14:07] Staff Member: sure, what's the problem?
    with a date heading whenever the day changes."""
    fmt = "%Y-%m-%d %H:%M UTC"
    out = [f"TICKET #{ticket['id']:04d} - {ticket['type_name']}", "=" * 60,
           f"Server:     {guild_name}", f"Opened by:  {owner_name}", f"Opened:     {ticket['created_at'].strftime(fmt)}",
           f"Closed by:  {closed_by}", f"Closed:     {now.strftime(fmt)}", f"Reason:     {reason or 'none given'}"]
    answers = ticket.get("answers") or []
    if answers:
        out += ["", "FORM ANSWERS", "-" * 60]
        for x in answers:
            out.append(f"{x.get('q', '')}")
            out.append(f"   {x.get('a', '')}")
    out += ["", "CONVERSATION", "-" * 60]
    day = None
    for m in messages:
        d = m["created_at"].strftime("%A %d %B %Y")
        if d != day:
            day = d
            out += ["", f"--- {d} ---"]
        who = (m.get("author") or "?") + (" [BOT]" if m.get("bot") else "")
        lines = (m.get("content") or "").splitlines() or []
        head = f"[{m['created_at'].strftime('%H:%M')}] {who}: "
        out.append(head + (lines[0] if lines else ""))
        pad = " " * 4
        out += [pad + ln for ln in lines[1:]]
        for title, desc in m.get("embeds") or []:
            out.append(pad + "[embed] " + " - ".join(x for x in (title, desc) if x).replace("\n", " "))
        for name, url in m.get("attachments") or []:
            out.append(pad + f"[file] {name}  {url}")
        if not lines and not m.get("embeds") and not m.get("attachments"):
            out[-1] += "(no text)"
    out += ["", "-" * 60,
            f"{len(messages)} message{'s' if len(messages) != 1 else ''}. File links come from Discord and stop working "
            "after a while, so save anything you need."]
    return "\n".join(out) + "\n"


def render_transcript(ticket: Mapping, messages: Sequence[Mapping], *, guild_name: str, owner_name: str,
                      closed_by: str, reason: Optional[str], now: datetime) -> str:
    """A standalone HTML page. Every piece of user text is escaped: staff open this file in their browser.

    `messages`: oldest first, each {"author", "avatar", "bot", "created_at", "content",
    "attachments": [(filename, url)], "embeds": [(title, description)]}."""
    esc = html.escape
    rows = []
    for m in messages:
        parts = [f'<div class="c">{esc(m.get("content") or "")}</div>'] if m.get("content") else []
        for title, desc in m.get("embeds") or []:
            parts.append(f'<div class="em"><b>{esc(title or "")}</b><div class="c">{esc(desc or "")}</div></div>')
        for name, url in m.get("attachments") or []:
            parts.append(f'<div class="att">📎 <a href="{esc(url, quote=True)}">{esc(name)}</a></div>')
        av = m.get("avatar") or ""
        img = f'<img class="av" src="{esc(av, quote=True)}" alt="">' if av else '<div class="av"></div>'
        when = m["created_at"].strftime("%Y-%m-%d %H:%M UTC")
        rows.append(f'<div class="m">{img}<div><span class="who">{esc(m.get("author") or "?")}</span>'
                    f'{"<span class=bot>BOT</span>" if m.get("bot") else ""}<span class="t">{when}</span>'
                    f'{"".join(parts) or "<div class=c><i>(no text)</i></div>"}</div></div>')
    qa = "".join(f'<div class="qa"><b>{esc(str(x.get("q", "")))}</b>{esc(str(x.get("a", "")))}</div>'
                 for x in ticket.get("answers") or [])
    opened = ticket["created_at"].strftime("%Y-%m-%d %H:%M UTC")
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Ticket #{ticket['id']:04d} transcript</title><style>{_CSS}</style></head><body>"
        f"<header><h1>Ticket #{ticket['id']:04d} · {esc(ticket['type_name'])}</h1>"
        f"<p>{esc(guild_name)} · opened by {esc(owner_name)} on {opened}</p>"
        f"<p>Closed by {esc(closed_by)} on {now.strftime('%Y-%m-%d %H:%M UTC')} · "
        f"reason: {esc(reason or 'none given')}</p></header><main>"
        "<div class=how>Open this file in your web browser (Chrome, Edge, Safari...) to read it as a chat. "
        "A plain-text copy (.txt) is attached too.</div>"
        f"{qa}{''.join(rows)}"
        f"<p class=note>{len(messages)} message{'s' if len(messages) != 1 else ''}. Attachment links come from Discord "
        "and stop working after a while, so save anything you need.</p></main></body></html>")
