"""Interactive announcement builder.

/announce new      -> opens a private builder panel (live preview, buttons, dropdowns, modals)
/announce edit     -> reopen a previously published announcement and update it in place
/announce history  -> last published announcements
/announce templates / template-delete

Images are downloaded ONCE when staff add them and stored in the database. Discord CDN links expire
after ~24h, so re-uploading them with every publish/edit keeps banners from breaking later.
"""
import asyncio
import io
import ipaddress
import logging
import re
import socket
from dataclasses import asdict, dataclass, field, fields
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from .. import config
from ..checks import require_staff

log = logging.getLogger("orion.announce")

MAX_IMG = 6 * 1024 * 1024
PALETTE = {"Violet": 0x7C3AED, "Dark purple": 0x4C1D95, "Midnight": 0x0B0614, "Silver": 0xCBCBDC,
           "White": 0xFFFFFF, "Crimson": 0xDC2626, "Emerald": 0x10B981, "Gold": 0xF59E0B}
PING_MODES = ["none", "here", "everyone"]


# ------------------------------------------------------------------ images
def _is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
        return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)
    except Exception:
        return False


def detect_image(data: bytes) -> str:
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format
            im.verify()
    except Exception:
        raise ValueError("That file isn't a valid image.")
    ext = {"PNG": "png", "JPEG": "jpg", "GIF": "gif", "WEBP": "webp"}.get(fmt)
    if not ext:
        raise ValueError("Use a PNG, JPG, GIF or WEBP image.")
    return ext


async def fetch_image(url: str) -> bytes:
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("Image links must start with https://")
    if not await asyncio.to_thread(_is_public, u.hostname):
        raise ValueError("That link points to a private address.")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(url.strip(), allow_redirects=False, headers={"User-Agent": "Orion"}) as r:
            if r.status != 200:
                raise ValueError(f"Couldn't download the image (HTTP {r.status}). Discord links expire after ~24h, "
                                 "so upload the image straight into `/announce new` instead.")
            data = bytearray()
            async for chunk in r.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > MAX_IMG:
                    raise ValueError("Image is larger than 6 MB.")
    return bytes(data)


async def store_image(db, data: bytes) -> int:
    if len(data) > MAX_IMG:
        raise ValueError("Image is larger than 6 MB.")
    ext = await asyncio.to_thread(detect_image, data)
    return await db.store_image(data, ext)


async def store_attachment(db, att: discord.Attachment) -> int:
    if att.size > MAX_IMG:
        raise ValueError("Image is larger than 6 MB.")
    return await store_image(db, await att.read())


# ------------------------------------------------------------------- draft
@dataclass
class Draft:
    title: str = ""
    description: str = ""
    footer: str = config.BRAND
    color: int = config.COLOR_ACCENT
    timestamp: bool = True
    fields: list = field(default_factory=list)   # [{"name","value","inline"}]
    thumb_id: int | None = None
    banner_id: int | None = None          # bottom banner
    top_banner_id: int | None = None      # top banner

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Draft":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


def build_embed(d: Draft, thumb: str | None = None, banner: str | None = None, when=None) -> discord.Embed:
    e = discord.Embed(title=d.title or None, description=d.description or None, color=d.color)
    for f in d.fields:
        e.add_field(name=f["name"], value=f["value"], inline=f.get("inline", False))
    if d.footer:
        e.set_footer(text=d.footer)
    if thumb:
        e.set_thumbnail(url=thumb)
    if banner:
        e.set_image(url=banner)
    if d.timestamp:
        e.timestamp = when or discord.utils.utcnow()
    return e


def build_embeds(d: Draft, thumb: str | None = None, banner: str | None = None, top: str | None = None,
                 when=None) -> list[discord.Embed]:
    """Discord embeds have ONE big-image slot (the bottom). A top banner is therefore a slim image-only
    embed sent first in the SAME message, so there is still one message ID and /announce edit keeps working."""
    main = build_embed(d, thumb, banner, when)
    if not top:
        return [main]
    head = discord.Embed(color=d.color)
    head.set_image(url=top)
    if not (d.title or d.description or d.fields or thumb or banner):
        # nothing but a top banner: don't send an empty second box, keep footer/timestamp on the banner
        if d.footer:
            head.set_footer(text=d.footer)
        head.timestamp = main.timestamp
        return [head]
    return [head, main]


# ------------------------------------------------------------------ modals
class ContentModal(discord.ui.Modal, title="Announcement content"):
    def __init__(self, b: "Builder"):
        super().__init__()
        self.b, d = b, b.draft
        self.t = discord.ui.TextInput(label="Title", default=d.title, required=False, max_length=256)
        self.desc = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph,
                                         default=d.description, required=False, max_length=4000)
        self.foot = discord.ui.TextInput(label="Footer", default=d.footer, required=False, max_length=200)
        self.col = discord.ui.TextInput(label="Custom colour (hex, optional)", required=False, max_length=7,
                                        placeholder="#7C3AED")
        for i in (self.t, self.desc, self.foot, self.col):
            self.add_item(i)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        d, notice = self.b.draft, None
        d.title, d.description, d.footer = self.t.value.strip(), self.desc.value.strip(), self.foot.value.strip()
        if self.col.value.strip():
            try:
                d.color = int(self.col.value.strip().lstrip("#"), 16)
                if not 0 <= d.color <= 0xFFFFFF:
                    raise ValueError
            except ValueError:
                notice = "⚠️ That colour isn't valid hex (example: #7C3AED)."
        await self.b.render(notice)


class ImagesModal(discord.ui.Modal, title="Thumbnail & banners"):
    def __init__(self, b: "Builder"):
        super().__init__()
        self.b = b
        self.thumb = discord.ui.TextInput(label="Thumbnail link (small, top-right)", required=False,
                                          placeholder="https://… or 'clear'")
        self.top = discord.ui.TextInput(label="Top banner link (large, above the text)", required=False,
                                        placeholder="https://… or 'clear'")
        self.banner = discord.ui.TextInput(label="Bottom banner link (large, below the text)", required=False,
                                           placeholder="https://… or 'clear'")
        self.add_item(self.thumb)
        self.add_item(self.top)
        self.add_item(self.banner)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        errors = []
        for inp, attr in ((self.thumb, "thumb_id"), (self.top, "top_banner_id"), (self.banner, "banner_id")):
            v = inp.value.strip()
            if not v:
                continue
            if v.lower() in ("clear", "none", "remove"):
                setattr(self.b.draft, attr, None)
                continue
            try:
                setattr(self.b.draft, attr, await store_image(self.b.bot.db, await fetch_image(v)))
            except ValueError as e:
                errors.append(f"{inp.label.split(' (')[0]}: {e}")
        await self.b.render("⚠️ " + " ".join(errors) if errors else None)


class FieldModal(discord.ui.Modal, title="Add a field"):
    def __init__(self, b: "Builder"):
        super().__init__()
        self.b = b
        self.n = discord.ui.TextInput(label="Field title", max_length=256)
        self.v = discord.ui.TextInput(label="Field text", style=discord.TextStyle.paragraph, max_length=1024)
        self.inline = discord.ui.TextInput(label="Show side-by-side? (yes / no)", default="no",
                                           required=False, max_length=3)
        for i in (self.n, self.v, self.inline):
            self.add_item(i)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        self.b.draft.fields.append({"name": self.n.value.strip(), "value": self.v.value.strip(),
                                    "inline": self.inline.value.strip().lower() in ("y", "yes", "true")})
        await self.b.render()


class TemplateNameModal(discord.ui.Modal, title="Save as template"):
    def __init__(self, b: "Builder"):
        super().__init__()
        self.b = b
        self.name = discord.ui.TextInput(label="Template name", max_length=50, placeholder="tournament-announcement")
        self.add_item(self.name)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        await self.b.bot.db.save_template(self.name.value, self.b.draft.to_dict(), i.user.id)
        await self.b.render(f"✅ Saved template **{self.name.value.strip().lower()}**.")


class TemplatePicker(discord.ui.View):
    def __init__(self, b: "Builder", names: list[str]):
        super().__init__(timeout=120)
        self.b = b
        sel = discord.ui.Select(placeholder="Choose a template", options=[
            discord.SelectOption(label=n[:100], value=n) for n in names[:25]])
        sel.callback = self.picked
        self.sel = sel
        self.add_item(sel)

    async def picked(self, i: discord.Interaction):
        data = await self.b.bot.db.get_template(self.sel.values[0])
        if data:
            self.b.draft = Draft.from_dict(data)
        await i.response.edit_message(content="Template loaded ✅", view=None)
        await self.b.render(f"✅ Loaded template **{self.sel.values[0]}**.")


# ----------------------------------------------------------------- builder
class Builder(discord.ui.View):
    def __init__(self, bot, origin: discord.Interaction, draft: Draft, channel_id=None,
                 mentions=None, ping="none", edit=None):
        super().__init__(timeout=840)  # stays inside the 15-minute interaction token life
        self.bot, self.origin, self.draft = bot, origin, draft
        self.author_id = origin.user.id
        self.channel_id, self.mentions, self.ping, self.edit = channel_id, mentions or [], ping, edit
        self.notice = None
        self._armed = False
        self._imgs: dict[int, tuple[bytes, str] | None] = {}
        if edit:
            self.pick_channel.disabled = True

    # -------------------------------------------------------- state helpers
    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.author_id:
            await i.response.send_message("This builder belongs to someone else.", ephemeral=True)
            return False
        if (i.data or {}).get("custom_id") != "ann:publish":
            self._armed = False
        return True

    def message_text(self) -> str:
        parts = {"everyone": ["@everyone"], "here": ["@here"]}.get(self.ping, [])
        parts += [f"<@&{m['id']}>" if m["t"] == "role" else f"<@{m['id']}>" for m in self.mentions]
        return " ".join(parts)

    def allowed(self) -> discord.AllowedMentions:
        return discord.AllowedMentions(
            everyone=self.ping != "none",
            roles=[discord.Object(m["id"]) for m in self.mentions if m["t"] == "role"],
            users=[discord.Object(m["id"]) for m in self.mentions if m["t"] == "user"])

    async def _img(self, image_id):
        if image_id is None:
            return None
        if image_id not in self._imgs:
            row = await self.bot.db.get_image(image_id)
            self._imgs[image_id] = (row["data"], row["ext"]) if row else None
        return self._imgs[image_id]

    async def assets(self):
        files, urls = [], {}
        for key, iid in (("thumbnail", self.draft.thumb_id), ("top_banner", self.draft.top_banner_id),
                         ("banner", self.draft.banner_id)):
            img = await self._img(iid)
            if img:
                name = f"{key}.{img[1]}"
                files.append(discord.File(io.BytesIO(img[0]), filename=name))
                urls[key] = f"attachment://{name}"
        return files, urls.get("thumbnail"), urls.get("banner"), urls.get("top_banner")

    def problems(self) -> list[str]:
        p, d, g = [], self.draft, self.origin.guild
        ch = g.get_channel(self.channel_id) if self.channel_id else None
        if not ch:
            p.append("pick a channel")
        if not (d.title or d.description or d.fields or d.banner_id or d.top_banner_id):
            p.append("add some content")
        size = len(build_embed(d))
        if size > 6000:
            p.append(f"the embed is too long ({size}/6000 characters)")
        if ch:
            you, me = ch.permissions_for(self.origin.user), ch.permissions_for(g.me)
            if not (you.send_messages and you.embed_links):
                p.append("you can't post embeds in that channel")
            if not (me.send_messages and me.embed_links and me.attach_files):
                p.append("I need Send Messages, Embed Links and Attach Files there")
            if self.ping != "none" and not self.edit:
                if not you.mention_everyone:
                    p.append("you don't have permission to mass-ping there")
                if not me.mention_everyone:
                    p.append("I need the Mention Everyone permission to mass-ping")
            for m in self.mentions:
                role = g.get_role(m["id"]) if m["t"] == "role" else None
                if role and not role.mentionable and not me.mention_everyone:
                    p.append(f"**{role.name}** isn't mentionable (make it mentionable or give me Mention Everyone)")
        return p

    # -------------------------------------------------------------- render
    def _sync(self):
        T = discord.SelectDefaultValueType
        self.pick_channel.default_values = (
            [discord.SelectDefaultValue(id=self.channel_id, type=T.channel)] if self.channel_id else [])
        self.pick_mentions.default_values = [
            discord.SelectDefaultValue(id=m["id"], type=T.role if m["t"] == "role" else T.user)
            for m in self.mentions]
        self.b_stamp.label = f"Timestamp: {'on' if self.draft.timestamp else 'off'}"
        self.b_ping.label = {"none": "Mass ping: off", "here": "Mass ping: @here",
                             "everyone": "Mass ping: @everyone"}[self.ping]
        self.b_ping.style = discord.ButtonStyle.secondary if self.ping == "none" else discord.ButtonStyle.danger
        self.b_ping.disabled = bool(self.edit)
        self.b_remove.disabled = not self.draft.fields
        self.b_field.disabled = len(self.draft.fields) >= 25
        self.b_publish.label = ("Confirm & publish" if self._armed else
                                "Save changes" if self.edit else "Publish")
        self.b_publish.style = discord.ButtonStyle.danger if self._armed else discord.ButtonStyle.success

    def _status(self) -> str:
        ch = f"<#{self.channel_id}>" if self.channel_id else "*not selected*"
        men = " ".join(f"<@&{m['id']}>" if m["t"] == "role" else f"<@{m['id']}>" for m in self.mentions) or "none"
        lines = [f"**{'✏️ Editing announcement' if self.edit else '📣 Announcement builder'}**",
                 f"Channel: {ch}  ·  Mentions: {men}  ·  Mass ping: **{self.ping}**"]
        if self.notice:
            lines.append(self.notice)
        return "\n".join(lines)

    async def render(self, notice: str | None = None):
        self.notice = notice
        self._sync()
        files, thumb, banner, top = await self.assets()
        d = self.draft
        embeds = build_embeds(d, thumb, banner, top)
        if not (d.title or d.description or d.fields or banner or top):
            embeds[-1].description = "*Empty. Click **Edit content** to start.*"
        try:
            await self.origin.edit_original_response(content=self._status(), embeds=embeds,
                                                     attachments=files, view=self)
        except discord.HTTPException:
            log.exception("Builder render failed")

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        try:
            await self.origin.edit_original_response(content="⏱️ Builder timed out. Run `/announce new` again.",
                                                     view=self)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------ controls
    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text, discord.ChannelType.news],
                       placeholder="Channel to publish in", row=0)
    async def pick_channel(self, i: discord.Interaction, sel: discord.ui.ChannelSelect):
        await i.response.defer()
        self.channel_id = sel.values[0].id if sel.values else None
        await self.render()

    @discord.ui.select(cls=discord.ui.MentionableSelect, placeholder="Roles / members to mention (optional)",
                       min_values=0, max_values=25, row=1)
    async def pick_mentions(self, i: discord.Interaction, sel: discord.ui.MentionableSelect):
        await i.response.defer()
        self.mentions = [{"t": "role" if isinstance(v, discord.Role) else "user", "id": v.id} for v in sel.values]
        await self.render()

    @discord.ui.button(label="Edit content", emoji="📝", style=discord.ButtonStyle.primary, row=2)
    async def b_content(self, i: discord.Interaction, _):
        await i.response.send_modal(ContentModal(self))

    @discord.ui.button(label="Images", emoji="🖼️", style=discord.ButtonStyle.secondary, row=2)
    async def b_images(self, i: discord.Interaction, _):
        await i.response.send_modal(ImagesModal(self))

    @discord.ui.button(label="Add field", emoji="➕", style=discord.ButtonStyle.secondary, row=2)
    async def b_field(self, i: discord.Interaction, _):
        await i.response.send_modal(FieldModal(self))

    @discord.ui.button(label="Remove last field", emoji="➖", style=discord.ButtonStyle.secondary, row=2)
    async def b_remove(self, i: discord.Interaction, _):
        await i.response.defer()
        if self.draft.fields:
            self.draft.fields.pop()
        await self.render()

    @discord.ui.button(label="Timestamp", emoji="🕒", style=discord.ButtonStyle.secondary, row=2)
    async def b_stamp(self, i: discord.Interaction, _):
        await i.response.defer()
        self.draft.timestamp = not self.draft.timestamp
        await self.render()

    @discord.ui.button(label="Mass ping", style=discord.ButtonStyle.secondary, row=3)
    async def b_ping(self, i: discord.Interaction, _):
        await i.response.defer()
        self.ping = PING_MODES[(PING_MODES.index(self.ping) + 1) % 3]
        await self.render()

    @discord.ui.button(label="Save template", emoji="💾", style=discord.ButtonStyle.secondary, row=3)
    async def b_save(self, i: discord.Interaction, _):
        await i.response.send_modal(TemplateNameModal(self))

    @discord.ui.button(label="Load template", emoji="📂", style=discord.ButtonStyle.secondary, row=3)
    async def b_load(self, i: discord.Interaction, _):
        names = await self.bot.db.list_templates()
        if not names:
            await i.response.send_message("No templates saved yet.", ephemeral=True)
            return
        await i.response.send_message("Pick a template (it replaces your current content):",
                                      view=TemplatePicker(self, names), ephemeral=True)

    @discord.ui.button(label="Publish", emoji="📣", style=discord.ButtonStyle.success, custom_id="ann:publish", row=3)
    async def b_publish(self, i: discord.Interaction, _):
        await i.response.defer()
        problems = self.problems()
        if problems:
            return await self.render("⚠️ Can't publish yet: " + "; ".join(problems) + ".")
        if self.ping != "none" and not self.edit and not self._armed:
            self._armed = True
            return await self.render(f"⚠️ This will ping **@{self.ping}**. Click **Confirm & publish** to continue.")
        await self._publish(i)

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.danger, row=3)
    async def b_cancel(self, i: discord.Interaction, _):
        await i.response.defer()
        self.stop()
        await self.origin.edit_original_response(content="Cancelled. Nothing was posted.", embed=None,
                                                 attachments=[], view=None)

    @discord.ui.select(placeholder="Embed colour", row=4, options=[
        discord.SelectOption(label=n, value=str(v)) for n, v in PALETTE.items()])
    async def pick_color(self, i: discord.Interaction, sel: discord.ui.Select):
        await i.response.defer()
        self.draft.color = int(sel.values[0])
        await self.render()

    # ------------------------------------------------------------- publish
    async def _publish(self, i: discord.Interaction):
        db, ch = self.bot.db, self.origin.guild.get_channel(self.channel_id)
        files, thumb, banner, top = await self.assets()
        when = self.edit["published_at"] if self.edit else None
        embeds = build_embeds(self.draft, thumb, banner, top, when)
        content = self.message_text() or None
        try:
            if self.edit:
                try:
                    msg = await ch.fetch_message(self.edit["message_id"])
                except discord.NotFound:
                    return await self.render("⚠️ The original message was deleted, so it can't be edited. "
                                             "Start a new announcement instead.")
                await msg.edit(content=content, embeds=embeds, attachments=files, allowed_mentions=self.allowed())
                await db.update_announcement(msg.id, self.draft.title, self.draft.to_dict(), self.mentions, self.ping)
            else:
                msg = await ch.send(content=content, embeds=embeds, files=files, allowed_mentions=self.allowed())
                await db.save_announcement(ch.id, msg.id, i.user.id, self.draft.title,
                                           self.draft.to_dict(), self.mentions, self.ping)
        except discord.HTTPException as e:
            log.exception("Publish failed")
            return await self.render(f"⚠️ Discord refused the message: {e.text}")
        self.stop()
        await self.origin.edit_original_response(
            content=f"✅ {'Updated' if self.edit else 'Published'}: {msg.jump_url}",
            embed=None, attachments=[], view=None)


# --------------------------------------------------------------------- cog
class Announce(commands.GroupCog, name="announce", description="Announcement builder (staff)"):
    def __init__(self, bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_staff(interaction)

    async def _template_ac(self, i: discord.Interaction, current: str):
        names = await self.bot.db.list_templates()
        return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n][:25]

    @app_commands.command(name="new", description="Open the announcement builder")
    @app_commands.describe(channel="Where to publish (you can change it in the builder)",
                           template="Start from a saved template",
                           thumbnail="Small image shown top-right", banner="Large image shown at the bottom",
                           top_banner="Large image shown at the top")
    @app_commands.autocomplete(template=_template_ac)
    async def new(self, i: discord.Interaction, channel: discord.TextChannel | None = None,
                  template: str | None = None, thumbnail: discord.Attachment | None = None,
                  banner: discord.Attachment | None = None, top_banner: discord.Attachment | None = None):
        await i.response.defer(ephemeral=True)
        db, draft, notes = self.bot.db, Draft(), []
        if template:
            data = await db.get_template(template)
            if data:
                draft = Draft.from_dict(data)
            else:
                notes.append(f"Template **{template}** doesn't exist.")
        for att, attr in ((thumbnail, "thumb_id"), (top_banner, "top_banner_id"), (banner, "banner_id")):
            if att:
                try:
                    setattr(draft, attr, await store_attachment(db, att))
                except ValueError as e:
                    notes.append(f"{attr.removesuffix('_id').replace('_', ' ')}: {e}")
        b = Builder(self.bot, i, draft, channel_id=channel.id if channel else None)
        await b.render("⚠️ " + " ".join(notes) if notes else None)

    @app_commands.command(name="edit", description="Edit an announcement you published with Orion")
    @app_commands.describe(message="Message link or ID")
    async def edit(self, i: discord.Interaction, message: str):
        m = re.search(r"(\d{17,20})\s*$", message)
        rec = await self.bot.db.get_announcement(int(m.group(1))) if m else None
        if not rec:
            return await i.response.send_message(
                "I can only edit announcements that were published through `/announce new`. "
                "Paste the message link or ID.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        b = Builder(self.bot, i, Draft.from_dict(rec["data"]), channel_id=rec["channel_id"],
                    mentions=rec["mentions"], ping=rec["ping_mode"], edit=dict(rec))
        await b.render()

    @app_commands.command(name="history", description="Recently published announcements")
    async def history(self, i: discord.Interaction):
        rows = await self.bot.db.recent_announcements(10)
        if not rows:
            return await i.response.send_message("Nothing published yet.", ephemeral=True)
        lines = [f"[{r['title'] or 'Untitled'}](https://discord.com/channels/{i.guild_id}/{r['channel_id']}/"
                 f"{r['message_id']}) · <#{r['channel_id']}> · <t:{int(r['published_at'].timestamp())}:R>"
                 for r in rows]
        await i.response.send_message(
            embed=discord.Embed(title="Recent announcements", description="\n".join(lines), color=config.COLOR_MAIN),
            ephemeral=True)

    @app_commands.command(name="templates", description="List saved templates")
    async def templates(self, i: discord.Interaction):
        names = await self.bot.db.list_templates()
        await i.response.send_message("Templates: " + (", ".join(f"`{n}`" for n in names) or "none yet"),
                                      ephemeral=True)

    @app_commands.command(name="template-delete", description="Delete a saved template")
    @app_commands.autocomplete(name=_template_ac)
    async def template_delete(self, i: discord.Interaction, name: str):
        ok = await self.bot.db.delete_template(name)
        await i.response.send_message("Deleted." if ok else "No template with that name.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Announce(bot))
