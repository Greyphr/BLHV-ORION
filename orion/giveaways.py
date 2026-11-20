"""Giveaways & rewards: pure helpers (no database, no Discord connection), so they are unit-tested.

  parse_duration / format_duration   "1d 6h", "90m", "2 weeks" <-> seconds
  evaluate_entry                     THE check-in rule: join = 1 entry, then at most 1 more per interval
  weighted_sample                    secure weighted random draw (more entries = better odds, never a guarantee)
  reward_lines / winner_lines        public text. They only ever show reward NAME and VALUE, never a code.
  csv_bytes                          export helper that neutralises spreadsheet-formula injection
"""
import csv
import io
import re
import secrets
from datetime import datetime, timedelta
from typing import Mapping, Optional, Sequence

from .levels import LEVEL_ROLES

# ------------------------------------------------------------------ constants
DEFAULT_ELIGIBLE = [name for _, name in LEVEL_ROLES if name != "Earth King"]   # Planetary ... Eternal True God

MIN_DURATION, MAX_DURATION = 5 * 60, 90 * 86400            # a giveaway lasts 5 minutes to 90 days
MIN_INTERVAL, MAX_INTERVAL = 10 * 60, 30 * 86400           # check-ins every 10 minutes to 30 days
INTERVAL_PRESETS = {"6h": 6 * 3600, "12h": 12 * 3600, "24h": 24 * 3600}
MAX_WINNERS = 50
MAX_AUTO_REASSIGNS = 3                                     # unclaimed prize passed on at most 3 times per slot
MAX_ROLE_ATTEMPTS = 5                                      # winner-role retries before staff are told to step in

# type -> (label, emoji, "code" = a secret code the bot hands over | "instructions" = delivery text)
REWARD_TYPES = {
    "gift_card":    ("Gift card code", "🎁", "code"),
    "cash":         ("Cash prize", "💵", "instructions"),
    "game_key":     ("Game key", "🎮", "code"),
    "subscription": ("Subscription code", "📺", "code"),
    "digital":      ("Digital product", "💾", "code"),
    "physical":     ("Physical product", "📦", "instructions"),
    "custom":       ("Custom reward", "✨", "instructions"),
}
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

COLOR_ENDED = 0x6B7280
COLOR_CANCELLED = 0xDC2626


# ------------------------------------------------------------------- durations
class DurationError(ValueError):
    pass


DURATION_HELP = "Use something like `6h`, `1d 12h`, `90m` or `2 weeks`."
_TOKEN = re.compile(r"(\d+)\s*(weeks?|w|days?|d|hours?|hrs?|h|minutes?|mins?|m)(?![a-z])")
_UNIT = {"w": 604800, "d": 86400, "h": 3600, "m": 60}


def parse_duration(text: str, minimum: int, maximum: int) -> int:
    """'1d 6h' -> 108000. Raises DurationError with a plain-words reason."""
    s = re.sub(r"\band\b|,", " ", (text or "").strip().lower())
    matches = list(_TOKEN.finditer(s))
    if not matches or _TOKEN.sub(" ", s).strip():
        raise DurationError(f"I couldn't read that time. {DURATION_HELP}")
    total = sum(int(m[1]) * _UNIT[m[2][0]] for m in matches)
    if total < minimum:
        raise DurationError(f"That's too short. The minimum is {format_duration(minimum)}.")
    if total > maximum:
        raise DurationError(f"That's too long. The maximum is {format_duration(maximum)}.")
    return total


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    parts = []
    for name, size in (("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n} {name}{'s' if n != 1 else ''}")
    return " ".join(parts[:2]) or "0 minutes"


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def place_label(position: int) -> str:
    return f"{MEDALS.get(position, '🎖️')} {ordinal(position)} Place"


# ---------------------------------------------------------------- entry rule
def evaluate_entry(last_entry_at: Optional[datetime], now: datetime, ends_at: datetime,
                   interval_seconds: int) -> tuple[str, Optional[datetime]]:
    """The one rule behind joining and checking in. Returns (state, next_entry_at):

      'closed'      the giveaway is over, nothing is granted
      'joined'      first time: the member gets their first entry
      'checked_in'  one full interval has passed since the last entry: +1 entry
      'cooldown'    too early: nothing is granted, next_entry_at says when it opens

    next_entry_at is None when the next entry would fall after the giveaway has ended."""
    if now >= ends_at:
        return "closed", None
    step = timedelta(seconds=interval_seconds)
    if last_entry_at is None:
        state, nxt = "joined", now + step
    else:
        nxt = last_entry_at + step
        if now < nxt:
            return "cooldown", (nxt if nxt < ends_at else None)
        state, nxt = "checked_in", now + step
    return state, (nxt if nxt < ends_at else None)


# ------------------------------------------------------------- winner draw
_RNG = secrets.SystemRandom()      # OS entropy; never seeded, so a draw can't be predicted or replayed


def weighted_sample(weights: Mapping[int, int], k: int, *, allow_repeat: bool = False, rng=None) -> list[int]:
    """Pick up to `k` user ids. Chance of each pick is proportional to that user's entries.
    Unique winners unless allow_repeat. Returns fewer than k only if there aren't enough participants."""
    rng = rng or _RNG
    pool = {u: w for u, w in sorted(weights.items()) if w > 0}
    picks: list[int] = []
    for _ in range(k):
        if not pool:
            break
        roll = rng.randrange(sum(pool.values()))
        acc = 0
        for user, w in pool.items():
            acc += w
            if roll < acc:
                break
        picks.append(user)
        if not allow_repeat:
            del pool[user]
    return picks


# --------------------------------------------------------------------- CSV
def _safe_cell(v):
    """A display name like '=HYPERLINK(...)' would run as a formula in Excel: prefix it with an apostrophe."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def csv_bytes(header: Sequence[str], rows: Sequence[Sequence]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([_safe_cell(c) for c in r])
    return buf.getvalue().encode("utf-8-sig")        # BOM so Excel opens non-ASCII names correctly


# --------------------------------------------------------- public text
def _cap(lines: Sequence[str], limit: int) -> str:
    """Join lines but stop (with a '...and N more') before a Discord field/description limit."""
    out, used = [], 0
    for n, line in enumerate(lines):
        if used + len(line) + 1 > limit - 30:
            out.append(f"…and {len(lines) - n} more")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def reward_lines(rewards: Sequence[Mapping]) -> list[str]:
    """Public prize list: position, name and (optional) value. Never anything secret."""
    lines = []
    for r in rewards:
        emoji = REWARD_TYPES.get(r["reward_type"], REWARD_TYPES["custom"])[1]
        line = f"{place_label(r['position'])} — {emoji} **{r['name']}**"
        if r["value"]:
            line += f" ({r['value']})"
        lines.append(line)
    return lines


def winner_lines(rewards: Sequence[Mapping]) -> list[str]:
    lines = []
    for r in rewards:
        who = f"<@{r['winner_id']}>" if r["winner_id"] else "*No eligible winner*"
        prize = r["value"] or r["name"]
        lines.append(f"{MEDALS.get(r['position'], '🎖️')} **{ordinal(r['position'])} Place:** {who} — {prize}")
    return lines
