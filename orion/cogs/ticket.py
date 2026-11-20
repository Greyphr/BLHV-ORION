"""Tickets: private support channels opened from a panel.

/ticket setup      (Sect Admin) creates the Tickets category, the panel channel and the staff-only log channel, adds the default
                   ticket types and posts the panel. Safe to run again: it only creates what is missing.
/ticket panel      post (or move) the panel        /ticket banner   change the panel or ticket banner
Inside a ticket:   close, claim, unclaim, add, remove, rename, priority, transfer, move, note, notes, reopen
Staff:             stats, history, blacklist add/remove/list

How a ticket lives:
  open    a member picks a type on the panel, fills the short form, and gets a private channel (ticket-0042-name)
          that only they and the staff for that type can see. Silent tickets get a warning, then auto-close.
  closed  the member loses access, an HTML transcript goes to the log channel (and to the member by DM with a 1-5 star
          rating). Staff can reopen it until the reopen window ends, then the channel is deleted.
Buttons survive restarts: fixed custom_ids, looked up by channel. Staff notes are stored in the database only.
"""
import asyncio
import io
import logging
from datetime import timedelta
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import alerts, config
from ..checks import admin_role, is_admin, is_staff, require_admin, require_staff, staff_role_ids
from ..ticket_ui import (ClosedView, CloseConfirm, CommentButton, Confirm, ControlView, IntakeModal, PanelView,
                         RateButton, ToolsView, TypeManager, comment_view, rate_view)
from ..ticket_ai_runner import TicketAIRunner
from ..tickets import (CATEGORY_NAME, DEFAULT_BUTTON, DEFAULT_TYPES, LOG_CHANNEL_NAME, MAX_TRANSCRIPT_MESSAGES,
                       PANEL_CHANNEL_NAME, PRIORITIES, channel_name, ping_role_ids, log_embed, panel_color, panel_embed,
                       render_transcript, render_transcript_text, resolve_emoji, setting, slug, star_line, stats_embed, ticket_embed)
from .announce import fetch_image, store_attachment, store_image

log = logging.getLogger("orion.ticket")
utcnow = discord.utils.utcnow

ACTIVITY_WRITE_SECONDS = 60      # a busy ticket writes its "last activity" at most once a minute
BANNER_KEYS = {"panel": "ticket_panel_banner_id", "ticket": "ticket_banner_id"}

MEMBER_PERMS = dict(view_channel=True, send_messages=True, read_message_history=True, attach_files=True,
                    embed_links=True)
# Discord refuses an overwrite that allows or denies a permission the bot itself lacks, so these only use what the
# README's invite link grants. Staff keep extra powers (like Manage Messages) through their own server roles.
STAFF_PERMS = dict(MEMBER_PERMS)
BOT_PERMS = dict(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)


class Tickets(commands.GroupCog, name="ticket", description="Support tickets"):
    blacklist = app_commands.Group(name="blacklist", description="Block members from opening tickets (staff)")

    def __init__(self, bot):
        self.bot = bot
        self.types: dict[int, dict] = {}
        self.open: dict[int, dict] = {}          # channel id -> {"id", "owner", "type_id", "warned", "replied"}
        self._last_write: dict[int, float] = {}
        self._open_locks: dict[int, asyncio.Lock] = {}
        self.ai = TicketAIRunner(self)

    async def cog_load(self):
        await self.reload_types()
        for t in await self.bot.db.ticket_open_list(1000):
            self._remember(t)
        self.bot.add_view(PanelView())
        self.bot.add_view(ControlView())
        self.bot.add_view(ClosedView())
        self.bot.add_dynamic_items(RateButton, CommentButton)
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()
        self.ai.cancel_all()

    # ------------------------------------------------------------- helpers
    @property
    def db(self):
        return self.bot.db

    @property
    def guild(self) -> Optional[discord.Guild]:
        return self.bot.get_guild(config.GUILD_ID)

    async def reload_types(self):
        self.types = {t["id"]: dict(t) for t in await self.db.tt_list()}

    def emoji(self, raw: str) -> str:
        """A type's emoji ready to show: custom emoji names are looked up in the server."""
        return resolve_emoji(raw, self.guild)

    def enabled_types(self) -> list[dict]:
        """Types shown on the panel, with their emoji already resolved (so 'rflogopurple' becomes the real emoji)."""
        return [{**t, "emoji": self.emoji(t["emoji"])} for t in self.types.values() if t["enabled"]]

    def _remember(self, t):
        if t["channel_id"]:
            self.open[t["channel_id"]] = {"id": t["id"], "owner": t["owner_id"], "type_id": t["type_id"],
                                          "warned": t["warned_at"] is not None,
                                          "replied": t["first_staff_reply_at"] is not None, "ai": t["ai_state"]}

    def handler_role_ids(self, type_id: Optional[int]) -> list[int]:
        """Roles that handle this type: its own roles, or the approved staff roles. Sect Admin is always added separately."""
        t = self.types.get(type_id) if type_id else None
        return list(t["staff_role_ids"]) if t and t["staff_role_ids"] else sorted(staff_role_ids(self.db))

    def is_handler(self, member, type_id: Optional[int]) -> bool:
        if not isinstance(member, discord.Member):
            return False
        if is_admin(member):
            return True
        ids = set(self.handler_role_ids(type_id))
        return any(r.id in ids for r in member.roles)

    def _staff_overwrites(self, guild: discord.Guild, type_id: Optional[int]) -> dict:
        ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
              guild.me: discord.PermissionOverwrite(**BOT_PERMS)}
        anbu = admin_role(guild)
        for role in [anbu] + [guild.get_role(r) for r in self.handler_role_ids(type_id)]:
            if role is not None:
                ow[role] = discord.PermissionOverwrite(**STAFF_PERMS)
        return ow

    def _category(self, guild: discord.Guild, type_id: Optional[int]) -> Optional[discord.CategoryChannel]:
        t = self.types.get(type_id) if type_id else None
        for cid in ((t or {}).get("category_id"), self.db.get("ticket_category_id")):
            ch = guild.get_channel(cid) if cid else None
            if isinstance(ch, discord.CategoryChannel):
                return ch
        return None

    def _name_of(self, uid: Optional[int]) -> str:
        if not uid:
            return "Orion (auto-close)"
        g = self.guild
        m = g.get_member(uid) if g else None
        return m.display_name if m else f"user {uid}"

    async def banner_file(self, which: str) -> tuple[Optional[str], list[discord.File]]:
        bid = self.db.get(BANNER_KEYS[which])
        row = await self.db.get_image(bid) if bid else None
        if not row:
            return None, []
        name = f"ticket-{which}-banner.{row['ext']}"
        return f"attachment://{name}", [discord.File(io.BytesIO(row["data"]), filename=name)]

    async def _ticket_here(self, i: discord.Interaction, *, staff: bool, open_only: bool = True):
        """The ticket of the channel this was used in, after the permission check. Replies itself on failure
        (the interaction must already be deferred)."""
        t = await self.db.ticket_by_channel(i.channel_id) if i.channel_id else None
        if t is None or t["status"] == "deleted":
            await i.followup.send("This isn't a ticket channel.", ephemeral=True)
            return None
        if open_only and t["status"] != "open":
            await i.followup.send("This ticket is closed. Reopen it first.", ephemeral=True)
            return None
        if staff and not self.is_handler(i.user, t["type_id"]):
            await i.followup.send("Only the staff handling this ticket type can do that.", ephemeral=True)
            return None
        if not staff and i.user.id != t["owner_id"] and not self.is_handler(i.user, t["type_id"]):
            await i.followup.send("Only the person who opened this ticket or staff can do that.", ephemeral=True)
            return None
        return t

    async def _say(self, channel_id: int, text: str, **kw):
        ch = self.bot.get_channel(channel_id)
        if ch is None:
            return None
        try:
            return await ch.send(text, allowed_mentions=kw.pop("allowed_mentions", discord.AllowedMentions.none()), **kw)
        except discord.HTTPException:
            log.warning("Could not post in ticket channel %s", channel_id, exc_info=True)
            return None

    # --------------------------------------------------------------- panel
    async def post_panel(self, channel: discord.TextChannel) -> str:
        perms = channel.permissions_for(channel.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links and perms.attach_files):
            return f"⚠️ I need **View Channel, Send Messages, Embed Links and Attach Files** in {channel.mention}."
        types = self.enabled_types()
        url, files = await self.banner_file("panel")
        try:
            msg = await channel.send(embed=panel_embed(self.db, types, url), files=files,
                                     view=PanelView(types, self.db.get("ticket_button_label") or DEFAULT_BUTTON))
        except discord.HTTPException as e:
            return f"⚠️ Discord refused the panel: {e.text or e}"
        old_ch, old_id = self.db.get("ticket_panel_channel_id"), self.db.get("ticket_panel_message_id")
        await self.db.set("ticket_panel_channel_id", channel.id)
        await self.db.set("ticket_panel_message_id", msg.id)
        if old_id and old_id != msg.id:
            old = self.bot.get_channel(old_ch or 0)
            if old is not None:
                try:
                    await old.get_partial_message(old_id).delete()
                except discord.HTTPException:
                    pass
        return f"✅ Panel posted in {channel.mention}."

    async def refresh_panel(self) -> Optional[str]:
        """Redraw the panel (types, text, banner). Returns a warning, or None when it worked or there is no panel."""
        ch = self.bot.get_channel(self.db.get("ticket_panel_channel_id") or 0)
        mid = self.db.get("ticket_panel_message_id")
        if ch is None or not mid:
            return None
        types = self.enabled_types()
        url, files = await self.banner_file("panel")
        try:
            await ch.get_partial_message(mid).edit(
                embed=panel_embed(self.db, types, url), attachments=files,
                view=PanelView(types, self.db.get("ticket_button_label") or DEFAULT_BUTTON))
            return None
        except discord.NotFound:
            return "⚠️ The panel message was deleted: post it again with `/ticket panel`."
        except discord.HTTPException as e:
            return f"⚠️ I couldn't update the panel: {e.text or e}"

    async def reset_panel_select(self, message: Optional[discord.Message]):
        """A dropdown keeps showing the last pick; redraw it so the same type can be picked again."""
        if message is None or len(self.enabled_types()) < 2:
            return
        try:
            await message.edit(view=PanelView(self.enabled_types(), self.db.get("ticket_button_label") or DEFAULT_BUTTON))
        except discord.HTTPException:
            pass

    async def set_banner(self, which: str, att: Optional[discord.Attachment], link: Optional[str],
                         remove: bool = False) -> str:
        what = "Panel banner" if which == "panel" else "Ticket banner"
        if remove or (link or "").strip().lower() in ("clear", "none", "remove"):
            new_id = None
        else:
            try:
                if att is not None:
                    new_id = await store_attachment(self.db, att)
                elif link and link.strip():
                    new_id = await store_image(self.db, await fetch_image(link.strip()))
                else:
                    return "Attach an image, paste a link, or choose remove."
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return "⚠️ I couldn't download that link."
            except ValueError as e:
                return f"⚠️ {e}"
        await self.db.set(BANNER_KEYS[which], new_id)
        done = f"🗑️ {what} removed." if new_id is None else f"✅ {what} updated."
        if which == "panel":
            note = await self.refresh_panel()
            return done + (f" {note}" if note else "")
        return done + " New tickets will show it."

    # --------------------------------------------------------------- setup
    async def setup_tickets(self, guild: discord.Guild) -> str:
        db, me = self.db, guild.me
        if not (guild.me.guild_permissions.manage_channels and guild.me.guild_permissions.manage_roles):
            return "⚠️ I need the **Manage Channels** and **Manage Roles** permissions to set up tickets."
        staff_ow = self._staff_overwrites(guild, None)
        made = []

        cat = guild.get_channel(db.get("ticket_category_id") or 0)
        if not isinstance(cat, discord.CategoryChannel):
            cat = await guild.create_category(CATEGORY_NAME, overwrites=staff_ow, reason="Ticket setup")
            await db.set("ticket_category_id", cat.id)
            made.append(f"category **{cat.name}**")

        logs = guild.get_channel(db.get("ticket_log_channel_id") or 0)
        if logs is None:
            logs = await guild.create_text_channel(LOG_CHANNEL_NAME, category=cat, overwrites=staff_ow,
                                                   topic="Ticket transcripts and ratings (staff only)",
                                                   reason="Ticket setup")
            await db.set("ticket_log_channel_id", logs.id)
            made.append(logs.mention)

        panel_ch = self.bot.get_channel(db.get("ticket_panel_channel_id") or 0)
        if panel_ch is None:
            public = {guild.default_role: discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=False),
                me: discord.PermissionOverwrite(**BOT_PERMS)}
            panel_ch = await guild.create_text_channel(PANEL_CHANNEL_NAME, category=cat, overwrites=public, position=0,
                                                       topic="Open a private ticket with the staff team",
                                                       reason="Ticket setup")
            made.append(panel_ch.mention)

        if await db.tt_count() == 0:
            for t in DEFAULT_TYPES:
                await db.tt_create(**t)
            await self.reload_types()
            made.append(f"{len(DEFAULT_TYPES)} ticket types")

        if self.db.get("ticket_panel_channel_id") == panel_ch.id and self.db.get("ticket_panel_message_id"):
            note = await self.refresh_panel()
            panel_line = note or f"Panel refreshed in {panel_ch.mention}."
            if note and "deleted" in note:
                panel_line = await self.post_panel(panel_ch)
        else:
            panel_line = await self.post_panel(panel_ch)

        return ("✅ Tickets are set up.\n"
                f"• Created: {', '.join(made) if made else 'nothing new (everything already existed)'}\n"
                f"• {panel_line}\n• Transcripts go to {logs.mention}\n\n"
                "Next: open **/admin → 🎫 Tickets** to pick staff roles per type, the ping role and the limits, and "
                "**🖼️ Ticket banner** to add banners.")

    async def create_type_category(self, ttype) -> str:
        g = self.guild
        if g is None or ttype is None:
            return "⚠️ I can't see the server."
        try:
            cat = await g.create_category(f"🎫・{ttype['name']}"[:100], overwrites=self._staff_overwrites(g, ttype["id"]),
                                          reason="Ticket type category")
        except discord.HTTPException as e:
            return f"⚠️ I couldn't create the category: {e.text or e}"
        await self.db.tt_update(ttype["id"], category_id=cat.id)
        return f"📁 Created **{cat.name}**. New **{ttype['name']}** tickets open there."

    async def open_types(self, i: discord.Interaction):
        """The ticket type editor. `i` must already be deferred ephemerally (used by /admin)."""
        await TypeManager(self, i).render()

    # ------------------------------------------------------------- opening
    async def start_open(self, i: discord.Interaction, type_id: Optional[int]):
        """Panel button/dropdown. A form must be the FIRST response, so only quick checks happen before it."""
        types = self.enabled_types()
        ttype = self.types.get(type_id) if type_id else (types[0] if len(types) == 1 else None)
        problem = None
        if not self.db.get("tickets_enabled", True):
            problem = "🚫 Tickets are switched off right now."
        elif ttype is None or not ttype["enabled"]:
            problem = "That ticket type isn't available any more."
        elif await self.db.bl_get(i.user.id):
            problem = "🚫 You can't open tickets. Contact a staff member if you think this is a mistake."
        elif await self.db.ticket_open_count(i.user.id) >= setting(self.db, "ticket_max_open"):
            problem = self._limit_text(i.user.id)
        if problem:
            await i.response.send_message(problem, ephemeral=True)
            return await self.reset_panel_select(i.message)
        if ttype["questions"]:
            return await i.response.send_modal(IntakeModal(self, ttype, i.message))
        await i.response.defer(ephemeral=True, thinking=True)
        await self.create_ticket(i, ttype, [])
        await self.reset_panel_select(i.message)

    def _limit_text(self, uid: int) -> str:
        mine = [cid for cid, o in self.open.items() if o["owner"] == uid]
        where = " ".join(f"<#{c}>" for c in mine[:3])
        n = setting(self.db, "ticket_max_open")
        return (f"You already have {'an open ticket' if n == 1 else f'{n} open tickets'}"
                + (f": {where}" if where else "") + ". Please use it, or close it first.")

    async def create_ticket(self, i: discord.Interaction, ttype, answers: list):
        """`i` is deferred ephemerally. Re-checks everything, since the form may have been open for minutes."""
        guild, user = i.guild, i.user
        lock = self._open_locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            if not self.db.get("tickets_enabled", True) or not self.types.get(ttype["id"], {}).get("enabled"):
                return await i.followup.send("That ticket type isn't available any more.", ephemeral=True)
            if await self.db.bl_get(user.id):
                return await i.followup.send("🚫 You can't open tickets.", ephemeral=True)
            cat = self._category(guild, ttype["id"])
            if cat is None:
                await alerts.alert_staff(self.bot, guild, "Someone tried to open a ticket, but the Tickets category is "
                                                          "missing. Run `/ticket setup`.", key="ticket-no-category")
                return await i.followup.send("⚠️ Tickets aren't set up yet. The staff team has been told.", ephemeral=True)
            tid = await self.db.ticket_open(owner_id=user.id, type_id=ttype["id"], type_name=ttype["name"],
                                            answers=answers, max_open=setting(self.db, "ticket_max_open"), now=utcnow())
            if tid is None:
                return await i.followup.send(self._limit_text(user.id), ephemeral=True)
            ow = self._staff_overwrites(guild, ttype["id"])
            ow[user] = discord.PermissionOverwrite(**MEMBER_PERMS)
            try:
                ch = await guild.create_text_channel(
                    channel_name(tid, user.name), category=cat, overwrites=ow, reason=f"Ticket #{tid} by {user}",
                    topic=f"Ticket #{tid} · {ttype['name']} · opened by {user} ({user.id})")
            except discord.HTTPException as e:
                await self.db.ticket_discard(tid)
                full = "maximum" in (e.text or "").lower() or getattr(e, "code", 0) == 50035
                await alerts.alert_staff(
                    self.bot, guild,
                    f"I couldn't create a ticket channel in **{cat.name}**: {e.text or e}."
                    + (" A category holds at most 50 channels: delete old tickets, or give this type its own "
                       "category in /admin → Tickets → Ticket types." if full else ""), key=f"ticket-create-{cat.id}")
                return await i.followup.send("⚠️ I couldn't open your ticket. The staff team has been told.", ephemeral=True)
            try:
                await self.db.ticket_set_channel(tid, ch.id)
            except Exception:
                # never leave a channel without a ticket row (or a row without a channel)
                log.exception("Ticket #%s: could not save its channel; rolling back", tid)
                await self.db.ticket_discard(tid)
                try:
                    await ch.delete(reason="Ticket could not be saved")
                except discord.HTTPException:
                    pass
                return await i.followup.send("⚠️ I couldn't open your ticket. Please try again.", ephemeral=True)
            ticket = await self.db.ticket_get(tid)
            self._remember(ticket)

        pings = [r for r in (guild.get_role(rid) for rid in ping_role_ids(self.db, ttype)) if r is not None]
        url, files = await self.banner_file("ticket")
        content = " ".join([user.mention] + [r.mention for r in pings])
        try:
            msg = await ch.send(content, embed=ticket_embed(dict(ticket), user.mention, banner_url=url,
                                                            color=panel_color(self.db)),
                                files=files, view=ControlView(),
                                allowed_mentions=discord.AllowedMentions(users=[user], roles=pings or False))
            await self.db.ticket_update(tid, control_message_id=msg.id)
            try:
                await msg.pin(reason="Ticket controls")
            except discord.HTTPException:
                pass
        except discord.HTTPException:
            log.warning("Ticket #%s: could not post the opening message", tid, exc_info=True)
        log.info("Ticket #%s (%s) opened by %s", tid, ttype["name"], user.id)
        if isinstance(user, discord.Member):
            try:
                await self.ai.start(ch, user, tid, ttype["name"])
            except Exception:
                log.exception("AI handler could not start on ticket #%s", tid)
        await i.followup.send(f"✅ Your ticket is open: {ch.mention}", ephemeral=True)

    def guild_color(self) -> int:
        return panel_color(self.db)

    async def redraw(self, ticket_id: int):
        """Redraw the message at the top of the ticket after a claim, transfer, move or priority change."""
        t = await self.db.ticket_get(ticket_id)
        ch = self.bot.get_channel(t["channel_id"] or 0) if t else None
        if ch is None or not t["control_message_id"]:
            return
        try:
            msg = await ch.fetch_message(t["control_message_id"])
            url = f"attachment://{msg.attachments[0].filename}" if msg.attachments else None
            await msg.edit(embed=ticket_embed(dict(t), f"<@{t['owner_id']}>", banner_url=url, color=panel_color(self.db)))
        except discord.HTTPException:
            log.info("Ticket #%s: could not redraw the top message", ticket_id)

    # ------------------------------------------------------------- closing
    async def ask_close(self, i: discord.Interaction):
        t = await self.db.ticket_by_channel(i.channel_id)
        if t is None or t["status"] != "open":
            return await i.response.send_message("This ticket is already closed.", ephemeral=True)
        if i.user.id != t["owner_id"] and not self.is_handler(i.user, t["type_id"]):
            return await i.response.send_message("Only the person who opened this ticket or staff can close it.",
                                                 ephemeral=True)
        await i.response.send_message("Close this ticket? A transcript is saved first.",
                                      view=CloseConfirm(self, t["id"], i.user.id), ephemeral=True)

    async def close_by_id(self, ticket_id: int, by: Optional[discord.abc.User], reason: Optional[str]) -> str:
        t = await self.db.ticket_get(ticket_id)
        if t is None or t["status"] != "open":
            return "This ticket is already closed."
        return await self.close_ticket(t, by, reason)

    async def close_ticket(self, t, by: Optional[discord.abc.User], reason: Optional[str]) -> str:
        now = utcnow()
        if not await self.db.ticket_close(t["id"], by.id if by else None, reason, now):
            return "This ticket is already closed."
        self.open.pop(t["channel_id"], None)
        guild, ch = self.guild, self.bot.get_channel(t["channel_id"] or 0)
        by_name = by.display_name if by else "Orion (auto-close)"
        make_files, count = None, 0
        if ch is not None:
            # Everyone added to the ticket (owner included) loses access; staff roles keep it.
            await self._lock_after_close(ch, t)
            make_files, count = await self._transcript(ch, t, by_name, reason, now)

        logs = self.bot.get_channel(self.db.get("ticket_log_channel_id") or 0)
        notes = await self.db.note_count(t["id"])
        saved = False
        if logs is not None:
            try:
                lm = await logs.send(embed=log_embed(dict(t), closed_by=by.id if by else None, reason=reason,
                                                     messages=count, notes=notes, now=now),
                                     files=make_files() if make_files else [], allowed_mentions=discord.AllowedMentions.none())
                await self.db.ticket_set_transcript(t["id"], lm.jump_url)
                saved = True
            except discord.HTTPException:
                log.warning("Ticket #%s: could not post to the log channel", t["id"], exc_info=True)
        elif guild:
            await alerts.alert_staff(self.bot, guild, "A ticket closed but there is no ticket log channel, so its "
                                                      "transcript wasn't saved. Run `/ticket setup` or pick one in /admin.",
                                     key="ticket-no-log")

        if self.db.get("ticket_dm_enabled", True) and guild:
            owner = guild.get_member(t["owner_id"])
            if owner is not None:
                e = discord.Embed(color=panel_color(self.db), title=f"🔒 Your ticket #{t['id']:04d} was closed",
                                  description=f"**{t['type_name']}** in **{guild.name}**\n"
                                              f"Closed by {by_name}" + (f": {reason}" if reason else "") +
                                              "\n\nYour transcript is attached: open the **.txt** to read it right here, or the **.html** in your browser. "
                                              "You can reopen it from the ticket channel until it is deleted. How did we do? Rate us below.")
                try:
                    await owner.send(embed=e, files=make_files() if make_files else [], view=rate_view(t["id"]))
                except discord.HTTPException:
                    pass                # DMs closed: nothing to do

        hours = setting(self.db, "ticket_delete_hours")
        gone = now + timedelta(hours=hours)
        if ch is not None:
            e = discord.Embed(color=panel_color(self.db), title="🔒 Ticket closed",
                              description=f"Closed by **{by_name}**" + (f"\n**Reason:** {reason}" if reason else "")
                                          + f"\n\nThis channel will be deleted <t:{int(gone.timestamp()) + 60}:R>. "
                                            "You or staff can reopen it until then.")
            try:
                await ch.send(embed=e, view=ClosedView())
            except discord.HTTPException:
                pass
        log.info("Ticket #%s closed by %s", t["id"], by.id if by else "auto-close")
        return f"🔒 Ticket #{t['id']:04d} closed." + (" Transcript saved." if saved else
                                                     " ⚠️ The transcript couldn't be posted to the log channel.")

    async def _lock_after_close(self, ch: discord.TextChannel, t):
        """The owner keeps READ-ONLY access (so they still see the Reopen button); everyone else added loses access."""
        guild = ch.guild
        for target, ow in list(ch.overwrites.items()):
            if isinstance(target, discord.Member) and target != guild.me and not self.is_handler(target, t["type_id"]):
                try:
                    if target.id == t["owner_id"]:
                        await ch.set_permissions(target, overwrite=discord.PermissionOverwrite(
                            view_channel=True, read_message_history=True, send_messages=False,
                            attach_files=False, embed_links=False), reason=f"Ticket #{t['id']} closed")
                    else:
                        await ch.set_permissions(target, overwrite=None, reason=f"Ticket #{t['id']} closed")
                except discord.HTTPException:
                    pass

    async def _transcript(self, ch: discord.TextChannel, t, closed_by: str, reason: Optional[str], now):
        """Returns (a function making fresh discord.Files [plain text, web page], message count).
        A File can only be sent once, so every send calls the function again."""
        msgs = []
        try:
            async for m in ch.history(limit=MAX_TRANSCRIPT_MESSAGES, oldest_first=True):
                msgs.append({"author": m.author.display_name, "avatar": m.author.display_avatar.url, "bot": m.author.bot,
                             "created_at": m.created_at, "content": m.clean_content,
                             "attachments": [(a.filename, a.url) for a in m.attachments],
                             "embeds": [(e.title, e.description) for e in m.embeds if e.title or e.description]})
        except discord.HTTPException:
            log.warning("Ticket #%s: could not read the channel history", t["id"], exc_info=True)
        owner = self._name_of(t["owner_id"])
        kw = dict(guild_name=ch.guild.name, owner_name=owner, closed_by=closed_by, reason=reason, now=now)
        page = render_transcript(dict(t), msgs, **kw).encode("utf-8")
        text = render_transcript_text(dict(t), msgs, **kw).encode("utf-8")
        base = f"transcript-{t['id']:04d}-{slug(owner)}"
        return (lambda: [discord.File(io.BytesIO(text), filename=base + ".txt"),
                         discord.File(io.BytesIO(page), filename=base + ".html")]), len(msgs)

    async def reopen(self, t, by: discord.abc.User) -> str:
        ch = self.bot.get_channel(t["channel_id"] or 0)
        if ch is None:
            return "The channel is gone, so this ticket can't be reopened."
        if not self.is_handler(by, t["type_id"]):          # a member reopening their own ticket
            if by.id != t["owner_id"]:
                return "Only the person who opened this ticket or staff can reopen it."
            if await self.db.bl_get(by.id):
                return "You can't reopen tickets right now."
            if await self.db.ticket_open_count(by.id) >= setting(self.db, "ticket_max_open"):
                return "You already have the maximum number of open tickets. Close one first."
        if not await self.db.ticket_reopen(t["id"], utcnow()):
            return "This ticket is already open."
        owner = ch.guild.get_member(t["owner_id"])
        if owner is not None:
            try:
                await ch.set_permissions(owner, overwrite=discord.PermissionOverwrite(**MEMBER_PERMS),
                                         reason=f"Ticket #{t['id']} reopened")
            except discord.HTTPException:
                pass
        self._remember(await self.db.ticket_get(t["id"]))
        await self._say(ch.id, f"🔓 Ticket reopened by {by.mention}." + (f" Welcome back <@{t['owner_id']}>!" if owner else
                                                                          " (The member has left the server.)"),
                        allowed_mentions=discord.AllowedMentions(users=[owner] if owner else False))
        return f"🔓 Ticket #{t['id']:04d} reopened."

    async def delete_channel(self, t, reason: str):
        ch = self.bot.get_channel(t["channel_id"] or 0)
        self.open.pop(t["channel_id"], None)
        if ch is not None:
            try:
                await ch.delete(reason=reason)
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.warning("Ticket #%s: could not delete the channel", t["id"], exc_info=True)
                return
        await self.db.ticket_mark_deleted(t["id"], utcnow())

    # ------------------------------------------------------------- buttons
    async def on_claim_button(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        t = await self._ticket_here(i, staff=True)
        if t:
            await i.followup.send(await self.claim(t, i.user), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def open_tools(self, i: discord.Interaction):
        t = await self.db.ticket_by_channel(i.channel_id)
        if t is None or t["status"] != "open":
            return await i.response.send_message("This ticket is closed.", ephemeral=True)
        if not self.is_handler(i.user, t["type_id"]):
            return await i.response.send_message("Staff tools are for the staff handling this ticket.", ephemeral=True)
        await i.response.send_message(f"🛠️ Staff tools for ticket #{t['id']:04d}. Only you can see this.",
                                      view=ToolsView(self, t, self.enabled_types(), i.user.id), ephemeral=True)

    async def on_reopen_button(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        t = await self._ticket_here(i, staff=False, open_only=False)
        if t:
            await i.followup.send(await self.reopen(t, i.user), ephemeral=True)

    async def on_transcript_button(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True, thinking=True)
        t = await self._ticket_here(i, staff=True, open_only=False)
        if t:
            make, n = await self._transcript(i.channel, t, i.user.display_name, t["close_reason"], utcnow())
            await i.followup.send(f"📜 Transcript of ticket #{t['id']:04d} ({n} messages).", files=make(), ephemeral=True)

    async def on_delete_button(self, i: discord.Interaction):
        t = await self.db.ticket_by_channel(i.channel_id)
        if t is None or not self.is_handler(i.user, t["type_id"]):
            return await i.response.send_message("Only staff can delete a ticket.", ephemeral=True)
        if t["status"] == "open":
            return await i.response.send_message("Close the ticket first, so its transcript is saved.", ephemeral=True)

        async def yes(ci: discord.Interaction):
            await ci.response.edit_message(content="🗑️ Deleting…", view=None)
            await self.delete_channel(t, f"Ticket #{t['id']} deleted by {ci.user}")

        await i.response.send_message("Delete this channel now? The transcript stays in the log channel.",
                                      view=Confirm(i.user.id, "Delete now", yes), ephemeral=True)

    # ---------------------------------------------------------- staff work
    async def claim(self, t, staff: discord.Member) -> str:
        if t["claimed_by"] and t["claimed_by"] != staff.id:
            return f"<@{t['claimed_by']}> is already handling this ticket. Use **Transfer** to take it over."
        if t["claimed_by"] == staff.id:
            return "You're already handling this ticket."
        if not await self.db.ticket_claim(t["id"], staff.id):
            return "Someone else claimed it a moment ago."
        await self._say(t["channel_id"], f"🙋 {staff.mention} is handling this ticket.")
        if self.open.get(t["channel_id"], {}).get("ai") == "active":
            await self.ai.stop(t["channel_id"], t["id"], "claimed by staff")
        await self.redraw(t["id"])
        return "✅ You're handling this ticket now."

    async def unclaim(self, ticket_id: int, staff: discord.Member) -> str:
        t = await self.db.ticket_get(ticket_id)
        if not t["claimed_by"]:
            return "Nobody is handling this ticket."
        if t["claimed_by"] != staff.id and not is_staff(staff, self.db):
            return "Only the person handling it, or Sect Admin and staff roles, can unclaim it."
        await self.db.ticket_unclaim(ticket_id)
        await self._say(t["channel_id"], f"↩️ {staff.mention} stepped away from this ticket.")
        await self.redraw(ticket_id)
        return "↩️ Unclaimed."

    async def transfer(self, ticket_id: int, by: discord.Member, target) -> str:
        t = await self.db.ticket_get(ticket_id)
        member = self.guild.get_member(target.id) if self.guild else None
        if t is None or t["status"] != "open":
            return "This ticket is closed."
        if member is None or member.bot or not self.is_handler(member, t["type_id"]):
            return "⚠️ You can only hand a ticket to staff who handle this ticket type."
        await self.db.ticket_claim(ticket_id, member.id, force=True)
        await self._say(t["channel_id"], f"🔁 {by.mention} handed this ticket to {member.mention}.",
                        allowed_mentions=discord.AllowedMentions(users=[member]))
        await self.redraw(ticket_id)
        return f"🔁 {member.mention} is handling it now."

    async def set_priority(self, ticket_id: int, by: discord.Member, level: str) -> str:
        if level not in PRIORITIES:
            return "Unknown priority."
        t = await self.db.ticket_get(ticket_id)
        await self.db.ticket_update(ticket_id, priority=level)
        await self._say(t["channel_id"], f"Priority set to **{PRIORITIES[level]}** by {by.mention}.")
        await self.redraw(ticket_id)
        return f"Priority is now **{PRIORITIES[level]}**."

    async def move(self, ticket_id: int, by: discord.Member, type_id: int) -> str:
        t, new = await self.db.ticket_get(ticket_id), self.types.get(type_id)
        if new is None:
            return "That ticket type doesn't exist any more."
        if t["type_id"] == type_id:
            return f"It's already a **{new['name']}** ticket."
        ch = self.bot.get_channel(t["channel_id"] or 0)
        if ch is None:
            return "I can't find this ticket's channel."
        # New staff for the new type; people added to the ticket (owner included) keep their access.
        ow = self._staff_overwrites(ch.guild, type_id)
        ow.update({k: v for k, v in ch.overwrites.items() if isinstance(k, discord.Member) and k != ch.guild.me})
        cat = self._category(ch.guild, type_id)
        try:
            await ch.edit(overwrites=ow, **({"category": cat} if cat and cat != ch.category else {}),
                          reason=f"Ticket #{t['id']} moved to {new['name']}")
        except discord.HTTPException as e:
            return f"⚠️ I couldn't move the channel: {e.text or e}"
        await self.db.ticket_update(ticket_id, type_id=type_id, type_name=new["name"])
        if t["channel_id"] in self.open:
            self.open[t["channel_id"]]["type_id"] = type_id
        await self._say(ch.id, f"📦 {by.mention} moved this ticket to **{self.emoji(new['emoji'])} {new['name']}**.")
        await self.redraw(ticket_id)
        return f"📦 Moved to **{new['name']}**."

    async def add_member(self, ticket_id: int, user) -> str:
        t = await self.db.ticket_get(ticket_id)
        ch = self.bot.get_channel(t["channel_id"] or 0)
        member = ch.guild.get_member(user.id) if ch else None
        if member is None:
            return "I can't find that member in the server."
        if member.bot:
            return "Bots can't be added to tickets."
        try:
            await ch.set_permissions(member, overwrite=discord.PermissionOverwrite(**MEMBER_PERMS),
                                     reason=f"Added to ticket #{t['id']}")
        except discord.HTTPException as e:
            return f"⚠️ I couldn't add them: {e.text or e}"
        await self._say(ch.id, f"➕ {member.mention} was added to this ticket.",
                        allowed_mentions=discord.AllowedMentions(users=[member]))
        return f"➕ Added {member.mention}."

    async def remove_member(self, ticket_id: int, user) -> str:
        t = await self.db.ticket_get(ticket_id)
        ch = self.bot.get_channel(t["channel_id"] or 0)
        if ch is None:
            return "I can't find this ticket's channel."
        if user.id == t["owner_id"]:
            return "You can't remove the person who opened the ticket. Close it instead."
        member = ch.guild.get_member(user.id)
        if member is None or member not in ch.overwrites:
            return "They weren't added to this ticket. (Staff see tickets through their role.)"
        try:
            await ch.set_permissions(member, overwrite=None, reason=f"Removed from ticket #{t['id']}")
        except discord.HTTPException as e:
            return f"⚠️ I couldn't remove them: {e.text or e}"
        await self._say(ch.id, f"➖ {member.mention} was removed from this ticket.")
        return f"➖ Removed {member.mention}."

    async def rename(self, ticket_id: int, name: str) -> str:
        t = await self.db.ticket_get(ticket_id)
        ch = self.bot.get_channel(t["channel_id"] or 0)
        new = slug(name, fallback="")
        if ch is None or not new:
            return "Use letters, numbers and dashes for the name."
        try:
            await asyncio.wait_for(ch.edit(name=new[:90], reason=f"Ticket #{t['id']} renamed"), timeout=8)
        except asyncio.TimeoutError:
            return ("⏳ Discord only allows 2 renames per channel every 10 minutes. Try again in a few minutes.")
        except discord.HTTPException as e:
            return f"⚠️ I couldn't rename it: {e.text or e}"
        return f"✏️ Renamed to **#{new[:90]}**."

    async def notes_message(self, ticket_id: int) -> dict:
        rows = await self.db.note_list(ticket_id)
        e = discord.Embed(color=config.COLOR_MAIN, title=f"📒 Staff notes · ticket #{ticket_id:04d}")
        for r in rows[-20:]:
            e.add_field(name=f"{self._name_of(r['author_id'])} · {r['created_at'].strftime('%d %b %H:%M')}"[:256],
                        value=r["text"][:1024], inline=False)
        if not rows:
            e.description = "*No notes yet.*"
        e.set_footer(text="Only staff can see notes. They are not in the transcript.")
        return {"embed": e}

    # -------------------------------------------------------------- rating
    async def on_rate(self, i: discord.Interaction, tid: int, n: int):
        if not await self.db.ticket_rate(tid, i.user.id, n, utcnow()):
            return await i.response.send_message("You've already rated this ticket. Thanks!", ephemeral=True)
        await i.response.edit_message(content=f"Thanks! You rated ticket #{tid:04d} **{star_line(n)}**.",
                                      view=comment_view(tid))
        await self._log_rating(tid)

    async def on_rate_comment(self, i: discord.Interaction, tid: int, text: str):
        ok = await self.db.ticket_rate_comment(tid, i.user.id, text)
        await i.response.send_message("💬 Thanks for the feedback!" if ok else "Rate the ticket first.", ephemeral=True)
        if ok:
            await self._log_rating(tid)

    async def _log_rating(self, tid: int):
        t = await self.db.ticket_get(tid)
        logs = self.bot.get_channel(self.db.get("ticket_log_channel_id") or 0)
        if t is None or logs is None:
            return
        handler = f" · handled by <@{t['claimed_by']}>" if t["claimed_by"] else ""
        text = (f"{star_line(t['rating'])} **Ticket #{tid:04d}** ({t['type_name']}) rated **{t['rating']}/5** by "
                f"<@{t['owner_id']}>{handler}" + (f"\n> {t['rating_comment'][:800]}" if t["rating_comment"] else ""))
        try:
            await logs.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    # ----------------------------------------------------------- listeners
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        o = self.open.get(message.channel.id)
        if o is None or message.author.bot or message.webhook_id:
            return
        staff = message.author.id != o["owner"] and self.is_handler(message.author, o["type_id"])
        if o.get("ai") == "active":
            if staff:
                await self.ai.stop(message.channel.id, o["id"], "a staff member replied")
            elif message.author.id == o["owner"]:
                self.ai.schedule(message.channel.id)
        now = utcnow()
        first_reply = staff and not o["replied"]
        recent = now.timestamp() - self._last_write.get(message.channel.id, 0) < ACTIVITY_WRITE_SECONDS
        if recent and not first_reply and not o["warned"]:
            return
        self._last_write[message.channel.id] = now.timestamp()
        o["warned"] = False
        o["replied"] = o["replied"] or staff
        try:
            await self.db.ticket_activity(message.channel.id, now, staff)
        except Exception:
            log.exception("Could not record ticket activity")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        """Someone deleted a ticket channel by hand: close the ticket so it stops counting against the member."""
        self.open.pop(channel.id, None)
        t = await self.db.ticket_by_channel(channel.id)
        if t is not None and t["status"] != "deleted":
            await self.db.ticket_mark_deleted(t["id"], utcnow())

    # ---------------------------------------------------------------- loop
    @tasks.loop(seconds=60)
    async def tick(self):
        now = utcnow()
        try:
            swept = await self.db.ticket_sweep_orphans(now - timedelta(minutes=5))
            if swept:
                log.warning("Removed %d ticket row(s) that never got a channel", swept)
            hours = setting(self.db, "ticket_autoclose_hours")
            warn = setting(self.db, "ticket_warn_hours")
            if hours > 0:
                for t in await self.db.ticket_idle(now, hours):
                    if self.bot.get_channel(t["channel_id"]) is None:
                        if self.guild is not None:          # deleted while the bot was offline
                            self.open.pop(t["channel_id"], None)
                            await self.db.ticket_mark_deleted(t["id"], now)
                        continue
                    await self.db.ticket_set_warned(t["id"], now)
                    if t["channel_id"] in self.open:
                        self.open[t["channel_id"]]["warned"] = True
                    closes = int((now + timedelta(hours=warn)).timestamp())
                    await self._say(t["channel_id"], f"⏰ <@{t['owner_id']}>, this ticket has been quiet for a while. "
                                                     f"It will close <t:{closes}:R> unless someone replies.",
                                    allowed_mentions=discord.AllowedMentions(users=True))
                for t in await self.db.ticket_warn_expired(now, warn):
                    await self.close_ticket(t, None, f"No reply for {hours + warn} hours")
            for t in await self.db.ticket_delete_due(now, setting(self.db, "ticket_delete_hours")):
                await self.delete_channel(t, f"Ticket #{t['id']} closed; reopen window over")
        except Exception:
            log.exception("Ticket loop error")

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ commands
    @app_commands.command(name="setup", description="Create the Tickets category, panel and log channels (Sect Admin)")
    @app_commands.check(require_admin)
    async def setup_cmd(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True, thinking=True)
        try:
            text = await self.setup_tickets(i.guild)
        except discord.HTTPException as e:
            text = f"⚠️ Discord refused part of the setup: {e.text or e}. Check my permissions and run it again."
        await i.followup.send(text, ephemeral=True)

    @app_commands.command(name="panel", description="Post the ticket panel in a channel (staff)")
    @app_commands.describe(channel="Where to post it (default: this channel). The old panel is removed.")
    @app_commands.check(require_staff)
    async def panel_cmd(self, i: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        await i.response.defer(ephemeral=True)
        await i.followup.send(await self.post_panel(channel or i.channel), ephemeral=True)

    @app_commands.command(name="banner", description="Change the ticket panel banner or the banner inside tickets (staff)")
    @app_commands.describe(target="Which banner", image="New banner (upload)", image_url="New banner (link)",
                           remove="Remove this banner")
    @app_commands.choices(target=[app_commands.Choice(name="Panel (where members open tickets)", value="panel"),
                                  app_commands.Choice(name="Ticket (top of every new ticket)", value="ticket")])
    @app_commands.check(require_staff)
    async def banner_cmd(self, i: discord.Interaction, target: app_commands.Choice[str],
                         image: Optional[discord.Attachment] = None, image_url: Optional[str] = None, remove: bool = False):
        await i.response.defer(ephemeral=True)
        await i.followup.send(await self.set_banner(target.value, image, image_url, remove), ephemeral=True)

    @app_commands.command(name="close", description="Close this ticket")
    @app_commands.describe(reason="Why (shown in the log and the member's DM)")
    async def close_cmd(self, i: discord.Interaction, reason: Optional[app_commands.Range[str, 1, 500]] = None):
        await i.response.defer(ephemeral=True, thinking=True)
        t = await self._ticket_here(i, staff=False)
        if t:
            await i.followup.send(await self.close_ticket(t, i.user, reason), ephemeral=True)

    @app_commands.command(name="reopen", description="Reopen this closed ticket (you, if it's yours, or staff)")
    async def reopen_cmd(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        t = await self._ticket_here(i, staff=False, open_only=False)
        if t:
            await i.followup.send(await self.reopen(t, i.user), ephemeral=True)

    @app_commands.command(name="ai", description="Turn Orion's AI helper on or off for this ticket (staff)")
    @app_commands.choices(state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
    async def ai_cmd(self, i: discord.Interaction, state: app_commands.Choice[str]):
        await i.response.defer(ephemeral=True)
        t = await self._ticket_here(i, staff=True)
        if t:
            await i.followup.send(await self.ai.set_for_ticket(t, state.value == "on"), ephemeral=True)

    @app_commands.command(name="claim", description="Handle this ticket (staff)")
    async def claim_cmd(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        t = await self._ticket_here(i, staff=True)
        if t:
            await i.followup.send(await self.claim(t, i.user), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="unclaim", description="Stop handling this ticket (staff)")
    async def unclaim_cmd(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        t = await self._ticket_here(i, staff=True)
        if t:
            await i.followup.send(await self.unclaim(t["id"], i.user), ephemeral=True)

    @app_commands.command(name="transfer", description="Hand this ticket to another staff member (staff)")
    async def transfer_cmd(self, i: discord.Interaction, member: discord.Member):
        await i.response.defer(ephemeral=True)
        t = await self._ticket_here(i, staff=True)
        if t:
            await i.followup.send(await self.transfer(t["id"], i.user, member), ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="add", description="Add someone to this ticket (staff)")
    async def add_cmd(self, i: discord.Interaction, member: discord.Member):
        await i.response.defer(ephemeral=True)
        t = await self._ticket_here(i, staff=True)
        if t:
            await i.followup.send(await self.add_member(t["id"], member), ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="remove", description="Remove someone from this ticket (staff)")
    async def remove_cmd(self, i: discord.Interaction, member: discord.Member):
        await i.response.defer(ephemeral=True)
        t = await self._ticket_here(i, staff=True)
        if t:
            await i.followup.send(await self.remove_member(t["id"], member), ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="rename", description="Rename this ticket channel (staff, max 2 per 10 minutes)")
    async def rename_cmd(self, i: discord.Interaction, name: app_commands.Range[str, 1, 90]):
        await i.response.defer(ephemeral=True, thinking=True)
        t = await self._ticket_here(i, staff=True)
        if t:
            await i.followup.send(await self.rename(t["id"], name), ephemeral=True)

    @app_commands.command(name="priority", description="Set this ticket's priority (staff)")
    @app_commands.choices(level=[app_commands.Choice(name=v, value=k) for k, v in PRIORITIES.items()])
    async def priority_cmd(self, i: discord.Interaction, level: app_commands.Choice[str]):
        await i.response.defer(ephemeral=True)
        t = await self._ticket_here(i, staff=True)
        if t:
            await i.followup.send(await self.set_priority(t["id"], i.user, level.value), ephemeral=True)

    async def _type_ac(self, i: discord.Interaction, current: str):
        return [app_commands.Choice(name=f"{t['name']}"[:100], value=str(t["id"]))
                for t in self.enabled_types() if current.lower() in t["name"].lower()][:25]

    @app_commands.command(name="move", description="Move this ticket to another ticket type (staff)")
    @app_commands.autocomplete(type=_type_ac)
    async def move_cmd(self, i: discord.Interaction, type: str):
        await i.response.defer(ephemeral=True)
        t = await self._ticket_here(i, staff=True)
        if t:
            msg = await self.move(t["id"], i.user, int(type)) if type.isdigit() else "Pick a type from the list."
            await i.followup.send(msg, ephemeral=True)

    @app_commands.command(name="note", description="Add a staff-only note to this ticket (never shown to the member)")
    async def note_cmd(self, i: discord.Interaction, text: app_commands.Range[str, 1, 1500]):
        await i.response.defer(ephemeral=True)
        t = await self._ticket_here(i, staff=True, open_only=False)
        if t:
            await self.db.note_add(t["id"], i.user.id, text)
            await i.followup.send("🗒️ Note saved. Only staff can see it, and it isn't in the transcript.", ephemeral=True)

    @app_commands.command(name="notes", description="Read this ticket's staff notes (staff)")
    async def notes_cmd(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        t = await self._ticket_here(i, staff=True, open_only=False)
        if t:
            await i.followup.send(**await self.notes_message(t["id"]), ephemeral=True)

    @app_commands.command(name="stats", description="Tickets per staff member, response time and ratings (staff)")
    @app_commands.describe(days="How far back to look (default 30)")
    @app_commands.check(require_staff)
    async def stats_cmd(self, i: discord.Interaction, days: app_commands.Range[int, 1, 365] = 30):
        await i.response.defer(ephemeral=True)
        await i.followup.send(embed=await self.stats(days), ephemeral=True)

    async def stats(self, days: int) -> discord.Embed:
        totals, staff = await self.db.ticket_stats(utcnow() - timedelta(days=days))
        me = self.bot.user.id if self.bot.user else 0
        return stats_embed(totals, [r for r in staff if r["uid"] != me], days, self._name_of)

    @app_commands.command(name="history", description="A member's past tickets (staff)")
    @app_commands.check(require_staff)
    async def history_cmd(self, i: discord.Interaction, member: discord.User):
        await i.response.defer(ephemeral=True)
        rows = await self.db.ticket_history(member.id, 15)
        e = discord.Embed(color=config.COLOR_MAIN, title=f"🎫 Tickets of {member.display_name}")
        icon = {"open": "🟢", "closed": "🔒", "deleted": "🗃️"}
        lines = []
        for r in rows:
            where = f"<#{r['channel_id']}>" if r["status"] == "open" else (
                f"[transcript]({r['transcript_url']})" if r["transcript_url"] else "no transcript")
            lines.append(f"{icon.get(r['status'], '•')} **#{r['id']:04d}** {r['type_name']} · "
                         f"<t:{int(r['created_at'].timestamp())}:d> · {where}"
                         + (f" · {'⭐' * r['rating']}" if r["rating"] else ""))
        e.description = "\n".join(lines) or "*No tickets yet.*"
        bl = await self.db.bl_get(member.id)
        if bl:
            e.add_field(name="🚫 Blacklisted", value=bl["reason"] or "*no reason given*", inline=False)
        await i.followup.send(embed=e, ephemeral=True)

    @blacklist.command(name="add", description="Stop a member from opening tickets")
    @app_commands.check(require_staff)
    async def bl_add(self, i: discord.Interaction, member: discord.User, reason: Optional[app_commands.Range[str, 1, 300]] = None):
        await self.db.bl_add(member.id, reason or "", i.user.id)
        await i.response.send_message(f"🚫 {member.mention} can't open tickets any more. Open tickets stay open.",
                                      ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @blacklist.command(name="remove", description="Let a member open tickets again")
    @app_commands.check(require_staff)
    async def bl_remove(self, i: discord.Interaction, member: discord.User):
        ok = await self.db.bl_remove(member.id)
        await i.response.send_message(f"✅ {member.mention} can open tickets again." if ok
                                      else f"{member.mention} wasn't blacklisted.", ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none())

    @blacklist.command(name="list", description="Who can't open tickets")
    @app_commands.check(require_staff)
    async def bl_list(self, i: discord.Interaction):
        rows = await self.db.bl_list(50)
        text = "\n".join(f"<@{r['user_id']}> · {r['reason'] or 'no reason'} · by <@{r['added_by']}>" for r in rows)
        await i.response.send_message(("🚫 **Ticket blacklist**\n" + text)[:1990] if rows else "Nobody is blacklisted.",
                                      ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Tickets(bot))
