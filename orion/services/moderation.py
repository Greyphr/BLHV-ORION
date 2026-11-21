"""Moderation rules with no Discord imports: durations, who may act on whom, spam and raid detection, escalation, settings.

The Discord side (cogs/moderation.py) only carries out what these rules allow, so a future API can reuse them unchanged.
"""
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

ACTIONS = ("warn", "note", "timeout", "untimeout", "kick", "ban", "unban", "pardon")
DEFAULT_ADMIN_ONLY = ("kick", "ban", "unban", "pardon")          # everything else: any staff member
MAX_TIMEOUT_SECONDS = 28 * 86400                                    # Discord's own limit
MAX_TEMPBAN_SECONDS = 365 * 86400
LEVELS = ("off", "alert", "act")

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> int:
    """'30m', '2h', '7d', '1d12h', '1w' -> seconds. Raises ValueError with a plain message."""
    t = (text or "").strip().lower().replace(" ", "")
    if not t or not re.fullmatch(r"(\d+[smhdw])+", t):
        raise ValueError("Write a duration like `30m`, `2h`, `7d` or `1d12h`.")
    total = sum(int(n) * _UNITS[u] for n, u in re.findall(r"(\d+)([smhdw])", t))
    if total <= 0:
        raise ValueError("The duration must be longer than zero.")
    return total


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return " ".join(parts[:3]) or "0s"


def required_level(action: str, admin_only) -> str:
    return "admin" if action in admin_only else "staff"


def hierarchy_problem(*, actor_id: int, actor_top: int, actor_is_admin: bool, target_id: int, target_top: int,
                      target_is_owner: bool, target_is_admin: bool, target_is_staff: bool, target_is_bot: bool,
                      bot_id: int, bot_top: int, needs_bot_hierarchy: bool = True) -> Optional[str]:
    """Why this moderator may NOT act on this member, or None if it is fine. Positions are Discord role positions."""
    if target_id == actor_id:
        return "You can't do that to yourself."
    if target_id == bot_id:
        return "I won't act on myself."
    if target_is_owner:
        return "That member is the Sect Patriarch / server owner."
    if target_is_bot:
        return "That is a bot account. Remove it from the server settings instead."
    if target_is_admin and not actor_is_admin:
        return "You can't act on a Sect Admin."
    if target_is_staff and not actor_is_admin:
        return "Staff can only be moderated by a Sect Admin."
    if not actor_is_admin and target_top >= actor_top:
        return "That member's highest role is equal to or above yours."
    if needs_bot_hierarchy and target_top >= bot_top:
        return "That member's highest role is above mine, so Discord won't let me act on them. Move my role higher."
    return None


def escalation_due(active_warns: int, every: int) -> bool:
    """True on every `every`-th active warning (3, 6, 9 ... when every=3). 0 = never."""
    return every > 0 and active_warns > 0 and active_warns % every == 0


# ------------------------------------------------------------------ settings
@dataclass
class ModSettings:
    admin_only: frozenset = field(default_factory=lambda: frozenset(DEFAULT_ADMIN_ONLY))
    dm: bool = True
    warn_window_days: int = 30
    escalate_every: int = 0                 # 0 = humans decide
    escalate_minutes: int = 60
    spam: str = "alert"
    spam_rate_count: int = 6
    spam_rate_seconds: int = 5
    spam_dup_count: int = 4
    spam_dup_seconds: int = 30
    spam_mentions: int = 6
    spam_timeout_minutes: int = 10
    raid: str = "alert"
    raid_joins: int = 8
    raid_seconds: int = 30
    raid_min_age_days: int = 7
    raid_timeout_minutes: int = 60
    exempt_channels: frozenset = frozenset()
    exempt_roles: frozenset = frozenset()


def load_settings(get: Callable) -> ModSettings:
    def num(key, default, lo, hi):
        try:
            return min(hi, max(lo, int(get(key, default))))
        except (TypeError, ValueError):
            return default

    def lvl(key):
        v = get(key, "alert")
        return v if v in LEVELS else "alert"

    def ids(key):
        v = get(key, [])
        return frozenset(int(x) for x in v if str(x).isdigit()) if isinstance(v, list) else frozenset()
    ao = get("mod_admin_only", None)
    return ModSettings(
        admin_only=frozenset(a for a in ao if a in ACTIONS) if isinstance(ao, list) else frozenset(DEFAULT_ADMIN_ONLY),
        dm=bool(get("mod_dm", True)), warn_window_days=num("mod_warn_window_days", 30, 1, 3650),
        escalate_every=num("mod_escalate_every", 0, 0, 50), escalate_minutes=num("mod_escalate_minutes", 60, 1, 40320),
        spam=lvl("automod_spam"), spam_rate_count=num("automod_rate_count", 6, 2, 50),
        spam_rate_seconds=num("automod_rate_seconds", 5, 1, 120), spam_dup_count=num("automod_dup_count", 4, 2, 50),
        spam_dup_seconds=num("automod_dup_seconds", 30, 5, 600), spam_mentions=num("automod_mentions", 6, 2, 50),
        spam_timeout_minutes=num("automod_timeout_minutes", 10, 1, 40320),
        raid=lvl("automod_raid"), raid_joins=num("automod_raid_joins", 8, 3, 200),
        raid_seconds=num("automod_raid_seconds", 30, 5, 600), raid_min_age_days=num("automod_raid_min_age_days", 7, 0, 3650),
        raid_timeout_minutes=num("automod_raid_timeout_minutes", 60, 1, 40320),
        exempt_channels=ids("automod_exempt_channels"), exempt_roles=ids("automod_exempt_roles"))


SETTING_NAMES = ["spam", "raid", "escalate-every", "escalate-minutes", "warn-window-days", "dm-members", "admin-only",
                 "spam-rate", "spam-duplicates", "spam-mentions", "spam-timeout-minutes", "raid-joins", "raid-timeout-minutes"]


def parse_setting(name: str, value: str) -> dict:
    """Validate a value typed in /mod set. Returns {config_key: value, ...} to store. Raises ValueError with a plain message."""
    key, val = _parse_one(name, value)
    pair = {"automod_rate": ("automod_rate_count", "automod_rate_seconds"),
            "automod_dup": ("automod_dup_count", "automod_dup_seconds"),
            "automod_raid_window": ("automod_raid_joins", "automod_raid_seconds")}.get(key)
    return dict(zip(pair, val)) if pair else {key: val}


def _parse_one(name: str, value: str):
    v, low = value.strip(), value.strip().lower()

    def whole(lo, hi, what):
        if not v.isdigit() or not lo <= int(v) <= hi:
            raise ValueError(f"{what} must be a whole number from {lo} to {hi}.")
        return int(v)
    if name in ("spam", "raid"):
        if low not in LEVELS:
            raise ValueError("Use `off` (do nothing), `alert` (tell staff only) or `act` (take action as well).")
        return f"automod_{name}", low
    if name == "escalate-every":
        return "mod_escalate_every", whole(0, 50, "Warnings before auto-timeout (0 = never)")
    if name == "escalate-minutes":
        return "mod_escalate_minutes", whole(1, 40320, "Auto-timeout length in minutes")
    if name == "warn-window-days":
        return "mod_warn_window_days", whole(1, 3650, "Days a warning counts toward escalation")
    if name == "dm-members":
        if low not in ("on", "off", "yes", "no", "true", "false"):
            raise ValueError("Use on or off.")
        return "mod_dm", low in ("on", "yes", "true")
    if name == "admin-only":
        acts = [a.strip().lower() for a in v.split(",") if a.strip()]
        bad = [a for a in acts if a not in ACTIONS]
        if bad:
            raise ValueError("Unknown action: " + ", ".join(bad) + ". Choose from: " + ", ".join(ACTIONS))
        return "mod_admin_only", acts
    if name == "spam-rate":                                       # "6/5" = 6 messages in 5 seconds
        m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", v)
        if not m or not (2 <= int(m[1]) <= 50 and 1 <= int(m[2]) <= 120):
            raise ValueError("Write it as messages/seconds, for example `6/5` (6 messages in 5 seconds).")
        return "automod_rate", (int(m[1]), int(m[2]))
    if name == "spam-duplicates":
        m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", v)
        if not m or not (2 <= int(m[1]) <= 50 and 5 <= int(m[2]) <= 600):
            raise ValueError("Write it as copies/seconds, for example `4/30`.")
        return "automod_dup", (int(m[1]), int(m[2]))
    if name == "spam-mentions":
        return "automod_mentions", whole(2, 50, "Mentions in one message")
    if name == "spam-timeout-minutes":
        return "automod_timeout_minutes", whole(1, 40320, "Spam timeout length in minutes")
    if name == "raid-joins":
        m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", v)
        if not m or not (3 <= int(m[1]) <= 200 and 5 <= int(m[2]) <= 600):
            raise ValueError("Write it as joins/seconds, for example `8/30`.")
        return "automod_raid_window", (int(m[1]), int(m[2]))
    if name == "raid-timeout-minutes":
        return "automod_raid_timeout_minutes", whole(1, 40320, "Raid timeout length in minutes")
    raise ValueError("Unknown setting.")


# ------------------------------------------------------------- spam detection
def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


class SpamDetector:
    """Per-member sliding windows: too many messages, the same message repeated, or too many mentions in one message.
    Memory stays bounded: members idle longer than the longest window are forgotten."""

    def __init__(self, s: ModSettings):
        self.s = s
        self._msgs: dict[int, deque] = {}
        self._calls = 0

    def update(self, s: ModSettings):
        self.s = s

    def check(self, user_id: int, content: str, mention_count: int, channel_id: int, message_id: int, now: float):
        """Returns None, or (kind, [(channel_id, message_id), ...]) for the messages that triggered it."""
        s = self.s
        horizon = max(s.spam_rate_seconds, s.spam_dup_seconds)
        dq = self._msgs.setdefault(user_id, deque())
        while dq and now - dq[0][0] > horizon:
            dq.popleft()
        norm = normalise(content)
        dq.append((now, norm, channel_id, message_id))
        self._calls += 1
        if self._calls % 500 == 0:
            self._purge(now, horizon)
        if mention_count >= s.spam_mentions:
            return "mass mentions", [(channel_id, message_id)]
        recent = [m for m in dq if now - m[0] <= s.spam_rate_seconds]
        if len(recent) >= s.spam_rate_count:
            dq.clear()
            return "message flood", [(m[2], m[3]) for m in recent]
        if norm:
            same = [m for m in dq if m[1] == norm and now - m[0] <= s.spam_dup_seconds]
            if len(same) >= s.spam_dup_count:
                dq.clear()
                return "repeated message", [(m[2], m[3]) for m in same]
        return None

    def _purge(self, now: float, horizon: float):
        for uid in [u for u, d in self._msgs.items() if not d or now - d[-1][0] > horizon]:
            del self._msgs[uid]


class RaidDetector:
    """Triggers once when `joins` members arrive within `seconds`; quiet for a window afterwards so one raid = one alert."""

    def __init__(self, joins: int, seconds: int):
        self.joins, self.seconds = joins, seconds
        self._recent: deque = deque()
        self._quiet_until = 0.0

    def update(self, joins: int, seconds: int):
        self.joins, self.seconds = joins, seconds

    def record(self, user_id: int, account_created_ts: float, now: float):
        """Returns the burst as [(user_id, account_created_ts)] when the threshold is hit, else None."""
        while self._recent and now - self._recent[0][0] > self.seconds:
            self._recent.popleft()
        self._recent.append((now, user_id, account_created_ts))
        if len(self._recent) >= self.joins and now >= self._quiet_until:
            self._quiet_until = now + self.seconds * 2
            return [(u, c) for _, u, c in self._recent]
        return None
