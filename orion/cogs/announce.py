"""Interactive announcement builder.

/announce new      -> opens a private builder panel (live preview, buttons, dropdowns, modals)
/announce edit     -> reopen a previously published announcement and update it in place
/announce history  -> last published announcements
/announce delete   -> delete a published announcement
/announce scheduled / unschedule -> announcements waiting to be published at a set time
/announce templates / template-delete   (8 built-in templates + your own)

Images are downloaded ONCE when staff add them and stored in the database. Discord CDN links expire
after ~24h, so re-uploading them with every publish/edit keeps banners from breaking later.
"""
import asyncio
import copy
import io
import ipaddress
import logging
import re
import socket
from dataclasses import asdict, dataclass, field, fields
from datetime import timedelta
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image

from .. import alerts, config
from ..checks import require_staff
from ..events import TimeParseError, parse_when

utcnow = discord.utils.utcnow
log = logging.getLogger("orion.announce")

MAX_IMG = 6 * 1024 * 1024
PALETTE = {"Sky blue": 0x38BDF8, "Deep blue": 0x1E3A8A, "Midnight": 0x060A1F, "Silver": 0xCBD5E1,
           "White": 0xFFFFFF, "Crimson": 0xDC2626, "Emerald": 0x10B981, "Gold": 0xFACC15}
MAX_BUTTONS = 5
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
    author: str = ""                      # small line above the title
    buttons: list = field(default_factory=list)   # link buttons: [{"label", "url"}], at most 5

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Draft":
        known = {f.name for f in fields(cls)}
        return cls(**{k: copy.deepcopy(v) for k, v in d.items() if k in known})   # never share lists with a template


def build_embed(d: Draft, thumb: str | None = None, banner: str | None = None, when=None) -> discord.Embed:
    e = discord.Embed(title=d.title or None, description=d.description or None, color=d.color)
    if d.author:
        e.set_author(name=d.author)
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


def parse_links(text: str) -> list[dict]:
    """'Label | https://link' per line, at most 5. Raises ValueError with a plain message."""
    out = []
    for n, line in enumerate((l.strip() for l in text.splitlines()), 1):
        if not line:
            continue
        label, sep, url = (x.strip() for x in line.partition("|"))
        if not sep or not label or not url:
            raise ValueError(f"line {n}: write it as  Label | https://link")
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.netloc or len(url) > 512:
            raise ValueError(f"line {n}: the link must start with http:// or https://")
        if len(label) > 80:
            raise ValueError(f"line {n}: the button label is too long (max 80)")
        out.append({"label": label, "url": url})
    if len(out) > MAX_BUTTONS:
        raise ValueError(f"at most {MAX_BUTTONS} link buttons")
    return out


def link_view(d: Draft) -> discord.ui.View | None:
    """Link buttons shown under the announcement (they need no listener, so they keep working forever)."""
    if not d.buttons:
        return None
    v = discord.ui.View(timeout=None)
    for b in d.buttons[:MAX_BUTTONS]:
        v.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label=b["label"], url=b["url"]))
    return v


async def load_assets(db, draft: Draft):
    """(files, thumbnail url, bottom banner url, top banner url) for a draft, without a Builder."""
    files, urls = [], {}
    for key, iid in (("thumbnail", draft.thumb_id), ("top_banner", draft.top_banner_id), ("banner", draft.banner_id)):
        row = await db.get_image(iid) if iid is not None else None
        if row:
            name = f"{key}.{row['ext']}"
            files.append(discord.File(io.BytesIO(row["data"]), filename=name))
            urls[key] = f"attachment://{name}"
    return files, urls.get("thumbnail"), urls.get("banner"), urls.get("top_banner")


def mention_parts(mentions: list, ping: str) -> tuple[str | None, discord.AllowedMentions]:
    parts = {"everyone": ["@everyone"], "here": ["@here"]}.get(ping, [])
    parts += [f"<@&{m['id']}>" if m["t"] == "role" else f"<@{m['id']}>" for m in mentions]
    allowed = discord.AllowedMentions(
        everyone=ping != "none", roles=[discord.Object(m["id"]) for m in mentions if m["t"] == "role"],
        users=[discord.Object(m["id"]) for m in mentions if m["t"] == "user"])
    return (" ".join(parts) or None), allowed


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
                                        placeholder="#38BDF8")
        self.auth = discord.ui.TextInput(label="Author line (small text above the title)", default=d.author,
                                         required=False, max_length=100)
        for i in (self.t, self.desc, self.foot, self.col, self.auth):
            self.add_item(i)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        d, notice = self.b.draft, None
        d.title, d.description, d.footer = self.t.value.strip(), self.desc.value.strip(), self.foot.value.strip()
        d.author = self.auth.value.strip()
        if self.col.value.strip():
            try:
                d.color = int(self.col.value.strip().lstrip("#"), 16)
                if not 0 <= d.color <= 0xFFFFFF:
                    raise ValueError
            except ValueError:
                notice = "⚠️ That colour isn't valid hex (example: #38BDF8)."
        await self.b.render(notice)


class ImagesModal(discord.ui.Modal, title="Images & link buttons"):
    def __init__(self, b: "Builder"):
        super().__init__()
        self.b = b
        self.thumb = discord.ui.TextInput(label="Thumbnail link (small, top-right)", required=False,
                                          placeholder="https://… or 'clear'")
        self.top = discord.ui.TextInput(label="Top banner link (large, above the text)", required=False,
                                        placeholder="https://… or 'clear'")
        self.banner = discord.ui.TextInput(label="Bottom banner link (large, below the text)", required=False,
                                           placeholder="https://… or 'clear'")
        self.links = discord.ui.TextInput(
            label="Link buttons: one per line, Label | https://link", style=discord.TextStyle.paragraph, required=False,
            max_length=1000, placeholder="Join the event | https://discord.gg/…",
            default="\n".join(f"{b['label']} | {b['url']}" for b in b.draft.buttons)[:1000] or None)
        self.add_item(self.thumb)
        self.add_item(self.top)
        self.add_item(self.banner)
        self.add_item(self.links)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        errors = []
        try:
            self.b.draft.buttons = parse_links(self.links.value)      # blank clears them
        except ValueError as e:
            errors.append(f"Link buttons: {e}")
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
                 mentions=None, ping="none", edit=None, when=None):
        super().__init__(timeout=840)  # stays inside the 15-minute interaction token life
        self.bot, self.origin, self.draft = bot, origin, draft
        self.author_id = origin.user.id
        self.channel_id, self.mentions, self.ping, self.edit = channel_id, mentions or [], ping, edit
        self.when = when                         # aware datetime: publish later instead of now
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
        if self.when and self.when <= utcnow():
            p.append("the scheduled time has already passed (start again with a later time)")
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
        self.b_publish.label = ("Confirm & publish" if self._armed else "Save changes" if self.edit else
                                "Schedule" if self.when else "Publish")
        self.b_publish.style = discord.ButtonStyle.danger if self._armed else discord.ButtonStyle.success

    def _status(self) -> str:
        ch = f"<#{self.channel_id}>" if self.channel_id else "*not selected*"
        men = " ".join(f"<@&{m['id']}>" if m["t"] == "role" else f"<@{m['id']}>" for m in self.mentions) or "none"
        lines = [f"**{'✏️ Editing announcement' if self.edit else '📣 Announcement builder'}**",
                 f"Channel: {ch}  ·  Mentions: {men}  ·  Mass ping: **{self.ping}**"]
        if self.when:
            lines.append(f"🗓️ Will be published <t:{int(self.when.timestamp())}:F> (<t:{int(self.when.timestamp())}:R>)")
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

    @discord.ui.button(label="Images & links", emoji="🖼️", style=discord.ButtonStyle.secondary, row=2)
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
        if self.when and not self.edit:
            sid = await db.announce_schedule_add(ch.id, i.user.id, self.draft.to_dict(), self.mentions, self.ping,
                                                 self.when)
            await db.record_audit(i.user.id, "announce.schedule", "announcement", sid, channel=ch.id,
                                  at=self.when.isoformat())
            self.stop()
            return await self.origin.edit_original_response(
                content=f"🗓️ Scheduled **#{sid}** for <t:{int(self.when.timestamp())}:F> in {ch.mention}. "
                        f"See or cancel it with `/announce scheduled`.", embed=None, attachments=[], view=None)
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
                await msg.edit(content=content, embeds=embeds, attachments=files, allowed_mentions=self.allowed(),
                               view=link_view(self.draft))
                await db.update_announcement(msg.id, self.draft.title, self.draft.to_dict(), self.mentions, self.ping)
            else:
                msg = await ch.send(content=content, embeds=embeds, files=files, allowed_mentions=self.allowed(),
                                    view=link_view(self.draft))
                await db.save_announcement(ch.id, msg.id, i.user.id, self.draft.title,
                                           self.draft.to_dict(), self.mentions, self.ping)
        except discord.HTTPException as e:
            log.exception("Publish failed")
            return await self.render(f"⚠️ Discord refused the message: {e.text}")
        await db.record_audit(i.user.id, "announce.edit" if self.edit else "announce.publish", "announcement", msg.id,
                              channel=ch.id, title=self.draft.title)
        self.stop()
        await self.origin.edit_original_response(
            content=f"✅ {'Updated' if self.edit else 'Published'}: {msg.jump_url}",
            embed=None, attachments=[], view=None)


# --------------------------------------------------------------------- cog
class Announce(commands.GroupCog, name="announce", description="Announcement builder (staff)"):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.db.announce_release_stuck()
        self.scheduler.start()

    async def cog_unload(self):
        self.scheduler.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_staff(interaction)

    # ------------------------------------------------------------ scheduler
    @tasks.loop(seconds=30)
    async def scheduler(self):
        for row in await self.bot.db.announce_claim_due(5):
            try:
                await self._publish_scheduled(row)
            except Exception:
                log.exception("Scheduled announcement #%s crashed", row["id"])
                await self.bot.db.announce_finish(row["id"], "failed", error="internal error")

    @scheduler.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    @scheduler.error
    async def _scheduler_error(self, exc):
        log.exception("Announcement scheduler stopped", exc_info=exc)

    async def _publish_scheduled(self, r):
        db, guild = self.bot.db, self.bot.get_guild(config.GUILD_ID)
        ch = guild.get_channel(r["channel_id"]) if guild else None
        if ch is None:
            return await db.announce_finish(r["id"], "failed", error="the channel no longer exists")
        draft = Draft.from_dict(r["data"])
        files, thumb, banner, top = await load_assets(db, draft)
        content, allowed = mention_parts(r["mentions"], r["ping_mode"])
        try:
            msg = await ch.send(content=content, embeds=build_embeds(draft, thumb, banner, top), files=files,
                                allowed_mentions=allowed, view=link_view(draft))
        except discord.HTTPException as e:
            retry = r["attempts"] < 3 and (e.status >= 500 or e.status == 429)
            if retry:
                return await db.announce_finish(r["id"], "pending", error=f"Discord error {e.status}",
                                                retry_at=utcnow() + timedelta(minutes=2))
            await db.announce_finish(r["id"], "failed", error=f"Discord refused it: {e.text}"[:300])
            return await alerts.alert_staff(
                self.bot, guild, f"Scheduled announcement #{r['id']} could not be published in {ch.mention}: {e.text} "
                f"(missing permissions?). Fix it and schedule it again.", key=f"announce-sched-{r['id']}")
        await db.save_announcement(ch.id, msg.id, r["author_id"], draft.title, draft.to_dict(), r["mentions"],
                                   r["ping_mode"])
        await db.announce_finish(r["id"], "published", message_id=msg.id)
        await db.record_audit(None, "announce.publish_scheduled", "announcement", msg.id, schedule_id=r["id"])

    async def _template_ac(self, i: discord.Interaction, current: str):
        names = await self.bot.db.list_templates()
        return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n][:25]

    @app_commands.command(name="new", description="Open the announcement builder")
    @app_commands.describe(channel="Where to publish (you can change it in the builder)",
                           template="Start from a saved template",
                           thumbnail="Small image shown top-right", banner="Large image shown at the bottom",
                           top_banner="Large image shown at the top",
                           when="Publish later: date and time in server time, e.g. 25/12 18:30 or 2026-12-25 18:30")
    @app_commands.autocomplete(template=_template_ac)
    async def new(self, i: discord.Interaction, channel: discord.TextChannel | None = None,
                  template: str | None = None, thumbnail: discord.Attachment | None = None,
                  banner: discord.Attachment | None = None, top_banner: discord.Attachment | None = None,
                  when: str | None = None):
        publish_at = None
        if when:
            try:
                publish_at = parse_when(when)
            except TimeParseError as e:
                return await i.response.send_message(f"⚠️ {e}", ephemeral=True)
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
        b = Builder(self.bot, i, draft, channel_id=channel.id if channel else None, when=publish_at)
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

    @app_commands.command(name="delete", description="Delete an announcement published with Orion")
    @app_commands.describe(message="Message link or ID")
    async def delete(self, i: discord.Interaction, message: str):
        m = re.search(r"(\d{17,20})\s*$", message)
        rec = await self.bot.db.get_announcement(int(m.group(1))) if m else None
        if not rec:
            return await i.response.send_message("I can only delete announcements published through `/announce new`.",
                                                 ephemeral=True)
        await i.response.defer(ephemeral=True)
        ch = i.guild.get_channel(rec["channel_id"])
        gone = ch is None
        if ch:
            try:
                await (await ch.fetch_message(rec["message_id"])).delete()
            except discord.NotFound:
                gone = True
            except discord.HTTPException as e:
                return await i.followup.send(f"⚠️ Discord refused to delete it: {e.text}", ephemeral=True)
        await self.bot.db.delete_announcement_record(rec["message_id"])
        await self.bot.db.record_audit(i.user.id, "announce.delete", "announcement", rec["message_id"],
                                       title=rec["title"])
        await i.followup.send("🗑️ Deleted." + (" (It was already gone from Discord.)" if gone else ""), ephemeral=True)

    @app_commands.command(name="scheduled", description="Announcements waiting to be published")
    async def scheduled(self, i: discord.Interaction):
        rows = await self.bot.db.announce_scheduled_list(15)
        if not rows:
            return await i.response.send_message("Nothing is scheduled. Use `/announce new when:...`.", ephemeral=True)
        lines = []
        for r in rows:
            title = (r["data"].get("title") or "Untitled")[:60]
            state = "🗓️" if r["status"] == "pending" else f"⚠️ failed ({r['error'] or '?'})"
            lines.append(f"`#{r['id']}` {state} **{title}** · <#{r['channel_id']}> · "
                         f"<t:{int(r['publish_at'].timestamp())}:F> (<t:{int(r['publish_at'].timestamp())}:R>)")
        await i.response.send_message(embed=discord.Embed(title="Scheduled announcements", color=config.COLOR_MAIN,
                                                          description="\n".join(lines)[:3900]), ephemeral=True)

    @app_commands.command(name="unschedule", description="Cancel a scheduled announcement")
    async def unschedule(self, i: discord.Interaction, id: int):
        ok = await self.bot.db.announce_cancel_scheduled(id)
        if ok:
            await self.bot.db.record_audit(i.user.id, "announce.unschedule", "announcement", id)
        await i.response.send_message("✅ Cancelled." if ok else "No pending scheduled announcement with that number.",
                                      ephemeral=True)

    @app_commands.command(name="templates", description="List saved templates")
    async def templates(self, i: discord.Interaction):
        names = await self.bot.db.list_templates()
        await i.response.send_message("Templates (the first eight are built in; save your own from the builder): "
                                      + (", ".join(f"`{n}`" for n in names) or "none"), ephemeral=True)

    @app_commands.command(name="template-delete", description="Delete a saved template")
    @app_commands.autocomplete(name=_template_ac)
    async def template_delete(self, i: discord.Interaction, name: str):
        ok = await self.bot.db.delete_template(name)
        await i.response.send_message("Deleted." if ok else "No saved template with that name (built-in templates can't "
                                      "be deleted).", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Announce(bot))
