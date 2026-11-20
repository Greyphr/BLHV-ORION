"""Staff alerts: tell the staff channel when the bot hits a problem it can't fix itself."""
import logging
import time

import discord

log = logging.getLogger("orion.alerts")

THROTTLE_SECONDS = 1800          # the same problem is reported at most once per 30 minutes
_last: dict[str, float] = {}


async def alert_staff(bot, guild: discord.Guild, text: str, key: str | None = None) -> bool:
    """Post `text` in the staff alerts channel. Returns True if something was posted.
    Identical problems (same `key`) are throttled so a misconfigured role can't spam the channel."""
    key = key or text
    now = time.monotonic()
    if key in _last and now - _last[key] < THROTTLE_SECONDS:
        return False
    _last[key] = now
    log.warning("Staff alert: %s", text)
    ch = guild.get_channel(bot.db.get("alert_channel_id", 0))
    if ch is None:
        return False
    try:
        await ch.send(f"⚠️ {text}\n-# Same-reason alerts are paused for 30 minutes.",
                      allowed_mentions=discord.AllowedMentions.none())
        return True
    except discord.HTTPException:
        log.warning("Could not post a staff alert in %s", ch)
        return False
