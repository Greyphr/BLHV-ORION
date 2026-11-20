"""Ticket views and forms. Persistent views use fixed custom_ids and look the ticket up by channel, so their buttons keep
working after a restart. Rating buttons live in DMs, so they carry the ticket number in their custom_id (DynamicItem)."""
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import discord

from . import config
from .tickets import resolve_emoji
from .tickets import (MAX_PING_ROLES, DEFAULT_BUTTON, DEFAULT_PANEL_TEXT, DEFAULT_PANEL_TITLE, MAX_QUESTIONS, PRIORITIES, SETTINGS,
                      TicketError, is_emoji_name, parse_color, parse_questions, parse_setting, setting)

if TYPE_CHECKING:
    from .cogs.ticket import Tickets

OPEN_ID, TYPE_ID = "ticket:open", "ticket:type"
CLOSE_ID, CLAIM_ID, TOOLS_ID = "ticket:close", "ticket:claim", "ticket:tools"
REOPEN_ID, DELETE_ID, TRANSCRIPT_ID = "ticket:reopen", "ticket:delete", "ticket:transcript"


def _cog(i: discord.Interaction) -> "Tickets":
    return i.client.get_cog("ticket")


def _opt_emoji(raw: str, guild=None):
    """A select option emoji; custom emoji names (rflogopurple) are looked up in the server."""
    from .tickets import resolve_emoji
    return resolve_emoji(raw, guild, fallback="🎫") if raw else None


# ------------------------------------------------------------------ panel
class TypeSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(custom_id=TYPE_ID, placeholder="What is your ticket about?", options=options, row=0)

    async def callback(self, i: discord.Interaction):
        await _cog(i).start_open(i, int(self.values[0]) if self.values and self.values[0].isdigit() else None)


class OpenButton(discord.ui.Button):
    def __init__(self, label: str, disabled: bool = False):
        super().__init__(custom_id=OPEN_ID, label=label[:80], emoji="🎫", style=discord.ButtonStyle.primary,
                         disabled=disabled, row=1)

    async def callback(self, i: discord.Interaction):
        await _cog(i).start_open(i, None)


class PanelView(discord.ui.View):
    """types=None builds the registration copy carrying every custom_id. Several types get a dropdown, one type a
    button, no enabled type a disabled button."""

    def __init__(self, types: Optional[list] = None, button_label: str = DEFAULT_BUTTON):
        super().__init__(timeout=None)
        if types is None or len(types) > 1:
            opts = [discord.SelectOption(label=t["name"][:100], value=str(t["id"]), emoji=_opt_emoji(t["emoji"]),
                                         description=(t["description"] or None) and t["description"][:100])
                    for t in (types or [])] or [discord.SelectOption(label="Support", value="0")]
            self.add_item(TypeSelect(opts))
        if types is None or len(types) <= 1:
            self.add_item(OpenButton(button_label, disabled=types == []))


class IntakeModal(discord.ui.Modal):
    def __init__(self, cog: "Tickets", ttype, panel_message: Optional[discord.Message]):
        super().__init__(title=f"{ttype['name']} ticket"[:45], timeout=900)
        self.cog, self.ttype, self.panel_message = cog, ttype, panel_message
        self.inputs = []
        for n, q in enumerate(ttype["questions"][:MAX_QUESTIONS]):
            ti = discord.ui.TextInput(label=q[:45], style=discord.TextStyle.paragraph, required=n == 0, max_length=1000)
            self.inputs.append((q, ti))
            self.add_item(ti)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True, thinking=True)
        answers = [{"q": q, "a": ti.value.strip()} for q, ti in self.inputs]
        await self.cog.create_ticket(i, self.ttype, answers)
        await self.cog.reset_panel_select(self.panel_message)


# --------------------------------------------------------- inside a ticket
class ControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id=CLOSE_ID)
    async def b_close(self, i: discord.Interaction, _):
        await _cog(i).ask_close(i)

    @discord.ui.button(label="Claim", emoji="🙋", style=discord.ButtonStyle.success, custom_id=CLAIM_ID)
    async def b_claim(self, i: discord.Interaction, _):
        await _cog(i).on_claim_button(i)

    @discord.ui.button(label="Staff tools", emoji="🛠️", style=discord.ButtonStyle.secondary, custom_id=TOOLS_ID)
    async def b_tools(self, i: discord.Interaction, _):
        await _cog(i).open_tools(i)


class ClosedView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Reopen", emoji="🔓", style=discord.ButtonStyle.success, custom_id=REOPEN_ID)
    async def b_reopen(self, i: discord.Interaction, _):
        await _cog(i).on_reopen_button(i)

    @discord.ui.button(label="Transcript", emoji="📜", style=discord.ButtonStyle.secondary, custom_id=TRANSCRIPT_ID)
    async def b_transcript(self, i: discord.Interaction, _):
        await _cog(i).on_transcript_button(i)

    @discord.ui.button(label="Delete now", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id=DELETE_ID)
    async def b_delete(self, i: discord.Interaction, _):
        await _cog(i).on_delete_button(i)


class Confirm(discord.ui.View):
    """A private yes/no for the person who pressed the original button."""

    def __init__(self, author_id: int, yes_label: str, on_yes: Callable[[discord.Interaction], Awaitable[None]],
                 style=discord.ButtonStyle.danger):
        super().__init__(timeout=120)
        self.author_id, self.on_yes = author_id, on_yes
        self.b_yes.label, self.b_yes.style = yes_label, style

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.author_id

    @discord.ui.button(label="Yes")
    async def b_yes(self, i: discord.Interaction, _):
        self.stop()
        await self.on_yes(i)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def b_no(self, i: discord.Interaction, _):
        self.stop()
        await i.response.edit_message(content="Cancelled.", view=None)


class ReasonModal(discord.ui.Modal, title="Close with a reason"):
    reason = discord.ui.TextInput(label="Reason (shown in the log and the DM)", style=discord.TextStyle.paragraph,
                                  max_length=500)

    def __init__(self, cog: "Tickets", ticket_id: int):
        super().__init__()
        self.cog, self.ticket_id = cog, ticket_id

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True, thinking=True)
        await i.followup.send(await self.cog.close_by_id(self.ticket_id, i.user, self.reason.value.strip()), ephemeral=True)


class CloseConfirm(discord.ui.View):
    def __init__(self, cog: "Tickets", ticket_id: int, author_id: int):
        super().__init__(timeout=120)
        self.cog, self.ticket_id, self.author_id = cog, ticket_id, author_id

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.author_id

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger)
    async def b_close(self, i: discord.Interaction, _):
        self.stop()
        await i.response.edit_message(content="🔒 Closing…", view=None)
        await i.followup.send(await self.cog.close_by_id(self.ticket_id, i.user, None), ephemeral=True)

    @discord.ui.button(label="Close with reason", emoji="📝", style=discord.ButtonStyle.primary)
    async def b_reason(self, i: discord.Interaction, _):
        self.stop()
        await i.response.send_modal(ReasonModal(self.cog, self.ticket_id))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def b_cancel(self, i: discord.Interaction, _):
        self.stop()
        await i.response.edit_message(content="Kept open.", view=None)


# ------------------------------------------------------------- staff tools
class RenameModal(discord.ui.Modal, title="Rename ticket channel"):
    name = discord.ui.TextInput(label="New channel name", max_length=90, placeholder="ticket-0042-billing")

    def __init__(self, cog: "Tickets", ticket_id: int):
        super().__init__()
        self.cog, self.ticket_id = cog, ticket_id

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True, thinking=True)
        await i.followup.send(await self.cog.rename(self.ticket_id, self.name.value), ephemeral=True)


class NoteModal(discord.ui.Modal, title="Staff note (never shown to the member)"):
    text = discord.ui.TextInput(label="Note", style=discord.TextStyle.paragraph, max_length=1500)

    def __init__(self, cog: "Tickets", ticket_id: int):
        super().__init__()
        self.cog, self.ticket_id = cog, ticket_id

    async def on_submit(self, i: discord.Interaction):
        await self.cog.bot.db.note_add(self.ticket_id, i.user.id, self.text.value.strip())
        await i.response.send_message("🗒️ Note saved. Only staff can see it, and it isn't in the transcript.",
                                      ephemeral=True)


class TransferView(discord.ui.View):
    def __init__(self, cog: "Tickets", ticket_id: int, author_id: int):
        super().__init__(timeout=180)
        self.cog, self.ticket_id, self.author_id = cog, ticket_id, author_id
        sel = discord.ui.UserSelect(placeholder="Hand this ticket to…")
        sel.callback = lambda i, s=sel: self._pick(i, s)
        self.add_item(sel)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.author_id

    async def _pick(self, i: discord.Interaction, sel):
        await i.response.edit_message(content="🔁 Transferring…", view=None)     # answer within Discord's 3 seconds
        await i.edit_original_response(content=await self.cog.transfer(self.ticket_id, i.user, sel.values[0]))


class ToolsView(discord.ui.View):
    """Private staff panel opened from a ticket's Staff tools button."""

    def __init__(self, cog: "Tickets", ticket, types: list, author_id: int):
        super().__init__(timeout=600)
        self.cog, self.ticket_id, self.author_id = cog, ticket["id"], author_id
        pr = discord.ui.Select(placeholder="Priority", row=0, options=[
            discord.SelectOption(label=v, value=k, default=k == ticket["priority"]) for k, v in PRIORITIES.items()])
        pr.callback = lambda i, s=pr: self._run(i, self.cog.set_priority(self.ticket_id, i.user, s.values[0]))
        self.add_item(pr)
        if len(types) > 1:
            mv = discord.ui.Select(placeholder="Move to another ticket type", row=1, options=[
                discord.SelectOption(label=t["name"][:100], value=str(t["id"]), emoji=_opt_emoji(t["emoji"], cog.guild),
                                     default=t["id"] == ticket["type_id"]) for t in types[:25]])
            mv.callback = lambda i, s=mv: self._run(i, self.cog.move(self.ticket_id, i.user, int(s.values[0])))
            self.add_item(mv)
        add = discord.ui.UserSelect(placeholder="Add someone to this ticket", row=2)
        add.callback = lambda i, s=add: self._run(i, self.cog.add_member(self.ticket_id, s.values[0]))
        rem = discord.ui.UserSelect(placeholder="Remove someone from this ticket", row=3)
        rem.callback = lambda i, s=rem: self._run(i, self.cog.remove_member(self.ticket_id, s.values[0]))
        self.add_item(add)
        self.add_item(rem)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.author_id

    async def _run(self, i: discord.Interaction, coro):
        await i.response.defer(ephemeral=True, thinking=True)
        await i.followup.send(await coro, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Transfer", emoji="🔁", row=4)
    async def b_transfer(self, i: discord.Interaction, _):
        await i.response.send_message("Who should take over?", view=TransferView(self.cog, self.ticket_id, i.user.id),
                                      ephemeral=True)

    @discord.ui.button(label="Rename", emoji="✏️", row=4)
    async def b_rename(self, i: discord.Interaction, _):
        await i.response.send_modal(RenameModal(self.cog, self.ticket_id))

    @discord.ui.button(label="Add note", emoji="🗒️", row=4)
    async def b_note(self, i: discord.Interaction, _):
        await i.response.send_modal(NoteModal(self.cog, self.ticket_id))

    @discord.ui.button(label="Notes", emoji="📒", row=4)
    async def b_notes(self, i: discord.Interaction, _):
        await i.response.send_message(**await self.cog.notes_message(self.ticket_id), ephemeral=True)

    @discord.ui.button(label="Unclaim", emoji="↩️", row=4)
    async def b_unclaim(self, i: discord.Interaction, _):
        await self._run(i, self.cog.unclaim(self.ticket_id, i.user))


# ----------------------------------------------------------------- rating
class RateButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:rate:(?P<tid>[0-9]+):(?P<n>[1-5])"):
    def __init__(self, tid: int, n: int):
        super().__init__(discord.ui.Button(label=str(n), emoji="⭐", style=discord.ButtonStyle.secondary,
                                           custom_id=f"ticket:rate:{tid}:{n}"))
        self.tid, self.n = tid, n

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["tid"]), int(match["n"]))

    async def callback(self, i: discord.Interaction):
        await _cog(i).on_rate(i, self.tid, self.n)


class CommentButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:ratec:(?P<tid>[0-9]+)"):
    def __init__(self, tid: int):
        super().__init__(discord.ui.Button(label="Add a comment", emoji="💬", style=discord.ButtonStyle.primary,
                                           custom_id=f"ticket:ratec:{tid}"))
        self.tid = tid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["tid"]))

    async def callback(self, i: discord.Interaction):
        await i.response.send_modal(CommentModal(self.tid))


class CommentModal(discord.ui.Modal, title="How did we do?"):
    text = discord.ui.TextInput(label="Your comment", style=discord.TextStyle.paragraph, max_length=800)

    def __init__(self, tid: int):
        super().__init__()
        self.tid = tid

    async def on_submit(self, i: discord.Interaction):
        await _cog(i).on_rate_comment(i, self.tid, self.text.value.strip())


def rate_view(tid: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for n in range(1, 6):
        v.add_item(RateButton(tid, n))
    return v


def comment_view(tid: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(CommentButton(tid))
    return v


# ----------------------------------------------------------- /admin forms
class PanelTextModal(discord.ui.Modal, title="Ticket panel text"):
    def __init__(self, dash):
        super().__init__()
        self.dash, db = dash, dash.bot.db
        col = db.get("ticket_color")
        self.t = discord.ui.TextInput(label="Title", max_length=256, default=db.get("ticket_panel_title") or DEFAULT_PANEL_TITLE)
        self.d = discord.ui.TextInput(label="Text", style=discord.TextStyle.paragraph, max_length=3500,
                                      default=db.get("ticket_panel_text") or DEFAULT_PANEL_TEXT)
        self.b = discord.ui.TextInput(label="Button label (used when there is one type)", max_length=80,
                                      default=db.get("ticket_button_label") or DEFAULT_BUTTON)
        self.c = discord.ui.TextInput(label="Colour (hex, or 'default')", required=False, max_length=7,
                                      placeholder="#4C1D95", default=f"#{col:06X}" if isinstance(col, int) else "")
        for x in (self.t, self.d, self.b, self.c):
            self.add_item(x)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        db = self.dash.bot.db
        try:
            color = parse_color(self.c.value)
        except TicketError as e:
            return await self.dash.render(f"⚠️ {e} Nothing was saved.")
        await db.set("ticket_panel_title", self.t.value.strip())
        await db.set("ticket_panel_text", self.d.value.strip())
        await db.set("ticket_button_label", self.b.value.strip())
        await db.set("ticket_color", "" if color is None else color)
        note = await self.dash.bot.get_cog("ticket").refresh_panel()
        await self.dash.render("✅ Panel text saved." + (f" {note}" if note else " The panel was updated."))


class SettingsModal(discord.ui.Modal, title="Ticket settings"):
    def __init__(self, dash):
        super().__init__()
        self.dash, self.inputs = dash, {}
        for key, (label, _default, lo, hi) in SETTINGS.items():
            ti = discord.ui.TextInput(label=f"{label}"[:45], default=str(setting(dash.bot.db, key)), max_length=4,
                                      placeholder=f"{lo}-{hi}")
            self.inputs[key] = ti
            self.add_item(ti)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        try:
            vals = {k: parse_setting(k, ti.value) for k, ti in self.inputs.items()}
        except TicketError as e:
            return await self.dash.render(f"⚠️ {e} Nothing was saved.")
        for k, v in vals.items():
            await self.dash.bot.db.set(k, v)
        await self.dash.render("✅ Ticket settings saved.")


class BannerLinkModal(discord.ui.Modal):
    def __init__(self, dash, which: str):
        super().__init__(title="Panel banner" if which == "panel" else "Ticket banner")
        self.dash, self.which = dash, which
        self.link = discord.ui.TextInput(label="Image link (or type clear to remove)", max_length=500,
                                         placeholder="https://…/banner.png")
        self.add_item(self.link)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        cog = self.dash.bot.get_cog("ticket")
        await self.dash.render(await cog.set_banner(self.which, None, self.link.value.strip()))


class TypeModal(discord.ui.Modal):
    def __init__(self, manager: "TypeManager", ttype=None):
        super().__init__(title="Edit ticket type" if ttype else "New ticket type")
        self.manager, self.ttype = manager, ttype
        self.name = discord.ui.TextInput(label="Name", max_length=60, default=ttype["name"] if ttype else None)
        self.emoji = discord.ui.TextInput(label="Emoji or server emoji name", max_length=60, required=False,
                                          placeholder="🛠️  or  rflogopurple",
                                          default=ttype["emoji"] if ttype else "🎫")
        self.desc = discord.ui.TextInput(label="Short description", max_length=100, required=False,
                                         default=ttype["description"] if ttype else None)
        self.qs = discord.ui.TextInput(label=f"Form questions (one per line, max {MAX_QUESTIONS})",
                                       style=discord.TextStyle.paragraph, required=False, max_length=400,
                                       default="\n".join(ttype["questions"]) if ttype else "What do you need help with?")
        for x in (self.name, self.emoji, self.desc, self.qs):
            self.add_item(x)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        try:
            questions = parse_questions(self.qs.value)
        except TicketError as e:
            return await self.manager.render(f"⚠️ {e} Nothing was saved.")
        emoji = self.emoji.value.strip()
        if is_emoji_name(emoji):
            emoji = emoji.strip(":")
            if resolve_emoji(emoji, self.manager.origin.guild, fallback="") == "":
                return await self.manager.render(
                    f"⚠️ I can't find an emoji called **{emoji}** in this server. Check the name, or use a normal "
                    "emoji like 🛠️. Nothing was saved.")
        elif emoji and (" " in emoji or (emoji[0].isascii() and not emoji.startswith("<"))):
            return await self.manager.render("⚠️ The emoji must be one emoji (🛠️) or the name of a server emoji "
                                             "(rflogopurple). Nothing was saved.")
        db = self.manager.cog.bot.db
        fields = dict(name=self.name.value.strip(), emoji=emoji, description=self.desc.value.strip(), questions=questions)
        if self.ttype:
            await db.tt_update(self.ttype["id"], **fields)
            self.manager.selected = self.ttype["id"]
        else:
            self.manager.selected = await db.tt_create(**fields)
        await self.manager.changed("✅ Saved.")


class TypeManager(discord.ui.View):
    """Private editor for ticket types, opened from /admin → Tickets."""

    def __init__(self, cog: "Tickets", origin: discord.Interaction):
        super().__init__(timeout=840)
        self.cog, self.origin, self.author_id = cog, origin, origin.user.id
        self.types: list = []
        self.selected: Optional[int] = None

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.author_id

    @property
    def current(self):
        return next((t for t in self.types if t["id"] == self.selected), None)

    def _build(self):
        self.clear_items()
        g, cur = self.origin.guild, self.current
        if self.types:
            pick = discord.ui.Select(placeholder="Pick a ticket type to edit", row=0, options=[
                discord.SelectOption(label=f"{t['name']}{'' if t['enabled'] else ' (off)'}"[:100], value=str(t["id"]),
                                     emoji=_opt_emoji(t["emoji"], self.origin.guild), default=t["id"] == self.selected)
                for t in self.types[:25]])
            pick.callback = lambda i, s=pick: self._pick(i, s)
            self.add_item(pick)
        if cur:
            roles = discord.ui.RoleSelect(
                placeholder="Staff roles for this type (empty = Sect Admin + staff roles)", row=1, min_values=0, max_values=10,
                default_values=[discord.SelectDefaultValue(id=r, type=discord.SelectDefaultValueType.role)
                                for r in cur["staff_role_ids"] if g.get_role(r)])
            roles.callback = lambda i, s=roles: self._set(i, staff_role_ids=[r.id for r in s.values if not r.is_default()])
            self.add_item(roles)
            cat = discord.ui.ChannelSelect(
                placeholder="Category for these tickets (empty = shared Tickets category)", row=2, min_values=0,
                max_values=1, channel_types=[discord.ChannelType.category],
                default_values=[discord.SelectDefaultValue(id=cur["category_id"], type=discord.SelectDefaultValueType.channel)]
                if cur["category_id"] and g.get_channel(cur["category_id"]) else [])
            cat.callback = lambda i, s=cat: self._set(i, category_id=s.values[0].id if s.values else None)
            self.add_item(cat)
            ping = discord.ui.RoleSelect(
                placeholder="Roles pinged for this type (empty = the server-wide ping roles)", row=3, min_values=0,
                max_values=MAX_PING_ROLES,
                default_values=[discord.SelectDefaultValue(id=r, type=discord.SelectDefaultValueType.role)
                                for r in cur["ping_role_ids"] if g.get_role(r)])
            ping.callback = lambda i, s=ping: self._set(i, ping_role_ids=[r.id for r in s.values if not r.is_default()])
            self.add_item(ping)
        for label, emoji, cb, style, off in (
                ("New type", "➕", self._new, discord.ButtonStyle.success, len(self.types) >= 10),
                ("Edit text & form", "📝", self._edit, discord.ButtonStyle.primary, cur is None),
                ("Turn off" if cur and cur["enabled"] else "Turn on", "⏯️", self._toggle, discord.ButtonStyle.secondary, cur is None),
                ("Own category", "📁", self._own_category, discord.ButtonStyle.secondary, cur is None),
                ("Delete", "🗑️", self._delete, discord.ButtonStyle.danger, cur is None)):
            b = discord.ui.Button(label=label, emoji=emoji, style=style, row=4, disabled=off)
            b.callback = cb
            self.add_item(b)

    def embed(self, notice: Optional[str]) -> discord.Embed:
        e = discord.Embed(color=config.COLOR_MAIN, title="🎫 Ticket types")
        lines = []
        for t in self.types:
            roles = " ".join(f"<@&{r}>" for r in t["staff_role_ids"]) or f"{config.ADMIN_ROLE_NAME} + staff roles"
            lines.append(f"{self.cog.emoji(t['emoji'])} **{t['name']}**{'' if t['enabled'] else ' · ❌ off'} · handled by {roles}"
                         + (f" · <#{t['category_id']}>" if t["category_id"] else "")
                         + (" · pings " + " ".join(f"<@&{r}>" for r in t["ping_role_ids"]) if t["ping_role_ids"] else "")
                         + f" · {len(t['questions'])} question{'s' if len(t['questions']) != 1 else ''}")
        e.description = ("\n".join(lines) or "*No ticket types yet. Press **New type**.*")[:3800]
        cur = self.current
        if cur:
            e.add_field(name=f"Editing: {cur['name']}", value=(
                "\n".join(f"{n}. {q}" for n, q in enumerate(cur["questions"], 1)) or "*No form: the ticket opens straight away.*"),
                inline=False)
        e.set_footer(text="Changes show on the ticket panel straight away. Up to 10 types.")
        if notice:
            e.add_field(name="​", value=notice, inline=False)
        return e

    async def render(self, notice: Optional[str] = None):
        self.types = list(await self.cog.bot.db.tt_list())
        if self.selected is None and self.types:
            self.selected = self.types[0]["id"]
        self._build()
        try:
            await self.origin.edit_original_response(content=None, embed=self.embed(notice), view=self)
        except discord.HTTPException:
            pass

    async def changed(self, notice: str):
        await self.cog.reload_types()
        note = await self.cog.refresh_panel()
        await self.render(notice + (f" {note}" if note else ""))

    async def _pick(self, i, sel):
        await i.response.defer()
        self.selected = int(sel.values[0])
        await self.render()

    async def _set(self, i, **fields):
        await i.response.defer()
        await self.cog.bot.db.tt_update(self.selected, **fields)
        await self.changed("✅ Saved.")

    async def _new(self, i):
        await i.response.send_modal(TypeModal(self))

    async def _edit(self, i):
        await i.response.send_modal(TypeModal(self, self.current))

    async def _toggle(self, i):
        await i.response.defer()
        cur = self.current
        await self.cog.bot.db.tt_update(cur["id"], enabled=not cur["enabled"])
        await self.changed("⏯️ " + ("Turned off: it's hidden from the panel." if cur["enabled"] else "Turned on."))

    async def _own_category(self, i):
        await i.response.defer()
        await self.changed(await self.cog.create_type_category(self.current))

    async def _delete(self, i):
        cur = self.current

        async def yes(ci: discord.Interaction):
            await ci.response.edit_message(content=f"🗑️ **{cur['name']}** deleted.", view=None)
            await self.cog.bot.db.tt_delete(cur["id"])
            self.selected = None
            await self.changed(f"🗑️ **{cur['name']}** deleted. Its open tickets keep working.")

        await i.response.send_message(f"Delete the **{cur['name']}** ticket type? Open tickets of this type stay open.",
                                      view=Confirm(i.user.id, "Delete it", yes), ephemeral=True)

    async def on_timeout(self):
        try:
            await self.origin.edit_original_response(view=None)
        except discord.HTTPException:
            pass
