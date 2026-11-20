"""The giveaway "card": the Rivals-style post (banner on top, your own text, divider, live stats, bottom banner,
buttons inside the card).

It is built in two steps so the logic can be unit-tested without Discord:
  1. build_spec / ended_spec / results_spec  -> CardSpec   (plain data: text blocks, image urls, buttons)
  2. CardView(spec, handlers)                -> the real discord.ui.LayoutView ("Components V2")

Notes on Discord's card messages: they can't carry `content` or `embeds`, and ALL text in one message together may
not exceed 4000 characters. fit() guarantees a live message is never too long, however much text an admin writes.
"""
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

import discord

from . import config
from .giveaways import COLOR_CANCELLED, COLOR_ENDED, _cap, format_duration, reward_lines, winner_lines

TEXT_LIMIT = 4000                 # Discord's limit for the text of one card message
SAFE_LIMIT = 3900                 # we stay under it
BODY_MAX = 3000                   # what an admin may type in the big text box

BUTTON_IDS = {"enter": "orion:gw:enter", "me": "orion:gw:me", "claim": "orion:gw:claim"}
DEFAULT_BUTTONS = {"enter": ("Enter / Check in", "🎟️"), "me": ("My entries", "📊"), "claim": ("CLAIM REWARD", "🎁")}

DEFAULT_BODY = """\
## 🎁 {title}
Stay active, join the giveaway and keep checking in for more entries. The more entries you have, the better your odds!

## 📅 Giveaway Dates
{starts_date} – {ends_date}

## 🎟️ How to Enter
- Press **Enter / Check in** below to join
- Come back every **{interval}** to earn **+1 entry**
- Missed a check-in? Your entries stay, nothing is lost

## 🏆 Rewards
{rewards}

## ✅ Who Can Enter
Members with any of these roles: {roles}

-# Winners are drawn automatically when the timer ends."""

PLACEHOLDERS = [
    ("{title}", "the giveaway title"),
    ("{prize}", "the prize pool text"),
    ("{rewards}", "the list of reward slots (1st, 2nd, …) with name and value"),
    ("{winners}", "number of winners"),
    ("{ends}", "countdown, e.g. “in 2 days”"),
    ("{ends_date}", "end date and time, shown in each member's own timezone"),
    ("{starts_date}", "start date and time"),
    ("{interval}", "check-in interval, e.g. “6 hours”"),
    ("{participants}", "how many people joined (live)"),
    ("{entries}", "total entries (live)"),
    ("{roles}", "the roles that may enter"),
    ("{id}", "the giveaway number"),
]


# --------------------------------------------------------------------- text
def make_context(gw: Mapping, rewards: Sequence[Mapping], *, participants: int, entries: int,
                 eligible_text: str) -> dict[str, str]:
    ends, start = int(gw["ends_at"].timestamp()), gw.get("starts_at") or gw["ends_at"]
    return {"title": gw["title"], "prize": gw["prize_pool"] or "", "winners": str(gw["winners_count"]),
            "rewards": "\n".join(reward_lines(rewards)) or "*(rewards will appear here)*",
            "ends": f"<t:{ends}:R>", "ends_date": f"<t:{ends}:f>", "starts_date": f"<t:{int(start.timestamp())}:f>",
            "interval": format_duration(gw["interval_seconds"]), "participants": f"{participants:,}",
            "entries": f"{entries:,}", "roles": eligible_text or "*nobody*", "id": str(gw.get("id") or "—")}


def render_text(template: str, ctx: Mapping[str, str]) -> str:
    """Fill {placeholders}. Unknown {things} stay exactly as typed; values are never re-scanned."""
    return re.sub(r"\{(\w+)\}", lambda m: ctx.get(m[1], m[0]), template)


def resolve_body(gw: Mapping) -> str:
    """The admin's text, with fallbacks for giveaways made before the text box existed."""
    if gw.get("body"):
        return gw["body"]
    legacy = "\n\n".join(x for x in (gw.get("description"), gw.get("extra_message")) if x)
    return f"## 🎁 {{title}}\n{legacy}" if legacy else DEFAULT_BODY


# ------------------------------------------------------------------ buttons
_CUSTOM_EMOJI = re.compile(r"^<a?:\w+:\d+>$")
_UNICODE_EMOJI = re.compile(
    "^(?:[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2B55\u2B1B\u2B1C\u23E9-\u23FA\u2194-\u21AA\u231A\u231B\u3030"
    "\u303D\u3297\u3299\u00A9\u00AE\u203C\u2049\u2122\u2139\u24C2\u25AA-\u25FE\U0001F1E6-\U0001F1FF]"
    "[\uFE0F\U0001F3FB-\U0001F3FF]*\u200D?|[0-9#*]\uFE0F?\u20E3)+$")


def parse_button(raw: Optional[str], default: tuple[str, str]) -> tuple[Optional[str], Optional[str]]:
    """'🔥 Join now' -> ('Join now', '🔥').  Blank -> the default.  An emoji alone -> an emoji-only button."""
    raw = (raw or "").strip()
    if not raw:
        return default
    head, _, rest = raw.partition(" ")
    if _CUSTOM_EMOJI.match(head) or _UNICODE_EMOJI.match(head):
        return (rest.strip()[:80] or None), head
    return raw[:80], None


# -------------------------------------------------------------------- specs
@dataclass
class ButtonSpec:
    kind: str                         # enter | me | claim
    label: Optional[str]
    emoji: Optional[str]
    style: str = "secondary"          # success | secondary | primary
    disabled: bool = False


@dataclass
class CardSpec:
    accent: int = config.COLOR_ACCENT
    body: str = ""
    extra: str = ""                   # shown under a divider (ended notice, winners)
    footer: str = ""
    ping: str = ""                    # text above the card (results: the winners' @mentions)
    top: Optional[str] = None         # image urls (attachment://… or https://…)
    bottom: Optional[str] = None
    thumb: Optional[str] = None
    buttons: list = field(default_factory=list)

    def text_len(self) -> int:
        return len(self.body) + len(self.extra) + len(self.footer) + len(self.ping)


def _button(gw: Mapping, kind: str, style: str) -> ButtonSpec:
    label, emoji = parse_button((gw.get("buttons") or {}).get(kind), DEFAULT_BUTTONS[kind])
    return ButtonSpec(kind, label, emoji, style)


def fit(spec: CardSpec) -> CardSpec:
    """Last line of defence: never exceed Discord's text limit on a live message."""
    over = spec.text_len() - SAFE_LIMIT
    if over > 0 and spec.body:
        spec.body = spec.body[:max(200, len(spec.body) - over - 1)].rstrip() + "…"
    return spec


def _accent(gw: Mapping, default: int) -> int:
    return gw["accent"] if gw.get("accent") is not None else default


def _footer(gw: Mapping, participants: int, entries: int) -> str:
    ident = f"Giveaway #{gw['id']}" if gw.get("id") else "Preview"
    return f"-# 👥 {participants:,} participants · 🎟️ {entries:,} entries · {ident} · {config.BRAND}"


def live_spec(gw, rewards, *, participants, entries, eligible_text, urls: Mapping) -> CardSpec:
    ctx = make_context(gw, rewards, participants=participants, entries=entries, eligible_text=eligible_text)
    return fit(CardSpec(accent=_accent(gw, config.COLOR_ACCENT), body=render_text(resolve_body(gw), ctx),
                        footer=_footer(gw, participants, entries), top=urls.get("top"), bottom=urls.get("bottom"),
                        thumb=urls.get("side"),
                        buttons=[_button(gw, "enter", "success"), _button(gw, "me", "secondary")]))


def ended_spec(gw, rewards, *, participants, entries, eligible_text, urls: Mapping) -> CardSpec:
    """The same card after the draw: your text stays, a big ENDED section with the winners is added."""
    cancelled = gw["status"] == "cancelled"
    won = any(r["winner_id"] for r in rewards)
    if cancelled:
        extra = "## 🛑 THIS GIVEAWAY WAS CANCELLED\nNo winners were drawn."
    else:
        extra = ("## 🛑 THIS GIVEAWAY HAS ENDED\nEntries are closed. Thank you for taking part!\n\n"
                 f"## 🏆 Winners\n{_cap(winner_lines(rewards), 1800) or '*No winners*'}")
        if won:
            extra += "\n\nWinners: press **CLAIM REWARD** below to get your prize."
    footer = _footer(gw, participants, entries)
    ctx = make_context(gw, rewards, participants=participants, entries=entries, eligible_text=eligible_text)
    body = render_text(resolve_body(gw), ctx)
    if len(body) + len(extra) + len(footer) > SAFE_LIMIT:         # long text + many winners: keep just the heading
        body = f"## 🎁 {gw['title']}"
    buttons = []
    if won and not cancelled:
        buttons = [_button(gw, "claim", "success"), _button(gw, "me", "secondary")]
    return fit(CardSpec(accent=COLOR_CANCELLED if cancelled else _accent(gw, COLOR_ENDED), body=body, extra=extra,
                        footer=footer, top=urls.get("top"), bottom=urls.get("bottom"), thumb=urls.get("side"),
                        buttons=buttons))


def results_spec(gw, rewards, *, urls: Mapping, ping: str = "") -> CardSpec:
    """The public winner announcement (the winners are @mentioned in `ping`, above the card)."""
    winners = _cap(winner_lines(rewards), 2600)
    won = any(r["winner_id"] for r in rewards)
    body = (f"## 🎉 GIVEAWAY RESULTS — {gw['title']}\n"
            "The giveaway has officially ended! Congratulations to our winners!\n\n"
            f"{winners}\n\n"
            "Congratulations to everyone who won, and thank you to everyone who participated!")
    if won:
        body += "\n\nWinners can click the **CLAIM REWARD** button below to receive their prizes."
    return fit(CardSpec(accent=_accent(gw, config.COLOR_ACCENT), body=body, ping=ping, top=urls.get("top"),
                        footer=f"-# Giveaway #{gw['id']} · {config.BRAND}" if gw.get("id") else "",
                        buttons=[_button(gw, "claim", "success")] if won else []))


# --------------------------------------------------------------------- view
_STYLES = {"success": discord.ButtonStyle.success, "secondary": discord.ButtonStyle.secondary,
           "primary": discord.ButtonStyle.primary}


class CardView(discord.ui.LayoutView):
    """CardSpec -> a real card message. `handlers` maps button kind -> async callback(interaction).
    Non-preview views are persistent (fixed custom_ids), so the buttons keep working after a restart."""

    def __init__(self, spec: CardSpec, handlers: Mapping, *, preview: bool = False):
        super().__init__(timeout=840 if preview else None)
        self.spec = spec
        if spec.ping:
            self.add_item(discord.ui.TextDisplay(spec.ping))
        card = discord.ui.Container(accent_colour=spec.accent)
        if spec.top:
            card.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(spec.top)))
        if spec.body:
            if spec.thumb:
                card.add_item(discord.ui.Section(spec.body, accessory=discord.ui.Thumbnail(spec.thumb)))
            else:
                card.add_item(discord.ui.TextDisplay(spec.body))
        if spec.extra:
            card.add_item(discord.ui.Separator())
            card.add_item(discord.ui.TextDisplay(spec.extra))
        if spec.footer:
            card.add_item(discord.ui.TextDisplay(spec.footer))
        if spec.bottom:
            card.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(spec.bottom)))
        if spec.buttons:
            card.add_item(discord.ui.Separator(visible=False))
            row = discord.ui.ActionRow()
            for b in spec.buttons:
                btn = discord.ui.Button(style=_STYLES[b.style], label=b.label, emoji=b.emoji, disabled=b.disabled,
                                        custom_id=None if preview else BUTTON_IDS[b.kind])
                btn.callback = handlers[b.kind]
                row.add_item(btn)
            card.add_item(row)
        self.add_item(card)
