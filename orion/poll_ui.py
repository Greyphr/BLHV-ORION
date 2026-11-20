"""Button-driven poll builder (used by the /admin panel). `/poll create` does the same thing in one command."""
import asyncio
import io
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Optional

import aiohttp
import discord

from .cogs.announce import fetch_image, store_image
from .giveaways import DurationError, format_duration, parse_duration
from .polls import MAX_DURATION, MAX_OPTION_LEN, MAX_QUESTION_LEN, MIN_DURATION, PollError, build_embed, parse_options

if TYPE_CHECKING:
    from .cogs.poll import Polls

log = logging.getLogger("orion.poll")
utcnow = discord.utils.utcnow


@dataclass
class PollDraft:
    question: str = ""
    options: list = field(default_factory=list)
    duration: Optional[int] = None          # seconds, None = open until staff close it
    private: bool = False
    final: bool = False                     # votes can't be changed
    role_ids: list = field(default_factory=list)
    banner_id: Optional[int] = None


class TextModal(discord.ui.Modal, title="Poll question & options"):
    def __init__(self, b: "PollBuilder"):
        super().__init__()
        self.b, d = b, b.draft
        self.q = discord.ui.TextInput(label="Question", max_length=MAX_QUESTION_LEN, default=d.question or None,
                                      placeholder="Which day should we run the tournament?")
        self.o = discord.ui.TextInput(label="Options: one per line (2 to 10)", style=discord.TextStyle.paragraph,
                                      default="\n".join(d.options) or None, max_length=900,
                                      placeholder="Friday\nSaturday\nSunday")
        self.t = discord.ui.TextInput(label="How long? e.g. 2h or 1d 12h (empty = until you close it)", required=False,
                                      max_length=20, default=format_duration(d.duration) if d.duration else None)
        for x in (self.q, self.o, self.t):
            self.add_item(x)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        d = self.b.draft
        try:
            opts = parse_options(self.o.value)
            secs = parse_duration(self.t.value, MIN_DURATION, MAX_DURATION) if self.t.value.strip() else None
        except (PollError, DurationError) as e:
            return await self.b.render(f"⚠️ {e}")
        d.question, d.options, d.duration = self.q.value.strip(), opts, secs
        await self.b.render()


class BannerModal(discord.ui.Modal, title="Poll banner"):
    def __init__(self, b: "PollBuilder"):
        super().__init__()
        self.b = b
        self.link = discord.ui.TextInput(label="Image link (or type clear to remove)", max_length=500,
                                         placeholder="https://…/banner.png")
        self.add_item(self.link)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        v = self.link.value.strip()
        if v.lower() in ("clear", "none", "remove"):
            self.b.draft.banner_id = None
            return await self.b.render()
        try:
            self.b.draft.banner_id = await store_image(self.b.cog.bot.db, await fetch_image(v))
        except ValueError as e:
            return await self.b.render(f"⚠️ Banner: {e}")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return await self.b.render("⚠️ Banner: I couldn't download that link.")
        await self.b.render()


class PollBuilder(discord.ui.View):
    """Private builder: the card preview is the message itself; every setting is a button or dropdown."""

    def __init__(self, cog: "Polls", origin: discord.Interaction):
        super().__init__(timeout=840)                 # stays inside the 15-minute interaction token life
        self.cog, self.origin, self.draft = cog, origin, PollDraft()
        self.author_id, self.channel_id = origin.user.id, origin.channel_id
        self._publishing = False

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.author_id:
            await i.response.send_message("This builder belongs to someone else.", ephemeral=True)
            return False
        return True

    # ------------------------------------------------------------ display
    def _poll(self) -> dict:
        d = self.draft
        return {"id": None, "question": d.question or "Your question appears here",
                "options": d.options or ["Option 1", "Option 2"], "anonymous": d.private, "allow_change": not d.final,
                "eligible_role_ids": d.role_ids,
                "ends_at": utcnow() + timedelta(seconds=d.duration) if d.duration else None}

    def problems(self) -> list[str]:
        d, p = self.draft, []
        if not d.question or not d.options:
            p.append("add the question and options (📝 Question & options)")
        if self.channel_id is None:
            p.append("pick the channel")
        return p

    def _sync(self):
        d = self.draft
        T = discord.SelectDefaultValueType
        self.pick_channel.default_values = [discord.SelectDefaultValue(id=self.channel_id, type=T.channel)] \
            if self.channel_id else []
        self.pick_roles.default_values = [discord.SelectDefaultValue(id=r, type=T.role) for r in d.role_ids[:25]]
        self.b_private.label = "Private: on" if d.private else "Private: off"
        self.b_private.emoji = "🔒" if d.private else "🌐"
        self.b_private.style = discord.ButtonStyle.primary if d.private else discord.ButtonStyle.secondary
        self.b_final.label = f"Votes final: {'on' if d.final else 'off'}"
        self.b_final.style = discord.ButtonStyle.primary if d.final else discord.ButtonStyle.secondary

    def status_text(self, notice: Optional[str]) -> str:
        d = self.draft
        ch = f"<#{self.channel_id}>" if self.channel_id else "*not selected*"
        lines = ["**📊 Poll builder**: the card below is what will be posted. Nothing is posted until you press **Publish**.",
                 f"Channel: {ch} · Runs: **{format_duration(d.duration) if d.duration else 'until you close it'}** · "
                 f"Who can vote: **{'only the chosen roles' if d.role_ids else 'everyone'}**",
                 ("🔒 **Private**: members never see who voted, only staff do." if d.private else
                  "🌐 **Public**: anyone can see who voted.")
                 + (" · Votes can't be changed." if d.final else " · Members can change their vote.")]
        if notice:
            lines.append(notice)
        return "\n".join(lines)

    async def render(self, notice: Optional[str] = None):
        self._sync()
        url, files = await self.cog._banner({"banner_id": self.draft.banner_id})
        try:
            await self.origin.edit_original_response(
                content=self.status_text(notice), embed=build_embed(self._poll(), {}, banner_url=url),
                attachments=files, view=self)
        except discord.HTTPException:
            log.exception("Poll builder render failed")

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        try:
            await self.origin.edit_original_response(content="⏱️ Builder timed out. Open it again from `/admin`.",
                                                     embed=None, attachments=[], view=None)
        except discord.HTTPException:
            pass

    # ----------------------------------------------------------- controls
    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text, discord.ChannelType.news],
                       placeholder="Channel to post the poll in", row=0)
    async def pick_channel(self, i: discord.Interaction, sel: discord.ui.ChannelSelect):
        await i.response.defer()
        self.channel_id = sel.values[0].id if sel.values else None
        await self.render()

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Only these roles can vote (leave empty = everyone)",
                       min_values=0, max_values=5, row=1)
    async def pick_roles(self, i: discord.Interaction, sel: discord.ui.RoleSelect):
        await i.response.defer()
        self.draft.role_ids = [r.id for r in sel.values if not r.is_default()]
        await self.render()

    @discord.ui.button(label="Question & options", emoji="📝", style=discord.ButtonStyle.primary, row=2)
    async def b_text(self, i: discord.Interaction, _):
        await i.response.send_modal(TextModal(self))

    @discord.ui.button(label="Banner", emoji="🖼️", style=discord.ButtonStyle.secondary, row=2)
    async def b_banner(self, i: discord.Interaction, _):
        await i.response.send_modal(BannerModal(self))

    @discord.ui.button(label="Private: off", style=discord.ButtonStyle.secondary, row=2)
    async def b_private(self, i: discord.Interaction, _):
        await i.response.defer()
        self.draft.private = not self.draft.private
        await self.render()

    @discord.ui.button(label="Votes final: off", style=discord.ButtonStyle.secondary, row=2)
    async def b_final(self, i: discord.Interaction, _):
        await i.response.defer()
        self.draft.final = not self.draft.final
        await self.render()

    @discord.ui.button(label="Publish", emoji="🚀", style=discord.ButtonStyle.success, row=3)
    async def b_publish(self, i: discord.Interaction, _):
        await i.response.defer()
        if self._publishing:
            return
        if problems := self.problems():
            return await self.render("⚠️ Can't publish yet: " + "; ".join(problems) + ".")
        d, g = self.draft, self.origin.guild
        self._publishing = True
        try:
            msg, pid, note = await self.cog.publish_poll(
                user=i.user, guild=g, channel=g.get_channel(self.channel_id), question=d.question, options=d.options,
                private=d.private, seconds=d.duration, role_ids=d.role_ids, votes_final=d.final, banner_id=d.banner_id)
        except PollError as e:
            self._publishing = False
            return await self.render(f"⚠️ {e}")
        except Exception:
            self._publishing = False
            log.exception("Publishing a poll failed")
            return await self.render("⚠️ Something went wrong. Nothing was posted.")
        self.stop()
        await self.origin.edit_original_response(
            content=f"✅ Poll **#{pid}** is live: {msg.jump_url}\nResults and voters: `/admin` → Polls → Manage.{note}",
            embed=None, attachments=[], view=None)

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.danger, row=3)
    async def b_cancel(self, i: discord.Interaction, _):
        await i.response.defer()
        self.stop()
        await self.origin.edit_original_response(content="Cancelled. Nothing was posted.", embed=None,
                                                 attachments=[], view=None)


class PollPick(discord.ui.View):
    """Choose one of the recent polls, then open its staff panel."""

    def __init__(self, cog: "Polls", origin: discord.Interaction, rows: list):
        super().__init__(timeout=300)
        self.cog, self.origin = cog, origin
        sel = discord.ui.Select(placeholder="Choose a poll", options=[
            discord.SelectOption(label=f"#{r['id']} · {r['question']}"[:100], value=str(r["id"]),
                                 description=f"{r['status']} · {'private' if r['anonymous'] else 'public'}")
            for r in rows[:25]])
        sel.callback = self._picked
        self.add_item(sel)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.origin.user.id

    async def _picked(self, i: discord.Interaction):
        await i.response.defer()
        self.stop()
        await self.cog.show_panel(self.origin, int(i.data["values"][0]))
