"""Giveaway panels: the creation builder and the post-publish management panel (buttons, dropdowns, modals).

  /giveaway create  -> Builder        (live preview of the public embed, nothing is posted until Publish)
  /giveaway manage  -> ManagePanel    (edit, extend, end early, participants, winners, reroll, export, audit ...)

Reward codes typed here go straight into the vault (encrypted) and are never echoed back to anyone.
"""
import asyncio
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Optional

import aiohttp
import discord

from . import config, vault
from .cogs.announce import fetch_image, store_attachment, store_image
from .giveaway_card import (BODY_MAX, DEFAULT_BODY, DEFAULT_BUTTONS, PLACEHOLDERS, SAFE_LIMIT, CardView, live_spec,
                            make_context, parse_button, render_text, resolve_body)
from .giveaways import (DEFAULT_ELIGIBLE, INTERVAL_PRESETS, MAX_DURATION, MAX_INTERVAL, MAX_WINNERS, MIN_DURATION,
                        MIN_INTERVAL, REWARD_TYPES, DurationError, csv_bytes, format_duration, ordinal,
                        parse_duration, place_label)

if TYPE_CHECKING:
    from .cogs.giveaway import Giveaways

log = logging.getLogger("orion.giveaway_ui")
utcnow = discord.utils.utcnow


def _yes(text: str) -> bool:
    return text.strip().lower() in ("y", "yes", "true", "on", "1")


def _jump(guild_id: int, gw) -> str:
    return f"https://discord.com/channels/{guild_id}/{gw['channel_id']}/{gw['message_id']}"


# =================================================================== drafts
@dataclass
class RewardDraft:
    name: str
    description: str = ""
    value: str = ""
    reward_type: str = "custom"
    secret: str = ""                  # plain text lives only in memory until Publish encrypts it


@dataclass
class GiveawayDraft:
    title: str = ""
    body: str = DEFAULT_BODY          # the big text box (markdown + {placeholders})
    prize_pool: str = ""
    banner_id: Optional[int] = None   # top banner
    bottom_id: Optional[int] = None   # bottom banner
    thumb_id: Optional[int] = None    # small side image
    accent: Optional[int] = None      # card colour
    buttons: dict = field(default_factory=dict)   # {"enter": "🔥 Join now", "me": ..., "claim": ...}
    duration: int = 3 * 86400
    interval: int = 86400
    winners: int = 1
    roles: list = field(default_factory=lambda: list(DEFAULT_ELIGIBLE))
    multi: bool = False
    reassign: bool = False
    claim_hours: Optional[int] = None
    rewards: list = field(default_factory=list)


# ================================================================ reward editor
class RewardModal(discord.ui.Modal):
    """One modal for adding and editing a reward slot, before or after publishing."""

    def __init__(self, rtype: str, existing: Optional[dict], cb):
        label, _, kind = REWARD_TYPES[rtype]
        super().__init__(title=f"{label} reward"[:45])
        self.cb, ex = cb, existing or {}
        self.name_in = discord.ui.TextInput(label="Reward name", default=ex.get("name", ""), max_length=100,
                                            placeholder="e.g. $50 Steam gift card")
        self.desc_in = discord.ui.TextInput(label="Description (optional)", style=discord.TextStyle.paragraph,
                                            default=ex.get("description", ""), required=False, max_length=300)
        self.value_in = discord.ui.TextInput(label="Value (optional)", default=ex.get("value", ""), required=False,
                                             max_length=50, placeholder="$50")
        if kind == "code":
            self.secret_in = discord.ui.TextInput(
                label="New code (blank = keep current)" if existing else "Reward code(s), one per line",
                style=discord.TextStyle.paragraph, required=not existing, max_length=1000,
                placeholder="XXXX-XXXX-XXXX")
        else:
            self.secret_in = discord.ui.TextInput(
                label="New delivery info (blank = keep)" if existing else "Delivery instructions (private)",
                style=discord.TextStyle.paragraph, required=False, max_length=1000,
                placeholder="What the winner needs to do / what you'll do to get it to them")
        for item in (self.name_in, self.desc_in, self.value_in, self.secret_in):
            self.add_item(item)

    async def on_submit(self, i: discord.Interaction):
        await self.cb(i, {"name": self.name_in.value.strip(), "description": self.desc_in.value.strip(),
                          "value": self.value_in.value.strip(), "secret": self.secret_in.value})


class RewardsEditor(discord.ui.View):
    """Add / edit / remove reward slots. `backend` decides whether that touches the builder's draft or the live
    giveaway in the database (see DraftRewards / LiveRewards)."""

    def __init__(self, backend, slots: list[dict]):
        super().__init__(timeout=600)
        self.backend, self.slots = backend, slots
        if backend.can_add:
            add = discord.ui.Select(placeholder="➕ Add a reward slot: pick its type", row=0, options=[
                discord.SelectOption(label=lab, value=k, emoji=em) for k, (lab, em, _) in REWARD_TYPES.items()])
            add.callback = self._on_add
            self.add_item(add)
        editable = [s for s in slots if not s["locked"]][:25]
        if editable:
            ed = discord.ui.Select(placeholder="✏️ Edit a slot", row=1, options=[
                discord.SelectOption(label=f"{ordinal(s['position'])} · {s['name']}"[:100], value=str(s["key"]))
                for s in editable])
            ed.callback = self._on_edit
            self.add_item(ed)
        self.b_remove.disabled = not backend.can_remove(len(slots))

    def text(self, notice: Optional[str] = None) -> str:
        lines = [f"🎁 **Reward slots ({len(self.slots)})**: slot 1 goes to the first drawn winner, slot 2 to the second, …"]
        lines += [s["line"] for s in self.slots] or ["*No slots yet. Add one with the first dropdown.*"]
        lines.append("-# Codes and delivery info are encrypted and never shown again. Pasting several codes "
                     "(one per line) creates one slot per code.")
        if notice:
            lines.insert(1, notice)
        return "\n".join(lines)[:1990]

    async def _on_add(self, i: discord.Interaction):
        rtype = i.data["values"][0]

        async def cb(mi, f):
            await show_rewards(mi, self.backend, await self.backend.add(rtype, f))
        await i.response.send_modal(RewardModal(rtype, None, cb))

    async def _on_edit(self, i: discord.Interaction):
        slot = next((s for s in self.slots if str(s["key"]) == i.data["values"][0]), None)
        if slot is None:
            return await show_rewards(i, self.backend, "⚠️ That slot no longer exists.")

        async def cb(mi, f):
            await show_rewards(mi, self.backend, await self.backend.edit(slot, f))
        await i.response.send_modal(RewardModal(slot["reward_type"], slot, cb))

    @discord.ui.button(label="Remove last slot", emoji="➖", style=discord.ButtonStyle.secondary, row=2)
    async def b_remove(self, i: discord.Interaction, _):
        await show_rewards(i, self.backend, await self.backend.remove_last())

    @discord.ui.button(label="Done", emoji="✅", style=discord.ButtonStyle.success, row=2)
    async def b_done(self, i: discord.Interaction, _):
        self.stop()
        await i.response.edit_message(content="✅ Rewards updated.", view=None)


async def show_rewards(i: discord.Interaction, backend, notice: Optional[str] = None, *, first: bool = False):
    view = RewardsEditor(backend, await backend.slots())
    if first:
        await i.response.send_message(view.text(notice), view=view, ephemeral=True)
    else:
        await i.response.edit_message(content=view.text(notice), view=view)


class DraftRewards:
    """Reward slots of the giveaway being built (plain text in memory until Publish)."""
    can_add = True

    def __init__(self, builder: "Builder"):
        self.b = builder

    def can_remove(self, n: int) -> bool:
        return n > 0

    async def slots(self) -> list[dict]:
        out = []
        for n, r in enumerate(self.b.draft.rewards):
            label, emoji, kind = REWARD_TYPES[r.reward_type]
            state = ("🔒 code saved" if r.secret else "⚠️ no code") if kind == "code" else (
                "📝 instructions saved" if r.secret else "no instructions")
            val = f" ({r.value})" if r.value else ""
            out.append({"key": n, "position": n + 1, "name": r.name, "description": r.description, "value": r.value,
                        "reward_type": r.reward_type, "locked": False,
                        "line": f"{place_label(n + 1)} — {emoji} **{r.name}**{val} · {label} · {state}"})
        return out

    async def add(self, rtype: str, f: dict) -> Optional[str]:
        d = self.b.draft
        kind = REWARD_TYPES[rtype][2]
        if kind == "code":
            secrets_ = [x.strip() for x in f["secret"].splitlines() if x.strip()]
            if not secrets_:
                return "⚠️ Enter the reward code (one per line)."
        else:
            secrets_ = [f["secret"].strip()]
        if not f["name"]:
            return "⚠️ A reward needs a name."
        if len(d.rewards) + len(secrets_) > MAX_WINNERS:
            return f"⚠️ A giveaway can have at most {MAX_WINNERS} reward slots."
        for s in secrets_:
            d.rewards.append(RewardDraft(f["name"], f["description"], f["value"], rtype, s))
        d.winners = max(d.winners, len(d.rewards))
        await self.b.render()
        return f"✅ Added {len(secrets_)} slot{'s' if len(secrets_) != 1 else ''}."

    async def edit(self, slot: dict, f: dict) -> Optional[str]:
        r = self.b.draft.rewards[slot["key"]]
        if not f["name"]:
            return "⚠️ A reward needs a name."
        r.name, r.description, r.value = f["name"], f["description"], f["value"]
        if f["secret"].strip():
            r.secret = f["secret"].strip().splitlines()[0].strip() if REWARD_TYPES[r.reward_type][2] == "code" \
                else f["secret"].strip()
        await self.b.render()
        return "✅ Slot updated."

    async def remove_last(self) -> Optional[str]:
        d = self.b.draft
        if d.rewards:
            d.rewards.pop()
            d.winners = max(1, len(d.rewards))
        await self.b.render()
        return "✅ Removed the last slot."


class LiveRewards:
    """Reward slots of a PUBLISHED giveaway. Secrets are encrypted straight away; claimed slots are locked."""

    def __init__(self, panel: "ManagePanel"):
        self.p = panel

    @property
    def can_add(self) -> bool:
        return bool(self.p.gw) and self.p.gw["status"] == "active"

    def can_remove(self, n: int) -> bool:
        return self.can_add and n > 1

    async def slots(self) -> list[dict]:
        out = []
        for r in await self.p.bot.db.gw_rewards(self.p.gid):
            label, emoji, kind = REWARD_TYPES.get(r["reward_type"], REWARD_TYPES["custom"])
            who = {"open": "no winner yet", "assigned": f"won by <@{r['winner_id']}>, unclaimed",
                   "claimed": f"claimed by <@{r['winner_id']}>", "delivered": f"delivered to <@{r['winner_id']}>"}[r["status"]]
            secret = "🔒 saved" if r["secret_enc"] else "none"
            val = f" ({r['value']})" if r["value"] else ""
            out.append({"key": r["id"], "position": r["position"], "name": r["name"], "description": r["description"],
                        "value": r["value"], "reward_type": r["reward_type"],
                        "locked": r["status"] in ("claimed", "delivered"),
                        "line": f"{place_label(r['position'])} — {emoji} **{r['name']}**{val} · {who} · secret {secret}"})
        return out

    async def add(self, rtype: str, f: dict) -> Optional[str]:
        db = self.p.bot.db
        kind = REWARD_TYPES[rtype][2]
        secrets_ = [x.strip() for x in f["secret"].splitlines() if x.strip()] if kind == "code" else [f["secret"].strip()]
        if not f["name"] or not secrets_:
            return "⚠️ Enter a name" + (" and a code." if kind == "code" else ".")
        have = len(await db.gw_rewards(self.p.gid))
        if have + len(secrets_) > MAX_WINNERS:
            return f"⚠️ A giveaway can have at most {MAX_WINNERS} reward slots."
        try:
            await vault.verify(db)
            blobs = [vault.encrypt(s) if s else None for s in secrets_]
        except vault.VaultError as e:
            return f"⚠️ {e}"
        added = 0
        for blob in blobs:
            if await db.reward_add(self.p.gid, {"name": f["name"], "description": f["description"], "value": f["value"],
                                                "reward_type": rtype, "secret_enc": blob}):
                added += 1
        await self.p.cog.audit(self.p.gid, self.p.author_id, "reward_add", slots=added, type=rtype)
        await self.p.after_change()
        return f"✅ Added {added} slot{'s' if added != 1 else ''}." if added else "⚠️ The giveaway is no longer active."

    async def edit(self, slot: dict, f: dict) -> Optional[str]:
        db = self.p.bot.db
        if not f["name"]:
            return "⚠️ A reward needs a name."
        fields = {"name": f["name"], "description": f["description"], "value": f["value"]}
        replaced = bool(f["secret"].strip())
        if replaced:
            try:
                await vault.verify(db)
                text = f["secret"].strip().splitlines()[0].strip() \
                    if REWARD_TYPES[slot["reward_type"]][2] == "code" else f["secret"].strip()
                fields["secret_enc"] = vault.encrypt(text)
            except vault.VaultError as e:
                return f"⚠️ {e}"
        if not await db.reward_update(slot["key"], **fields):
            return "⚠️ That reward was already claimed, so it can't be changed."
        await self.p.cog.audit(self.p.gid, self.p.author_id, "reward_update", reward_id=slot["key"],
                               position=slot["position"], secret_replaced=replaced)
        await self.p.after_change()
        return "✅ Slot updated." + (" The old code is replaced." if replaced else "")

    async def remove_last(self) -> Optional[str]:
        if not await self.p.bot.db.reward_remove_last(self.p.gid):
            return "⚠️ Can't remove it (a giveaway keeps at least one slot, and only live ones can shrink)."
        await self.p.cog.audit(self.p.gid, self.p.author_id, "reward_remove_last")
        await self.p.after_change()
        return "✅ Removed the last slot."


# ===================================================================== modals
class DetailsModal(discord.ui.Modal, title="Giveaway text"):
    def __init__(self, b: "Builder"):
        super().__init__()
        self.b, d = b, b.draft
        self.t = discord.ui.TextInput(label="Title", default=d.title, max_length=100, placeholder="$1,000 Giveaway")
        self.body = discord.ui.TextInput(label="Card text (see ℹ️ for placeholders)", style=discord.TextStyle.paragraph,
                                         default=d.body, required=False, max_length=BODY_MAX)
        self.pool = discord.ui.TextInput(label="Prize pool", default=d.prize_pool, required=False, max_length=100,
                                         placeholder="$1,000 in gift cards")
        for item in (self.t, self.body, self.pool):
            self.add_item(item)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        d = self.b.draft
        d.title, d.body, d.prize_pool = self.t.value.strip(), self.body.value.strip() or DEFAULT_BODY, self.pool.value.strip()
        await self.b.render()


class SettingsModal(discord.ui.Modal, title="Giveaway settings"):
    def __init__(self, b: "Builder"):
        super().__init__()
        self.b, d = b, b.draft
        self.winners = discord.ui.TextInput(label=f"Number of winners (1-{MAX_WINNERS})", default=str(d.winners), max_length=2)
        self.dur = discord.ui.TextInput(label="Duration", default=format_duration(d.duration), max_length=30,
                                        placeholder="3d, 12h, 1 week …")
        self.interval = discord.ui.TextInput(label="Check-in every (e.g. 8h, 90m, 2d)", default=format_duration(d.interval),
                                             max_length=30, placeholder="how often members can earn +1 entry")
        self.claim = discord.ui.TextInput(label="Claim deadline in hours (blank = none)", required=False, max_length=4,
                                          default=str(d.claim_hours) if d.claim_hours else "")
        for item in (self.winners, self.dur, self.interval, self.claim):
            self.add_item(item)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        d, errors = self.b.draft, []
        try:
            n = int(self.winners.value.strip())
            if not 1 <= n <= MAX_WINNERS:
                raise ValueError
            if n < len(d.rewards):
                errors.append(f"You have {len(d.rewards)} reward slots: remove slots first to lower the winners.")
            else:
                d.winners = n
        except ValueError:
            errors.append(f"Winners must be a number from 1 to {MAX_WINNERS}.")
        try:
            d.duration = parse_duration(self.dur.value, MIN_DURATION, MAX_DURATION)
        except DurationError as e:
            errors.append(f"Duration: {e}")
        try:
            d.interval = parse_duration(self.interval.value, MIN_INTERVAL, MAX_INTERVAL)
        except DurationError as e:
            errors.append(f"Check-in interval: {e}")
        c = self.claim.value.strip()
        if not c or c == "0":
            d.claim_hours = None
        elif c.isdigit() and 1 <= int(c) <= 2160:
            d.claim_hours = int(c)
        else:
            errors.append("Claim deadline must be 1 to 2160 hours, or blank for none.")
        await self.b.render("⚠️ " + " ".join(errors) if errors else None)


class IntervalModal(discord.ui.Modal, title="Custom check-in interval"):
    def __init__(self, owner, current: int, apply):
        super().__init__()
        self.owner, self.apply = owner, apply
        self.val = discord.ui.TextInput(label="How often can people earn an entry?", default=format_duration(current),
                                        max_length=30, placeholder="90m, 8h, 2d …")
        self.add_item(self.val)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        try:
            await self.apply(parse_duration(self.val.value, MIN_INTERVAL, MAX_INTERVAL))
        except DurationError as e:
            await self.owner.render(f"⚠️ Check-in interval: {e}")


class ImagesModal(discord.ui.Modal, title="Giveaway images"):
    """Top banner, bottom banner and a small side image. Blank = keep, 'clear' = remove."""
    SLOTS = (("top", "Top banner: link (blank = keep)"), ("bottom", "Bottom banner: link (blank = keep)"),
             ("side", "Side image: link (blank = keep)"))

    def __init__(self, owner, apply):
        super().__init__()
        self.owner, self.apply, self.inputs = owner, apply, {}
        for key, label in self.SLOTS:
            t = discord.ui.TextInput(label=label, required=False, max_length=500, placeholder="https://…   or   clear")
            self.inputs[key] = t
            self.add_item(t)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        updates, errors = {}, []
        for key, t in self.inputs.items():
            v = t.value.strip()
            if not v:
                continue
            if v.lower() in ("clear", "none", "remove"):
                updates[key] = None
                continue
            try:
                updates[key] = await store_image(self.owner.bot.db, await fetch_image(v))
            except (ValueError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                errors.append(f"{key}: {e if isinstance(e, ValueError) else 'could not download it'}")
        if updates:
            await self.apply(updates)
        if errors:
            await self.owner.render("⚠️ Images: " + "; ".join(errors))


_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


class StyleModal(discord.ui.Modal, title="Card style & button names"):
    def __init__(self, owner, accent: Optional[int], buttons: dict, apply):
        super().__init__()
        self.owner, self.apply = owner, apply
        btn = buttons or {}
        self.colour = discord.ui.TextInput(label="Card colour, hex (blank = default)", required=False, max_length=7,
                                           default=f"#{accent:06X}" if accent is not None else "", placeholder="#7C3AED")
        self.fields = {}
        for kind, label in (("enter", "Enter button (emoji + text)"), ("me", "Status button (emoji + text)"),
                            ("claim", "Claim button (emoji + text)")):
            d_label, d_emoji = DEFAULT_BUTTONS[kind]
            self.fields[kind] = discord.ui.TextInput(label=label, required=False, max_length=80,
                                                     default=btn.get(kind, ""), placeholder=f"{d_emoji} {d_label}")
        for item in (self.colour, *self.fields.values()):
            self.add_item(item)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        c, accent = self.colour.value.strip(), None
        if c:
            m = _HEX.match(c)
            if not m:
                return await self.owner.render("⚠️ The colour must look like #7C3AED.")
            accent = int(m[1], 16)
        buttons = {k: f.value.strip() for k, f in self.fields.items() if f.value.strip()}
        await self.apply(accent, buttons)


def help_text() -> str:
    lines = ["**Card text placeholders** (they update by themselves):"]
    lines += [f"`{k}` {v}" for k, v in PLACEHOLDERS]
    lines.append("\n**Formatting:** `## Big heading`, `### Smaller heading`, `-# small grey text`, `- bullet`, `**bold**`, "
                 "`*italic*`, emojis, and `<t:UNIX:f>` dates. A blank line starts a new paragraph.")
    lines.append("**Images:** top banner, bottom banner and a small side image (Images button).")
    return "\n".join(lines)


# ==================================================================== builder
class Builder(discord.ui.View):
    """Private creation panel. Two messages: this control panel, and a live preview of the real card below it."""

    def __init__(self, cog: "Giveaways", origin: discord.Interaction, draft: GiveawayDraft,
                 channel_id: Optional[int] = None):
        super().__init__(timeout=840)          # stays inside the 15-minute interaction token life
        self.cog, self.bot, self.origin, self.draft = cog, cog.bot, origin, draft
        self.author_id, self.channel_id = origin.user.id, channel_id
        self.preview_msg = None
        self._publishing = False
        self._panel_sent = False

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.author_id:
            await i.response.send_message("This builder belongs to someone else.", ephemeral=True)
            return False
        return True

    # ------------------------------------------------------------- preview
    def ids(self) -> dict:
        d = self.draft
        return {"top": d.banner_id, "bottom": d.bottom_id, "side": d.thumb_id}

    def _gw(self) -> dict:
        d, now = self.draft, utcnow()
        return {"id": None, "title": d.title or "Untitled giveaway", "body": d.body, "description": "",
                "extra_message": "", "prize_pool": d.prize_pool, "winners_count": max(d.winners, len(d.rewards)),
                "starts_at": now, "ends_at": now + timedelta(seconds=d.duration), "interval_seconds": d.interval,
                "status": "active", "claim_hours": d.claim_hours, "accent": d.accent, "buttons": d.buttons}

    def _rewards(self) -> list[dict]:
        return [{"position": n, "name": r.name, "value": r.value, "reward_type": r.reward_type, "winner_id": None}
                for n, r in enumerate(self.draft.rewards, 1)]

    def problems(self) -> list[str]:
        d, g, p = self.draft, self.origin.guild, []
        if not d.title:
            p.append("add a title (Text)")
        if not d.rewards:
            p.append("add at least one reward slot (Rewards)")
        elif d.winners != len(d.rewards):
            p.append(f"you set {d.winners} winners but have {len(d.rewards)} reward slots (Settings / Rewards)")
        if not d.roles:
            p.append("choose at least one eligible role")
        elif not any(discord.utils.get(g.roles, name=n) for n in d.roles):
            p.append("none of the eligible roles exist on this server")
        ch = g.get_channel(self.channel_id) if self.channel_id else None
        if ch is None:
            p.append("pick the announcement channel")
        else:
            me = ch.permissions_for(g.me)
            need = [("View Channel", me.view_channel), ("Send Messages", me.send_messages)] + (
                [("Attach Files", me.attach_files)] if any(self.ids().values()) else [])
            miss = [n for n, ok in need if not ok]
            if miss:
                p.append(f"I need **{', '.join(miss)}** in {ch.mention}")
        ctx = make_context(self._gw(), self._rewards(), participants=0, entries=0,
                           eligible_text=self.cog.eligible_text(g, d.roles))
        shown = len(render_text(resolve_body({"body": d.body}), ctx))
        if shown + 250 > SAFE_LIMIT:
            p.append(f"the card text is too long ({shown:,} characters once placeholders are filled in; the limit is about "
                     f"{SAFE_LIMIT - 250:,})")
        return p

    def status_text(self, notice: Optional[str]) -> str:
        d, ch = self.draft, f"<#{self.channel_id}>" if self.channel_id else "*not selected*"
        n_img = sum(1 for v in self.ids().values() if v)
        lines = ["**🎁 Giveaway builder**: the card below is exactly what will be posted. Nothing is published until you press **Publish**.",
                 f"Channel: {ch} · Duration: **{format_duration(d.duration)}** · Check-in: **{format_duration(d.interval)}** · "
                 f"Winners: **{d.winners}** · Slots: **{len(d.rewards)}** · Images: **{n_img}/3**",
                 f"Multiple wins: **{'on' if d.multi else 'off'}** · Re-assign unclaimed: **{'on' if d.reassign else 'off'}** · "
                 f"Claim deadline: **{f'{d.claim_hours}h' if d.claim_hours else 'none'}**"]
        if notice:
            lines.append(notice)
        return "\n".join(lines)

    def _sync(self):
        d, g = self.draft, self.origin.guild
        T = discord.SelectDefaultValueType
        self.pick_channel.default_values = (
            [discord.SelectDefaultValue(id=self.channel_id, type=T.channel)] if self.channel_id else [])
        have = [discord.utils.get(g.roles, name=n) for n in d.roles]
        self.pick_roles.default_values = [discord.SelectDefaultValue(id=r.id, type=T.role) for r in have if r][:25]
        preset = next((k for k, v in INTERVAL_PRESETS.items() if v == d.interval), None)
        self.pick_interval.options = [
            discord.SelectOption(label=f"Every {format_duration(v)}", value=k, default=k == preset)
            for k, v in INTERVAL_PRESETS.items()] + [
            discord.SelectOption(label=("Custom: every " + format_duration(d.interval)) if preset is None else "✏️ Custom: type your own…",
                                 value="custom", default=preset is None,
                                 description="Any time from 10 minutes to 30 days" if preset is not None else "Pick again to change it")]
        self.b_multi.label = f"Multiple wins: {'on' if d.multi else 'off'}"
        self.b_multi.style = discord.ButtonStyle.primary if d.multi else discord.ButtonStyle.secondary
        self.b_reassign.label = f"Re-assign unclaimed: {'on' if d.reassign else 'off'}"
        self.b_reassign.style = discord.ButtonStyle.primary if d.reassign else discord.ButtonStyle.secondary

    async def _show_preview(self, view, files):
        if self.preview_msg is not None:
            try:
                return await self.preview_msg.edit(view=view, attachments=files)
            except discord.NotFound:             # the admin dismissed it: draw a new one
                self.preview_msg = None
        kw = {"files": files} if files else {}
        self.preview_msg = await self.origin.followup.send(view=view, ephemeral=True, wait=True, **kw)

    async def _draw_preview(self) -> bool:
        d = self.draft
        spec = live_spec(self._gw(), self._rewards(), participants=0, entries=0,
                         eligible_text=self.cog.eligible_text(self.origin.guild, d.roles),
                         urls=await self.cog.urls_for(self.ids()))
        await self._show_preview(CardView(spec, self.cog.preview_handlers, preview=True),
                                 await self.cog.files_for(self.ids()))
        return True

    async def _drop_preview(self):
        if self.preview_msg is not None:
            try:
                await self.preview_msg.delete()
            except discord.HTTPException:
                pass
            self.preview_msg = None

    async def render(self, notice: Optional[str] = None):
        self._sync()
        if not self._panel_sent:
            # The first followup after defer() BECOMES the original response. The preview (a Components V2 card, which
            # cannot carry `content`/a classic view) must therefore be a *second* message, so claim the original
            # response for the control panel first. Otherwise the panel's edit overwrote/failed on the preview and the
            # admin only ever saw a read-only card.
            try:
                await self.origin.edit_original_response(content=self.status_text(notice), view=self)
                self._panel_sent = True
            except discord.HTTPException:
                log.exception("Giveaway builder panel failed")
        try:
            await self._draw_preview()
        except discord.HTTPException:
            if self.draft.buttons:               # almost certainly a button emoji Discord doesn't accept
                self.draft.buttons = {}
                notice = ("⚠️ Discord rejected one of the button emojis, so the button names were reset. "
                          "Use a normal emoji or one from this server.")
                try:
                    await self._draw_preview()
                except discord.HTTPException:
                    log.exception("Giveaway preview failed")
            else:
                log.exception("Giveaway preview failed")
        try:
            await self.origin.edit_original_response(content=self.status_text(notice), view=self)
        except discord.HTTPException:
            log.exception("Giveaway builder render failed")

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        await self._drop_preview()
        try:
            await self.origin.edit_original_response(content="⏱️ Builder timed out. Run `/giveaway create` again.", view=self)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------- controls
    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text, discord.ChannelType.news],
                       placeholder="Announcement channel", row=0)
    async def pick_channel(self, i: discord.Interaction, sel: discord.ui.ChannelSelect):
        await i.response.defer()
        self.channel_id = sel.values[0].id if sel.values else None
        await self.render()

    @discord.ui.select(placeholder="Check-in interval: pick one or type your own", row=1,
                       options=[discord.SelectOption(label="placeholder", value="6h")])
    async def pick_interval(self, i: discord.Interaction, sel: discord.ui.Select):
        v = sel.values[0]
        if v == "custom":
            async def apply(seconds):
                self.draft.interval = seconds
                await self.render()
            return await i.response.send_modal(IntervalModal(self, self.draft.interval, apply))
        await i.response.defer()
        self.draft.interval = INTERVAL_PRESETS[v]
        await self.render()

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Roles allowed to enter (any ONE is enough)",
                       min_values=1, max_values=25, row=2)
    async def pick_roles(self, i: discord.Interaction, sel: discord.ui.RoleSelect):
        await i.response.defer()
        self.draft.roles = [r.name for r in sel.values if not r.is_default()] or self.draft.roles
        await self.render()

    @discord.ui.button(label="Text", emoji="📝", style=discord.ButtonStyle.primary, row=3)
    async def b_details(self, i: discord.Interaction, _):
        await i.response.send_modal(DetailsModal(self))

    @discord.ui.button(label="Settings", emoji="⚙️", style=discord.ButtonStyle.primary, row=3)
    async def b_settings(self, i: discord.Interaction, _):
        await i.response.send_modal(SettingsModal(self))

    @discord.ui.button(label="Rewards", emoji="🎁", style=discord.ButtonStyle.primary, row=3)
    async def b_rewards(self, i: discord.Interaction, _):
        await show_rewards(i, DraftRewards(self), first=True)

    @discord.ui.button(label="Images", emoji="🖼️", style=discord.ButtonStyle.secondary, row=3)
    async def b_images(self, i: discord.Interaction, _):
        async def apply(updates):
            d = self.draft
            d.banner_id = updates.get("top", d.banner_id)
            d.bottom_id = updates.get("bottom", d.bottom_id)
            d.thumb_id = updates.get("side", d.thumb_id)
            await self.render()
        await i.response.send_modal(ImagesModal(self, apply))

    @discord.ui.button(label="Style", emoji="🎨", style=discord.ButtonStyle.secondary, row=3)
    async def b_style(self, i: discord.Interaction, _):
        async def apply(accent, buttons):
            self.draft.accent, self.draft.buttons = accent, buttons
            await self.render()
        await i.response.send_modal(StyleModal(self, self.draft.accent, self.draft.buttons, apply))

    @discord.ui.button(label="Multiple wins", style=discord.ButtonStyle.secondary, row=4)
    async def b_multi(self, i: discord.Interaction, _):
        await i.response.defer()
        self.draft.multi = not self.draft.multi
        await self.render()

    @discord.ui.button(label="Re-assign unclaimed", style=discord.ButtonStyle.secondary, row=4)
    async def b_reassign(self, i: discord.Interaction, _):
        await i.response.defer()
        self.draft.reassign = not self.draft.reassign
        await self.render("ℹ️ Re-assigning needs a **claim deadline** (Settings)." if self.draft.reassign
                          and not self.draft.claim_hours else None)

    @discord.ui.button(label="Publish", emoji="🚀", style=discord.ButtonStyle.success, row=4)
    async def b_publish(self, i: discord.Interaction, _):
        await i.response.defer()
        if self._publishing:
            return
        problems = self.problems()
        if problems:
            return await self.render("⚠️ Can't publish yet: " + "; ".join(problems) + ".")
        self._publishing = True
        try:
            msg, gid = await self.cog.publish(self.draft, i.user, self.channel_id)
        except vault.VaultError as e:
            self._publishing = False
            return await self.render(f"⚠️ {e}")
        except discord.HTTPException as e:
            self._publishing = False
            return await self.render(f"⚠️ Discord refused the post: {e.text or e}")
        except Exception:
            self._publishing = False
            log.exception("Publishing a giveaway failed")
            return await self.render("⚠️ Something went wrong while publishing. Nothing was posted.")
        self.stop()
        await self._drop_preview()
        await self.origin.edit_original_response(
            content=f"✅ Giveaway **#{gid}** is live: {msg.jump_url}\nManage it with `/giveaway manage`.", view=None)

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.danger, row=4)
    async def b_cancel(self, i: discord.Interaction, _):
        await i.response.defer()
        self.stop()
        await self._drop_preview()
        await self.origin.edit_original_response(content="Cancelled. Nothing was posted.", view=None)

    @discord.ui.button(label="Placeholders", emoji="ℹ️", style=discord.ButtonStyle.secondary, row=4)
    async def b_help(self, i: discord.Interaction, _):
        await i.response.send_message(help_text(), ephemeral=True)


# ============================================================ small helper views
class Pager(discord.ui.View):
    def __init__(self, owner_id: int, title: str, lines: list[str], per: int = 15, footer: str = ""):
        super().__init__(timeout=300)
        self.owner_id, self.title, self.lines, self.per, self.footer, self.page = owner_id, title, lines, per, footer, 0
        self.pages = max(1, -(-len(lines) // per))
        self._style()

    def _style(self):
        self.b_prev.disabled, self.b_next.disabled = self.page <= 0, self.page >= self.pages - 1

    def embed(self) -> discord.Embed:
        chunk = self.lines[self.page * self.per:(self.page + 1) * self.per]
        e = discord.Embed(title=self.title, description="\n".join(chunk) or "*Nobody yet.*", color=config.COLOR_MAIN)
        e.set_footer(text=f"Page {self.page + 1}/{self.pages}" + (f" · {self.footer}" if self.footer else ""))
        return e

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.owner_id

    @discord.ui.button(label="◀ Prev")
    async def b_prev(self, i: discord.Interaction, _):
        self.page -= 1
        self._style()
        await i.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next ▶")
    async def b_next(self, i: discord.Interaction, _):
        self.page += 1
        self._style()
        await i.response.edit_message(embed=self.embed(), view=self)


class RolesView(discord.ui.View):
    """Pick the roles that may enter (this giveaway) or the Giveaway Winner role (server-wide)."""

    def __init__(self, panel: "ManagePanel", mode: str):
        super().__init__(timeout=300)
        self.panel, self.mode = panel, mode
        g, T = panel.origin.guild, discord.SelectDefaultValueType
        if mode == "eligible":
            have = [discord.utils.get(g.roles, name=n) for n in panel.gw["eligible_roles"]]
            sel = discord.ui.RoleSelect(placeholder="Roles allowed to enter (any ONE is enough)", min_values=1,
                                        max_values=25, default_values=[discord.SelectDefaultValue(id=r.id, type=T.role)
                                                                       for r in have if r][:25])
        else:
            cur = g.get_role(panel.bot.db.get("giveaway_winner_role_id", 0))
            sel = discord.ui.RoleSelect(placeholder="Role given to every giveaway winner", min_values=1, max_values=1,
                                        default_values=[discord.SelectDefaultValue(id=cur.id, type=T.role)] if cur else [])
            reset = discord.ui.Button(label=f"Use “{config.GIVEAWAY_WINNER_ROLE_NAME}” (create it if missing)", row=1)
            reset.callback = self._reset
            self.add_item(reset)
        sel.callback = self._picked
        self.sel = sel
        self.add_item(sel)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.panel.author_id

    async def _picked(self, i: discord.Interaction):
        p, db = self.panel, self.panel.bot.db
        roles = [r for r in self.sel.values if not r.is_default() and not r.managed]
        if not roles:
            return await i.response.edit_message(content="⚠️ Pick a normal role (not @everyone or a bot role).", view=self)
        if self.mode == "eligible":
            await db.gw_update(p.gid, eligible_roles=[r.name for r in roles])
            await p.cog.audit(p.gid, p.author_id, "eligibility_update", roles=[r.name for r in roles])
            msg = "✅ Eligible roles updated: " + ", ".join(r.mention for r in roles)
        else:
            await db.set("giveaway_winner_role_id", roles[0].id)
            await p.cog.audit(p.gid, p.author_id, "winner_role_update", role=roles[0].name)
            msg = f"✅ Winners will now receive {roles[0].mention}."
        self.stop()
        await i.response.edit_message(content=msg, view=None)
        await p.after_change()

    async def _reset(self, i: discord.Interaction):
        p = self.panel
        await i.response.defer()
        role = await p.cog.winner_role(p.origin.guild, reset=True)
        self.stop()
        if role is None:
            return await i.edit_original_response(content="⚠️ I couldn't create the role. I need **Manage Roles**.", view=None)
        await p.cog.audit(p.gid, p.author_id, "winner_role_update", role=role.name)
        await i.edit_original_response(content=f"✅ Winners will now receive {role.mention}.", view=None)
        await p.after_change()


class WinnersView(discord.ui.View):
    """Winners overview + manual delivery / re-opening a claim / retrying roles."""

    def __init__(self, panel: "ManagePanel", rewards: list):
        super().__init__(timeout=300)
        self.panel = panel
        deliver = [r for r in rewards if r["winner_id"] and r["status"] in ("assigned", "claimed")]
        reopen = [r for r in rewards if r["status"] == "claimed"]
        for placeholder, rows, cb in (("📦 Mark a reward as delivered", deliver, self._deliver),
                                      ("🔓 Re-open a claim (winner lost their code)", reopen, self._reopen)):
            if rows:
                s = discord.ui.Select(placeholder=placeholder, options=[
                    discord.SelectOption(label=f"{ordinal(r['position'])} · {r['name']}"[:100], value=str(r["id"]))
                    for r in rows[:25]])
                s.callback = cb
                self.add_item(s)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.panel.author_id

    async def _deliver(self, i: discord.Interaction):
        p, rid = self.panel, int(i.data["values"][0])
        row = await p.bot.db.reward_mark_delivered(rid, i.user.id, utcnow())
        if row is None:
            return await i.response.send_message("⚠️ That reward can't be marked delivered (already delivered?).", ephemeral=True)
        await p.cog.audit(p.gid, i.user.id, "mark_delivered", reward_id=rid, position=row["position"], winner=row["winner_id"])
        await i.response.send_message(f"✅ {ordinal(row['position'])} place marked as **delivered** to <@{row['winner_id']}>.",
                                      ephemeral=True)
        await p.after_change()

    async def _reopen(self, i: discord.Interaction):
        p, rid = self.panel, int(i.data["values"][0])
        row = await p.bot.db.reward_get(rid)
        if row is None or row["status"] != "claimed":
            return await i.response.send_message("⚠️ That reward isn't in the 'claimed' state.", ephemeral=True)
        await p.bot.db.reward_unclaim(rid)
        await p.cog.audit(p.gid, i.user.id, "reopen_claim", reward_id=rid, position=row["position"], winner=row["winner_id"])
        await i.response.send_message(f"✅ <@{row['winner_id']}> can press **CLAIM REWARD** again to see their "
                                      f"{ordinal(row['position'])}-place reward.", ephemeral=True)

    @discord.ui.button(label="Retry winner roles", emoji="🏆", style=discord.ButtonStyle.secondary, row=4)
    async def b_roles(self, i: discord.Interaction, _):
        await i.response.defer(ephemeral=True)
        p = self.panel
        await p.bot.db.rewards_reset_role_attempts(p.gid)
        await p.cog.assign_roles(p.gid, force=True)
        left = await p.bot.db.rewards_needing_role(1000, p.gid)
        await i.followup.send("✅ Every winner has the role now." if not left else
                              f"⚠️ {len({r['winner_id'] for r in left})} winner(s) still don't have it. "
                              "Check that my role is above the winner role and that I have **Manage Roles**.", ephemeral=True)


class RerollView(discord.ui.View):
    def __init__(self, panel: "ManagePanel", rows: list):
        super().__init__(timeout=300)
        self.panel, self.choice = panel, None
        sel = discord.ui.Select(placeholder="Which winner should be replaced?", options=[
            discord.SelectOption(label=f"{ordinal(r['position'])} · {r['name']}"[:100], value=str(r["id"]),
                                 description=f"currently {panel.cog.name_of(panel.origin.guild, r['winner_id'])}"[:100])
            for r in rows[:25]])
        sel.callback = self._picked
        self.add_item(sel)
        self.b_go.disabled = True

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.panel.author_id

    async def _picked(self, i: discord.Interaction):
        self.choice = int(i.data["values"][0])
        self.b_go.disabled = False
        await i.response.edit_message(content="Ready. The replaced winner is never re-picked.", view=self)

    @discord.ui.button(label="Reroll this winner", emoji="🔁", style=discord.ButtonStyle.danger)
    async def b_go(self, i: discord.Interaction, _):
        await i.response.defer(ephemeral=True)
        self.stop()
        ok, text = await self.panel.cog.reroll(self.panel.gid, self.choice, i.user.id)
        await i.edit_original_response(content=("✅ " if ok else "⚠️ ") + text, view=None)
        await self.panel.after_change()


class PickView(discord.ui.View):
    """`/giveaway manage` without an argument: choose one of the recent giveaways."""

    def __init__(self, cog: "Giveaways", origin: discord.Interaction, rows: list):
        super().__init__(timeout=300)
        self.cog, self.origin = cog, origin
        sel = discord.ui.Select(placeholder="Choose a giveaway", options=[
            discord.SelectOption(label=f"#{r['id']} · {r['title']}"[:100], value=str(r["id"]),
                                 description=f"{r['status']} · {r['winners_count']} winner(s)")
            for r in rows[:25]])
        sel.callback = self._picked
        self.add_item(sel)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return i.user.id == self.origin.user.id

    async def _picked(self, i: discord.Interaction):
        self.stop()
        await i.response.edit_message(content="Opening …", view=None)
        await ManagePanel(self.cog, i, int(i.data["values"][0])).render()


# ============================================================== manage panel
class EditModal(discord.ui.Modal, title="Edit giveaway"):
    def __init__(self, panel: "ManagePanel"):
        super().__init__()
        self.p, gw = panel, panel.gw
        self.t = discord.ui.TextInput(label="Title", default=gw["title"], max_length=100)
        self.body = discord.ui.TextInput(label="Card text ({placeholders} work)", style=discord.TextStyle.paragraph,
                                         default=resolve_body(gw)[:BODY_MAX], required=False, max_length=BODY_MAX)
        self.pool = discord.ui.TextInput(label="Prize pool", default=gw["prize_pool"], required=False, max_length=100)
        self.interval = discord.ui.TextInput(label="Check-in interval", default=format_duration(gw["interval_seconds"]),
                                             max_length=30)
        for item in (self.t, self.body, self.pool, self.interval):
            self.add_item(item)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        p = self.p
        try:
            interval = parse_duration(self.interval.value, MIN_INTERVAL, MAX_INTERVAL)
        except DurationError as e:
            return await p.render(f"⚠️ Check-in interval: {e}")
        if not self.t.value.strip():
            return await p.render("⚠️ The title can't be empty.")
        old = p.gw
        fields = {"title": self.t.value.strip(), "body": self.body.value.strip() or DEFAULT_BODY,
                  "prize_pool": self.pool.value.strip(), "interval_seconds": interval}
        changed = [k for k, v in fields.items() if (old[k] if k != "body" else resolve_body(old)) != v]
        await p.bot.db.gw_update(p.gid, **fields)
        await p.cog.audit(p.gid, p.author_id, "edit", changed=changed)
        await p.after_change(notice="✅ Giveaway updated.")


class ExtendModal(discord.ui.Modal, title="Extend the timer"):
    def __init__(self, panel: "ManagePanel"):
        super().__init__()
        self.p = panel
        self.val = discord.ui.TextInput(label="Add how much time?", max_length=30, placeholder="12h, 1d, 90m …")
        self.add_item(self.val)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        p = self.p
        try:
            secs = parse_duration(self.val.value, 60, MAX_DURATION)
        except DurationError as e:
            return await p.render(f"⚠️ {e}")
        new_end = await p.bot.db.gw_extend(p.gid, secs, utcnow())
        if new_end is None:
            return await p.render("⚠️ This giveaway isn't running any more, so it can't be extended.")
        await p.cog.audit(p.gid, p.author_id, "extend", added=format_duration(secs), new_end=new_end.isoformat())
        await p.after_change(notice=f"✅ Extended by **{format_duration(secs)}**. New end: <t:{int(new_end.timestamp())}:f>")


class ClaimRulesModal(discord.ui.Modal, title="Claim & win rules"):
    def __init__(self, panel: "ManagePanel"):
        super().__init__()
        self.p, gw = panel, panel.gw
        self.hours = discord.ui.TextInput(label="Claim deadline, hours (blank = none)", required=False, max_length=4,
                                          default=str(gw["claim_hours"]) if gw["claim_hours"] else "")
        self.reassign = discord.ui.TextInput(label="Re-assign unclaimed prizes? (yes / no)", max_length=3,
                                             default="yes" if gw["auto_reassign"] else "no")
        self.multi = discord.ui.TextInput(label="One person may win several slots? (yes/no)", max_length=3,
                                          default="yes" if gw["allow_multiple_wins"] else "no")
        for item in (self.hours, self.reassign, self.multi):
            self.add_item(item)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        p, h = self.p, self.hours.value.strip()
        if h and not (h.isdigit() and 1 <= int(h) <= 2160):
            return await p.render("⚠️ Claim deadline must be 1 to 2160 hours, or blank for none.")
        fields = {"claim_hours": int(h) if h and h != "0" else None, "auto_reassign": _yes(self.reassign.value)}
        notes = []
        if p.gw["status"] == "active":
            fields["allow_multiple_wins"] = _yes(self.multi.value)
        elif _yes(self.multi.value) != p.gw["allow_multiple_wins"]:
            notes.append("“Several slots per person” can't change after the draw.")
        if fields["auto_reassign"] and not fields["claim_hours"]:
            notes.append("Re-assigning only works with a claim deadline.")
        await p.bot.db.gw_update(p.gid, **fields)
        await p.cog.audit(p.gid, p.author_id, "claim_rules", **{k: v for k, v in fields.items()})
        await p.after_change(notice="✅ Rules saved. " + " ".join(f"⚠️ {n}" for n in notes))


class ManagePanel(discord.ui.View):
    """Everything an admin can do with a published giveaway. Every action lands in the audit log."""

    def __init__(self, cog: "Giveaways", origin: discord.Interaction, gid: int):
        super().__init__(timeout=840)
        self.cog, self.bot, self.origin, self.gid = cog, cog.bot, origin, gid
        self.author_id = origin.user.id
        self.gw = None
        self._armed: Optional[str] = None

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.author_id:
            await i.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return False
        if (i.data or {}).get("custom_id") not in ("gwm:end", "gwm:cancel"):
            self._armed = None
        return True

    # -------------------------------------------------------------- render
    async def after_change(self, notice: Optional[str] = None):
        await self.cog.refresh_message(self.gid)
        await self.render(notice)

    async def render(self, notice: Optional[str] = None):
        db, g = self.bot.db, self.origin.guild
        self.gw = gw = await db.gw_get(self.gid)
        if gw is None:
            self.stop()
            return await self.origin.edit_original_response(content="That giveaway no longer exists.", embed=None, view=None)
        rewards = await db.gw_rewards(self.gid)
        people, entries = await db.gw_stats(self.gid)
        status, active, ended = gw["status"], gw["status"] == "active", gw["status"] == "ended"
        ts = int(gw["ends_at"].timestamp())
        e = discord.Embed(title=f"🎛️ #{gw['id']} · {gw['title']}"[:256], color=config.COLOR_ACCENT,
                          url=_jump(g.id, gw) if gw["message_id"] else None)
        e.add_field(name="Status", value={"active": f"🟢 Live · ends <t:{ts}:R>", "ended": f"🏁 Ended <t:{ts}:R>",
                                          "cancelled": "🛑 Cancelled"}[status], inline=True)
        e.add_field(name="Participants", value=f"**{people:,}** · {entries:,} entries", inline=True)
        e.add_field(name="Check-in", value=f"every {format_duration(gw['interval_seconds'])}", inline=True)
        if ended:
            n = {s: sum(1 for r in rewards if r["status"] == s) for s in ("assigned", "claimed", "delivered")}
            e.add_field(name="Rewards", value=f"{n['assigned']} unclaimed · {n['claimed']} claimed · {n['delivered']} delivered",
                        inline=True)
            if not gw["results_posted"]:
                e.add_field(name="⚠️ Results", value="Not announced yet. I'll keep retrying.", inline=True)
        else:
            e.add_field(name="Slots", value=f"**{gw['winners_count']}** winner(s)", inline=True)
        e.add_field(name="Eligible roles", value=self.cog.eligible_text(g, gw["eligible_roles"]) or "*none*", inline=False)
        wr = g.get_role(self.bot.db.get("giveaway_winner_role_id", 0))
        e.add_field(name="Winner role", value=wr.mention if wr else f"*{config.GIVEAWAY_WINNER_ROLE_NAME} (created on the first draw)*",
                    inline=True)
        e.add_field(name="Rules", value=(f"Claim deadline: **{str(gw['claim_hours']) + 'h' if gw['claim_hours'] else 'none'}** · "
                                         f"Re-assign: **{'on' if gw['auto_reassign'] else 'off'}** · "
                                         f"Multiple wins: **{'on' if gw['allow_multiple_wins'] else 'off'}**"), inline=True)
        if notice:
            e.description = notice
        for b, on in ((self.b_extend, active), (self.b_end, active), (self.b_cancel, active), (self.b_elig, active),
                      (self.b_winners, ended), (self.b_reroll, ended)):
            b.disabled = not on
        self.b_end.label = "Confirm: end & draw now" if self._armed == "end" else "End early"
        self.b_end.style = discord.ButtonStyle.danger if self._armed == "end" else discord.ButtonStyle.secondary
        self.b_cancel.label = "Confirm: cancel giveaway" if self._armed == "cancel" else "Cancel"
        self.b_cancel.style = discord.ButtonStyle.danger if self._armed == "cancel" else discord.ButtonStyle.secondary
        try:
            await self.origin.edit_original_response(content=None, embed=e, view=self)
        except discord.HTTPException:
            log.exception("Manage panel render failed")

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        try:
            await self.origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass

    # ---------------------------------------------------------------- row 0
    @discord.ui.button(label="Edit", emoji="✏️", style=discord.ButtonStyle.primary, row=0)
    async def b_edit(self, i: discord.Interaction, _):
        await i.response.send_modal(EditModal(self))

    @discord.ui.button(label="Images", emoji="🖼️", style=discord.ButtonStyle.secondary, row=0)
    async def b_banner(self, i: discord.Interaction, _):
        cols = {"top": "banner_id", "bottom": "bottom_banner_id", "side": "thumb_id"}

        async def apply(updates):
            await self.bot.db.gw_update(self.gid, **{cols[k]: v for k, v in updates.items()})
            await self.cog.audit(self.gid, self.author_id, "images_update", slots=sorted(updates),
                                 cleared=sorted(k for k, v in updates.items() if v is None))
            await self.cog.refresh_message(self.gid, reupload=True)
            await self.cog.refresh_results(self.gid, reupload=True)
            await self.render("✅ Images updated. (To upload a file instead of a link, use `/giveaway images`.)")
        await i.response.send_modal(ImagesModal(self, apply))

    @discord.ui.button(label="Extend timer", emoji="⏱️", style=discord.ButtonStyle.secondary, row=0)
    async def b_extend(self, i: discord.Interaction, _):
        await i.response.send_modal(ExtendModal(self))

    @discord.ui.button(label="End early", emoji="🏁", style=discord.ButtonStyle.secondary, custom_id="gwm:end", row=0)
    async def b_end(self, i: discord.Interaction, _):
        await i.response.defer()
        if self._armed != "end":
            self._armed = "end"
            return await self.render("⚠️ This closes entries and **draws the winners right now**. Press the red button to confirm.")
        self._armed = None
        done = await self.cog.end_giveaway(self.gid, self.author_id, early=True)
        await self.render("🏁 Giveaway ended. Winners drawn and announced." if done else "⚠️ It had already ended.")

    @discord.ui.button(label="Cancel", emoji="🛑", style=discord.ButtonStyle.secondary, custom_id="gwm:cancel", row=0)
    async def b_cancel(self, i: discord.Interaction, _):
        await i.response.defer()
        if self._armed != "cancel":
            self._armed = "cancel"
            return await self.render("⚠️ This cancels the giveaway **without drawing anyone**. Press the red button to confirm.")
        self._armed = None
        done = await self.cog.cancel_giveaway(self.gid, self.author_id)
        await self.render("🛑 Giveaway cancelled." if done else "⚠️ It isn't running any more.")

    # ---------------------------------------------------------------- row 1
    @discord.ui.button(label="Participants", emoji="👥", style=discord.ButtonStyle.secondary, row=1)
    async def b_people(self, i: discord.Interaction, _):
        await i.response.defer(ephemeral=True)
        rows = await self.bot.db.gw_participants(self.gid)
        lines = [f"`{n}.` <@{r['user_id']}> · joined <t:{int(r['joined_at'].timestamp())}:R>"
                 for n, r in enumerate(sorted(rows, key=lambda r: r["joined_at"]), 1)]
        pager = Pager(i.user.id, f"👥 Participants ({len(rows)})", lines)
        await i.followup.send(embed=pager.embed(), view=pager, ephemeral=True)

    @discord.ui.button(label="Entries", emoji="🎟️", style=discord.ButtonStyle.secondary, row=1)
    async def b_entries(self, i: discord.Interaction, _):
        await i.response.defer(ephemeral=True)
        rows = await self.bot.db.gw_participants(self.gid)
        total = sum(r["entries"] for r in rows) or 1
        lines = [f"`#{n}` <@{r['user_id']}> — **{r['entries']}** entr{'y' if r['entries'] == 1 else 'ies'} "
                 f"({r['entries'] / total:.1%} of the pool)" for n, r in enumerate(rows, 1)]
        pager = Pager(i.user.id, f"🎟️ Entries ({total if rows else 0} total)", lines)
        await i.followup.send(embed=pager.embed(), view=pager, ephemeral=True)

    @discord.ui.button(label="Winners", emoji="🏆", style=discord.ButtonStyle.secondary, row=1)
    async def b_winners(self, i: discord.Interaction, _):
        await i.response.defer(ephemeral=True)
        rewards = await self.bot.db.gw_rewards(self.gid)
        lines = []
        for r in rewards:
            who = f"<@{r['winner_id']}>" if r["winner_id"] else "*no winner*"
            state = {"open": "", "assigned": " · ⏳ unclaimed", "claimed": f" · ✅ claimed <t:{int(r['claimed_at'].timestamp())}:R>"
                     if r["claimed_at"] else " · ✅ claimed",
                     "delivered": " · 📦 delivered"}[r["status"]]
            if r["status"] == "assigned" and r["claim_deadline_at"]:
                state += f" (deadline <t:{int(r['claim_deadline_at'].timestamp())}:R>)"
            lines.append(f"{place_label(r['position'])}: {who} — **{r['name']}**{state}")
        e = discord.Embed(title="🏆 Winners", description="\n".join(lines)[:4000] or "*No winners.*", color=config.COLOR_ACCENT)
        await i.followup.send(embed=e, view=WinnersView(self, rewards), ephemeral=True)

    @discord.ui.button(label="Audit log", emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def b_audit(self, i: discord.Interaction, _):
        await i.response.defer(ephemeral=True)
        rows = await self.bot.db.audit_list(self.gid, 40)
        lines = []
        for r in rows:
            who = f"<@{r['actor_id']}>" if r["actor_id"] else "bot"
            det = ", ".join(f"{k}={v}" for k, v in (r["details"] or {}).items())[:120]
            lines.append(f"<t:{int(r['created_at'].timestamp())}:d> <t:{int(r['created_at'].timestamp())}:T> · "
                         f"**{r['action']}** · {who}" + (f" · `{det}`" if det else ""))
        pager = Pager(i.user.id, "📜 Audit log (newest first)", lines, per=8)
        await i.followup.send(embed=pager.embed(), view=pager, ephemeral=True)

    @discord.ui.button(label="Export", emoji="📤", style=discord.ButtonStyle.secondary, row=1)
    async def b_export(self, i: discord.Interaction, _):
        await i.response.defer(ephemeral=True)
        db, g = self.bot.db, self.origin.guild
        part, ledger, rewards = await db.gw_participants(self.gid), await db.gw_ledger(self.gid), await db.gw_rewards(self.gid)
        draws = await db.pool.fetch("SELECT kind, record, picks, created_by, created_at FROM giveaway_draws "
                                    "WHERE giveaway_id=$1 ORDER BY id", self.gid)
        name = lambda uid: (g.get_member(uid).display_name if g.get_member(uid) else "")          # noqa: E731
        iso = lambda d: d.isoformat() if d else ""                                                # noqa: E731
        files = [
            discord.File(io.BytesIO(csv_bytes(
                ["user_id", "display_name", "entries", "joined_at", "last_entry_at"],
                [[r["user_id"], name(r["user_id"]), r["entries"], iso(r["joined_at"]), iso(r["last_entry_at"])] for r in part])),
                f"giveaway-{self.gid}-participants.csv"),
            discord.File(io.BytesIO(csv_bytes(
                ["entry_id", "user_id", "kind", "created_at"],
                [[r["id"], r["user_id"], r["kind"], iso(r["created_at"])] for r in ledger])),
                f"giveaway-{self.gid}-entry-ledger.csv"),
            discord.File(io.BytesIO(csv_bytes(
                ["position", "reward", "value", "winner_id", "winner_name", "status", "claimed_at", "delivered_at"],
                [[r["position"], r["name"], r["value"], r["winner_id"] or "", name(r["winner_id"]) if r["winner_id"] else "",
                  r["status"], iso(r["claimed_at"]), iso(r["delivered_at"])] for r in rewards])),
                f"giveaway-{self.gid}-winners.csv"),
        ]
        if draws:
            blob = json.dumps([{"kind": d["kind"], "at": iso(d["created_at"]), "by": d["created_by"],
                                "record": d["record"], "picks": d["picks"]} for d in draws], indent=2, default=str)
            files.append(discord.File(io.BytesIO(blob.encode()), f"giveaway-{self.gid}-draw-record.json"))
        await self.cog.audit(self.gid, i.user.id, "export", participants=len(part), ledger_rows=len(ledger))
        await i.followup.send("📤 Participant, entry-ledger and winner records (reward codes are never exported).",
                              files=files, ephemeral=True)

    # ---------------------------------------------------------------- row 2
    @discord.ui.button(label="Rewards", emoji="🎁", style=discord.ButtonStyle.secondary, row=2)
    async def b_rewards(self, i: discord.Interaction, _):
        await show_rewards(i, LiveRewards(self), first=True)

    @discord.ui.button(label="Reroll winner", emoji="🔁", style=discord.ButtonStyle.secondary, row=2)
    async def b_reroll(self, i: discord.Interaction, _):
        rows = [r for r in await self.bot.db.gw_rewards(self.gid) if r["winner_id"] and r["status"] == "assigned"]
        if not rows:
            return await i.response.send_message("Nothing to reroll: every prize is already claimed or delivered, "
                                                 "or there are no winners.", ephemeral=True)
        await i.response.send_message("Pick the winner to replace (only **unclaimed** prizes can be rerolled):",
                                      view=RerollView(self, rows), ephemeral=True)

    @discord.ui.button(label="Eligibility roles", emoji="🎯", style=discord.ButtonStyle.secondary, row=2)
    async def b_elig(self, i: discord.Interaction, _):
        await i.response.send_message("Who may enter this giveaway?", view=RolesView(self, "eligible"), ephemeral=True)

    @discord.ui.button(label="Winner role", emoji="👑", style=discord.ButtonStyle.secondary, row=2)
    async def b_wrole(self, i: discord.Interaction, _):
        await i.response.send_message("Which role do winners get? (this applies to all giveaways)",
                                      view=RolesView(self, "winner"), ephemeral=True)

    @discord.ui.button(label="Claim rules", emoji="⚙️", style=discord.ButtonStyle.secondary, row=2)
    async def b_rules(self, i: discord.Interaction, _):
        await i.response.send_modal(ClaimRulesModal(self))

    # ---------------------------------------------------------------- row 3
    @discord.ui.button(label="Style", emoji="🎨", style=discord.ButtonStyle.secondary, row=3)
    async def b_style(self, i: discord.Interaction, _):
        async def apply(accent, buttons):
            db, old = self.bot.db, (self.gw["accent"], dict(self.gw["buttons"] or {}))
            await db.gw_update(self.gid, accent=accent, buttons=buttons)
            if not await self.cog.refresh_message(self.gid):          # Discord refused (bad button emoji?): undo
                await db.gw_update(self.gid, accent=old[0], buttons=old[1])
                await self.cog.refresh_message(self.gid)
                return await self.render("⚠️ Discord rejected that style (probably a button emoji), so nothing changed. "
                                         "Use a normal emoji or one from this server.")
            await self.cog.audit(self.gid, self.author_id, "style_update", accent=accent, buttons=buttons)
            await self.cog.refresh_results(self.gid)
            await self.render("✅ Style updated.")
        await i.response.send_modal(StyleModal(self, self.gw["accent"], dict(self.gw["buttons"] or {}), apply))

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, row=3)
    async def b_refresh(self, i: discord.Interaction, _):
        await i.response.defer()
        await self.cog.refresh_message(self.gid)
        await self.render()

    @discord.ui.button(label="Close", emoji="✖️", style=discord.ButtonStyle.secondary, row=3)
    async def b_close(self, i: discord.Interaction, _):
        await i.response.defer()
        self.stop()
        await self.origin.edit_original_response(content="Panel closed.", embed=None, view=None)
