"""Moderation: warn / note / timeout / kick / ban / unban, numbered cases, member reports, auto-moderation.

Rules live in services/moderation.py; this file carries them out on Discord. Safety rules enforced for every action:
you can't act on yourself, the owner, bots, someone above you, or (unless you are a Sect Admin) staff; the bot's own role must
be above the target; everything is recorded as a case, in the staff mod-log channel, and in the audit log.
"""
import asyncio
import logging
import re
import time
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import alerts, config
from ..checks import admin_role, is_admin, is_staff, require_admin, staff_role_ids
from ..mod_ui import case_embed, case_line, dm_text, report_embed
from ..services import moderation as rules
from ..services.moderation import ModSettings, RaidDetector, SpamDetector, format_duration, parse_duration

log = logging.getLogger("orion.moderation")
utcnow = discord.utils.utcnow
NO_REASON = "No reason given."
LINK_RE = re.compile(r"^https://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/\d+/\d+$")


class ReportView(discord.ui.View):
    """Buttons under a report in the mod log. Persistent: they keep working after a restart."""

    def __init__(self, cog: "Moderation"):
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if is_staff(i.user, self.cog.bot.db):
            return True
        await i.response.send_message("Only staff can handle reports.", ephemeral=True)
        return False

    async def _set(self, i: discord.Interaction, status: str):
        db = self.cog.bot.db
        rep = await db.mod_report_by_message(i.message.id)
        if rep is None:
            return await i.response.send_message("I can't find that report any more.", ephemeral=True)
        await db.mod_report_update(rep["id"], status, i.user.id)
        await db.record_audit(i.user.id, f"report.{status}", "report", rep["id"])
        rep = await db.mod_report_get(rep["id"])
        await i.response.edit_message(embed=report_embed(rep), view=self if status == "reviewing" else None)

    @discord.ui.button(label="Reviewing", emoji="🔎", style=discord.ButtonStyle.primary, custom_id="modrep:review")
    async def review(self, i: discord.Interaction, _):
        await self._set(i, "reviewing")

    @discord.ui.button(label="Close", emoji="✅", style=discord.ButtonStyle.success, custom_id="modrep:close")
    async def close(self, i: discord.Interaction, _):
        await self._set(i, "closed")

    @discord.ui.button(label="Dismiss", emoji="🗑️", style=discord.ButtonStyle.secondary, custom_id="modrep:dismiss")
    async def dismiss(self, i: discord.Interaction, _):
        await self._set(i, "dismissed")


class Moderation(commands.Cog):
    mod = app_commands.Group(name="mod", description="Moderation settings (Sect Admin)")

    def __init__(self, bot):
        self.bot = bot
        self.spam = SpamDetector(ModSettings())
        self.raid = RaidDetector(8, 30)
        self._alerted: dict[tuple, float] = {}

    async def cog_load(self):
        self.bot.add_view(ReportView(self))
        self.tempbans.start()

    async def cog_unload(self):
        self.tempbans.cancel()

    @property
    def db(self):
        return self.bot.db

    def settings(self) -> ModSettings:
        return rules.load_settings(self.db.get)

    # ----------------------------------------------------------- shared helpers
    async def _gate(self, i: discord.Interaction, action: str) -> bool:
        """Staff only; actions on the admin-only list also need the Sect Admin role (or the Patriarch)."""
        if not is_staff(i.user, self.db):
            await i.response.send_message("Only staff can use this.", ephemeral=True)
            return False
        if rules.required_level(action, self.settings().admin_only) == "admin" and not is_admin(i.user):
            await i.response.send_message(f"`{action}` needs the **{config.ADMIN_ROLE_NAME}** role.", ephemeral=True)
            return False
        return True

    def _problem(self, actor: discord.Member, target: discord.Member, needs_bot: bool = True) -> Optional[str]:
        g = actor.guild
        return rules.hierarchy_problem(
            actor_id=actor.id, actor_top=actor.top_role.position, actor_is_admin=is_admin(actor),
            target_id=target.id, target_top=target.top_role.position, target_is_owner=target.id == g.owner_id
            or target.id == config.OWNER_ID, target_is_admin=is_admin(target), target_is_staff=is_staff(target, self.db),
            target_is_bot=target.bot, bot_id=g.me.id, bot_top=g.me.top_role.position, needs_bot_hierarchy=needs_bot)

    def _bot_can(self, guild: discord.Guild, perm: str) -> bool:
        return getattr(guild.me.guild_permissions, perm, False)

    async def _log(self, guild: discord.Guild, *, embed: discord.Embed, content: str | None = None,
                   view: discord.ui.View | None = None, ping: bool = False) -> Optional[discord.Message]:
        ch = guild.get_channel(self.db.get("mod_log_channel_id", 0))
        if ch is None:
            await alerts.alert_staff(self.bot, guild, "No moderation log channel is set. Run `/mod setup` so cases and "
                                     "reports have somewhere to go.", key="mod-no-log-channel")
            return None
        roles = []
        if ping:
            roles = [r for r in [admin_role(guild)] + [guild.get_role(x) for x in staff_role_ids(self.db)] if r]
            content = " ".join(r.mention for r in roles) + (f" {content}" if content else "")
        try:
            return await ch.send(content=content, embed=embed, view=view,
                                 allowed_mentions=discord.AllowedMentions(roles=roles, users=False))
        except discord.HTTPException:
            log.warning("Could not post to the mod log", exc_info=True)
            return None

    async def _dm(self, user: discord.abc.User, text: str) -> bool:
        if not self.settings().dm:
            return False
        try:
            await user.send(text)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _record(self, guild: discord.Guild, action: str, target_id: int, moderator_id: Optional[int], reason: str, *,
                      source: str = "manual", duration: Optional[int] = None, report_id: Optional[int] = None):
        """Create the case, post it to the mod log, write the audit entry. Returns the case row."""
        expires = utcnow() + timedelta(seconds=duration) if duration else None
        c = await self.db.mod_add_case(action, target_id, moderator_id, reason or NO_REASON, source=source,
                                       duration_seconds=duration, expires_at=expires, report_id=report_id)
        msg = await self._log(guild, embed=case_embed(c))
        if msg:
            await self.db.mod_set_log_message(c["id"], msg.id)
        await self.db.record_audit(moderator_id, f"mod.{action}", "member", target_id, case=c["id"], reason=(reason or "")[:200],
                                   source=source)
        return c

    def _reply(self, c, extra: str = "") -> str:
        return f"✅ Case **#{c['id']}** · {c['action']} recorded.{extra}"

    # ------------------------------------------------------------------ commands
    @app_commands.command(name="warn", description="Warn a member (creates a case)")
    @app_commands.describe(member="Who to warn", reason="Why (the member sees this)")
    async def warn(self, i: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 500]):
        if not await self._gate(i, "warn"):
            return
        if p := self._problem(i.user, member, needs_bot=False):
            return await i.response.send_message(f"⚠️ {p}", ephemeral=True)
        await i.response.defer(ephemeral=True)
        c = await self._record(i.guild, "warn", member.id, i.user.id, reason)
        dm_ok = await self._dm(member, dm_text("warn", i.guild.name, c["id"], reason))
        extra = "" if dm_ok else " (I couldn't DM them.)"
        s = self.settings()
        n = await self.db.mod_active_warns(member.id, utcnow() - timedelta(days=s.warn_window_days))
        if rules.escalation_due(n, s.escalate_every):
            extra += await self._escalate(i.guild, member, n, s)
        await i.followup.send(self._reply(c, f" They now have **{n}** active warning(s).{extra}"), ephemeral=True)

    async def _escalate(self, guild: discord.Guild, member: discord.Member, warns: int, s: ModSettings) -> str:
        secs = s.escalate_minutes * 60
        reason = f"Automatic: {warns} active warnings in {s.warn_window_days} days."
        try:
            await member.timeout(timedelta(seconds=secs), reason=reason)
        except discord.HTTPException:
            await alerts.alert_staff(self.bot, guild, f"{member.mention} reached {warns} warnings but I couldn't time them "
                                     f"out (check my role position and Moderate Members).", key=f"mod-escalate-{member.id}")
            return " ⚠️ Auto-timeout failed (see the alerts channel)."
        c = await self._record(guild, "timeout", member.id, None, reason, source="escalation", duration=secs)
        await self._dm(member, dm_text("timeout", guild.name, c["id"], reason, secs, c["expires_at"]))
        return f" ⏱️ Auto-timeout applied for {format_duration(secs)} (case #{c['id']})."

    @app_commands.command(name="note", description="Add a private staff note about a member (the member is not told)")
    async def note(self, i: discord.Interaction, member: discord.Member, note: app_commands.Range[str, 1, 500]):
        if not await self._gate(i, "note"):
            return
        await i.response.defer(ephemeral=True)
        c = await self._record(i.guild, "note", member.id, i.user.id, note)
        await i.followup.send(self._reply(c), ephemeral=True)

    @app_commands.command(name="timeout", description="Time a member out (they can't talk or join voice)")
    @app_commands.describe(duration="e.g. 30m, 2h, 7d (max 28d)")
    async def timeout(self, i: discord.Interaction, member: discord.Member, duration: str,
                      reason: app_commands.Range[str, 1, 500]):
        if not await self._gate(i, "timeout"):
            return
        try:
            secs = parse_duration(duration)
        except ValueError as e:
            return await i.response.send_message(f"⚠️ {e}", ephemeral=True)
        if secs > rules.MAX_TIMEOUT_SECONDS:
            return await i.response.send_message("⚠️ Discord allows timeouts of at most 28 days.", ephemeral=True)
        if p := self._problem(i.user, member):
            return await i.response.send_message(f"⚠️ {p}", ephemeral=True)
        if not self._bot_can(i.guild, "moderate_members"):
            return await i.response.send_message("⚠️ I need the **Moderate Members** permission.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        try:
            await member.timeout(timedelta(seconds=secs), reason=f"{i.user}: {reason}"[:400])
        except discord.HTTPException as e:
            return await i.followup.send(f"⚠️ Discord refused: {e.text}", ephemeral=True)
        c = await self._record(i.guild, "timeout", member.id, i.user.id, reason, duration=secs)
        dm_ok = await self._dm(member, dm_text("timeout", i.guild.name, c["id"], reason, secs, c["expires_at"]))
        await i.followup.send(self._reply(c, "" if dm_ok else " (I couldn't DM them.)"), ephemeral=True)

    @app_commands.command(name="untimeout", description="End a member's timeout early")
    async def untimeout(self, i: discord.Interaction, member: discord.Member,
                        reason: app_commands.Range[str, 1, 500] = NO_REASON):
        if not await self._gate(i, "untimeout"):
            return
        if p := self._problem(i.user, member):
            return await i.response.send_message(f"⚠️ {p}", ephemeral=True)
        await i.response.defer(ephemeral=True)
        try:
            await member.timeout(None, reason=f"{i.user}: {reason}"[:400])
        except discord.HTTPException as e:
            return await i.followup.send(f"⚠️ Discord refused: {e.text}", ephemeral=True)
        await self.db.mod_revoke_active(member.id, "timeout", i.user.id, reason)
        c = await self._record(i.guild, "untimeout", member.id, i.user.id, reason)
        await i.followup.send(self._reply(c), ephemeral=True)

    @app_commands.command(name="kick", description="Kick a member (Sect Admin by default)")
    async def kick(self, i: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 500]):
        if not await self._gate(i, "kick"):
            return
        if p := self._problem(i.user, member):
            return await i.response.send_message(f"⚠️ {p}", ephemeral=True)
        if not self._bot_can(i.guild, "kick_members"):
            return await i.response.send_message("⚠️ I need the **Kick Members** permission.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        dm_ok = await self._dm(member, dm_text("kick", i.guild.name, None, reason))      # must be before: afterwards we share no server
        try:
            await member.kick(reason=f"{i.user}: {reason}"[:400])
        except discord.HTTPException as e:
            return await i.followup.send(f"⚠️ Discord refused: {e.text}" + (" (They were already sent the DM.)" if dm_ok else ""),
                                         ephemeral=True)
        c = await self._record(i.guild, "kick", member.id, i.user.id, reason)
        await i.followup.send(self._reply(c, "" if dm_ok else " (I couldn't DM them.)"), ephemeral=True)

    @app_commands.command(name="ban", description="Ban a member or a user ID (Sect Admin by default)")
    @app_commands.describe(user="Member or user", duration="Leave empty for a permanent ban, or e.g. 7d for a temporary one",
                           delete_days="Also delete their messages from the last N days (0-7)")
    async def ban(self, i: discord.Interaction, user: discord.User, reason: app_commands.Range[str, 1, 500],
                  duration: str | None = None, delete_days: app_commands.Range[int, 0, 7] = 0):
        if not await self._gate(i, "ban"):
            return
        secs = None
        if duration:
            try:
                secs = parse_duration(duration)
            except ValueError as e:
                return await i.response.send_message(f"⚠️ {e}", ephemeral=True)
            if secs > rules.MAX_TEMPBAN_SECONDS:
                return await i.response.send_message("⚠️ A temporary ban can be at most 365 days. Leave the duration empty "
                                                     "for a permanent one.", ephemeral=True)
        member = i.guild.get_member(user.id)
        if member and (p := self._problem(i.user, member)):
            return await i.response.send_message(f"⚠️ {p}", ephemeral=True)
        if user.id in (i.user.id, i.guild.me.id, i.guild.owner_id, config.OWNER_ID):
            return await i.response.send_message("⚠️ You can't ban that account.", ephemeral=True)
        if not self._bot_can(i.guild, "ban_members"):
            return await i.response.send_message("⚠️ I need the **Ban Members** permission.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        try:
            await i.guild.fetch_ban(user)
            return await i.followup.send("That user is already banned.", ephemeral=True)
        except discord.NotFound:
            pass
        expires = utcnow() + timedelta(seconds=secs) if secs else None
        dm_ok = await self._dm(user, dm_text("ban", i.guild.name, None, reason, secs, expires)) if member else False
        try:
            await i.guild.ban(user, reason=f"{i.user}: {reason}"[:400], delete_message_seconds=delete_days * 86400)
        except discord.HTTPException as e:
            return await i.followup.send(f"⚠️ Discord refused: {e.text}", ephemeral=True)
        c = await self._record(i.guild, "ban", user.id, i.user.id, reason, duration=secs)
        note = (f" Temporary: they are unbanned automatically {format_duration(secs)} from now." if secs else "") \
            + ("" if dm_ok or not member else " (I couldn't DM them.)")
        await i.followup.send(self._reply(c, note), ephemeral=True)

    @app_commands.command(name="unban", description="Unban a user by ID (Sect Admin by default)")
    @app_commands.describe(user_id="The user's ID")
    async def unban(self, i: discord.Interaction, user_id: str, reason: app_commands.Range[str, 1, 500] = NO_REASON):
        if not await self._gate(i, "unban"):
            return
        if not user_id.strip().isdigit():
            return await i.response.send_message("⚠️ Paste the numeric user ID.", ephemeral=True)
        uid = int(user_id)
        await i.response.defer(ephemeral=True)
        try:
            await i.guild.unban(discord.Object(uid), reason=f"{i.user}: {reason}"[:400])
        except discord.NotFound:
            return await i.followup.send("That user isn't banned.", ephemeral=True)
        except discord.HTTPException as e:
            return await i.followup.send(f"⚠️ Discord refused: {e.text}", ephemeral=True)
        await self.db.mod_revoke_active(uid, "ban", i.user.id, reason)
        c = await self._record(i.guild, "unban", uid, i.user.id, reason)
        await i.followup.send(self._reply(c), ephemeral=True)

    @app_commands.command(name="pardon", description="Remove a warning so it no longer counts (Sect Admin by default)")
    async def pardon(self, i: discord.Interaction, case: int, reason: app_commands.Range[str, 1, 300] = NO_REASON):
        if not await self._gate(i, "pardon"):
            return
        c = await self.db.mod_get_case(case)
        if c is None or c["action"] != "warn":
            return await i.response.send_message("That isn't a warning case.", ephemeral=True)
        if not await self.db.mod_revoke_case(case, i.user.id, reason):
            return await i.response.send_message("That warning was already pardoned.", ephemeral=True)
        await self.db.record_audit(i.user.id, "mod.pardon", "case", case, reason=reason[:200])
        await self._log(i.guild, embed=case_embed(await self.db.mod_get_case(case)),
                        content=f"🕊️ Warning pardoned by {i.user.mention}")
        await i.response.send_message(f"✅ Warning #{case} pardoned.", ephemeral=True)

    # ---------------------------------------------------------------- case views
    @app_commands.command(name="case", description="Show one moderation case")
    async def case(self, i: discord.Interaction, number: int):
        if not is_staff(i.user, self.db):
            return await i.response.send_message("Only staff can use this.", ephemeral=True)
        c = await self.db.mod_get_case(number)
        if c is None:
            return await i.response.send_message("No case with that number.", ephemeral=True)
        await i.response.send_message(embed=case_embed(c), ephemeral=True)

    @app_commands.command(name="case-reason", description="Change the reason written on a case")
    async def case_reason(self, i: discord.Interaction, number: int, reason: app_commands.Range[str, 1, 500]):
        if not is_staff(i.user, self.db):
            return await i.response.send_message("Only staff can use this.", ephemeral=True)
        if not await self.db.mod_set_reason(number, reason):
            return await i.response.send_message("No case with that number.", ephemeral=True)
        await self.db.record_audit(i.user.id, "mod.case_reason", "case", number, reason=reason[:200])
        await i.response.send_message(f"✅ Case #{number} updated.", ephemeral=True)

    @app_commands.command(name="cases", description="A member's moderation history (staff)")
    async def cases(self, i: discord.Interaction, user: discord.User):
        if not is_staff(i.user, self.db):
            return await i.response.send_message("Only staff can use this.", ephemeral=True)
        rows = await self.db.mod_cases_for(user.id, 12)
        summary = await self.db.mod_summary(user.id)
        e = discord.Embed(title=f"Moderation history · {user}", color=config.COLOR_MAIN,
                          description="\n".join(case_line(c) for c in rows)[:3900] or "*Clean record.*")
        e.set_footer(text=f"{summary['total']} case(s) · {summary['active_warns']} active warning(s)")
        await i.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="warnings", description="See warnings (yours, or a member's if you are staff)")
    async def warnings(self, i: discord.Interaction, member: discord.User | None = None):
        target = member or i.user
        if target.id != i.user.id and not is_staff(i.user, self.db):
            return await i.response.send_message("You can only look at your own warnings.", ephemeral=True)
        rows = [c for c in await self.db.mod_cases_for(target.id, 25) if c["action"] == "warn"][:10]
        if not rows:
            return await i.response.send_message("No warnings. ✨", ephemeral=True)
        staff_view = is_staff(i.user, self.db)
        await i.response.send_message(embed=discord.Embed(
            title=f"Warnings · {target.display_name if hasattr(target, 'display_name') else target}", color=config.COLOR_MAIN,
            description="\n".join(case_line(c, show_moderator=staff_view) for c in rows)[:3900]), ephemeral=True)

    # ------------------------------------------------------------------- reports
    @app_commands.command(name="report", description="Report a member to the staff (private)")
    @app_commands.describe(member="Who you are reporting", reason="What happened", message_link="Link to the message (optional)")
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.user.id)
    async def report(self, i: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 10, 800],
                     message_link: str | None = None):
        if member.id == i.user.id or member.bot:
            return await i.response.send_message("You can't report yourself or a bot.", ephemeral=True)
        link = None
        if message_link:
            m = LINK_RE.match(message_link.strip())
            if not m or int(m.group(1)) != i.guild_id:
                return await i.response.send_message("The message link must be a Discord link to a message in this server.",
                                                     ephemeral=True)
            link = message_link.strip()
        if await self.db.mod_report_open_count(i.user.id) >= 5:
            return await i.response.send_message("You already have 5 open reports. Please wait for staff to handle them.",
                                                 ephemeral=True)
        if await self.db.mod_report_recent_duplicate(i.user.id, member.id, utcnow() - timedelta(hours=24)):
            return await i.response.send_message("You already reported that member in the last 24 hours.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        r = await self.db.mod_report_add(i.user.id, member.id, reason, link)
        msg = await self._log(i.guild, embed=report_embed(r), view=ReportView(self), ping=True)
        if msg:
            await self.db.mod_report_set_message(r["id"], msg.id)
        await i.followup.send(f"✅ Thank you. Report **#{r['id']}** was sent to the staff." if msg else
                              f"✅ Report **#{r['id']}** is saved, but the staff channel isn't set up yet.", ephemeral=True)

    @app_commands.command(name="reports", description="Open reports (staff)")
    async def reports(self, i: discord.Interaction):
        if not is_staff(i.user, self.db):
            return await i.response.send_message("Only staff can use this.", ephemeral=True)
        rows = await self.db.mod_report_list(limit=15)
        if not rows:
            return await i.response.send_message("No open reports. ✨", ephemeral=True)
        lines = [f"`#{r['id']}` **{r['status']}** · <@{r['target_id']}> · reported by <@{r['reporter_id']}> · "
                 f"<t:{int(r['created_at'].timestamp())}:R>\n└ {r['reason'][:100]}" for r in rows]
        await i.response.send_message(embed=discord.Embed(title="Open reports", color=config.COLOR_MAIN,
                                                          description="\n".join(lines)[:3900]), ephemeral=True)

    # ------------------------------------------------------------- /mod settings
    @mod.command(name="setup", description="Create the staff-only mod-log channel")
    @app_commands.check(require_admin)
    async def setup_(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        g = i.guild
        ch = g.get_channel(self.db.get("mod_log_channel_id", 0))
        if ch is None:
            hidden = {g.default_role: discord.PermissionOverwrite(view_channel=False),
                      g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                        read_message_history=True, mention_everyone=True)}
            for role in [admin_role(g)] + [g.get_role(r) for r in sorted(staff_role_ids(self.db))]:
                if role:
                    hidden[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
            try:
                ch = await g.create_text_channel("🛡️・mod-log", overwrites=hidden, topic="Moderation cases and reports (staff only)")
            except discord.HTTPException as e:
                return await i.followup.send(f"⚠️ Couldn't create the channel: {e.text}", ephemeral=True)
            await self.db.set("mod_log_channel_id", ch.id)
        await self.db.record_audit(i.user.id, "mod.setup", "channel", ch.id)
        miss = [n for n in ("kick_members", "ban_members", "moderate_members", "manage_messages")
                if not self._bot_can(g, n)]
        await i.followup.send(f"✅ Mod log: {ch.mention}." + (f"\n⚠️ I'm missing: {', '.join(m.replace('_', ' ').title() for m in miss)}. "
                              "Re-invite me with the new link from the README." if miss else ""), ephemeral=True)

    @mod.command(name="status", description="Show the moderation settings")
    @app_commands.check(require_admin)
    async def status(self, i: discord.Interaction):
        s = self.settings()
        e = discord.Embed(title="🛡️ Moderation", color=config.COLOR_MAIN)
        ch = i.guild.get_channel(self.db.get("mod_log_channel_id", 0))
        e.add_field(name="Mod log", value=ch.mention if ch else "⚠️ not set: run `/mod setup`", inline=False)
        e.add_field(name="Admin-only actions", value=", ".join(sorted(s.admin_only)) or "none", inline=False)
        e.add_field(name="Spam protection", value=f"**{s.spam}** · {s.spam_rate_count} msgs/{s.spam_rate_seconds}s · "
                    f"{s.spam_dup_count} copies/{s.spam_dup_seconds}s · {s.spam_mentions}+ mentions · timeout {s.spam_timeout_minutes}m")
        e.add_field(name="Raid protection", value=f"**{s.raid}** · {s.raid_joins} joins/{s.raid_seconds}s · "
                    f"accounts under {s.raid_min_age_days}d get a {s.raid_timeout_minutes}m timeout when set to `act`")
        e.add_field(name="Warning escalation", value=(f"every **{s.escalate_every}** warnings in {s.warn_window_days}d → "
                    f"timeout {s.escalate_minutes}m" if s.escalate_every else "off (staff decide)"), inline=False)
        e.add_field(name="DM members", value="yes" if s.dm else "no", inline=True)
        e.add_field(name="Exempt", value=f"{len(s.exempt_channels)} channel(s), {len(s.exempt_roles)} role(s)", inline=True)
        await i.response.send_message(embed=e, ephemeral=True)

    @mod.command(name="set", description="Change a moderation setting")
    @app_commands.describe(setting="What to change", value="New value (spam/raid: off, alert or act; rates like 6/5)")
    @app_commands.choices(setting=[app_commands.Choice(name=n, value=n) for n in rules.SETTING_NAMES])
    @app_commands.check(require_admin)
    async def set_(self, i: discord.Interaction, setting: app_commands.Choice[str], value: str):
        try:
            changes = rules.parse_setting(setting.value, value)
        except ValueError as e:
            return await i.response.send_message(f"⚠️ {e}", ephemeral=True)
        for k, v in changes.items():
            await self.db.set(k, v)
        await self.db.record_audit(i.user.id, "mod.set", "config", setting.value, changes=changes)
        await i.response.send_message(f"✅ **{setting.value}** updated.", ephemeral=True)

    @mod.command(name="exempt", description="Toggle a channel or role being skipped by auto-moderation")
    @app_commands.check(require_admin)
    async def exempt(self, i: discord.Interaction, channel: discord.TextChannel | None = None,
                     role: discord.Role | None = None):
        if not (channel or role):
            return await i.response.send_message("Pick a channel or a role.", ephemeral=True)
        out = []
        for key, obj in (("automod_exempt_channels", channel), ("automod_exempt_roles", role)):
            if obj is None:
                continue
            cur = [int(x) for x in self.db.get(key, [])]
            on = obj.id not in cur
            cur = cur + [obj.id] if on else [x for x in cur if x != obj.id]
            await self.db.set(key, cur)
            out.append(f"{obj.mention} is now **{'exempt' if on else 'checked again'}**")
        await self.db.record_audit(i.user.id, "mod.exempt", "config", None, changed=out)
        await i.response.send_message("✅ " + "; ".join(out), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ---------------------------------------------------------------- auto-moderation
    def _exempt(self, member: discord.Member, channel_id: int, s: ModSettings) -> bool:
        return (is_staff(member, self.db) or channel_id in s.exempt_channels
                or any(r.id in s.exempt_roles for r in member.roles))

    def _throttled(self, key: tuple, seconds: float = 60) -> bool:
        now = time.monotonic()
        if now - self._alerted.get(key, -1e9) < seconds:
            return True
        self._alerted[key] = now
        if len(self._alerted) > 2000:
            self._alerted = {k: v for k, v in self._alerted.items() if now - v < seconds}
        return False

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if (msg.guild is None or msg.guild.id != config.GUILD_ID or msg.author.bot
                or not isinstance(msg.author, discord.Member)):
            return
        s = self.settings()
        if s.spam == "off" or self._exempt(msg.author, msg.channel.id, s):
            return
        self.spam.update(s)
        mentions = len({m.id for m in msg.mentions}) + len(msg.role_mentions)
        hit = self.spam.check(msg.author.id, msg.content, mentions, msg.channel.id, msg.id, time.monotonic())
        if hit:
            try:
                await self._handle_spam(msg, hit[0], hit[1], s)
            except Exception:
                log.exception("Auto-moderation failed on a spam hit")

    async def _handle_spam(self, msg: discord.Message, kind: str, refs: list, s: ModSettings):
        member, guild = msg.author, msg.guild
        if self._throttled(("spam", member.id)):
            return
        acted = ""
        if s.spam == "act" and self._bot_can(guild, "moderate_members") and not member.is_timed_out():
            for ch_id, m_id in set(refs):
                ch = guild.get_channel(ch_id)
                if ch and self._bot_can(guild, "manage_messages"):
                    try:
                        await ch.get_partial_message(m_id).delete()
                    except discord.HTTPException:
                        pass
            secs = s.spam_timeout_minutes * 60
            reason = f"Automatic: {kind}."
            try:
                await member.timeout(timedelta(seconds=secs), reason=reason)
                c = await self._record(guild, "timeout", member.id, None, reason, source="automod", duration=secs)
                await self._dm(member, dm_text("timeout", guild.name, c["id"], reason, secs, c["expires_at"]))
                acted = f" Messages removed and member timed out for {format_duration(secs)} (case #{c['id']})."
            except discord.HTTPException:
                acted = " ⚠️ I tried to act but couldn't (check my role position and permissions)."
        e = discord.Embed(title="🚨 Possible spam", color=0xF59E0B,
                          description=f"{member.mention} · **{kind}** in {msg.channel.mention}.{acted or ' Alert only: no action taken.'}")
        e.add_field(name="Jump", value=msg.jump_url, inline=False)
        e.set_footer(text=f"User ID {member.id}")
        await self._log(guild, embed=e)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild.id != config.GUILD_ID or member.bot:
            return
        s = self.settings()
        if s.raid == "off":
            return
        self.raid.update(s.raid_joins, s.raid_seconds)
        burst = self.raid.record(member.id, member.created_at.timestamp(), time.time())
        if burst:
            try:
                await self._handle_raid(member.guild, burst, s)
            except Exception:
                log.exception("Auto-moderation failed on a raid alert")

    async def _handle_raid(self, guild: discord.Guild, burst: list, s: ModSettings):
        now = time.time()
        young = [u for u, created in burst if (now - created) < s.raid_min_age_days * 86400]
        acted = []
        if s.raid == "act" and self._bot_can(guild, "moderate_members"):
            secs = s.raid_timeout_minutes * 60
            for uid in young:
                m = guild.get_member(uid)
                if m is None or m.is_timed_out() or self._exempt(m, 0, s):
                    continue
                try:
                    await m.timeout(timedelta(seconds=secs), reason="Automatic: suspected raid (new account)")
                    c = await self._record(guild, "timeout", uid, None, "Automatic: suspected raid (new account).",
                                           source="automod", duration=secs)
                    acted.append(c["id"])
                except discord.HTTPException:
                    continue
        sample = " ".join(f"<@{u}>" for u, _ in burst[:15]) + (" …" if len(burst) > 15 else "")
        e = discord.Embed(title="🚨 Possible raid", color=0xDC2626,
                          description=f"**{len(burst)}** members joined within {s.raid_seconds}s; **{len(young)}** of those accounts "
                                      f"are under {s.raid_min_age_days} days old.\n{sample}")
        e.add_field(name="Action", value=(f"Timed out {len(acted)} new account(s) for {format_duration(s.raid_timeout_minutes * 60)}."
                                          if acted else "Alert only: no action taken. Use `/mod set raid act` to time out young accounts "
                                          "automatically, or ban them with `/ban`."), inline=False)
        await self._log(guild, embed=e, ping=True)

    # --------------------------------------------------------------- temporary bans
    @tasks.loop(seconds=60)
    async def tempbans(self):
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return
        for c in await self.db.mod_due_tempbans(10):
            try:
                await guild.unban(discord.Object(c["target_id"]), reason=f"Temporary ban (case #{c['id']}) expired")
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.warning("Could not lift temporary ban %s", c["id"], exc_info=True)
                await alerts.alert_staff(self.bot, guild, f"I couldn't lift the temporary ban in case #{c['id']} "
                                         f"(<@{c['target_id']}>). Check my Ban Members permission.", key=f"tempban-{c['id']}")
                continue
            await self.db.mod_revoke_case(c["id"], None, "Temporary ban expired")
            await self._record(guild, "unban", c["target_id"], None, f"Temporary ban from case #{c['id']} expired.",
                               source="system")

    @tempbans.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    @tempbans.error
    async def _tempbans_error(self, exc):
        log.exception("Temporary-ban timer stopped", exc_info=exc)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
