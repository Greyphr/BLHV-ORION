import json
import logging
import random
from typing import NamedTuple, Optional

import asyncpg

from . import config
from . import migrations
from .achievement_db import AchievementStore
from .ai_db import AiStore
from .audit_db import AuditStore
from .event_db import EventStore
from .giveaway_db import GiveawayStore
from .keys_db import KeyStore
from .poll_db import PollStore
from .ticket_db import TicketStore
from .timeutil import day_key, month_key, week_key

log = logging.getLogger("orion.db")

RULE_DEFAULTS = {
    "msg_min": config.MSG_XP_MIN, "msg_max": config.MSG_XP_MAX, "cooldown": config.MSG_COOLDOWN,
    "daily_bonus": config.DAILY_BONUS, "min_chars": config.MIN_MSG_CHARS,
    "voice_per_10min": config.VOICE_XP_PER_10MIN, "voice_cap": config.VOICE_DAILY_CAP,
}


class Award(NamedTuple):
    old_xp: int
    new_xp: int
    gained: int
    bonus: int = 0


class Database(AuditStore, AiStore, AchievementStore, EventStore, GiveawayStore, PollStore, TicketStore, KeyStore):
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None
        self.cfg: dict = {}
        self.excluded_channels: set[int] = set()
        self.excluded_roles: set[int] = set()

    # ------------------------------------------------------------ lifecycle
    async def connect(self):
        async def _init(conn):
            await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads,
                                      schema="pg_catalog")
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10, init=_init)
        async with self.pool.acquire() as c:
            await migrations.apply(c)
            for r in await c.fetch("SELECT key, value FROM config"):
                self.cfg[r["key"]] = r["value"]
            for r in await c.fetch("SELECT kind, target_id FROM xp_exclusions"):
                (self.excluded_channels if r["kind"] == "channel" else self.excluded_roles).add(r["target_id"])
        log.info("Database ready (%d config keys)", len(self.cfg))

    async def close(self):
        if self.pool:
            await self.pool.close()

    # --------------------------------------------------------------- config
    def get(self, key: str, default=None):
        return self.cfg.get(key, default)

    async def set(self, key: str, value):
        await self.pool.execute(           # database first: if it fails, the cache must not claim the value is saved
            "INSERT INTO config(key, value) VALUES($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", key, value)
        self.cfg[key] = value

    def rule(self, name: str) -> int:
        """XP rule; admins can override the defaults from /admin (stored as rule_<name>)."""
        return self.cfg.get("rule_" + name, RULE_DEFAULTS[name])

    async def reset_rules(self):
        await self.pool.execute("DELETE FROM config WHERE left(key, 5) = 'rule_'")
        for k in [k for k in self.cfg if k.startswith("rule_")]:
            del self.cfg[k]

    # ----------------------------------------------------------- exclusions
    async def set_exclusion(self, kind: str, target_id: int, excluded: bool):
        bucket = self.excluded_channels if kind == "channel" else self.excluded_roles
        if excluded:
            bucket.add(target_id)
            await self.pool.execute(
                "INSERT INTO xp_exclusions VALUES($1,$2) ON CONFLICT DO NOTHING", kind, target_id)
        else:
            bucket.discard(target_id)
            await self.pool.execute(
                "DELETE FROM xp_exclusions WHERE kind=$1 AND target_id=$2", kind, target_id)

    # ------------------------------------------------------------ XP writes
    @staticmethod
    async def _period(conn, user_id: int, gained: int):
        if gained == 0:
            return
        if gained < 0:      # XP was taken away: lower this week's / month's totals too (never below zero)
            await conn.execute(
                "UPDATE xp_period SET xp = GREATEST(0, xp + $4) WHERE user_id = $3 AND "
                "((period_type = 'week' AND period_key = $1) OR (period_type = 'month' AND period_key = $2))",
                week_key(), month_key(), user_id, gained)
            return
        await conn.execute(
            "INSERT INTO xp_period(period_type, period_key, user_id, xp) "
            "VALUES ('week', $1, $3, $4), ('month', $2, $3, $4) "
            "ON CONFLICT (period_type, period_key, user_id) "
            "DO UPDATE SET xp = xp_period.xp + EXCLUDED.xp",
            week_key(), month_key(), user_id, gained)

    async def award_message(self, user_id: int, now) -> Optional[Award]:
        """Applies cooldown + daily bonus atomically. Returns None if nothing was awarded."""
        today = day_key()
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT DO NOTHING", user_id)
            row = await c.fetchrow("SELECT * FROM users WHERE user_id=$1 FOR UPDATE", user_id)
            last = row["last_msg_xp"]
            if last and (now - last).total_seconds() < self.rule("cooldown"):
                return None
            base = random.randint(self.rule("msg_min"), self.rule("msg_max"))
            bonus = self.rule("daily_bonus") if row["last_daily"] != today else 0
            gain = base + bonus
            await c.execute(
                "UPDATE users SET xp = xp + $2, messages = messages + 1, "
                "last_msg_xp = $3, last_daily = $4 WHERE user_id = $1",
                user_id, gain, now, today)
            await self._period(c, user_id, gain)
            return Award(row["xp"], row["xp"] + gain, gain, bonus)

    async def award_voice(self, user_id: int, minutes: int = 1) -> Optional[Award]:
        """One payout for `minutes` of eligible voice (a full block). Respects the daily cap."""
        today = day_key()
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT DO NOTHING", user_id)
            row = await c.fetchrow("SELECT * FROM users WHERE user_id=$1 FOR UPDATE", user_id)
            used = row["voice_xp_today"] if row["voice_day"] == today else 0
            if user_id in config.VOICE_PERK_USER_IDS:
                # perk members: no daily cap, plus a percentage boost on every payout
                grant = round(self.rule("voice_per_10min") * (1 + config.VOICE_PERK_BOOST))
            else:
                grant = min(self.rule("voice_per_10min"), self.rule("voice_cap") - used)
            if grant <= 0:
                return None
            await c.execute(
                "UPDATE users SET xp = xp + $2, voice_minutes = voice_minutes + $5, "
                "voice_day = $3, voice_xp_today = $4 WHERE user_id = $1",
                user_id, grant, today, used + grant, minutes)
            await self._period(c, user_id, grant)
            return Award(row["xp"], row["xp"] + grant, grant)

    async def adjust_xp(self, user_id: int, mode: str, amount: int) -> Award:
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT DO NOTHING", user_id)
            old = await c.fetchval("SELECT xp FROM users WHERE user_id=$1 FOR UPDATE", user_id)
            new = {"add": old + amount, "remove": old - amount, "set": amount}[mode]
            new = max(0, new)
            await c.execute("UPDATE users SET xp=$2 WHERE user_id=$1", user_id, new)
            await self._period(c, user_id, new - old)
            return Award(old, new, new - old)

    async def import_xp(self, user_id: int, xp: int) -> Award:
        """Bring a member UP to `xp` (never lowers it, so re-running is safe). Doesn't touch weekly/monthly boards."""
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT DO NOTHING", user_id)
            old = await c.fetchval("SELECT xp FROM users WHERE user_id=$1 FOR UPDATE", user_id)
            new = max(old, xp)
            if new != old:
                await c.execute("UPDATE users SET xp=$2 WHERE user_id=$1", user_id, new)
            return Award(old, new, new - old)

    async def reset_user(self, user_id: int):
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("DELETE FROM users WHERE user_id=$1", user_id)
            await c.execute("DELETE FROM xp_period WHERE user_id=$1", user_id)

    # ------------------------------------------------------------ XP reads
    async def get_user(self, user_id: int):
        return await self.pool.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)

    async def period_xp(self, user_id: int) -> dict:
        """XP earned this week / this month (server time), for profiles."""
        rows = await self.pool.fetch(
            "SELECT period_type, xp FROM xp_period WHERE user_id=$1 AND "
            "((period_type='week' AND period_key=$2) OR (period_type='month' AND period_key=$3))",
            user_id, week_key(), month_key())
        got = {r["period_type"]: r["xp"] for r in rows}
        return {"week": got.get("week", 0), "month": got.get("month", 0)}

    async def record_join_dates(self, joins: list[tuple[int, object]]) -> int:
        """Copy Discord join dates onto existing user rows (only where they differ). Returns rows touched."""
        if not joins:
            return 0
        res = await self.pool.execute(
            "UPDATE users u SET joined_server_at = j.at FROM (SELECT unnest($1::bigint[]) AS id, "
            "unnest($2::timestamptz[]) AS at) j WHERE u.user_id = j.id AND u.joined_server_at IS DISTINCT FROM j.at",
            [u for u, _ in joins], [a for _, a in joins])
        return int(res.split()[-1])

    async def get_rank(self, user_id: int) -> Optional[int]:
        return await self.pool.fetchval(
            "SELECT CASE WHEN EXISTS (SELECT 1 FROM users WHERE user_id=$1) "
            "THEN (SELECT COUNT(*) + 1 FROM users WHERE xp > (SELECT xp FROM users WHERE user_id=$1)) "
            "END", user_id)

    async def leaderboard(self, kind: str, limit: int, offset: int):
        """kind: 'all' | 'week' | 'month'. Returns (rows, total)."""
        if kind == "all":
            rows = await self.pool.fetch(
                "SELECT user_id, xp FROM users WHERE xp > 0 "
                "ORDER BY xp DESC, user_id LIMIT $1 OFFSET $2", limit, offset)
            total = await self.pool.fetchval("SELECT COUNT(*) FROM users WHERE xp > 0")
        else:
            key = week_key() if kind == "week" else month_key()
            rows = await self.pool.fetch(
                "SELECT user_id, xp FROM xp_period WHERE period_type=$1 AND period_key=$2 AND xp > 0 "
                "ORDER BY xp DESC, user_id LIMIT $3 OFFSET $4", kind, key, limit, offset)
            total = await self.pool.fetchval(
                "SELECT COUNT(*) FROM xp_period WHERE period_type=$1 AND period_key=$2 AND xp > 0",
                kind, key)
        return rows, total

    # --------------------------------------------------- announcement builder
    async def store_image(self, data: bytes, ext: str) -> int:
        return await self.pool.fetchval(
            "INSERT INTO announcement_images(data, ext) VALUES($1,$2) RETURNING id", data, ext)

    async def get_image(self, image_id: int):
        return await self.pool.fetchrow("SELECT data, ext FROM announcement_images WHERE id=$1", image_id)

    async def save_template(self, name: str, data: dict, author_id: int):
        await self.pool.execute(
            "INSERT INTO announcement_templates(name, data, created_by) VALUES($1,$2,$3) "
            "ON CONFLICT (name) DO UPDATE SET data = EXCLUDED.data, created_by = EXCLUDED.created_by",
            name.strip().lower(), data, author_id)

    async def get_template(self, name: str):
        return await self.pool.fetchval(
            "SELECT data FROM announcement_templates WHERE name=$1", name.strip().lower())

    async def list_templates(self) -> list[str]:
        return [r["name"] for r in await self.pool.fetch("SELECT name FROM announcement_templates ORDER BY name")]

    async def delete_template(self, name: str) -> bool:
        return (await self.pool.execute(
            "DELETE FROM announcement_templates WHERE name=$1", name.strip().lower())) == "DELETE 1"

    async def save_announcement(self, channel_id, message_id, author_id, title, data, mentions, ping):
        await self.pool.execute(
            "INSERT INTO announcements(channel_id, message_id, author_id, title, data, mentions, ping_mode) "
            "VALUES($1,$2,$3,$4,$5,$6,$7)", channel_id, message_id, author_id, title, data, mentions, ping)

    async def update_announcement(self, message_id, title, data, mentions, ping):
        await self.pool.execute(
            "UPDATE announcements SET title=$2, data=$3, mentions=$4, ping_mode=$5, edited_at=now() "
            "WHERE message_id=$1", message_id, title, data, mentions, ping)

    async def get_announcement(self, message_id: int):
        return await self.pool.fetchrow("SELECT * FROM announcements WHERE message_id=$1", message_id)

    async def recent_announcements(self, limit: int = 10):
        return await self.pool.fetch("SELECT * FROM announcements ORDER BY published_at DESC LIMIT $1", limit)

    # ------------------------------------------------------- twitch streamers
    async def twitch_add(self, login: str, added_by: int, display_name: str, profile_image_url: str) -> bool:
        res = await self.pool.execute(
            "INSERT INTO twitch_streamers(login, display_name, profile_image_url, added_by) "
            "VALUES($1,$2,$3,$4) ON CONFLICT (login) DO NOTHING",
            login.lower(), display_name or login, profile_image_url or "", added_by)
        return res == "INSERT 0 1"

    async def twitch_remove(self, login: str) -> bool:
        return (await self.pool.execute("DELETE FROM twitch_streamers WHERE login=$1", login.lower())) == "DELETE 1"

    async def twitch_list(self):
        return await self.pool.fetch("SELECT * FROM twitch_streamers ORDER BY login")

    async def twitch_set_live(self, login: str, is_live: bool, stream_id: Optional[str] = None):
        """Live: store the flag (and the stream id when given, otherwise keep the old one). Offline: clear both."""
        if is_live:
            await self.pool.execute(
                "UPDATE twitch_streamers SET is_live = TRUE, stream_id = COALESCE($2, stream_id) WHERE login=$1",
                login.lower(), stream_id)
        else:
            await self.pool.execute(
                "UPDATE twitch_streamers SET is_live = FALSE, stream_id = NULL, missed_checks = 0 WHERE login=$1",
                login.lower())

    async def twitch_bump_missed(self, login: str) -> int:
        return await self.pool.fetchval(
            "UPDATE twitch_streamers SET missed_checks = missed_checks + 1 WHERE login=$1 RETURNING missed_checks",
            login.lower()) or 0

    async def twitch_reset_missed(self, login: str):
        await self.pool.execute(
            "UPDATE twitch_streamers SET missed_checks = 0 WHERE login=$1 AND missed_checks <> 0", login.lower())
