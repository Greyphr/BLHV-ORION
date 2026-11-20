"""Polls (public & private).

/poll create   question, options ("Yes | No | Maybe"), public or private, optional timer / role lock / channel
/poll manage   staff-only panel: live results, WHO voted for what (this is the only place a private poll shows names),
               end now, export CSV, delete

Members press an option button on the poll message (persistent: survives restarts). One vote each; they can change it
unless the poll was created with votes final. Public polls get a "Who voted" button; private polls never show names to
members. Timers are stored timestamps checked by a loop, so a poll that expired while the bot was offline is closed on
the next start.
"""
import asyncio
import io
import logging
from collections import defaultdict
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import aiohttp

from .. import config
from ..checks import require_staff
from ..giveaways import DurationError, csv_bytes, format_duration, parse_duration
from .announce import fetch_image, store_attachment, store_image
from ..poll_ui import PollBuilder, PollPick
from ..polls import MAX_DURATION, MAX_OPTIONS, MIN_DURATION, PollError, build_embed, parse_options

log = logging.getLogger("orion.poll")
utcnow = discord.utils.utcnow

VOTE_PREFIX, VOTERS_ID = "poll:o:", "poll:voters"


class VoteButton(discord.ui.Button):
    def __init__(self, cog: "Polls", idx: int, label: str, disabled: bool = False):
        super().__init__(label=label[:80], style=discord.ButtonStyle.secondary, custom_id=f"{VOTE_PREFIX}{idx}",
                         row=idx // 5, disabled=disabled)
        self.cog, self.idx = cog, idx

    async def callback(self, i: discord.Interaction):
        await self.cog.on_vote(i, self.idx)


class VotersButton(discord.ui.Button):
    def __init__(self, cog: "Polls", row: int, disabled: bool = False):
        super().__init__(label="Who voted", emoji="👥", style=discord.ButtonStyle.primary, custom_id=VOTERS_ID,
                         row=row, disabled=disabled)
        self.cog = cog

    async def callback(self, i: discord.Interaction):
        await self.cog.on_voters(i)


class PollView(discord.ui.View):
    """The buttons under a poll message. Persistent: the same custom_ids are re-registered at start-up."""

    def __init__(self, cog: "Polls", options, *, voters_button: bool, disabled: bool = False):
        super().__init__(timeout=None)
        for n, name in enumerate(options):
            self.add_item(VoteButton(cog, n, name, disabled))
        if voters_button:
            self.add_item(VotersButton(cog, (len(options) - 1) // 5 + 1, disabled))


class ConfirmDelete(discord.ui.View):
    def __init__(self, cog: "Polls", poll_id: int, author_id: int):
        super().__init__(timeout=120)
        self.cog, self.poll_id, self.author_id = cog, poll_id, author_id

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.author_id

    @discord.ui.button(label="Yes, delete it", style=discord.ButtonStyle.danger)
    async def yes(self, i: discord.Interaction, _):
        await i.response.defer()
        await self.cog.delete_poll(self.poll_id)
        self.stop()
        await i.edit_original_response(content=f"🗑️ Poll #{self.poll_id} and all its votes were deleted.", view=None)

    @discord.ui.button(label="Keep it", style=discord.ButtonStyle.secondary)
    async def no(self, i: discord.Interaction, _):
        await i.response.defer()
        self.stop()
        await i.edit_original_response(content="Kept.", view=None)


class PanelBannerModal(discord.ui.Modal, title="Poll banner"):
    def __init__(self, panel: "ManagePanel"):
        super().__init__()
        self.panel = panel
        self.link = discord.ui.TextInput(label="Image link (or type clear to remove)", max_length=500,
                                         placeholder="https://…/banner.png")
        self.add_item(self.link)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        cog, v = self.panel.cog, self.link.value.strip()
        try:
            new_id = None if v.lower() in ("clear", "none", "remove") else await cog._store_banner(None, v)
        except ValueError as e:
            return await self.panel.render(f"⚠️ Banner: {e}")
        await cog.bot.db.poll_set_banner(self.panel.poll_id, new_id)
        ok = await cog.refresh(self.panel.poll_id, reupload=True)
        await self.panel.render(("🗑️ Banner removed." if new_id is None else "✅ Banner updated.")
                                + ("" if ok else " ⚠️ I couldn't edit the poll message."))


class ManagePanel(discord.ui.View):
    """Staff-only. The ONLY place a private poll reveals who voted."""

    def __init__(self, cog: "Polls", origin: discord.Interaction, poll_id: int):
        super().__init__(timeout=840)
        self.cog, self.origin, self.poll_id = cog, origin, poll_id
        self.author_id = origin.user.id

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.author_id:
            await i.response.send_message("This panel belongs to someone else. Run `/poll manage` yourself.", ephemeral=True)
            return False
        return True

    async def render(self, notice: Optional[str] = None):
        poll = await self.cog.bot.db.poll_get(self.poll_id)
        if poll is None:
            return await self.origin.edit_original_response(content="That poll no longer exists.", embed=None, view=None)
        e = await self.cog.staff_embed(poll)
        self.b_end.disabled = poll["status"] != "active"
        await self.origin.edit_original_response(content=notice, embed=e, view=self)

    async def on_timeout(self):
        try:
            await self.origin.edit_original_response(view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="End now", emoji="🏁", style=discord.ButtonStyle.primary, row=0)
    async def b_end(self, i: discord.Interaction, _):
        await i.response.defer()
        ended = await self.cog.finish(self.poll_id)
        await self.render("🏁 Poll closed and the final results were posted." if ended else "That poll was already closed.")

    @discord.ui.button(label="Export CSV", emoji="📤", style=discord.ButtonStyle.secondary, row=0)
    async def b_export(self, i: discord.Interaction, _):
        await i.response.defer(ephemeral=True)
        poll = await self.cog.bot.db.poll_get(self.poll_id)
        if poll is None:
            return await i.followup.send("That poll no longer exists.", ephemeral=True)
        guild = self.cog.guild
        rows = [(r["user_id"], self.cog.name_of(guild, r["user_id"]) if guild else "", poll["options"][r["option_idx"]],
                 r["voted_at"].isoformat(), r["changed_at"].isoformat() if r["changed_at"] else "")
                for r in await self.cog.bot.db.poll_voters(self.poll_id)
                if r["option_idx"] < len(poll["options"])]
        data = csv_bytes(["user_id", "name", "option", "voted_at", "changed_at"], rows)
        await i.followup.send("📤 Every vote with the voter.",
                              file=discord.File(io.BytesIO(data), filename=f"poll-{self.poll_id}-votes.csv"), ephemeral=True)

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
    async def b_refresh(self, i: discord.Interaction, _):
        await i.response.defer()
        await self.render()

    @discord.ui.button(label="Banner", emoji="🖼️", style=discord.ButtonStyle.secondary, row=1)
    async def b_banner(self, i: discord.Interaction, _):
        await i.response.send_modal(PanelBannerModal(self))

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def b_delete(self, i: discord.Interaction, _):
        await i.response.send_message(
            f"Delete poll #{self.poll_id}, its message and **all votes**? This can't be undone.",
            view=ConfirmDelete(self.cog, self.poll_id, self.author_id), ephemeral=True)

    @discord.ui.button(label="Close", emoji="✖️", style=discord.ButtonStyle.secondary, row=1)
    async def b_close(self, i: discord.Interaction, _):
        await i.response.defer()
        self.stop()
        await self.origin.edit_original_response(content="Panel closed.", embed=None, view=None)


class Polls(commands.GroupCog, name="poll", description="Public & private polls (staff)"):
    def __init__(self, bot):
        self.bot = bot
        self._dirty: set[int] = set()
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_load(self):
        # One registered view carrying every possible custom_id keeps the buttons of EVERY poll alive after a restart.
        self.bot.add_view(PollView(self, ["·"] * MAX_OPTIONS, voters_button=True))
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_staff(interaction)

    @property
    def guild(self) -> Optional[discord.Guild]:
        return self.bot.get_guild(config.GUILD_ID)

    @staticmethod
    def name_of(guild: discord.Guild, uid: int) -> str:
        m = guild.get_member(uid) if guild else None
        return m.display_name if m else f"user {uid}"

    # ------------------------------------------------------------ commands
    @app_commands.command(name="create", description="Post a poll (public: voters visible, private: only staff see voters)")
    @app_commands.describe(
        question="What are you asking?",
        options="2 to 10 options separated by |   e.g.  Friday | Saturday | Sunday",
        visibility="Public shows who voted. Private hides voters from members (staff can still see)",
        duration="How long it runs, e.g. 2h, 1d 12h. Leave empty to close it yourself",
        channel="Where to post it (default: this channel)",
        role="Only members with this role can vote",
        role2="A second role that can also vote (any one is enough)",
        votes_final="Don't let members change their vote (default: they can)",
        banner="Banner image shown under the results (upload)",
        banner_url="Banner image as a link (use this OR the upload)")
    @app_commands.choices(visibility=[app_commands.Choice(name="Public (voters visible)", value="public"),
                                      app_commands.Choice(name="Private (voters hidden, staff only)", value="private")])
    async def create(self, i: discord.Interaction, question: app_commands.Range[str, 3, 250], options: str,
                     visibility: app_commands.Choice[str], duration: Optional[str] = None,
                     channel: Optional[discord.TextChannel] = None, role: Optional[discord.Role] = None,
                     role2: Optional[discord.Role] = None, votes_final: bool = False,
                     banner: Optional[discord.Attachment] = None, banner_url: Optional[str] = None):
        await i.response.defer(ephemeral=True)
        try:
            opts = parse_options(options)
            secs = parse_duration(duration, MIN_DURATION, MAX_DURATION) if duration and duration.strip() else None
        except (PollError, DurationError) as e:
            return await i.followup.send(f"⚠️ {e}", ephemeral=True)
        banner_id, banner_note = None, ""
        try:
            banner_id = await self._store_banner(banner, banner_url)
        except ValueError as e:
            banner_note = f"\n⚠️ Banner skipped: {e}"
        role_ids = [r.id for r in (role, role2) if r is not None and not r.is_default()]
        try:
            msg, pid, note = await self.publish_poll(
                user=i.user, guild=i.guild, channel=channel or i.channel, question=question, options=opts,
                private=visibility.value == "private", seconds=secs, role_ids=role_ids, votes_final=votes_final,
                banner_id=banner_id)
        except PollError as e:
            return await i.followup.send(f"⚠️ {e}", ephemeral=True)
        await i.followup.send(
            f"✅ Poll **#{pid}** is live: {msg.jump_url}\n"
            f"{'🔒 Private: members never see who voted.' if visibility.value == 'private' else '🌐 Public: anyone can see who voted.'} "
            f"{'Runs for ' + format_duration(secs) + '.' if secs else 'It stays open until you end it.'}\n"
            "Results and voters: `/poll manage`." + (banner_note or note), ephemeral=True)

    async def publish_poll(self, *, user, guild, channel, question, options, private, seconds, role_ids, votes_final,
                           banner_id) -> tuple[discord.Message, int, str]:
        """Store + post a poll. Returns (message, poll id, note about anything skipped). Raises PollError for the admin."""
        perms = channel.permissions_for(guild.me) if channel else None
        if channel is None or not perms.view_channel or not perms.send_messages or not perms.embed_links:
            where = channel.mention if channel else "that channel"
            raise PollError(f"I need **View Channel, Send Messages and Embed Links** in {where}.")
        db, note = self.bot.db, ""
        pid = await db.poll_create(question=question.strip(), options=options, anonymous=private,
                                   allow_change=not votes_final, role_ids=list(role_ids), channel_id=channel.id,
                                   created_by=user.id, banner_id=banner_id,
                                   ends_at=utcnow() + timedelta(seconds=seconds) if seconds else None)
        poll = await db.poll_get(pid)
        url, files = await self._banner(poll)
        if files and not perms.attach_files:
            url, files, note = None, [], "\n⚠️ Banner skipped: I need **Attach Files** in that channel."
        try:
            msg = await channel.send(embed=build_embed(poll, {}, banner_url=url), view=self._view(poll), files=files,
                                     allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            await db.poll_delete(pid)
            raise PollError(f"Discord refused the post: {e.text or e}")
        await db.poll_set_message(pid, msg.id)
        log.info("Poll #%s created by %s (%s)", pid, user.id, "private" if private else "public")
        return msg, pid, note

    async def _ac(self, i: discord.Interaction, current: str):
        rows = await self.bot.db.poll_list(25)
        return [app_commands.Choice(name=f"#{r['id']} · {r['question']} ({r['status']})"[:100], value=str(r["id"]))
                for r in rows if current.lower() in r["question"].lower() or current in str(r["id"])][:25]

    async def _store_banner(self, att: Optional[discord.Attachment], url: Optional[str]) -> Optional[int]:
        """Upload wins over a link. Raises ValueError with a plain-words reason."""
        if att is not None:
            return await store_attachment(self.bot.db, att)
        if url and url.strip():
            try:
                return await store_image(self.bot.db, await fetch_image(url.strip()))
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise ValueError("I couldn't download that link.")
        return None

    async def _banner(self, poll) -> tuple[Optional[str], list[discord.File]]:
        """(attachment:// url for the embed, the file to upload). The file stays on the message once uploaded."""
        if not poll["banner_id"]:
            return None, []
        row = await self.bot.db.get_image(poll["banner_id"])
        if not row:
            return None, []
        name = f"poll-banner.{row['ext']}"
        return f"attachment://{name}", [discord.File(io.BytesIO(row["data"]), filename=name)]

    async def _banner_url(self, poll) -> Optional[str]:
        if not poll["banner_id"]:
            return None
        ext = await self.bot.db.image_ext(poll["banner_id"])
        return f"attachment://poll-banner.{ext}" if ext else None

    @app_commands.command(name="banner", description="Add, replace or remove the banner of a poll")
    @app_commands.describe(poll="Which poll", image="New banner (upload)", image_url="New banner (link)",
                           remove="Remove the banner")
    @app_commands.autocomplete(poll=_ac)
    async def banner_cmd(self, i: discord.Interaction, poll: str, image: Optional[discord.Attachment] = None,
                         image_url: Optional[str] = None, remove: bool = False):
        await i.response.defer(ephemeral=True)
        row = await self.bot.db.poll_get(int(poll)) if poll.isdigit() else None
        if row is None:
            return await i.followup.send("I couldn't find that poll.", ephemeral=True)
        if not remove and image is None and not (image_url and image_url.strip()):
            return await i.followup.send("Attach an image, paste a link, or tick **remove**.", ephemeral=True)
        try:
            new_id = None if remove else await self._store_banner(image, image_url)
        except ValueError as e:
            return await i.followup.send(f"⚠️ {e}", ephemeral=True)
        await self.bot.db.poll_set_banner(row["id"], new_id)
        ok = await self.refresh(row["id"], reupload=True)
        await i.followup.send(("🗑️ Banner removed." if remove else "✅ Banner updated.")
                              + ("" if ok else " ⚠️ I couldn't edit the poll message (deleted, or I lack permission)."),
                              ephemeral=True)

    @app_commands.command(name="manage", description="Results, who voted, end early, export (staff only)")
    @app_commands.describe(poll="Which poll (default: the latest)")
    @app_commands.autocomplete(poll=_ac)
    async def manage(self, i: discord.Interaction, poll: Optional[str] = None):
        await i.response.defer(ephemeral=True)
        await self.open_manage(i, poll)

    async def open_manage(self, i: discord.Interaction, poll: Optional[str] = None):
        """Staff panel for one poll, or a picker. `i` must already be deferred ephemerally (also used by /admin)."""
        if poll is None:
            rows = await self.bot.db.poll_list(25)
            if not rows:
                return await i.followup.send("No polls yet. Start one with `/poll create`.", ephemeral=True)
            return await i.followup.send("Which poll?", view=PollPick(self, i, rows), ephemeral=True)
        if not poll.isdigit() or await self.bot.db.poll_get(int(poll)) is None:
            return await i.followup.send("I couldn't find that poll.", ephemeral=True)
        await self.show_panel(i, int(poll))

    async def show_panel(self, origin: discord.Interaction, poll_id: int):
        await ManagePanel(self, origin, poll_id).render()

    async def open_builder(self, i: discord.Interaction):
        """Button-driven builder. `i` must already be deferred ephemerally (also used by /admin)."""
        await PollBuilder(self, i).render()

    # ------------------------------------------------------------- display
    def _view(self, poll, *, ended: bool = False) -> PollView:
        return PollView(self, poll["options"], voters_button=not poll["anonymous"], disabled=ended)

    async def staff_embed(self, poll) -> discord.Embed:
        db = self.bot.db
        counts = await db.poll_counts(poll["id"])
        e = build_embed(dict(poll), counts, ended=poll["status"] != "active")
        e.title = f"🛠️ {e.title}"
        by_opt: dict[int, list[int]] = defaultdict(list)
        for r in await db.poll_voters(poll["id"]):
            by_opt[r["option_idx"]].append(r["user_id"])
        e.clear_fields()
        for n, name in enumerate(poll["options"]):
            ids, text, shown = by_opt.get(n, []), "", 0
            for uid in ids:
                piece = f"<@{uid}> "
                if len(text) + len(piece) > 900:
                    break
                text, shown = text + piece, shown + 1
            if len(ids) > shown:
                text += f"… +{len(ids) - shown} more (use Export CSV)"
            e.add_field(name=f"{n + 1}. {name} ({len(ids)})"[:256], value=text or "*nobody yet*", inline=False)
        e.set_footer(text=("🔒 Private poll: members cannot see this list" if poll["anonymous"] else "🌐 Public poll")
                     + f" · Poll #{poll['id']} · " + ("open" if poll["status"] == "active" else "ended"))
        return e

    async def _channel(self, cid: int):
        ch = self.bot.get_channel(cid)
        if ch is not None:
            return ch
        try:
            return await self.bot.fetch_channel(cid)
        except discord.HTTPException:
            return None

    async def refresh(self, poll_id: int, reupload: bool = False) -> bool:
        """Redraw the poll message from the database (live tally, or final results once ended).
        reupload=True also re-sends the banner file (needed when the banner was added, replaced or removed);
        ordinary refreshes leave the message's existing attachment alone."""
        async with self._locks[poll_id]:
            poll = await self.bot.db.poll_get(poll_id)
            if poll is None or not poll["message_id"]:
                return False
            ch = await self._channel(poll["channel_id"])
            if ch is None:
                return False
            ended = poll["status"] != "active"
            extra = {}
            if reupload:
                url, files = await self._banner(poll)
                extra["attachments"] = files
            else:
                url = await self._banner_url(poll)
            try:
                await ch.get_partial_message(poll["message_id"]).edit(
                    embed=build_embed(dict(poll), await self.bot.db.poll_counts(poll_id), ended=ended, banner_url=url),
                    view=self._view(poll, ended=ended), **extra)
                return True
            except discord.NotFound:
                log.info("Poll #%s: the message was deleted", poll_id)
            except discord.HTTPException:
                log.warning("Poll #%s: could not update the message", poll_id, exc_info=True)
            return False

    async def finish(self, poll_id: int) -> bool:
        if not await self.bot.db.poll_end(poll_id):
            return False
        self._dirty.discard(poll_id)
        await self.refresh(poll_id)
        poll = await self.bot.db.poll_get(poll_id)
        ch = await self._channel(poll["channel_id"]) if poll else None
        if ch is not None and poll["message_id"]:
            try:
                await ch.send(f"📊 **Poll ended:** {poll['question']}",
                              reference=discord.MessageReference(message_id=poll["message_id"], channel_id=ch.id,
                                                                 fail_if_not_exists=False),
                              allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
        log.info("Poll #%s ended", poll_id)
        return True

    async def delete_poll(self, poll_id: int):
        poll = await self.bot.db.poll_get(poll_id)
        if poll and poll["message_id"]:
            ch = await self._channel(poll["channel_id"])
            if ch is not None:
                try:
                    await ch.get_partial_message(poll["message_id"]).delete()
                except discord.HTTPException:
                    pass
        self._dirty.discard(poll_id)
        await self.bot.db.poll_delete(poll_id)

    # ------------------------------------------------------- member buttons
    async def on_vote(self, i: discord.Interaction, idx: int):
        await i.response.defer(ephemeral=True)
        poll = await self.bot.db.poll_by_message(i.message.id) if i.message else None
        if poll is None:
            return await i.followup.send("I can't find this poll any more.", ephemeral=True)
        need = set(poll["eligible_role_ids"])
        if need and not (isinstance(i.user, discord.Member) and any(r.id in need for r in i.user.roles)):
            roles = " or ".join(f"<@&{r}>" for r in need)
            return await i.followup.send(f"🚫 Only members with {roles} can vote in this poll.", ephemeral=True,
                                         allowed_mentions=discord.AllowedMentions.none())
        out = await self.bot.db.poll_vote(poll["id"], i.user.id, idx, utcnow())
        pick = poll["options"][idx] if idx < len(poll["options"]) else "?"
        seen = ("🔒 Your vote is private: other members can't see it." if poll["anonymous"]
                else "🌐 This is a public poll, so others can see how you voted.")
        text = {
            "new": f"✅ Vote recorded: **{pick}**.\n{seen}",
            "changed": f"🔁 Vote changed to **{pick}**.\n{seen}",
            "same": f"You already voted **{pick}**.",
            "locked": ("⛔ Votes are final in this poll, so you can't change yours"
                       + (f" (you voted **{poll['options'][out.previous]}**)." if out.previous is not None else ".")),
            "ended": "⏹️ This poll has ended.",
        }[out.state]
        await i.followup.send(text, ephemeral=True)
        if out.state in ("new", "changed"):
            self._dirty.add(poll["id"])

    async def on_voters(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        poll = await self.bot.db.poll_by_message(i.message.id) if i.message else None
        if poll is None:
            return await i.followup.send("I can't find this poll any more.", ephemeral=True)
        if poll["anonymous"]:
            return await i.followup.send("🔒 This is a private poll. Only staff can see who voted.", ephemeral=True)
        by_opt: dict[int, list[int]] = defaultdict(list)
        for r in await self.bot.db.poll_voters(poll["id"]):
            by_opt[r["option_idx"]].append(r["user_id"])
        e = discord.Embed(color=config.COLOR_MAIN, title=f"👥 Who voted: {poll['question']}"[:256])
        for n, name in enumerate(poll["options"]):
            ids, text, shown = by_opt.get(n, []), "", 0
            for uid in ids:
                piece = f"<@{uid}> "
                if len(text) + len(piece) > 900:
                    break
                text, shown = text + piece, shown + 1
            if len(ids) > shown:
                text += f"… +{len(ids) - shown} more"
            e.add_field(name=f"{n + 1}. {name} ({len(ids)})"[:256], value=text or "*nobody yet*", inline=False)
        await i.followup.send(embed=e, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ---------------------------------------------------------------- loop
    @tasks.loop(seconds=5)
    async def tick(self):
        try:
            for r in await self.bot.db.poll_due(utcnow()):
                await self.finish(r["id"])
            for pid in list(self._dirty):
                self._dirty.discard(pid)
                await self.refresh(pid)
        except Exception:
            log.exception("Poll loop error")

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()
        try:
            self._dirty.update(r["id"] for r in await self.bot.db.poll_active())   # catch up on votes made while offline
        except Exception:
            log.exception("Poll start-up refresh failed")


async def setup(bot):
    await bot.add_cog(Polls(bot))
