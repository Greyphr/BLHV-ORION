"""Giveaways & rewards.

/giveaway create            interactive builder (title, banner, rewards, winners, duration, check-in interval, channel)
/giveaway manage [giveaway] management panel (edit, extend, end early, participants, winners, reroll, export, audit ...)

Members press buttons on the public message (persistent: they survive restarts):
  Enter / Check in   join, then earn +1 entry per check-in interval (needs one of the eligible roles)
  My entries         private status: entries, time left, participants, next check-in
  CLAIM REWARD       after the draw: winners get their code privately (ephemeral + DM), once

Timers are stored timestamps, never in-memory countdowns: a loop every 20s ends expired giveaways, posts results,
retries winner roles and handles claim deadlines. If the bot was offline when a giveaway expired, the first
pass after start-up processes it.
"""
import asyncio
import io
import logging
import time
from collections import defaultdict
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import alerts, config, vault
from ..checks import require_staff
from ..giveaway_card import ButtonSpec, CardSpec, CardView, ended_spec, live_spec, results_spec
from ..giveaway_ui import Builder, GiveawayDraft, ManagePanel, PickView
from ..giveaways import (MAX_AUTO_REASSIGNS, MAX_ROLE_ATTEMPTS, REWARD_TYPES, ordinal, place_label, weighted_sample)
from ..roles import hierarchy_problem
from .announce import store_attachment

log = logging.getLogger("orion.giveaway")
utcnow = discord.utils.utcnow

NOT_SELECTED = ("Unfortunately, you weren't selected this time. Thank you for participating, "
                "and better luck in our next giveaway!")
ROLE_RETRY_SECONDS = 300


# ------------------------------------------------------------ persistent views
class MyEntriesView(discord.ui.View):
    """Private status panel. The button is disabled while the member is on cooldown."""

    def __init__(self, cog: "Giveaways", gw, label: str, disabled: bool):
        super().__init__(timeout=600)
        self.cog, self.gw = cog, gw
        self.b.label, self.b.disabled = label, disabled

    @discord.ui.button(emoji="🎟️", style=discord.ButtonStyle.success)
    async def b(self, i: discord.Interaction, _):
        await i.response.defer(ephemeral=True)
        await self.cog.enter_flow(i, self.gw)


# ------------------------------------------------------------------------ cog
class Giveaways(commands.GroupCog, name="giveaway", description="Giveaways & rewards (staff)"):
    def __init__(self, bot):
        self.bot = bot
        self.handlers = {"enter": self.on_enter, "me": self.on_me, "claim": self.on_claim}
        self.preview_handlers = {k: self._preview_only for k in self.handlers}
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)       # draw / reroll per giveaway
        self._edit_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)  # message edits per giveaway
        self._dirty: set[int] = set()
        self._role_next: dict[int, float] = {}
        self._denied: set[tuple[int, int]] = set()
        self._booted = False

    async def cog_load(self):
        # One registered card carrying all three button ids: that is what keeps the buttons of EVERY giveaway alive
        # after a restart (Discord routes a click by its custom_id).
        self.bot.add_view(CardView(CardSpec(buttons=[ButtonSpec("enter", "Enter", None), ButtonSpec("me", "Me", None),
                                                     ButtonSpec("claim", "Claim", None)]), self.handlers))
        self.tick.start()

    @staticmethod
    async def _preview_only(i: discord.Interaction):
        await i.response.send_message("👀 This is only the preview. The buttons work once you press **Publish**.", ephemeral=True)

    def cog_unload(self):
        self.tick.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_staff(interaction)

    # ------------------------------------------------------------- helpers
    @property
    def guild(self) -> Optional[discord.Guild]:
        return self.bot.get_guild(config.GUILD_ID)

    async def audit(self, gid, actor, action: str, **details):
        try:
            await self.bot.db.audit(gid, actor, action, details)
        except Exception:
            log.exception("Could not write audit entry %s", action)

    async def _member(self, guild: discord.Guild, uid: int) -> Optional[discord.Member]:
        m = guild.get_member(uid)
        if m is not None:
            return m
        try:
            return await guild.fetch_member(uid)
        except discord.NotFound:
            return None
        except discord.HTTPException:
            return None

    async def _channel(self, cid: int):
        g = self.guild
        if g is None:
            return None
        ch = g.get_channel(cid)
        if ch is not None:
            return ch
        try:
            return await g.fetch_channel(cid)
        except discord.HTTPException:
            return None

    def name_of(self, guild: discord.Guild, uid: int) -> str:
        m = guild.get_member(uid)
        return m.display_name if m else f"user {uid}"

    def eligible_text(self, guild: discord.Guild, names) -> str:
        out = []
        for n in names:
            r = discord.utils.get(guild.roles, name=n)
            out.append(r.mention if r else f"**{n}**")
        return " ".join(out)

    # ---- images: three slots per giveaway (top banner, bottom banner, small side image)
    SLOTS = (("top", "banner_id", "banner_top"), ("bottom", "bottom_banner_id", "banner_bottom"),
             ("side", "thumb_id", "side_image"))

    @staticmethod
    def ids_of(gw) -> dict:
        return {"top": gw["banner_id"], "bottom": gw["bottom_banner_id"], "side": gw["thumb_id"]}

    async def urls_for(self, ids: dict) -> dict:
        """attachment:// references for the stored images (the files travel with the message)."""
        out = {}
        for slot, _, name in self.SLOTS:
            ext = await self.bot.db.image_ext(ids[slot]) if ids.get(slot) else None
            out[slot] = f"attachment://{name}.{ext}" if ext else None
        return out

    async def files_for(self, ids: dict) -> list[discord.File]:
        files = []
        for slot, _, name in self.SLOTS:
            row = await self.bot.db.get_image(ids[slot]) if ids.get(slot) else None
            if row:
                files.append(discord.File(io.BytesIO(row["data"]), filename=f"{name}.{row['ext']}"))
        return files

    async def spec_for(self, gw) -> CardSpec:
        db, g = self.bot.db, self.guild
        rewards = await db.gw_rewards(gw["id"])
        people, entries = await db.gw_stats(gw["id"])
        kw = dict(participants=people, entries=entries, eligible_text=self.eligible_text(g, gw["eligible_roles"]),
                  urls=await self.urls_for(self.ids_of(gw)))
        return live_spec(gw, rewards, **kw) if gw["status"] == "active" else ended_spec(gw, rewards, **kw)

    async def render(self, gw) -> CardView:
        """The public message for a giveaway in its current state."""
        return CardView(await self.spec_for(gw), self.handlers)

    async def refresh_message(self, gid: int, reupload: bool = False) -> bool:
        """Redraw the public card. Returns False if Discord refused the edit."""
        async with self._edit_locks[gid]:
            gw = await self.bot.db.gw_get(gid)
            if not gw or not gw["message_id"]:
                return True
            ch = await self._channel(gw["channel_id"])
            if ch is None:
                return True
            kw = {"view": await self.render(gw)}
            if reupload:
                kw["attachments"] = await self.files_for(self.ids_of(gw))
            try:
                await ch.get_partial_message(gw["message_id"]).edit(**kw)
            except discord.NotFound:
                log.info("Giveaway %s: the public message was deleted", gid)
            except discord.HTTPException:
                log.warning("Giveaway %s: could not update its message", gid, exc_info=True)
                return False
            return True

    async def refresh_results(self, gid: int, reupload: bool = False):
        gw = await self.bot.db.gw_get(gid)
        if not gw or not gw["results_message_id"]:
            return
        ch = await self._channel(gw["channel_id"])
        if ch is None:
            return
        rewards = await self.bot.db.gw_rewards(gid)
        kw = {"view": CardView(results_spec(gw, rewards, urls=await self.urls_for(self.ids_of(gw)),
                                            ping=self._ping_text(rewards)), self.handlers)}
        if reupload:
            kw["attachments"] = await self.files_for(self.ids_of(gw))
        try:
            await ch.get_partial_message(gw["results_message_id"]).edit(**kw)
        except discord.HTTPException:
            log.warning("Giveaway %s: could not update the results message", gid)

    @staticmethod
    def _ping_text(rewards) -> str:
        winners = list(dict.fromkeys(r["winner_id"] for r in rewards if r["winner_id"]))
        return ("🎉 " + " ".join(f"<@{w}>" for w in winners[:60])) if winners else ""

    # ------------------------------------------------------------- commands
    @app_commands.command(name="create", description="Open the giveaway builder")
    @app_commands.describe(channel="Where to publish (you can change it in the builder)",
                           banner="Top banner image", bottom_banner="Bottom banner image",
                           side_image="Small image next to the text")
    async def create(self, i: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                     banner: Optional[discord.Attachment] = None, bottom_banner: Optional[discord.Attachment] = None,
                     side_image: Optional[discord.Attachment] = None):
        await i.response.defer(ephemeral=True)
        await self.open_builder(i, channel, banner, bottom_banner, side_image)

    async def open_builder(self, i: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                           banner: Optional[discord.Attachment] = None, bottom_banner: Optional[discord.Attachment] = None,
                           side_image: Optional[discord.Attachment] = None):
        """Start the private builder. `i` must already be deferred ephemerally (also used by the /admin panel)."""
        try:
            await vault.verify(self.bot.db)
        except vault.VaultError as e:
            return await i.followup.send(f"⚠️ {e}", ephemeral=True)
        draft, errors = GiveawayDraft(), []
        for att, attr, label in ((banner, "banner_id", "Top banner"), (bottom_banner, "bottom_id", "Bottom banner"),
                                 (side_image, "thumb_id", "Side image")):
            if att:
                try:
                    setattr(draft, attr, await store_attachment(self.bot.db, att))
                except ValueError as e:
                    errors.append(f"{label}: {e}")
        note = "⚠️ " + "; ".join(errors) if errors else None
        await Builder(self, i, draft, channel.id if channel else None).render(note)

    async def _gw_ac(self, i: discord.Interaction, current: str):
        rows = await self.bot.db.gw_list(25)
        out = [app_commands.Choice(name=f"#{r['id']} · {r['title']} ({r['status']})"[:100], value=str(r["id"]))
               for r in rows if current.lower() in r["title"].lower() or current in str(r["id"])]
        return out[:25]

    @app_commands.command(name="manage", description="Manage a giveaway (participants, winners, rerolls, export ...)")
    @app_commands.describe(giveaway="Pick a giveaway (default: choose from a list)")
    @app_commands.autocomplete(giveaway=_gw_ac)
    async def manage(self, i: discord.Interaction, giveaway: Optional[str] = None):
        await i.response.defer(ephemeral=True)
        await self.open_manage(i, giveaway)

    async def open_manage(self, i: discord.Interaction, giveaway: Optional[str] = None):
        """Management panel (or a picker). `i` must already be deferred ephemerally (also used by the /admin panel)."""
        if giveaway is None:
            rows = await self.bot.db.gw_list(25)
            if not rows:
                return await i.followup.send("No giveaways yet. Start one with `/giveaway create`.", ephemeral=True)
            return await i.followup.send("Which giveaway?", view=PickView(self, i, rows), ephemeral=True)
        gid = int(giveaway.lstrip("#")) if giveaway.lstrip("#").isdigit() else 0
        if not await self.bot.db.gw_get(gid):
            return await i.followup.send("I couldn't find that giveaway.", ephemeral=True)
        await ManagePanel(self, i, gid).render()

    @app_commands.command(name="images", description="Upload new images for a giveaway (top, bottom, side)")
    @app_commands.describe(giveaway="Which giveaway", banner="New top banner", bottom_banner="New bottom banner",
                           side_image="New small side image")
    @app_commands.autocomplete(giveaway=_gw_ac)
    async def images(self, i: discord.Interaction, giveaway: str, banner: Optional[discord.Attachment] = None,
                     bottom_banner: Optional[discord.Attachment] = None, side_image: Optional[discord.Attachment] = None):
        await i.response.defer(ephemeral=True)
        gid = int(giveaway.lstrip("#")) if giveaway.lstrip("#").isdigit() else 0
        if not await self.bot.db.gw_get(gid):
            return await i.followup.send("I couldn't find that giveaway.", ephemeral=True)
        fields, errors = {}, []
        for att, col, label in ((banner, "banner_id", "Top banner"), (bottom_banner, "bottom_banner_id", "Bottom banner"),
                                (side_image, "thumb_id", "Side image")):
            if att:
                try:
                    fields[col] = await store_attachment(self.bot.db, att)
                except ValueError as e:
                    errors.append(f"{label}: {e}")
        if fields:
            await self.bot.db.gw_update(gid, **fields)
            await self.audit(gid, i.user.id, "images_update", slots=sorted(fields))
            await self.refresh_message(gid, reupload=True)
            await self.refresh_results(gid, reupload=True)
        await i.followup.send((f"✅ Updated {len(fields)} image(s). " if fields else "Nothing to update: attach at least one image. ")
                              + ("⚠️ " + "; ".join(errors) if errors else ""), ephemeral=True)

    # ------------------------------------------------------------- publish
    async def publish(self, draft: GiveawayDraft, author: discord.Member, channel_id: int):
        """Store the giveaway (rewards encrypted) and post the public message. Returns (message, giveaway_id)."""
        db, g = self.bot.db, self.guild
        await vault.verify(db)
        ch = g.get_channel(channel_id)
        now = utcnow()
        rewards = [{"name": r.name, "description": r.description, "value": r.value, "reward_type": r.reward_type,
                    "secret_enc": vault.encrypt(r.secret) if r.secret else None} for r in draft.rewards]
        gid = await db.gw_create({
            "title": draft.title, "body": draft.body, "prize_pool": draft.prize_pool, "banner_id": draft.banner_id,
            "bottom_banner_id": draft.bottom_id, "thumb_id": draft.thumb_id, "accent": draft.accent,
            "buttons": draft.buttons, "channel_id": channel_id,
            "interval_seconds": draft.interval, "starts_at": now, "ends_at": now + timedelta(seconds=draft.duration),
            "eligible_roles": list(draft.roles), "allow_multiple_wins": draft.multi,
            "claim_hours": draft.claim_hours, "auto_reassign": draft.reassign and bool(draft.claim_hours),
            "created_by": author.id}, rewards)
        msg = None
        try:
            gw = await db.gw_get(gid)
            kw = {"view": await self.render(gw), "allowed_mentions": discord.AllowedMentions.none()}
            files = await self.files_for(self.ids_of(gw))
            if files:
                kw["files"] = files
            msg = await ch.send(**kw)
            await db.gw_set_message(gid, msg.id)
        except Exception:
            await db.gw_delete(gid)
            if msg:
                try:
                    await msg.delete()
                except discord.HTTPException:
                    pass
            raise
        await self.audit(gid, author.id, "create", title=draft.title, slots=len(rewards), channel=channel_id,
                         ends=(now + timedelta(seconds=draft.duration)).isoformat(), roles=list(draft.roles),
                         interval=draft.interval)
        return msg, gid

    # ------------------------------------------------------- member buttons
    async def _gw_from_click(self, i: discord.Interaction):
        return await self.bot.db.gw_by_message(i.message.id) if i.message else None

    async def on_enter(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        gw = await self._gw_from_click(i)
        if gw is None:
            return await i.followup.send("I can't find this giveaway any more.", ephemeral=True)
        await self.enter_flow(i, gw)

    def _next_text(self, next_at) -> str:
        if next_at is None:
            return "No further entries can be earned before the giveaway ends."
        ts = int(next_at.timestamp())
        return f"Your next entry opens <t:{ts}:R> (<t:{ts}:f>)."

    async def enter_flow(self, i: discord.Interaction, gw):
        """Join or check in. Role check -> atomic ledger write -> private answer."""
        if gw["status"] != "active" or gw["ends_at"] <= utcnow():
            return await i.followup.send("⏹️ This giveaway has ended.", ephemeral=True)
        user = i.user
        names = set(gw["eligible_roles"])
        if not isinstance(user, discord.Member) or not any(r.name in names for r in user.roles):
            roles = ", ".join(f"**{n}**" for n in gw["eligible_roles"])
            return await i.followup.send(
                f"🚫 You don't meet the requirements for **{gw['title']}**.\n"
                f"You need at least one of these roles: {roles}.\nKeep chatting and levelling up to earn one!", ephemeral=True)
        out = await self.bot.db.gw_enter(gw["id"], user.id, utcnow())
        end = int(gw["ends_at"].timestamp())
        if out.state == "joined":
            text = (f"🎉 You're in **{gw['title']}**! You have **1** entry.\n{self._next_text(out.next_at)}")
        elif out.state == "checked_in":
            text = f"✅ Checked in! You now have **{out.entries}** entries.\n{self._next_text(out.next_at)}"
        elif out.state == "cooldown":
            text = (f"⏳ You've already checked in. You have **{out.entries}** "
                    f"entr{'y' if out.entries == 1 else 'ies'}.\n{self._next_text(out.next_at)}")
        else:
            return await i.followup.send("⏹️ This giveaway has ended.", ephemeral=True)
        await i.followup.send(f"{text}\n-# Giveaway ends <t:{end}:R>", ephemeral=True)
        if out.state in ("joined", "checked_in"):
            self._dirty.add(gw["id"])

    async def on_me(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        gw = await self._gw_from_click(i)
        if gw is None:
            return await i.followup.send("I can't find this giveaway any more.", ephemeral=True)
        db = self.bot.db
        row = await db.gw_participant(gw["id"], i.user.id)
        people, entries = await db.gw_stats(gw["id"])
        active = gw["status"] == "active" and gw["ends_at"] > utcnow()
        end = int(gw["ends_at"].timestamp())
        e = discord.Embed(title=f"📊 Your entries: {gw['title']}"[:256], color=config.COLOR_ACCENT)
        e.add_field(name="🎟️ Your entries", value=f"**{row['entries']}**" if row else "You haven't joined yet", inline=True)
        e.add_field(name="⏰ Time remaining", value=f"<t:{end}:R>" if active else "Ended", inline=True)
        e.add_field(name="👥 Participants", value=f"**{people:,}** · {entries:,} entries", inline=True)
        label, disabled = "Enter giveaway", not active
        if row and active:
            nxt = row["last_entry_at"] + timedelta(seconds=gw["interval_seconds"])
            if nxt > utcnow():
                label, disabled = "Check-in on cooldown", True
                e.add_field(name="🔁 Next entry", value=self._next_text(nxt if nxt < gw["ends_at"] else None), inline=False)
            else:
                label = "Check in now"
                e.add_field(name="🔁 Next entry", value="**Available now!**", inline=False)
        await i.followup.send(embed=e, view=MyEntriesView(self, gw, label, disabled), ephemeral=True)

    # ------------------------------------------------------------- claiming
    def reward_embed(self, gw, r, secret: str) -> discord.Embed:
        label, emoji, kind = REWARD_TYPES.get(r["reward_type"], REWARD_TYPES["custom"])
        e = discord.Embed(title=f"{emoji} You won {place_label(r['position'])} in {gw['title']}!"[:256],
                          color=config.COLOR_ACCENT)
        e.add_field(name="Reward", value=f"**{r['name']}**" + (f" ({r['value']})" if r["value"] else ""), inline=False)
        if r["description"]:
            e.add_field(name="Details", value=r["description"][:1024], inline=False)
        if kind == "code":
            e.add_field(name="Your code", value=f"```{secret.replace('```', chr(39) * 3)}```\nClick the code block to copy it.",
                        inline=False)
        else:
            e.add_field(name="How you get it", value=(secret or "A staff member will contact you to deliver this reward.")[:1024],
                        inline=False)
        e.set_footer(text="Keep this private. For safety it is shown once; staff can re-open your claim if you lose it.")
        return e

    async def on_claim(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        db, uid, now = self.bot.db, i.user.id, utcnow()
        gw = await self._gw_from_click(i)
        if gw is None or gw["status"] != "ended":
            return await i.followup.send("There's nothing to claim here.", ephemeral=True)
        mine = await db.gw_user_rewards(gw["id"], uid)
        if not mine:
            if (gw["id"], uid) not in self._denied:
                self._denied.add((gw["id"], uid))
                await self.audit(gw["id"], uid, "claim_denied", reason="not a winner")
            return await i.followup.send(NOT_SELECTED, ephemeral=True)

        embeds, claimed, notes = [], [], []
        for r in mine:
            tag = f"**{ordinal(r['position'])} place** ({r['name']})"
            if r["status"] == "claimed":
                notes.append(f"ℹ️ {tag} was already claimed"
                             + (f" <t:{int(r['claimed_at'].timestamp())}:R>" if r["claimed_at"] else "")
                             + ". For safety it can't be shown again; ask a staff member to re-open your claim if you lost it.")
            elif r["status"] == "delivered":
                notes.append(f"📦 {tag} was marked as **delivered** by the staff. If you haven't received it, contact them.")
            elif r["status"] == "assigned":
                row = await db.reward_claim(r["id"], uid, now)
                if row is None:
                    notes.append(f"⌛ The claim window for {tag} has closed. Contact the staff.")
                    await self.audit(gw["id"], uid, "claim_denied", reward_id=r["id"], reason="window closed or already claimed")
                    continue
                try:
                    embeds.append(self.reward_embed(gw, row, vault.decrypt(row["secret_enc"])))
                    claimed.append(row)
                except vault.VaultError as e:
                    await db.reward_unclaim(r["id"])
                    notes.append(f"⚠️ I can't open {tag} right now. The staff have been told.")
                    g = self.guild
                    if g:
                        await alerts.alert_staff(self.bot, g, f"Giveaway #{gw['id']}: {e}", key="vault-decrypt")
        if embeds:
            try:
                for n in range(0, len(embeds), 10):
                    await i.followup.send(embeds=embeds[n:n + 10], ephemeral=True)
            except discord.HTTPException:
                for row in claimed:                      # never burn a claim the winner couldn't see
                    await db.reward_unclaim(row["id"])
                return await i.followup.send("⚠️ I couldn't send your reward. Please press the button again.", ephemeral=True)
            for row in claimed:
                await self.audit(gw["id"], uid, "claim", reward_id=row["id"], position=row["position"])
            try:                                          # copy in DMs so it can be found later
                await i.user.send(content=f"🎁 Your reward from **{gw['title']}** (kept here in case you need it again):",
                                  embeds=embeds[:10])
                notes.append("📬 I also sent it to your DMs.")
            except discord.HTTPException:
                notes.append("📭 I couldn't DM you (your DMs are closed), so **save the code from the message above**.")
        if notes:
            await i.followup.send("\n".join(notes), ephemeral=True)

    # --------------------------------------------------------------- roles
    async def winner_role(self, guild: discord.Guild, reset: bool = False) -> Optional[discord.Role]:
        db = self.bot.db
        role = None if reset else guild.get_role(db.get("giveaway_winner_role_id", 0))
        if role is None:
            role = discord.utils.get(guild.roles, name=config.GIVEAWAY_WINNER_ROLE_NAME)
        if role is None:
            try:
                role = await guild.create_role(name=config.GIVEAWAY_WINNER_ROLE_NAME, colour=discord.Colour(0xF59E0B),
                                               reason="Giveaway winner role")
            except discord.HTTPException:
                return None
        if db.get("giveaway_winner_role_id") != role.id:
            await db.set("giveaway_winner_role_id", role.id)
        return role

    async def assign_roles(self, gid: Optional[int] = None, force: bool = False):
        """Give the winner role to everyone who won and doesn't have it yet. Failures are audited, retried every
        few minutes and, after MAX_ROLE_ATTEMPTS, flagged to the staff channel."""
        db, g = self.bot.db, self.guild
        if g is None:
            return
        rows = await db.rewards_needing_role(MAX_ROLE_ATTEMPTS, gid)
        now = time.monotonic()
        rows = [r for r in rows if force or now >= self._role_next.get(r["id"], 0)]
        if not rows:
            return
        by_user = defaultdict(list)
        for r in rows:
            by_user[r["winner_id"]].append(r)
        role = await self.winner_role(g)
        problem = ("I couldn't find or create the winner role (I need **Manage Roles**)" if role is None
                   else hierarchy_problem(g, role))
        for uid, rs in by_user.items():
            ids = [r["id"] for r in rs]
            final = all(r["role_attempts"] + 1 >= MAX_ROLE_ATTEMPTS for r in rs)
            reason = problem
            if not reason:
                member = await self._member(g, uid)
                if member is None:
                    await db.rewards_role_failed(ids, gone=True)
                    await self.audit(rs[0]["giveaway_id"], None, "role_skipped", user=uid, reason="left the server")
                    continue
                try:
                    if role not in member.roles:
                        await member.add_roles(role, reason="Giveaway winner")
                    await db.rewards_role_done(ids)
                    await self.audit(rs[0]["giveaway_id"], None, "role_assigned", user=uid, role=role.name)
                    continue
                except discord.HTTPException as e:
                    reason = f"Discord refused ({e.status}): {e.text or e}"
            await db.rewards_role_failed(ids, gone=final)
            for r in rs:
                self._role_next[r["id"]] = now + ROLE_RETRY_SECONDS
            await self.audit(rs[0]["giveaway_id"], None, "role_fail", user=uid, reason=str(reason), final=final)
            await alerts.alert_staff(
                self.bot, g,
                f"Giveaway #{rs[0]['giveaway_id']}: I couldn't give the winner role to <@{uid}>: {reason}. "
                + ("I've stopped retrying: add the role by hand, or fix my permissions and press **Retry winner roles** "
                   "in `/giveaway manage`." if final else "I'll retry in a few minutes."), key=f"gw-role-{rs[0]['giveaway_id']}")

    # ------------------------------------------------------------- the draw
    async def _eligible_weights(self, guild, gid: int, exclude: set[int]) -> dict[int, int]:
        """Entries per person counted from the ledger. Members who left the server, bots and `exclude` are out."""
        out = {}
        for uid, n in (await self.bot.db.gw_weights(gid)).items():
            if uid in exclude:
                continue
            m = await self._member(guild, uid)
            if m is not None and not m.bot:
                out[uid] = n
        return out

    async def end_giveaway(self, gid: int, actor: Optional[int], early: bool = False) -> bool:
        """Close entries, draw (weighted, secure random), assign winners and slots in ONE transaction, then give
        roles, post the results and update the original message. Safe to call twice: only one call wins."""
        db, g = self.bot.db, self.guild
        async with self._locks[gid]:
            gw = await db.gw_get(gid)
            if gw is None or gw["status"] != "active" or g is None:
                return False
            now = utcnow()
            await db.gw_close_entries(gid, now)
            rewards = await db.gw_rewards(gid)
            weights = await self._eligible_weights(g, gid, set())
            picks = weighted_sample(weights, len(rewards), allow_repeat=gw["allow_multiple_wins"])
            assignments = [(r["id"], u) for r, u in zip(rewards, picks)]
            record = {"method": "weighted random, secrets.SystemRandom (OS entropy)",
                      "weights": {str(u): n for u, n in weights.items()}, "total_entries": sum(weights.values()),
                      "slots": len(rewards), "unique_winners": not gw["allow_multiple_wins"],
                      "early": early, "participants_total": len(await db.gw_weights(gid))}
            if not await db.gw_finalize(gid, assignments, record, now, gw["claim_hours"], actor):
                return False
        await self.audit(gid, actor, "draw", winners=[u for _, u in assignments], slots=len(rewards),
                         participants=len(weights), early=early)
        await self.assign_roles(gid)
        await self.publish_results(gid)
        await self.refresh_message(gid)
        return True

    async def cancel_giveaway(self, gid: int, actor: int) -> bool:
        if not await self.bot.db.gw_cancel(gid, utcnow()):
            return False
        await self.audit(gid, actor, "cancel")
        await self.refresh_message(gid)
        return True

    async def publish_results(self, gid: int):
        db, g = self.bot.db, self.guild
        gw = await db.gw_get(gid)
        if gw is None or gw["status"] != "ended" or gw["results_posted"] or g is None:
            return
        rewards = await db.gw_rewards(gid)
        winners = list(dict.fromkeys(r["winner_id"] for r in rewards if r["winner_id"]))
        ch = await self._channel(gw["channel_id"])
        ping = self._ping_text(rewards) or "The giveaway has ended with no eligible entries."
        try:
            if ch is None:
                raise discord.NotFound(type("R", (), {"status": 404, "reason": "gone"})(), "channel missing")
            kw = {"view": CardView(results_spec(gw, rewards, urls=await self.urls_for(self.ids_of(gw)), ping=ping),
                                   self.handlers),
                  "allowed_mentions": discord.AllowedMentions(users=[discord.Object(w) for w in winners[:60]],
                                                              roles=False, everyone=False)}
            files = await self.files_for(self.ids_of(gw))
            if files:
                kw["files"] = files
            msg = await ch.send(**kw)
        except discord.HTTPException as e:
            n = await db.gw_bump_announce(gid)
            permanent = isinstance(e, (discord.NotFound, discord.Forbidden))
            if permanent or n >= 5:
                await db.gw_mark_announced(gid, None)
                await alerts.alert_staff(self.bot, g, f"Giveaway #{gid} ended but I couldn't post the results "
                                         f"({e.text or e}). Winners are in `/giveaway manage`.", key=f"gw-announce-{gid}")
                await self.audit(gid, None, "announce_fail", error=str(e.text or e))
            return
        await db.gw_mark_announced(gid, msg.id)
        await self.audit(gid, None, "results_posted", message_id=msg.id)

    async def reroll(self, gid: int, reward_id: int, actor: Optional[int], reason: str = "admin") -> tuple[bool, str]:
        """Replace the winner of ONE unclaimed slot. Anyone ever picked in this giveaway is excluded."""
        db, g = self.bot.db, self.guild
        async with self._locks[gid]:
            gw, r = await db.gw_get(gid), await db.reward_get(reward_id)
            if gw is None or r is None or r["giveaway_id"] != gid or g is None or gw["status"] != "ended":
                return False, "That giveaway or slot isn't available for a reroll."
            if r["status"] in ("claimed", "delivered"):
                return False, "That prize was already claimed or delivered, so it can't be rerolled."
            weights = await self._eligible_weights(g, gid, await db.draw_picked_users(gid))
            picks = weighted_sample(weights, 1)
            if not picks:
                return False, "There's nobody else eligible to take this slot."
            new, old, now = picks[0], r["winner_id"], utcnow()
            row = await db.reward_set_winner(reward_id, new, now, gw["claim_hours"], auto=reason == "deadline")
            if row is None:
                return False, "That prize was claimed a moment ago."
            await db.draw_add(gid, "reassign" if reason == "deadline" else "reroll",
                              {"method": "weighted random, secrets.SystemRandom (OS entropy)", "reason": reason,
                               "weights": {str(u): n for u, n in weights.items()}, "replaced": old},
                              [{"reward_id": reward_id, "user_id": new}], actor)
        await self.audit(gid, actor, "reroll" if reason != "deadline" else "reassign", position=r["position"],
                         old=old, new=new, reason=reason)
        await self.assign_roles(gid, force=True)
        if old and not await db.user_has_other_wins(old, reward_id):
            role, member = await self.winner_role(g), await self._member(g, old)
            if role and member and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Replaced as giveaway winner")
                except discord.HTTPException:
                    pass
        await self.refresh_results(gid)
        await self.refresh_message(gid)
        ch = await self._channel(gw["channel_id"])
        if ch:
            why = "didn't claim in time" if reason == "deadline" else "was replaced"
            try:
                await ch.send(f"🔁 **{gw['title']}**: the {place_label(r['position'])} winner {why}. "
                              f"New winner: <@{new}>! Press **CLAIM REWARD** on the results message.",
                              allowed_mentions=discord.AllowedMentions(users=[discord.Object(new)]))
            except discord.HTTPException:
                pass
        return True, f"{ordinal(r['position'])} place is now <@{new}>."

    # ------------------------------------------------------------ the loop
    async def process(self):
        db = self.bot.db
        if not self._booted:                      # after a restart, re-sync every live embed once
            self._booted = True
            self._dirty.update(await db.gw_active_ids())
        for gw in await db.gw_due(utcnow()):      # includes giveaways that expired while the bot was offline
            try:
                if await self.end_giveaway(gw["id"], None):
                    log.info("Giveaway %s ended", gw["id"])
            except Exception:
                log.exception("Could not end giveaway %s", gw["id"])
        for gw in await db.gw_unannounced():
            try:
                await self.publish_results(gw["id"])
                await self.refresh_message(gw["id"])
            except Exception:
                log.exception("Could not post results for giveaway %s", gw["id"])
        try:
            await self.assign_roles()
        except Exception:
            log.exception("Winner role pass failed")
        g = self.guild
        for r in await db.rewards_past_deadline(utcnow()):
            try:
                if r["auto_reassign"] and r["reassign_count"] < MAX_AUTO_REASSIGNS:
                    ok, text = await self.reroll(r["giveaway_id"], r["id"], None, reason="deadline")
                    if ok:
                        continue
                    why = text
                else:
                    why = "re-assigning is off" if not r["auto_reassign"] else "it was already passed on too many times"
                await db.reward_flag_deadline(r["id"])
                if g:
                    await alerts.alert_staff(
                        self.bot, g, f"Giveaway #{r['giveaway_id']} ({r['giveaway_title']}): the {ordinal(r['position'])}-place "
                        f"prize for <@{r['winner_id']}> wasn't claimed in time ({why}). Use `/giveaway manage` to reroll it "
                        "or mark it delivered.", key=f"gw-deadline-{r['id']}")
            except Exception:
                log.exception("Deadline handling failed for reward %s", r["id"])
        for gid in list(self._dirty):
            self._dirty.discard(gid)
            await self.refresh_message(gid)

    @tasks.loop(seconds=config.GIVEAWAY_TICK_SECONDS)
    async def tick(self):
        try:
            await self.process()
        except Exception:
            log.exception("Giveaway loop error")

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()
        try:
            if await self.bot.db.pool.fetchval("SELECT EXISTS (SELECT 1 FROM giveaways)"):
                await vault.verify(self.bot.db)
        except vault.VaultError as e:
            log.error("Giveaway vault problem: %s", e)
            if self.guild:
                await alerts.alert_staff(self.bot, self.guild, f"Giveaway reward vault: {e}", key="vault-boot")


async def setup(bot):
    await bot.add_cog(Giveaways(bot))
