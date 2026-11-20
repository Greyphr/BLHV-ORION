"""Twitch live announcements.

/twitch add <name>      watch a Twitch channel
/twitch remove <name>   stop watching it
/twitch list            watched channels (🔴 = live right now)
/twitch channel [#ch]   where the "now live" posts go (default: the channel you run it in)
/twitch ping <mode>     role (default) / @everyone / @here / no ping
/twitch preview         see what an announcement looks like (only you can see it)

Twitch is polled every minute. A "now live" post goes out once per stream: restarts, Twitch blips
(a few missed checks) and failed database writes cannot cause a second @everyone for the same stream.
"""
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import alerts, config
from ..checks import require_staff
from ..twitch_api import TwitchAPI, TwitchRateLimited

log = logging.getLogger("orion.twitch")

LOGIN_RE = re.compile(r"^[a-z0-9_]{1,25}$")
PING_MODES = ("role", "everyone", "here", "none")
DEFAULT_PING = "role"
ANNOUNCE_CHANNEL_TYPES = (discord.TextChannel, discord.Thread, discord.VoiceChannel)


# ------------------------------------------------------------------ helpers
def normalize_twitch_username(raw: str) -> str | None:
    """'name', '@name', 'twitch.tv/name' or a full URL -> a valid lower-case login, else None."""
    text = (raw or "").strip().lower()
    if not text:
        return None
    if "twitch.tv" in text:
        _, _, text = text.partition("twitch.tv")
        text = text.strip("/\\")
        text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.lstrip("@\\")
    return text if LOGIN_RE.match(text) else None


def decide(stored_live: bool, stored_stream_id: str | None, announced: bool, current_stream_id: str | None):
    """A streamer is live right now. Returns (announce, remember_id):
    announce      -> post a "now live" message
    remember_id   -> don't post, but quietly store the current stream id (old record without one)"""
    if not (stored_live or announced):
        return True, False                       # normal go-live
    if stored_live and not announced:            # live per the database only (the bot was off for a while)
        if stored_stream_id is None:
            return False, True
        if current_stream_id is not None and stored_stream_id != current_stream_id:
            return True, False                   # a NEW stream deserves its own announcement
    return False, False


def build_live_embed(login: str, info, stream: dict) -> discord.Embed:
    display = stream.get("user_name") or info["display_name"] or login
    avatar = info["profile_image_url"] or ""
    e = discord.Embed(title=stream.get("title") or "Live on Twitch!", url=f"https://twitch.tv/{login}",
                      description=f"playing **{stream.get('game_name') or 'something'}**", color=config.COLOR_ACCENT)
    e.set_author(name=f"{display} is now live!", url=f"https://twitch.tv/{login}", icon_url=avatar or None)
    if avatar:
        e.set_thumbnail(url=avatar)
    thumb = stream.get("thumbnail_url", "")
    if thumb:
        # cache-bust so Discord doesn't keep showing an old preview frame
        e.set_image(url=f"{thumb.replace('{width}', '440').replace('{height}', '248')}?t={int(time.time())}")
    e.set_footer(text="Twitch")
    return e


def ping_parts(mode: str, role_id: int | None = None):
    """-> (text appended to the message, AllowedMentions)"""
    if mode == "role":
        rid = role_id or config.TWITCH_PING_ROLE_ID
        if not rid:
            return "", discord.AllowedMentions.none()      # no ping role chosen yet
        return f" <@&{rid}>", discord.AllowedMentions(everyone=False, users=False, roles=[discord.Object(id=rid)])
    if mode == "everyone":
        return " @everyone", discord.AllowedMentions(everyone=True, users=False, roles=False)
    if mode == "here":
        return " @here", discord.AllowedMentions(everyone=True, users=False, roles=False)
    return "", discord.AllowedMentions.none()


# --------------------------------------------------------------------- cog
class Twitch(commands.GroupCog, name="twitch", description="Twitch live announcements (staff)"):
    def __init__(self, bot):
        self.bot = bot
        self.api = (TwitchAPI(config.TWITCH_CLIENT_ID, config.TWITCH_CLIENT_SECRET)
                    if config.TWITCH_CLIENT_ID and config.TWITCH_CLIENT_SECRET else None)
        self._announced: set[str] = set()     # logins announced since their last offline transition
        self._rate_limited_until = 0.0
        if self.api:
            self.check_streams.start()
        else:
            log.warning("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not set: Twitch announcements are off")

    def cog_unload(self):
        self.check_streams.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_staff(interaction)

    def _need_api(self) -> str | None:
        if self.api:
            return None
        return ("Twitch isn't set up yet. Add `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` to the bot's `.env` "
                "(from https://dev.twitch.tv/console/apps) and restart it.")

    def _ping_role_id(self) -> int:
        """Role pinged in "role" mode: the one chosen with /twitch ping, else the TWITCH_PING_ROLE_ID default."""
        return int(self.bot.db.get("twitch_ping_role_id") or config.TWITCH_PING_ROLE_ID or 0)

    def _ping_label(self) -> str:
        mode = self.bot.db.get("twitch_ping", DEFAULT_PING)
        return f"role {self._ping_role_id()}" if mode == "role" else mode

    def _announcement(self, login: str, info, stream: dict):
        """-> (content, embed, allowed_mentions)"""
        display = stream.get("user_name") or info["display_name"] or login
        ping, allowed = ping_parts(self.bot.db.get("twitch_ping", DEFAULT_PING), self._ping_role_id())
        content = f"🔴 **{display}** is now live! https://twitch.tv/{login}{ping}"
        return content, build_live_embed(login, info, stream), allowed

    # --------------------------------------------------------------- commands
    @app_commands.command(name="add", description="Watch a Twitch channel and announce when it goes live")
    @app_commands.describe(twitch_username="The login from twitch.tv/<name> (a link works too)")
    async def add(self, i: discord.Interaction, twitch_username: str):
        if msg := self._need_api():
            return await i.response.send_message(msg, ephemeral=True)
        login = normalize_twitch_username(twitch_username)
        if login is None:
            return await i.response.send_message(
                "That doesn't look like a Twitch username. Use the login from `twitch.tv/<name>`: "
                "letters, numbers and underscores, up to 25 characters.", ephemeral=True)
        await i.response.defer()
        try:
            user = await self.api.get_user_info(login)
        except TwitchRateLimited:
            return await i.followup.send("Twitch is having trouble right now. Please try again in a bit.")
        except Exception:
            log.exception("Twitch lookup failed for %s", login)
            return await i.followup.send("Twitch is having trouble right now. Please try again.")
        if not user:
            return await i.followup.send(f"❌ Couldn't find a Twitch user named `{login}`.")
        if not await self.bot.db.twitch_add(login, i.user.id, user.get("display_name", login),
                                            user.get("profile_image_url", "")):
            return await i.followup.send(f"⚠️ `{login}` is already being watched.")
        out = f"✅ Now watching **{user['display_name']}**. I'll post when they go live."
        if not self.bot.db.get("twitch_channel_id"):
            out += "\n⚠️ No announcement channel yet. Run `/twitch channel` in the channel you want."
        await i.followup.send(out)

    @app_commands.command(name="remove", description="Stop watching a Twitch channel")
    async def remove(self, i: discord.Interaction, twitch_username: str):
        login = normalize_twitch_username(twitch_username)
        if login is None:
            return await i.response.send_message("That doesn't look like a Twitch username.", ephemeral=True)
        self._announced.discard(login)
        if await self.bot.db.twitch_remove(login):
            await i.response.send_message(f"✅ Stopped watching `{login}`.")
        else:
            await i.response.send_message(f"⚠️ `{login}` wasn't being watched.", ephemeral=True)

    @app_commands.command(name="list", description="Show the watched Twitch channels")
    async def list_cmd(self, i: discord.Interaction):
        rows = await self.bot.db.twitch_list()
        ch = self.bot.db.get("twitch_channel_id")
        if not rows:
            return await i.response.send_message("No Twitch channels are being watched yet. Use `/twitch add`.",
                                                 ephemeral=True)
        lines = [f"• `{r['login']}`" + (" 🔴 live" if r["is_live"] else "") for r in rows]
        e = discord.Embed(color=config.COLOR_MAIN, title="Watched Twitch channels", description="\n".join(lines))
        e.set_footer(text=f"Announcements: {'#' + i.guild.get_channel(ch).name if ch and i.guild.get_channel(ch) else 'no channel set'}"
                          f" · ping: {self._ping_label()}")
        await i.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="channel", description="Choose where live announcements are posted")
    @app_commands.describe(channel="Defaults to the channel you run this in")
    async def channel(self, i: discord.Interaction, channel: discord.TextChannel | None = None):
        ch = channel or i.channel
        if not isinstance(ch, ANNOUNCE_CHANNEL_TYPES):
            return await i.response.send_message("Pick a server text channel.", ephemeral=True)
        await self.bot.db.set("twitch_channel_id", ch.id)
        out = f"✅ Live announcements will be posted in {ch.mention}."
        perms = ch.permissions_for(i.guild.me)
        if not (perms.send_messages and perms.embed_links):
            out += "\n⚠️ I'm missing **Send Messages** and/or **Embed Links** there. Fix that or nothing will show up."
        mode = self.bot.db.get("twitch_ping", DEFAULT_PING)
        if mode in ("everyone", "here") and not perms.mention_everyone:
            out += "\n⚠️ I'm missing **Mention Everyone** there, so the @everyone/@here ping won't work."
        if mode == "role":
            role = i.guild.get_role(self._ping_role_id())
            if role is None:
                out += f"\n⚠️ I can't find the ping role (`{self._ping_role_id()}`) in this server."
            elif not (role.mentionable or perms.mention_everyone):
                out += f"\n⚠️ {role.mention} isn't mentionable and I'm missing **Mention Everyone**, so the ping won't work."
        await i.response.send_message(out, ephemeral=True)

    @app_commands.command(name="ping", description="Choose how live announcements ping people")
    @app_commands.describe(mode="Who gets pinged", role="Which role to ping (only used with \"Ping role\")")
    @app_commands.choices(mode=[app_commands.Choice(name="Ping role", value="role"),
                                app_commands.Choice(name="@everyone", value="everyone"),
                                app_commands.Choice(name="@here", value="here"),
                                app_commands.Choice(name="No ping", value="none")])
    async def ping(self, i: discord.Interaction, mode: app_commands.Choice[str], role: discord.Role | None = None):
        if role is not None and mode.value != "role":
            return await i.response.send_message("The role option only works together with **Ping role**.", ephemeral=True)
        if role is not None and role.is_default():
            return await i.response.send_message("That's @everyone. Pick **@everyone** as the mode instead.", ephemeral=True)
        await self.bot.db.set("twitch_ping", mode.value)
        if role is not None:
            await self.bot.db.set("twitch_ping_role_id", role.id)
        out = f"✅ Live announcements now use: **{mode.name}**."
        if mode.value == "role":
            target = role or i.guild.get_role(self._ping_role_id())
            out = f"✅ Live announcements will now ping {target.mention if target else '`' + str(self._ping_role_id()) + '`'}."
            if target and not target.mentionable and not i.guild.me.guild_permissions.mention_everyone:
                out += f"\n⚠️ {target.name} isn't mentionable and I'm missing **Mention Everyone**, so the ping won't work."
        await i.response.send_message(out, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="preview", description="See what a live announcement looks like (only you see it)")
    async def preview(self, i: discord.Interaction):
        rows = await self.bot.db.twitch_list()
        r = rows[0] if rows else {"display_name": "SampleStreamer", "profile_image_url": ""}
        login = r["login"] if rows else "samplestreamer"
        fake = {"user_name": r["display_name"], "title": "Ranked grind with chat!", "game_name": "Marvel Rivals"}
        content, embed, _ = self._announcement(login, r, fake)
        await i.response.send_message(content=content.replace("@everyone", "`@everyone`").replace("@here", "`@here`")
                                      .replace(f"<@&{self._ping_role_id()}>", f"`@role {self._ping_role_id()}`"),
                                      embed=embed, ephemeral=True)

    # ------------------------------------------------------------------- loop
    def _apply_rate_limit(self, reset_at):
        now = time.time()
        self._rate_limited_until = reset_at if reset_at is not None and now < reset_at <= now + 300 else now + 60

    @tasks.loop(seconds=config.TWITCH_CHECK_SECONDS)
    async def check_streams(self):
        try:
            await self.run_once()
        except Exception:
            log.exception("Twitch check cycle failed")

    @check_streams.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _channel(self, guild: discord.Guild):
        cid = self.bot.db.get("twitch_channel_id", 0)
        ch = guild.get_channel_or_thread(cid) if cid else None
        return ch if isinstance(ch, ANNOUNCE_CHANNEL_TYPES) else None

    async def run_once(self):
        db = self.bot.db
        if time.time() < self._rate_limited_until or not db.get("twitch_channel_id"):
            return
        rows = await db.twitch_list()
        self._announced &= {r["login"] for r in rows}      # forget streamers that were removed
        if not rows:
            return
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return
        try:
            live_now = await self.api.get_live_streams([r["login"] for r in rows])
        except TwitchRateLimited as e:
            self._apply_rate_limit(e.reset_at)
            log.warning("Twitch rate-limited us; skipping this cycle")
            return
        except Exception:
            log.exception("Error checking Twitch streams")
            return

        channel = None
        for r in rows:
            login = r["login"]
            try:
                stream = live_now.get(login)
                if stream is not None:
                    announce, remember = decide(r["is_live"], r["stream_id"], login in self._announced,
                                                stream.get("id"))
                    if announce:
                        channel = channel or await self._channel(guild)
                        if channel is None:
                            await alerts.alert_staff(
                                self.bot, guild, "Twitch announcements can't be posted: the channel set with "
                                "`/twitch channel` is missing or I can't see it.", key="twitch-channel")
                            return
                        content, embed, allowed = self._announcement(login, r, stream)
                        try:
                            await channel.send(content=content, embed=embed, allowed_mentions=allowed)
                        except discord.HTTPException:
                            log.exception("Failed to post the live announcement for %s", login)
                            await alerts.alert_staff(
                                self.bot, guild, f"I couldn't post a Twitch announcement in {channel.mention} "
                                "(check Send Messages / Embed Links / Mention Everyone there).", key="twitch-send")
                            continue                       # not marked announced, so it retries next cycle
                        # remembered in memory right away: if the DB write below fails we still won't double-ping
                        self._announced.add(login)
                        await db.twitch_set_live(login, True, stream.get("id"))
                    elif remember or (login in self._announced and not r["is_live"]):
                        await db.twitch_set_live(login, True, stream.get("id"))   # quiet bookkeeping, no post
                    await db.twitch_reset_missed(login)
                elif r["is_live"] or login in self._announced:
                    # offline blips must repeat before the stream counts as ended
                    if await db.twitch_bump_missed(login) >= config.TWITCH_OFFLINE_GRACE:
                        await db.twitch_set_live(login, False)
                        self._announced.discard(login)
            except Exception:
                log.exception("Error handling Twitch streamer %s", login)


async def setup(bot):
    await bot.add_cog(Twitch(bot))
