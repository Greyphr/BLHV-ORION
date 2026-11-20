import asyncio
import datetime as dt
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import alerts, config, roles
from ..levels import is_realm_change, level_from_xp, rank_name, realm_of, role_name_for_level
from ..images import render_levelup
from ..timeutil import TZ, now_local
from ..ui import build_profile, ladder_message, rank_card, send_leaderboard, _embed

log = logging.getLogger("orion.leveling")

_STRIP = re.compile(r"https?://\S+|<[@#:a][^>]*>|[^\w]", re.UNICODE)
_CMD_PREFIX = "!?.$;,+-%>/~"
_NIGHTLY = dt.time(hour=4, minute=0, tzinfo=TZ)   # nightly role check, 04:00 in the bot's timezone


class Leveling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._last_award: dict[int, float] = {}
        self._last_text: dict[int, str] = {}
        self._voice_mins: dict[int, int] = {}     # eligible voice minutes since the last payout (resets on restart)
        self.voice_tick.start()
        self.nightly_resync.start()
        self.startup_resync.start()

    def cog_unload(self):
        self.voice_tick.cancel()
        self.nightly_resync.cancel()
        self.startup_resync.cancel()

    # -------------------------------------------------------------- helpers
    def _excluded(self, member: discord.Member, channel) -> bool:
        db = self.bot.db
        if channel is not None:
            ids = {channel.id, getattr(channel, "category_id", None), getattr(channel, "parent_id", None)}
            if ids & db.excluded_channels:
                return True
        return any(r.id in db.excluded_roles for r in member.roles)

    def _real_content(self, msg: discord.Message):
        """Returns normalised text if the message counts as 'real content', else None."""
        if msg.type not in (discord.MessageType.default, discord.MessageType.reply):
            return None
        raw = msg.content.strip()
        if len(raw) > 1 and raw[0] in _CMD_PREFIX and raw[1].isalnum():
            return None  # looks like a bot command
        clean = _STRIP.sub("", raw).lower()
        return clean if len(clean) >= self.bot.db.rule("min_chars") else None

    async def after_xp(self, member: discord.Member, old_xp: int, new_xp: int):
        old_l, new_l = level_from_xp(old_xp), level_from_xp(new_xp)
        if old_l == new_l:
            return
        old_role, new_role = role_name_for_level(old_l), role_name_for_level(new_l)
        if old_role != new_role or old_l == 0:
            await roles.sync_and_alert(self.bot, member, new_l)
        await self.bot.db.log_progression(member.id, old_xp, new_xp, old_l, new_l, "activity")
        if is_realm_change(old_l, new_l):
            await self.post_log(member.guild, f"🌌 {member.mention} broke through: **{rank_name(old_l)}** → **{rank_name(new_l)}** ({new_xp:,} XP)")
        if new_l > old_l:
            mode = self.bot.db.get("levelup_mode", "every")      # every | realm
            if mode == "every" or is_realm_change(old_l, new_l):
                await self.announce(member, new_l, new_role if new_role != old_role else None)

    async def post_log(self, guild: discord.Guild, text: str):
        """Staff-only #cultivation-logs line (progressions, XP adjustments). Never raises."""
        ch = guild.get_channel(self.bot.db.get("cultivation_log_channel_id", 0))
        if ch:
            try:
                await ch.send(text[:1900], allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.warning("Could not post to the cultivation log channel")

    async def announce(self, member: discord.Member, level: int, new_rank: str | None):
        ch = member.guild.get_channel(self.bot.db.get("levelup_channel_id", 0))
        if not ch or not self.bot.db.get("levelup_announce_enabled", True):
            return
        mentions = discord.AllowedMentions(users=[member])

        # Preferred: the "LEVEL UP" banner (avatar, username, level and rank drawn onto the template).
        try:
            avatar = await member.display_avatar.replace(size=256, format="png").read()
            buf = await asyncio.to_thread(render_levelup, avatar, member.display_name, level,
                                          role_name_for_level(level))
            e = _embed()
            if new_rank:
                e.description = f"🌌 Broke through to the **{new_rank}** realm!"
            e.set_image(url="attachment://levelup.png")
            await ch.send(content=member.mention, embed=e, file=discord.File(buf, "levelup.png"),
                          allowed_mentions=mentions)
            return
        except Exception:
            log.warning("Level-up card failed for %s, falling back to the text embed", member, exc_info=True)

        # Fallback (card couldn't be drawn or the channel doesn't allow image uploads): the plain embed.
        e = _embed(description=f"🔷 {member.mention} advanced to **{rank_name(level)}**!")
        if new_rank:
            e.add_field(name="New realm", value=f"**{new_rank}**")
        e.set_thumbnail(url=member.display_avatar.url)
        try:
            await ch.send(content=member.mention, embed=e, allowed_mentions=mentions)
        except discord.HTTPException:
            log.warning("Could not post level-up in #%s", ch)

    # ------------------------------------------------------- role self-healing
    async def _resync_and_report(self, label: str):
        """Make everyone's level role match their XP (fixes drift, e.g. people who joined while the bot was off)."""
        try:
            guild = self.bot.get_guild(config.GUILD_ID)
            if not guild:
                return
            changed, problem = await roles.resync_all(guild, self.bot.db)
            await self.bot.db.record_join_dates([(m.id, m.joined_at) for m in guild.members
                                                 if not m.bot and m.joined_at])
            if problem:
                await alerts.alert_staff(self.bot, guild, f"{label} role check stopped: {problem}", key=problem)
            elif changed:
                log.info("%s role check fixed %d member(s)", label, changed)
        except Exception:
            log.exception("%s role check failed", label)

    @tasks.loop(time=_NIGHTLY)
    async def nightly_resync(self):
        await self._resync_and_report("Nightly")

    @nightly_resync.before_loop
    async def _before_nightly(self):
        await self.bot.wait_until_ready()

    @tasks.loop(count=1)
    async def startup_resync(self):
        await asyncio.sleep(30)          # let the member cache settle first
        await self._resync_and_report("Startup")

    @startup_resync.before_loop
    async def _before_startup(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------- messages
    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if msg.author.bot or not msg.guild or msg.guild.id != config.GUILD_ID:
            return
        if not isinstance(msg.author, discord.Member):
            return
        if not self.bot.db.get("xp_messages_enabled", True):
            return
        clean = self._real_content(msg)
        if clean is None:
            return
        uid = msg.author.id
        if self._last_text.get(uid) == clean:   # identical to previous message -> spam
            return
        self._last_text[uid] = clean
        if time.monotonic() - self._last_award.get(uid, 0) < self.bot.db.rule("cooldown"):
            return
        if self._excluded(msg.author, msg.channel):
            return
        try:
            res = await self.bot.db.award_message(uid, discord.utils.utcnow())
        except Exception:
            log.exception("award_message failed")
            return
        if res:
            self._last_award[uid] = time.monotonic()
            await self.after_xp(msg.author, res.old_xp, res.new_xp)

    # ---------------------------------------------------------------- voice
    @tasks.loop(minutes=1)
    async def voice_tick(self):
        # discord.py never restarts a loop after an unexpected exception, so nothing may escape this method.
        try:
            await self._voice_pass()
        except Exception:
            log.exception("voice XP pass failed (will retry next minute)")

    async def _voice_pass(self):
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild or not self.bot.db.get("xp_voice_enabled", True):
            return
        present: set[int] = set()
        for vc in guild.voice_channels:
            if vc == guild.afk_channel or vc.name == config.AFK_CHANNEL_NAME:
                continue
            humans = [m for m in vc.members if not m.bot]
            # a lone member normally earns nothing, except perk members who count even when alone
            if len(humans) < 2 and not any(m.id in config.VOICE_PERK_USER_IDS for m in humans):
                continue
            for m in humans:
                present.add(m.id)
                v = m.voice
                if not v or v.self_mute or v.self_deaf or v.mute or v.deaf:
                    continue
                if self._excluded(m, vc):
                    continue
                n = self._voice_mins.get(m.id, 0) + 1
                if n < config.VOICE_BLOCK_MINUTES:
                    self._voice_mins[m.id] = n
                    continue
                self._voice_mins[m.id] = 0
                try:
                    res = await self.bot.db.award_voice(m.id, config.VOICE_BLOCK_MINUTES)
                    if res:
                        await self.after_xp(m, res.old_xp, res.new_xp)
                except Exception:
                    log.exception("voice award failed for %s", m)
        for uid in [u for u in self._voice_mins if u not in present]:   # left voice: forget the partial block
            del self._voice_mins[uid]

    @voice_tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------- commands
    @app_commands.command(name="rank", description="Show your (or someone's) level card")
    @app_commands.describe(member="Whose profile to show")
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        await interaction.response.defer()
        embed, file = await build_profile(self.bot, member or interaction.user)
        await interaction.followup.send(embed=embed, file=file)

    @app_commands.command(name="leaderboard", description="Top members by XP")
    async def leaderboard(self, interaction: discord.Interaction):
        await send_leaderboard(self.bot, interaction, ephemeral=False)

    @app_commands.command(name="levels", description="Show the rank ladder")
    async def levels(self, interaction: discord.Interaction):
        await interaction.response.defer()           # answer inside Discord's 3 s window; the banner upload can take longer
        await interaction.followup.send(**ladder_message())


async def setup(bot):
    await bot.add_cog(Leveling(bot))
