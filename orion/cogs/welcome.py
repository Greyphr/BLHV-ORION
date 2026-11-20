import asyncio
import copy
import io
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

from .. import config, roles
from ..checks import require_staff
from ..images import DEFAULT_LAYOUT, DM_BANNER, WELCOME_TEMPLATE, render_welcome
from ..levels import level_from_xp
from ..ui import _embed

log = logging.getLogger("orion.welcome")

DEFAULT_WELCOME = ""  # public welcome is just the card; staff can add text with /welcome edit
# The old built-in text; cleared automatically if a server saved it before the text was removed.
LEGACY_DEFAULT_WELCOME = ("Welcome to Orion. Your gateway to the Blue Heaven community. "
                          "Stay connected, stay active, and make your presence count. 🟣\n\n"
                          "You are member **#{count}**.\n\n"
                          "**Always forward**")
DEFAULT_DM = ("Hey {user}, welcome to **Orion**! Say hi in the server and start levelling up — "
              "every message and every minute in voice counts toward your rank. 🟣")
DEFAULT_LEAVE = "**{user}** has left the server. We're now **{count}** members."


DM_POSITIONS = {"top": "Above the text", "bottom": "Below the text", "off": "Hidden"}
DM_BANNER_MAX_W = 1600          # wider uploads are scaled down; DMs don't need more


def build_dm_embeds(title: str, text: str, footer: str, color: int, position: str,
                    has_banner: bool) -> list[discord.Embed]:
    """The welcome DM. A Discord embed has ONE big-image slot (at the bottom), so a TOP banner is a slim
    image-only embed sent first in the same message, same colour so the two read as one card."""
    main = discord.Embed(color=color, title=title or None, description=text or None)
    if footer:
        main.set_footer(text=footer)
    show = has_banner and position in ("top", "bottom")
    if not show:
        return [main]
    url = "attachment://dm_banner.png"
    if position == "bottom":
        main.set_image(url=url)
        return [main]
    head = discord.Embed(color=color)
    head.set_image(url=url)
    if not (title or text):                 # banner only: keep the footer on the banner, no empty second box
        if footer:
            head.set_footer(text=footer)
        return [head]
    return [head, main]


def welcome_text(db) -> str:
    text = db.get("welcome_text")
    if text is None or text == LEGACY_DEFAULT_WELCOME:
        return DEFAULT_WELCOME
    return text


def fmt(text: str, member: discord.Member) -> str:
    return (text.replace("{mention}", member.mention).replace("{user}", member.display_name)
                .replace("{server}", member.guild.name).replace("{count}", f"{member.guild.member_count:,}"))


def find_welcome_channel(bot, guild: discord.Guild):
    ch = guild.get_channel(bot.db.get("welcome_channel_id", 0))
    if ch:
        return ch
    if guild.system_channel:
        return guild.system_channel
    return next((c for c in guild.text_channels if "welcome" in c.name.lower()), None)


def find_leave_channel(bot, guild: discord.Guild):
    """The channel chosen with /welcome leave-channel, otherwise the welcome channel (the old behaviour)."""
    ch = guild.get_channel(bot.db.get("leave_channel_id", 0))
    return ch or find_welcome_channel(bot, guild)


class TextModal(discord.ui.Modal, title="Edit welcome texts"):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.welcome = discord.ui.TextInput(
            label="Welcome ({mention} {user} {server} {count})", style=discord.TextStyle.paragraph,
            default=welcome_text(db), max_length=1500)
        self.dm = discord.ui.TextInput(
            label="Welcome DM", style=discord.TextStyle.paragraph,
            default=db.get("dm_text", DEFAULT_DM), max_length=1500)
        self.leave = discord.ui.TextInput(
            label="Leave message", style=discord.TextStyle.paragraph,
            default=db.get("leave_text", DEFAULT_LEAVE), max_length=500)
        for i in (self.welcome, self.dm, self.leave):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        await self.db.set("welcome_text", self.welcome.value)
        await self.db.set("dm_text", self.dm.value)
        await self.db.set("leave_text", self.leave.value)
        await interaction.response.send_message("Saved. Use `/welcome preview` to check it.", ephemeral=True)


class Welcome(commands.GroupCog, name="welcome", description="Welcome system settings (staff)"):
    def __init__(self, bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_staff(interaction)

    # ------------------------------------------------------------- building
    def layout(self) -> dict:
        lay = copy.deepcopy(DEFAULT_LAYOUT)
        for k, v in self.bot.db.get("welcome_layout", {}).items():
            lay.setdefault(k, {}).update(v)
        return lay

    async def build(self, member: discord.Member):
        avatar = await member.display_avatar.replace(size=512, format="png").read()
        buf = await asyncio.to_thread(render_welcome, avatar, member.display_name,
                                      member.guild.member_count, self.layout())
        text = fmt(welcome_text(self.bot.db), member)
        e = _embed(description=text) if text else _embed()
        e.set_image(url="attachment://welcome.png")
        return e, discord.File(buf, "welcome.png")

    def build_dm(self, member: discord.Member):
        """-> (embeds, file or None) for the welcome DM, using the settings from /admin -> Welcome DM."""
        db = self.bot.db
        color = db.get("dm_color")
        color = color if isinstance(color, int) else config.COLOR_MAIN
        title = fmt(db.get("dm_title", ""), member)
        text = fmt(db.get("dm_text", DEFAULT_DM), member)
        footer = db.get("dm_footer", "")
        has_banner = DM_BANNER.exists()
        if not (title or text or footer or has_banner):   # never send a completely empty message
            text = fmt(DEFAULT_DM, member)
        embeds = build_dm_embeds(title, text, footer, color, db.get("dm_banner_position", "top"), has_banner)
        shows_banner = any(e.image and e.image.url == "attachment://dm_banner.png" for e in embeds)
        return embeds, (discord.File(DM_BANNER, "dm_banner.png") if shows_banner else None)

    # --------------------------------------------------------------- events
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or member.guild.id != config.GUILD_ID:
            return
        db = self.bot.db
        if db.get("autorole_enabled", True):
            try:
                row = await db.get_user(member.id)        # a returning member keeps the rank they earned
                await roles.sync_and_alert(self.bot, member, level_from_xp(row["xp"]) if row else 0)
            except Exception:                             # a role/DB hiccup must not cancel the welcome message
                log.exception("Auto role failed for %s", member)
        if db.get("welcome_enabled", True):
            ch = find_welcome_channel(self.bot, member.guild)
            if ch:
                try:
                    embed, file = await self.build(member)
                    await ch.send(content=member.mention, embed=embed, file=file,
                                  allowed_mentions=discord.AllowedMentions(users=[member]))
                except Exception:
                    log.exception("Welcome message failed")
        if db.get("dm_enabled", True):
            try:
                embeds, file = self.build_dm(member)
                await member.send(embeds=embeds, **({"file": file} if file else {}))
            except discord.HTTPException:
                pass  # DMs closed
            except Exception:
                log.exception("Welcome DM failed")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot or member.guild.id != config.GUILD_ID or not self.bot.db.get("leave_enabled", True):
            return
        ch = find_leave_channel(self.bot, member.guild)
        if ch:
            try:
                await ch.send(embed=_embed(description=fmt(self.bot.db.get("leave_text", DEFAULT_LEAVE), member)))
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------- commands
    @app_commands.command(name="channel", description="Set the welcome channel")
    async def channel(self, i: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.set("welcome_channel_id", channel.id)
        note = ""
        if not self.bot.db.get("leave_channel_id"):
            note = "\nLeave messages go here too until you pick their own channel with `/welcome leave-channel`."
        await i.response.send_message(f"Welcome channel set to {channel.mention}.{note}", ephemeral=True)

    @app_commands.command(name="leave-channel", description="Set the channel where 'member left' messages go")
    @app_commands.describe(channel="Leave it empty to send leave messages to the welcome channel again")
    async def leave_channel(self, i: discord.Interaction, channel: discord.TextChannel | None = None):
        if channel is None:
            await self.bot.db.set("leave_channel_id", 0)
            ch = find_welcome_channel(self.bot, i.guild)
            return await i.response.send_message(
                "Leave messages will go to the welcome channel" + (f" ({ch.mention})." if ch else " (none set yet)."),
                ephemeral=True)
        await self.bot.db.set("leave_channel_id", channel.id)
        perms = channel.permissions_for(i.guild.me)
        warn = "" if (perms.view_channel and perms.send_messages and perms.embed_links) else \
            "\n⚠️ I'm missing **View Channel**, **Send Messages** or **Embed Links** there, so nothing will show up."
        await i.response.send_message(f"✅ Leave messages will now be posted in {channel.mention}.{warn}", ephemeral=True)

    @app_commands.command(name="edit", description="Edit the welcome, DM and leave texts")
    async def edit(self, i: discord.Interaction):
        await i.response.send_modal(TextModal(self.bot.db))

    @app_commands.command(name="toggle", description="Turn a welcome feature on or off")
    @app_commands.choices(feature=[
        app_commands.Choice(name="Welcome message", value="welcome_enabled"),
        app_commands.Choice(name="Welcome DM", value="dm_enabled"),
        app_commands.Choice(name="Leave message", value="leave_enabled"),
        app_commands.Choice(name="Auto role (Earth King / returning rank)", value="autorole_enabled")])
    async def toggle(self, i: discord.Interaction, feature: app_commands.Choice[str], enabled: bool):
        await self.bot.db.set(feature.value, enabled)
        await i.response.send_message(f"**{feature.name}** is now {'on' if enabled else 'off'}.", ephemeral=True)

    @app_commands.command(name="preview", description="Preview the welcome card using your own profile")
    async def preview(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        embed, file = await self.build(i.user)
        await i.followup.send(content=i.user.mention, embed=embed, file=file, ephemeral=True)

    @app_commands.command(name="layout", description="Move/resize the avatar, username or member count on the card")
    @app_commands.describe(x="Horizontal position 0-1 (0.5 = centre)", y="Vertical position 0-1",
                           size="Avatar diameter / text height as a fraction of image width",
                           count_format="Member count text, e.g. 'MEMBER #{count}' (count element only)")
    @app_commands.choices(element=[app_commands.Choice(name=n, value=n) for n in ("avatar", "username", "count")])
    async def layout_cmd(self, i: discord.Interaction, element: app_commands.Choice[str],
                         x: app_commands.Range[float, 0, 1] | None = None,
                         y: app_commands.Range[float, 0, 1] | None = None,
                         size: app_commands.Range[float, 0.01, 0.6] | None = None,
                         count_format: str | None = None):
        saved = copy.deepcopy(self.bot.db.get("welcome_layout", {}))
        el = saved.setdefault(element.value, {})
        for k, v in (("x", x), ("y", y), ("size", size)):
            if v is not None:
                el[k] = v
        if count_format and element.value == "count":
            el["format"] = count_format
        await self.bot.db.set("welcome_layout", saved)
        await i.response.send_message(f"Updated **{element.name}**. Run `/welcome preview` to check.", ephemeral=True)

    @app_commands.command(name="banner", description="Replace the welcome banner image")
    @app_commands.describe(image="PNG / JPG / WEBP with blank spots for the avatar, name and count")
    async def banner(self, i: discord.Interaction, image: discord.Attachment):
        await i.response.defer(ephemeral=True)
        if image.size > 8 * 1024 * 1024:
            return await i.followup.send("That image is over 8 MB.", ephemeral=True)
        data = await image.read()

        def save() -> tuple[int, int]:
            from PIL import Image
            with Image.open(io.BytesIO(data)) as im:
                im = im.convert("RGBA")
                tmp = WELCOME_TEMPLATE.with_suffix(".tmp.png")
                im.save(tmp, "PNG")
                os.replace(tmp, WELCOME_TEMPLATE)   # atomic swap
                return im.size
        try:
            w, h = await asyncio.to_thread(save)
        except OSError:
            log.exception("banner save failed")
            return await i.followup.send("I couldn't write the file (is the assets folder read-only?).", ephemeral=True)
        except Exception:
            return await i.followup.send("That doesn't look like a valid image.", ephemeral=True)
        await i.followup.send(f"✅ Banner saved ({w}×{h}). Run `/welcome preview` and adjust with `/welcome layout`.",
                              ephemeral=True)

    @app_commands.command(name="banner-reset", description="Go back to the default WELCOME banner")
    async def banner_reset(self, i: discord.Interaction):
        WELCOME_TEMPLATE.unlink(missing_ok=True)
        await i.response.send_message("Banner reset. The default WELCOME banner will be used.", ephemeral=True)

    @app_commands.command(name="dm-banner", description="Set the banner shown on the welcome DM")
    @app_commands.describe(image="PNG / JPG / WEBP, wide images (about 3:1) look best")
    async def dm_banner(self, i: discord.Interaction, image: discord.Attachment):
        await i.response.defer(ephemeral=True)
        if image.size > 8 * 1024 * 1024:
            return await i.followup.send("That image is over 8 MB.", ephemeral=True)
        data = await image.read()

        def save() -> tuple[int, int]:
            from PIL import Image, ImageOps
            with Image.open(io.BytesIO(data)) as im:
                im = ImageOps.exif_transpose(im).convert("RGBA")
                if im.width > DM_BANNER_MAX_W:
                    im = im.resize((DM_BANNER_MAX_W, round(im.height * DM_BANNER_MAX_W / im.width)),
                                   Image.LANCZOS)
                tmp = DM_BANNER.with_suffix(".tmp.png")
                im.save(tmp, "PNG")
                os.replace(tmp, DM_BANNER)          # atomic swap
                return im.size
        try:
            w, h = await asyncio.to_thread(save)
        except OSError:
            log.exception("dm banner save failed")
            return await i.followup.send("I couldn't write the file (is the assets folder read-only?).", ephemeral=True)
        except Exception:
            return await i.followup.send("That doesn't look like a valid image.", ephemeral=True)
        await i.followup.send(f"✅ DM banner saved ({w}×{h}). Open `/admin` → **Welcome DM** to choose its position "
                              "and preview it.", ephemeral=True)

    @app_commands.command(name="dm-banner-reset", description="Remove the banner from the welcome DM")
    async def dm_banner_reset(self, i: discord.Interaction):
        DM_BANNER.unlink(missing_ok=True)
        await i.response.send_message("DM banner removed.", ephemeral=True)

    @app_commands.command(name="layout-reset", description="Reset the card layout to defaults")
    async def layout_reset(self, i: discord.Interaction):
        await self.bot.db.set("welcome_layout", {})
        await i.response.send_message("Layout reset.", ephemeral=True)

    @app_commands.command(name="settings", description="Show current welcome settings")
    async def settings(self, i: discord.Interaction):
        db = self.bot.db
        ch = find_welcome_channel(self.bot, i.guild)
        lv = find_leave_channel(self.bot, i.guild)
        yes = lambda k: "on" if db.get(k, True) else "off"
        await i.response.send_message(
            f"Welcome channel: {ch.mention if ch else 'none found, use /welcome channel'}\n"
            f"Leave channel: {lv.mention if lv else 'none found, use /welcome leave-channel'}\n"
            f"Welcome: {yes('welcome_enabled')} · DM: {yes('dm_enabled')} · Leave: {yes('leave_enabled')} · "
            f"Auto role: {yes('autorole_enabled')}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Welcome(bot))
