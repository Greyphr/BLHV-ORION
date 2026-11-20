"""Events, scrims and tournaments (staff create them, members sign up with buttons).

/event create        community event (movie night, hangout, giveaway ...)
/scrim create        scrim sign-up (game, slots, rank)
/tournament create   tournament sign-up (game, format, prize, registration deadline)

Each posts one embed with Join / Leave buttons. The embed shows the live participant list and slot count;
when it is full, new people go on a waitlist and the first one is promoted (and DMed) when someone leaves.
Default channel and ping role per type are set in /admin -> Events & Tournaments.
"""
import asyncio
import logging
from collections import defaultdict
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..checks import require_staff
from ..events import KINDS, TimeParseError, event_embed, parse_when, split_signups
from ..timeutil import TZ

log = logging.getLogger("orion.events")

TOURNAMENT_FORMATS = ["Single elimination", "Double elimination", "Round robin", "Swiss", "Other"]
WHEN_DESC = f"Date and time in {TZ.key} time, e.g. 2026-11-20 19:30 or tomorrow 20:00"
DESC_DESC = "Details (type \\n for a new line)"
Short = app_commands.Range[str, 1, 100]


def _clean(text: Optional[str]) -> str:
    return (text or "").replace("\\n", "\n").strip()


class EventView(discord.ui.View):
    """Persistent Join / Leave buttons shared by every event message (the event is found by message id),
    so sign-ups keep working after a bot restart."""

    def __init__(self, cog: "Events"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Join", emoji="✅", style=discord.ButtonStyle.success, custom_id="orion:event:join")
    async def join(self, i: discord.Interaction, _):
        await self.cog.on_click(i, joining=True)

    @discord.ui.button(label="Leave", emoji="🚪", style=discord.ButtonStyle.secondary, custom_id="orion:event:leave")
    async def leave(self, i: discord.Interaction, _):
        await self.cog.on_click(i, joining=False)


class Events(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.view = EventView(self)
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_load(self):
        self.bot.add_view(self.view)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_staff(interaction)

    event = app_commands.Group(name="event", description="Community events (staff)")
    scrim = app_commands.Group(name="scrim", description="Scrim sign-ups (staff)")
    tournament = app_commands.Group(name="tournament", description="Tournament sign-ups (staff)")

    # ------------------------------------------------------------ create
    @event.command(name="create", description="Post a community event with Join / Leave buttons")
    @app_commands.describe(title="Event name", when=WHEN_DESC, description=DESC_DESC,
                           slots="Max participants (0 = unlimited)",
                           channel="Where to post it (default: set in /admin, else this channel)",
                           notify="Role to ping (default: set in /admin)")
    async def event_create(self, i: discord.Interaction, title: Short, when: str,
                           description: app_commands.Range[str, 0, 1500] = "",
                           slots: app_commands.Range[int, 0, 500] = 0,
                           channel: Optional[discord.TextChannel] = None, notify: Optional[discord.Role] = None):
        await self._publish(i, "event", title=title, when=when, slots=slots, description=description,
                            channel=channel, notify=notify, extra={})

    @scrim.command(name="create", description="Post a scrim sign-up with Join / Leave buttons")
    @app_commands.describe(game="Game being played", when=WHEN_DESC, slots="Max players (default 10)",
                           rank="Skill level or rank wanted, e.g. Gold-Plat", description=DESC_DESC,
                           title="Custom title (default: '<game> Scrim')",
                           channel="Where to post it (default: set in /admin, else this channel)",
                           notify="Role to ping (default: set in /admin)")
    async def scrim_create(self, i: discord.Interaction, game: app_commands.Range[str, 1, 60], when: str,
                           slots: app_commands.Range[int, 0, 500] = KINDS["scrim"]["slots"],
                           rank: app_commands.Range[str, 0, 60] = "",
                           description: app_commands.Range[str, 0, 1500] = "",
                           title: app_commands.Range[str, 0, 100] = "",
                           channel: Optional[discord.TextChannel] = None, notify: Optional[discord.Role] = None):
        await self._publish(i, "scrim", title=title.strip() or f"{game.strip()} Scrim", when=when, slots=slots,
                            description=description, channel=channel, notify=notify,
                            extra={"game": game.strip(), "rank": rank.strip()})

    @tournament.command(name="create", description="Post a tournament sign-up with Join / Leave buttons")
    @app_commands.describe(title="Tournament name", game="Game being played", when=WHEN_DESC,
                           format="Bracket format", slots="Max participants (default 16, 0 = unlimited)",
                           prize="Prize, if any", registration_closes="Sign-ups close at (same format as 'when')",
                           description=DESC_DESC,
                           channel="Where to post it (default: set in /admin, else this channel)",
                           notify="Role to ping (default: set in /admin)")
    @app_commands.choices(format=[app_commands.Choice(name=f, value=f) for f in TOURNAMENT_FORMATS])
    async def tournament_create(self, i: discord.Interaction, title: Short, game: app_commands.Range[str, 1, 60],
                                when: str, format: Optional[app_commands.Choice[str]] = None,
                                slots: app_commands.Range[int, 0, 500] = KINDS["tournament"]["slots"],
                                prize: app_commands.Range[str, 0, 100] = "",
                                registration_closes: Optional[str] = None,
                                description: app_commands.Range[str, 0, 1500] = "",
                                channel: Optional[discord.TextChannel] = None,
                                notify: Optional[discord.Role] = None):
        await self._publish(i, "tournament", title=title, when=when, slots=slots, description=description,
                            channel=channel, notify=notify, reg_closes=registration_closes,
                            extra={"game": game.strip(), "format": format.value if format else "",
                                   "prize": prize.strip()})

    # ----------------------------------------------------------- publish
    async def _publish(self, i: discord.Interaction, kind: str, *, title: str, when: str, slots: int,
                       description: str, channel: Optional[discord.TextChannel], notify: Optional[discord.Role],
                       extra: dict, reg_closes: Optional[str] = None):
        await i.response.defer(ephemeral=True)
        db, guild, info = self.bot.db, i.guild, KINDS[kind]
        try:
            starts = parse_when(when)
            closes = parse_when(reg_closes) if reg_closes else None
        except TimeParseError as e:
            return await i.followup.send(f"⚠️ {e}", ephemeral=True)
        if closes and closes >= starts:
            return await i.followup.send("⚠️ Registration has to close **before** the start time.", ephemeral=True)

        channel = channel or guild.get_channel(db.get(f"{kind}_channel_id", 0)) or i.channel
        notify = notify or guild.get_role(db.get(f"{kind}_role_id", 0))
        if notify and (notify.is_default() or notify.managed):
            return await i.followup.send(
                "⚠️ Pick a normal role to ping. For @everyone or @here use `/announce`.", ephemeral=True)

        perms = channel.permissions_for(guild.me)
        missing = [n for n, ok in (("View Channel", perms.view_channel), ("Send Messages", perms.send_messages),
                                   ("Embed Links", perms.embed_links)) if not ok]
        if missing:
            return await i.followup.send(
                f"⚠️ I can't post in {channel.mention}: I'm missing **{', '.join(missing)}** there.", ephemeral=True)

        extra = {k: v for k, v in extra.items() if v}
        ev_id = await db.create_event(kind, title.strip(), _clean(description), starts, closes, slots,
                                      channel.id, notify.id if notify else None, extra, i.user.id)
        try:
            ev = await db.get_event(ev_id)
            msg = await channel.send(
                content=notify.mention if notify else None, embed=event_embed(ev, []), view=self.view,
                allowed_mentions=discord.AllowedMentions(roles=[notify]) if notify else discord.AllowedMentions.none())
        except discord.HTTPException as e:
            await db.delete_event(ev_id)
            log.warning("Could not post %s in %s: %s", kind, channel, e)
            return await i.followup.send(f"⚠️ Discord refused the post in {channel.mention}: {e.text or e}",
                                         ephemeral=True)
        try:
            await db.set_event_message(ev_id, msg.id)
        except Exception:
            await db.delete_event(ev_id)
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            raise
        await i.followup.send(f"✅ {info['label']} **{title.strip()}** is live in {channel.mention}: {msg.jump_url}",
                              ephemeral=True)

    # ----------------------------------------------------------- buttons
    async def refresh(self, event_id: int, message: discord.Message):
        """Re-render the public embed from the database (serialised per event so edits never go out of order)."""
        async with self._locks[event_id]:
            ev = await self.bot.db.get_event(event_id)
            if ev is None:
                return
            ids = await self.bot.db.event_signups(event_id)
            try:
                await message.edit(embed=event_embed(ev, ids))
            except discord.HTTPException:
                log.warning("Could not update event message %s", message.id)

    async def on_click(self, i: discord.Interaction, joining: bool):
        await i.response.defer(ephemeral=True)
        db = self.bot.db
        ev = await db.get_event_by_message(i.message.id)
        if ev is None or ev["status"] != "open":
            return await i.followup.send("This event is no longer open.", ephemeral=True)
        now = discord.utils.utcnow()
        if ev["starts_at"] <= now:
            return await i.followup.send("This event has already started.", ephemeral=True)
        title, slots = ev["title"], ev["slots"]

        if joining:
            if ev["reg_closes_at"] and ev["reg_closes_at"] <= now:
                return await i.followup.send("Registration for this event is closed.", ephemeral=True)
            state, pos = await db.join_event(ev["id"], i.user.id)
            if state == "joined":
                where = f"slot **{pos}/{slots}**" if slots else f"spot **#{pos}**"
                text = f"✅ You're in **{title}** ({where})."
            elif state == "waitlisted":
                text = (f"⏳ **{title}** is full. You're **#{pos - slots} on the waitlist**; "
                        "I'll DM you if a slot opens.")
            elif state == "already":
                text = f"You're already signed up for **{title}** (#{pos})."
            else:
                text = "This event no longer exists."
            await i.followup.send(text, ephemeral=True)
        else:
            left, promoted = await db.leave_event(ev["id"], i.user.id)
            await i.followup.send(f"You left **{title}**." if left else "You weren't signed up for this event.",
                                  ephemeral=True)
            if promoted:
                await self._tell_promoted(i.guild, promoted, ev, i.message.jump_url)

        await self.refresh(ev["id"], i.message)

    async def _tell_promoted(self, guild: discord.Guild, user_id: int, ev, url: str):
        member = guild.get_member(user_id)
        if member is None:
            return
        try:
            await member.send(f"🎉 A slot opened up: you're now **in** for **{ev['title']}** in **{guild.name}**.\n{url}")
        except discord.HTTPException:
            pass          # DMs closed: they can still see the updated list


async def setup(bot):
    await bot.add_cog(Events(bot))
