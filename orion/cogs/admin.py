import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import config, roles
from ..checks import admin_role, require_admin, require_staff, require_xp_manager, staff_role_ids
from types import SimpleNamespace as NS

from ..levels import level_from_xp, rank_name
from ..services import cultivation
from ..ui import PanelView, panel_embed

log = logging.getLogger("orion.admin")
CAT_NAME = "🌌・Cultivation"
LEVELUP_NAME = "📣・realm-announcements"
PANEL_NAME = "🏆・cultivation-leaderboard"


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cmd = interaction.command
        root = getattr(cmd, "root_parent", None) or cmd
        if root is not None and root.name == "xp":
            return await require_xp_manager(interaction)  # /xp ... -> Sect Admin / Patriarch only
        return await require_staff(interaction)

    xp = app_commands.Group(name="xp", description="Manage member XP (Sect Admin / Patriarch only)")

    # ---------------------------------------------------------------- setup
    @app_commands.command(name="setup", description="Create the levelling category, channels, roles and panel")
    @app_commands.check(require_admin)
    async def setup_cmd(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        db, guild, me = self.bot.db, i.guild, i.guild.me
        await roles.ensure_level_roles(guild)

        locked = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=False, add_reactions=False,
                create_public_threads=False, create_private_threads=False, send_messages_in_threads=False),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                            attach_files=True, read_message_history=True),
        }

        async def ensure(key: str, name: str, **kw):
            ch = guild.get_channel(db.get(key, 0))
            if ch is None:
                ch = await (guild.create_category(name, overwrites=locked) if kw.get("category") is None and key == "category_id"
                            else guild.create_text_channel(name, overwrites=locked, **kw))
                await db.set(key, ch.id)
            return ch

        cat = await ensure("category_id", CAT_NAME)
        lvl = await ensure("levelup_channel_id", LEVELUP_NAME, category=cat,
                           topic="Rank and realm advancements")
        pnl = await ensure("panel_channel_id", PANEL_NAME, category=cat,
                           topic="Your profile, rank and the leaderboard")

        alerts_ch = guild.get_channel(db.get("alert_channel_id", 0))
        if alerts_ch is None:      # private channel: staff + the bot only
            hidden = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                      me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                      read_message_history=True)}
            staff = admin_role(guild)
            staff_roles = ([staff] if staff else []) + [guild.get_role(r) for r in sorted(staff_role_ids(db))]
            for role in staff_roles:
                if role:
                    hidden[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
            alerts_ch = await guild.create_text_channel(
                "🔔・orion-alerts", category=cat, overwrites=hidden,
                topic="Orion reports problems here (staff only)")
            await db.set("alert_channel_id", alerts_ch.id)

        logs_ch = guild.get_channel(db.get("cultivation_log_channel_id", 0))
        if logs_ch is None:        # staff-only progression / XP-adjustment log
            hidden = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                      me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                      read_message_history=True)}
            for role in ([admin_role(guild)] + [guild.get_role(r) for r in sorted(staff_role_ids(db))]):
                if role:
                    hidden[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
            logs_ch = await guild.create_text_channel("📜・cultivation-logs", category=cat, overwrites=hidden,
                                                      topic="Realm transitions and XP adjustments (staff only)")
            await db.set("cultivation_log_channel_id", logs_ch.id)

        view = PanelView(self.bot)
        msg = None
        if db.get("panel_message_id"):
            try:
                msg = await pnl.fetch_message(db.get("panel_message_id"))
                await msg.edit(embed=panel_embed(db), view=view)
            except discord.NotFound:
                msg = None
        if msg is None:
            msg = await pnl.send(embed=panel_embed(db), view=view)
            await db.set("panel_message_id", msg.id)

        wc = None
        if not db.get("welcome_channel_id"):
            wc = guild.system_channel or next((c for c in guild.text_channels if "welcome" in c.name.lower()), None)
            if wc:
                await db.set("welcome_channel_id", wc.id)

        given, failed, problem = await roles.assign_base_role(guild)
        if problem:
            base_line = f"• ⚠️ Couldn't give members **Earth King**: {problem}"
        else:
            base_line = f"• Gave **Earth King** to {given} member(s)" + (f" ({failed} failed)" if failed else "")

        await i.followup.send(
            f"✅ Realm roles ready.\n• Realm announcements: {lvl.mention}\n• Leaderboard: {pnl.mention}\n"
            f"• Welcome channel: {wc.mention if wc else 'unchanged (use `/welcome channel` if none is set)'}\n"
            f"{base_line}\n• Cultivation logs: {logs_ch.mention}\n• Staff alerts: {alerts_ch.mention}\n\n"
            "Make sure my role sits **above** the level roles (Earth King … Eternal True God) in Server Settings → Roles.",
            ephemeral=True)

    # ------------------------------------------------------------- xp group
    async def _apply(self, i: discord.Interaction, member: discord.Member, mode: str, amount: int):
        change = await cultivation.adjust_member_xp(self.bot.db, i.user.id, member.id, mode, amount)
        res = NS(old_xp=change.old_xp, new_xp=change.new_xp)
        log_cog = self.bot.get_cog("Leveling")
        if log_cog:
            await log_cog.post_log(member.guild, f"🛠️ {i.user.mention} **{mode}** {amount:,} XP · {member.mention}: "
                                   f"{change.old_xp:,} → {change.new_xp:,} ({change.new_rank})")
        cog = self.bot.get_cog("Leveling")
        warn = ""
        try:
            await roles.sync_member_role(member, level_from_xp(res.new_xp), strict=True)
        except roles.RoleSyncError as e:
            warn = f"\n⚠️ XP saved, but I couldn't update their role: {e}"
        await i.followup.send(
            f"{member.mention}: {res.old_xp:,} → **{res.new_xp:,} XP** "
            f"({rank_name(level_from_xp(res.old_xp))} → {rank_name(level_from_xp(res.new_xp))}){warn}", ephemeral=True)
        if cog and level_from_xp(res.new_xp) > level_from_xp(res.old_xp):
            await cog.announce(member, level_from_xp(res.new_xp), None)

    @xp.command(name="add", description="Give XP to a member")
    async def xp_add(self, i: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1_000_000]):
        await i.response.defer(ephemeral=True)
        await self._apply(i, member, "add", amount)

    @xp.command(name="remove", description="Take XP from a member")
    async def xp_remove(self, i: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1_000_000]):
        await i.response.defer(ephemeral=True)
        await self._apply(i, member, "remove", amount)

    @xp.command(name="set", description="Set a member's total XP")
    async def xp_set(self, i: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 0, 10_000_000]):
        await i.response.defer(ephemeral=True)
        await self._apply(i, member, "set", amount)

    @xp.command(name="reset", description="Wipe a member's XP completely")
    async def xp_reset(self, i: discord.Interaction, member: discord.Member):
        await i.response.defer(ephemeral=True)
        await self.bot.db.reset_user(member.id)
        warn = ""
        try:
            await roles.sync_member_role(member, 0, strict=True)
        except roles.RoleSyncError as e:
            warn = f"\n⚠️ I couldn't update their role: {e}"
        await i.followup.send(f"{member.mention} has been reset.{warn}", ephemeral=True)

    @xp.command(name="exclude-channel", description="Stop XP in a channel or whole category")
    async def ex_ch(self, i: discord.Interaction, channel: discord.TextChannel | discord.VoiceChannel | discord.CategoryChannel):
        await self.bot.db.set_exclusion("channel", channel.id, True)
        await i.response.send_message(f"No XP in {channel.mention}.", ephemeral=True)

    @xp.command(name="include-channel", description="Allow XP in a channel/category again")
    async def in_ch(self, i: discord.Interaction, channel: discord.TextChannel | discord.VoiceChannel | discord.CategoryChannel):
        await self.bot.db.set_exclusion("channel", channel.id, False)
        await i.response.send_message(f"XP re-enabled in {channel.mention}.", ephemeral=True)

    @xp.command(name="exclude-role", description="Members with this role earn no XP")
    async def ex_role(self, i: discord.Interaction, role: discord.Role):
        await self.bot.db.set_exclusion("role", role.id, True)
        await i.response.send_message(f"**{role.name}** no longer earns XP.", ephemeral=True)

    @xp.command(name="include-role", description="Let this role earn XP again")
    async def in_role(self, i: discord.Interaction, role: discord.Role):
        await self.bot.db.set_exclusion("role", role.id, False)
        await i.response.send_message(f"**{role.name}** earns XP again.", ephemeral=True)

    @xp.command(name="exclusions", description="List XP exclusions")
    async def exclusions(self, i: discord.Interaction):
        db = self.bot.db
        ch = ", ".join(f"<#{c}>" for c in db.excluded_channels) or "none"
        rl = ", ".join(f"<@&{r}>" for r in db.excluded_roles) or "none"
        await i.response.send_message(f"Channels/categories: {ch}\nRoles: {rl}", ephemeral=True)

    @xp.command(name="resync-roles", description="Re-apply the correct level role to every member")
    async def resync(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        changed, problem = await roles.resync_all(i.guild, self.bot.db)
        msg = f"Done. Updated {changed} member(s)."
        if problem:
            msg = f"⚠️ Stopped after {changed} update(s): {problem}"
        await i.followup.send(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Admin(bot))
