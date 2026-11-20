"""/admin: one dashboard (buttons, dropdowns, modals) to configure Orion without touching code.

Sections: Overview · Giveaways · Polls · Tickets · Ticket banner · Announcements · XP & Levelling · Features · Welcome ·
Welcome DM · Channels · Staff access · Experimental feature
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import ai, config
from ..checks import is_admin, is_staff, require_staff, staff_role_ids
from ..db import RULE_DEFAULTS
from ..levels import MAX_LEVEL, total_xp_for_level
from ..ui import refresh_panel
from ..images import DM_BANNER
from .announce import Builder as AnnounceBuilder, Draft as AnnounceDraft
from .welcome import DEFAULT_DM, DM_POSITIONS, TextModal, find_welcome_channel
from ..ticket_ui import BannerLinkModal, PanelTextModal, SettingsModal as TicketSettingsModal
from ..tickets import (AI_HELPER_KEY, MAX_PING_ROLES, PRIORITIES, server_ping_role_ids, SETTINGS as TICKET_SETTINGS, panel_embed as ticket_panel_embed,
                       setting as ticket_setting, ticket_embed)

log = logging.getLogger("orion.dashboard")

PAGES = {"overview": "📋 Overview", "giveaways": "🎁 Giveaways", "polls": "📊 Polls", "tickets": "🎫 Tickets",
         "ticketbanner": "🖼️ Ticket banner", "announce": "📣 Announcements",
         "xp": "⚡ XP & Levelling", "features": "🎛️ Features",
         "welcome": "👋 Welcome", "dm": "💌 Welcome DM", "channels": "📍 Channels", "staff": "🛡️ Staff access",
         "experimental": "🧪 Experimental feature"}

FEATURES = [
    ("welcome_enabled", "Welcome message + card"),
    ("dm_enabled", "Welcome DM"),
    ("leave_enabled", "Leave message"),
    ("autorole_enabled", "Auto role on join (Earth King, or earned rank)"),
    ("xp_messages_enabled", "XP from messages"),
    ("xp_voice_enabled", "XP from voice"),
    ("levelup_announce_enabled", "Level-up announcements"),
    ("music_enabled", "Music commands (/play, /queue, ...)"),
    ("tickets_enabled", "Tickets (members can open new ones)"),
    ("ticket_dm_enabled", "Ticket DM on close (transcript + rating)"),
]

MSG_SPECS = [("msg_min", "Min XP per message", 1, 1000), ("msg_max", "Max XP per message", 1, 2000),
             ("cooldown", "Cooldown seconds", 0, 3600), ("daily_bonus", "Daily bonus XP", 0, 100000),
             ("min_chars", "Min letters/digits", 1, 50)]
VOICE_SPECS = [("voice_per_10min", "Voice XP per 10 minutes", 0, 10000), ("voice_cap", "Daily voice XP cap", 0, 100000)]


def months_to_max(daily_xp: float) -> str:
    if daily_xp <= 0:
        return "never"
    return f"~{total_xp_for_level(MAX_LEVEL) / daily_xp / 30:.1f} months"


class RulesModal(discord.ui.Modal):
    def __init__(self, dash: "Dashboard", title: str, specs):
        super().__init__(title=title)
        self.dash, self.specs, self.inputs = dash, specs, {}
        for key, label, lo, hi in specs:
            ti = discord.ui.TextInput(label=f"{label} ({lo}-{hi})", default=str(dash.bot.db.rule(key)), max_length=8)
            self.inputs[key] = ti
            self.add_item(ti)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        vals = {}
        for key, label, lo, hi in self.specs:
            try:
                v = int(self.inputs[key].value.replace(",", "").strip())
                if not lo <= v <= hi:
                    raise ValueError
            except ValueError:
                return await self.dash.render(f"⚠️ **{label}** must be a whole number from {lo} to {hi}. Nothing was saved.")
            vals[key] = v
        db = self.dash.bot.db
        if vals.get("msg_max", db.rule("msg_max")) < vals.get("msg_min", db.rule("msg_min")):
            return await self.dash.render("⚠️ Max XP can't be lower than min XP. Nothing was saved.")
        for k, v in vals.items():
            await db.set("rule_" + k, v)
        await refresh_panel(self.dash.bot)
        await self.dash.render("✅ Saved. The rank panel was updated too.")


class DMModal(discord.ui.Modal, title="Welcome DM content"):
    def __init__(self, dash: "Dashboard"):
        super().__init__()
        self.dash, db = dash, dash.bot.db
        col = db.get("dm_color")
        self.t = discord.ui.TextInput(label="Title (optional, {user} {server} {count})", required=False,
                                      default=db.get("dm_title", ""), max_length=256)
        self.msg = discord.ui.TextInput(label="Message ({mention} {user} {server} {count})",
                                        style=discord.TextStyle.paragraph, required=False,
                                        default=db.get("dm_text", DEFAULT_DM), max_length=2000)
        self.foot = discord.ui.TextInput(label="Footer (optional)", required=False,
                                         default=db.get("dm_footer", ""), max_length=200)
        self.col = discord.ui.TextInput(label="Colour (hex, or 'default')", required=False, max_length=7,
                                        placeholder="#7C3AED",
                                        default=f"#{col:06X}" if isinstance(col, int) else "")
        for i in (self.t, self.msg, self.foot, self.col):
            self.add_item(i)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer()
        db, notice = self.dash.bot.db, "✅ Welcome DM saved."
        raw = self.col.value.strip()
        color = None
        if raw.lower() in ("", "default", "reset"):
            color = False                                   # no colour typed: use the default purple
        else:
            try:
                color = int(raw.lstrip("#"), 16)
                if not 0 <= color <= 0xFFFFFF:
                    raise ValueError
            except ValueError:
                color, notice = False, "⚠️ That colour isn't valid hex (example: #7C3AED). Everything else was saved."
        await db.set("dm_title", self.t.value.strip())
        await db.set("dm_text", self.msg.value.strip())
        await db.set("dm_footer", self.foot.value.strip())
        await db.set("dm_color", "" if color is False else color)
        await self.dash.render(notice)


class Dashboard(discord.ui.View):
    def __init__(self, bot, origin: discord.Interaction, page: str = "overview"):
        super().__init__(timeout=840)
        self.bot, self.origin, self.page = bot, origin, page
        self.author_id = origin.user.id
        self.notice = None
        self.data: dict = {}

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.author_id:
            await i.response.send_message("This dashboard belongs to someone else. Run `/admin` for your own.",
                                          ephemeral=True)
            return False
        return True

    # ------------------------------------------------------------- helpers
    @property
    def db(self):
        return self.bot.db

    def _btn(self, label, cb, style=discord.ButtonStyle.secondary, row=1, emoji=None):
        b = discord.ui.Button(label=label, style=style, row=row, emoji=emoji)
        b.callback = cb
        self.add_item(b)

    def _chan_default(self, ids):
        g = self.origin.guild
        return [discord.SelectDefaultValue(id=c, type=discord.SelectDefaultValueType.channel)
                for c in ids if g.get_channel(c)]

    def _role_default(self, ids):
        g = self.origin.guild
        return [discord.SelectDefaultValue(id=r, type=discord.SelectDefaultValueType.role)
                for r in ids if g.get_role(r)]

    # --------------------------------------------------------------- pages
    def _build(self):
        self.clear_items()
        nav = discord.ui.Select(placeholder="Section", row=0, options=[
            discord.SelectOption(label=n, value=k, default=k == self.page) for k, n in PAGES.items()])
        nav.callback = lambda i, s=nav: self._nav(i, s)
        self.add_item(nav)
        getattr(self, f"_build_{self.page}")()

    def _build_overview(self):
        pass

    def _build_giveaways(self):
        self._btn("New giveaway", self._gw_new, discord.ButtonStyle.success, emoji="🎁")
        self._btn("Manage giveaways", self._gw_manage, discord.ButtonStyle.primary, emoji="🛠️")

    def _build_polls(self):
        self._btn("New poll", self._poll_new, discord.ButtonStyle.success, emoji="📊")
        self._btn("Manage polls", self._poll_manage, discord.ButtonStyle.primary, emoji="🛠️")

    def _build_tickets(self):
        S, P = discord.ButtonStyle.success, discord.ButtonStyle.primary
        self._btn("Run setup", self._tk_setup, S, emoji="🧰")
        self._btn("Refresh panel", self._tk_panel, P, emoji="🔄")
        self._btn("Panel text", self._tk_text, emoji="📝")
        self._btn("Settings", self._tk_settings, emoji="⚙️")
        self._btn("Ticket types", self._tk_types, P, row=2, emoji="🗂️")
        self._btn("Stats", self._tk_stats, row=2, emoji="📈")
        self._btn("Blacklist", self._tk_blacklist, row=2, emoji="🚫")
        lg = discord.ui.ChannelSelect(placeholder="Transcript / log channel (staff only)", row=3,
                                      channel_types=[discord.ChannelType.text],
                                      default_values=self._chan_default([self.db.get("ticket_log_channel_id", 0)]))
        lg.callback = lambda i, s=lg: self._set_channel(i, s, "ticket_log_channel_id")
        self.add_item(lg)
        ping = discord.ui.RoleSelect(placeholder="Roles pinged when a ticket opens (empty = no ping)", row=4,
                                     min_values=0, max_values=MAX_PING_ROLES,
                                     default_values=self._role_default(server_ping_role_ids(self.db)))
        ping.callback = lambda i, s=ping: self._tk_ping(i, s)
        self.add_item(ping)

    def _build_ticketbanner(self):
        P = discord.ButtonStyle.primary
        has_p, has_t = bool(self.db.get("ticket_panel_banner_id")), bool(self.db.get("ticket_banner_id"))
        self._btn("Panel banner (link)", lambda i: self._bn_link(i, "panel"), P, emoji="🖼️")
        self._btn("Ticket banner (link)", lambda i: self._bn_link(i, "ticket"), P, emoji="🎫")
        self._btn("Preview panel", self._bn_preview_panel, row=2, emoji="👁️")
        self._btn("Preview ticket", self._bn_preview_ticket, row=2, emoji="👁️")
        for label, which, on in (("Remove panel banner", "panel", has_p), ("Remove ticket banner", "ticket", has_t)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.danger, row=3, emoji="🗑️", disabled=not on)
            b.callback = lambda i, w=which: self._bn_remove(i, w)
            self.add_item(b)

    def _build_announce(self):
        self._btn("New announcement", self._ann_new, discord.ButtonStyle.success, emoji="📣")
        self._btn("History", self._ann_history, emoji="🕘")
        self._btn("Templates", self._ann_templates, emoji="📑")

    def _build_xp(self):
        P = discord.ButtonStyle.primary
        self._btn("Message XP", self._msg_modal, P, emoji="💬")
        self._btn("Voice XP", self._voice_modal, P, emoji="🎙️")
        self._btn("Reset to defaults", self._reset, discord.ButtonStyle.danger, emoji="♻️")
        T = discord.ChannelType
        ch = discord.ui.ChannelSelect(
            placeholder="Channels / categories that earn NO XP", row=2, min_values=0, max_values=25,
            channel_types=[T.text, T.voice, T.category, T.news, T.forum, T.stage_voice],
            default_values=self._chan_default(sorted(self.db.excluded_channels)[:25]))
        ch.callback = lambda i, s=ch: self._ex_channels(i, s)
        self.add_item(ch)
        ro = discord.ui.RoleSelect(
            placeholder="Roles that earn NO XP", row=3, min_values=0, max_values=25,
            default_values=self._role_default(sorted(self.db.excluded_roles)[:25]))
        ro.callback = lambda i, s=ro: self._ex_roles(i, s)
        self.add_item(ro)

    def _build_features(self):
        sel = discord.ui.Select(placeholder="Tick what should be ON", row=1, min_values=0, max_values=len(FEATURES),
                                options=[discord.SelectOption(label=n, value=k, default=self.db.get(k, True))
                                         for k, n in FEATURES])
        sel.callback = lambda i, s=sel: self._features(i, s)
        self.add_item(sel)

    def _build_welcome(self):
        self._btn("Edit texts", self._edit_texts, discord.ButtonStyle.primary, emoji="📝")
        self._btn("Preview", self._preview, emoji="👁️")

    def _build_dm(self):
        pos = self.db.get("dm_banner_position", "top")
        sel = discord.ui.Select(placeholder="Banner position", row=1, options=[
            discord.SelectOption(label=f"Banner: {n}", value=k, default=k == pos) for k, n in DM_POSITIONS.items()])
        sel.callback = lambda i, s=sel: self._dm_position(i, s)
        self.add_item(sel)
        self._btn("Edit DM", self._dm_edit, discord.ButtonStyle.primary, row=2, emoji="📝")
        self._btn("Preview", self._dm_preview, row=2, emoji="👁️")
        b = discord.ui.Button(label="Remove banner", style=discord.ButtonStyle.danger, row=2, emoji="🗑️",
                              disabled=not DM_BANNER.exists())
        b.callback = self._dm_remove
        self.add_item(b)

    def _build_channels(self):
        T = [discord.ChannelType.text, discord.ChannelType.news]
        w = discord.ui.ChannelSelect(placeholder="Welcome channel", row=1, channel_types=T,
                                     default_values=self._chan_default([self.db.get("welcome_channel_id", 0)]))
        w.callback = lambda i, s=w: self._set_channel(i, s, "welcome_channel_id")
        lv = discord.ui.ChannelSelect(placeholder="Leave channel (empty = same as welcome)", row=2, channel_types=T,
                                      default_values=self._chan_default([self.db.get("leave_channel_id", 0)]))
        lv.callback = lambda i, s=lv: self._set_channel(i, s, "leave_channel_id")
        l = discord.ui.ChannelSelect(placeholder="Level-up announcement channel", row=3, channel_types=T,
                                     default_values=self._chan_default([self.db.get("levelup_channel_id", 0)]))
        l.callback = lambda i, s=l: self._set_channel(i, s, "levelup_channel_id")
        a = discord.ui.ChannelSelect(placeholder="Staff alerts channel (bot problems)", row=4, channel_types=T,
                                     default_values=self._chan_default([self.db.get("alert_channel_id", 0)]))
        a.callback = lambda i, s=a: self._set_channel(i, s, "alert_channel_id")
        self.add_item(w)
        self.add_item(lv)
        self.add_item(l)
        self.add_item(a)

    def _build_experimental(self):
        on = bool(self.db.get(AI_HELPER_KEY, False))
        self._btn("Turn OFF" if on else "Turn ON", self._ai_toggle,
                  discord.ButtonStyle.danger if on else discord.ButtonStyle.success,
                  emoji="⏸️" if on else "▶️")
        self._btn("Test the AI connection", self._ai_test, emoji="🔌")

    def _build_staff(self):
        sel = discord.ui.RoleSelect(placeholder="Roles allowed to manage Orion (max 10)", row=1, min_values=0,
                                    max_values=10,
                                    default_values=self._role_default(self.db.get("staff_role_ids", [])))
        sel.callback = lambda i, s=sel: self._staff(i, s)
        self.add_item(sel)

    # -------------------------------------------------------------- embeds
    def embed(self) -> discord.Embed:
        db, g = self.db, self.origin.guild
        e = discord.Embed(color=config.COLOR_MAIN, title=f"{config.BRAND} · dashboard · {PAGES[self.page][2:].strip()}")
        r = db.rule
        if self.page == "overview":
            e.add_field(name="Features", value="\n".join(
                f"{'✅' if db.get(k, True) else '❌'} {n}" for k, n in FEATURES)
                + f"\n{'✅' if db.get(AI_HELPER_KEY, False) else '❌'} 🧪 AI ticket helper (experimental)", inline=False)
            e.add_field(name="XP rules", value=(
                f"💬 {r('msg_min')}–{r('msg_max')} XP / {r('cooldown')}s · 🌅 +{r('daily_bonus')} daily\n"
                f"🎙️ {r('voice_per_10min')} XP / 10 min · cap {r('voice_cap'):,}/day"), inline=False)
            wc = find_welcome_channel(self.bot, g)
            e.add_field(name="Channels", value=(
                f"Welcome: {wc.mention if wc else '*none*'}\n"
                f"Level-ups: {'<#%s>' % db.get('levelup_channel_id') if db.get('levelup_channel_id') else '*run /setup*'}\n"
                f"Panel: {'<#%s>' % db.get('panel_channel_id') if db.get('panel_channel_id') else '*run /setup*'}"),
                inline=True)
            staff = " ".join(f"<@&{x}>" for x in sorted(staff_role_ids(db))) or "none"
            e.add_field(name="Staff access", value=f"**{config.ADMIN_ROLE_NAME}** + {staff}", inline=True)
            gws = sum(1 for r in self.data.get("gw", []) if r["status"] == "active")
            pls = sum(1 for r in self.data.get("polls", []) if r["status"] == "active")
            tks = len(self.data.get("tickets", []))
            e.add_field(name="Running now", value=f"🎁 {gws} giveaway{'s' if gws != 1 else ''} · 📊 {pls} open poll{'s' if pls != 1 else ''}"
                                                  f" · 🎫 {tks}{'+' if tks >= 25 else ''} open ticket{'s' if tks != 1 else ''}",
                        inline=False)
            e.set_footer(text="Use the dropdown below to open a section.")
        elif self.page == "giveaways":
            live = [r for r in self.data.get("gw", []) if r["status"] == "active"]
            lines = [f"🎁 [#{r['id']} · {r['title']}]({self._jump(r['channel_id'], r['message_id'])}) · "
                     f"ends <t:{int(r['ends_at'].timestamp())}:R>" for r in live[:8]]
            e.description = ("**New giveaway** opens the builder: title, text, banners, rewards and codes, winners, timer, "
                             "check-in interval and the roles allowed to enter. You see a live preview and nothing is posted "
                             "until you press Publish.\n**Manage giveaways** lets you edit, extend, end early, see "
                             "participants, reroll, export and more.\n\n"
                             + ("**Running now**\n" + "\n".join(lines) if lines else "*No giveaway is running.*"))
        elif self.page == "polls":
            live = [r for r in self.data.get("polls", []) if r["status"] == "active"]
            lines = [f"{'🔒' if r['anonymous'] else '🌐'} [#{r['id']} · {r['question']}]"
                     f"({self._jump(r['channel_id'], r['message_id'])})"
                     + (f" · ends <t:{int(r['ends_at'].timestamp())}:R>" if r["ends_at"] else "") for r in live[:8]]
            e.description = ("**New poll** opens the builder: question, options, public or private, timer, banner, "
                             "who can vote and whether votes can be changed.\n"
                             "🌐 **Public**: everyone can see who voted. 🔒 **Private**: only staff can, under "
                             "**Manage polls** (results, who voted, banner, end now, export, delete).\n\n"
                             + ("**Open now**\n" + "\n".join(lines) if lines else "*No poll is open.*"))
        elif self.page == "tickets":
            cog = self.bot.get_cog("ticket")
            types = cog.enabled_types() if cog else []
            cat, logc, pch = db.get("ticket_category_id"), db.get("ticket_log_channel_id"), db.get("ticket_panel_channel_id")
            pings = server_ping_role_ids(db)
            lines = [f"<#{r['channel_id']}> · {r['type_name']} · <@{r['owner_id']}>"
                     + (f" · {PRIORITIES.get(r['priority'], '')}" if r["priority"] != "normal" else "")
                     + ("" if r["claimed_by"] else " · unclaimed") for r in self.data.get("tickets", [])[:8]]
            e.description = (
                ("✅ Members can open tickets" if db.get("tickets_enabled", True)
                 else "❌ Tickets are switched off (see Features)") + "\n"
                f"**Category:** {f'<#{cat}>' if cat else '*none: press Run setup*'} · "
                f"**Panel:** {f'<#{pch}>' if pch else '*not posted*'} · **Log:** {f'<#{logc}>' if logc else '*none*'}\n"
                f"**Ping roles:** {' '.join(f'<@&{r}>' for r in pings) if pings else '*none*'}"
                " (a ticket type can have its own in Ticket types)\n"
                f"**Types:** {', '.join(t['emoji'] + ' ' + t['name'] for t in types) or '*none*'}\n"
                + "\n".join(f"{TICKET_SETTINGS[k][0]}: **{ticket_setting(db, k)}**" for k in TICKET_SETTINGS) + "\n\n"
                "**Run setup** creates the category, the panel and the log channel (Sect Admin only, safe to repeat).\n"
                "**Ticket types** adds, edits, turns off or removes the sections in the ticket dropdown, and sets each "
                "one's emoji, staff roles, category, ping roles and form questions.\n"
                "Move the panel with `/ticket panel #channel`.\n\n"
                + ("**Open now**\n" + "\n".join(lines) if lines else "*No ticket is open.*"))[:4000]
        elif self.page == "ticketbanner":
            e.description = (
                f"**Panel banner:** {'uploaded' if db.get('ticket_panel_banner_id') else '*none*'}: shown on the "
                "message where members open tickets.\n"
                f"**Ticket banner:** {'uploaded' if db.get('ticket_banner_id') else '*none*'}: shown at the top of "
                "every new ticket.\n\n"
                "Use the buttons to set a banner from an image link, or **upload** one with "
                "`/ticket banner` (pick panel or ticket, then attach the image). Don't use Discord image links: "
                "they expire.")
        elif self.page == "announce":
            names = self.data.get("templates", [])
            e.description = ("**New announcement** opens the announcement builder (embed, images, mentions, templates).\n"
                             "**History** lists the latest ones; edit any with `/announce edit`.\n\n"
                             f"Saved templates: {', '.join(f'`{n}`' for n in names[:15]) or '*none yet*'}")
        elif self.page == "xp":
            avg = (r("msg_min") + r("msg_max")) / 2
            voice = lambda mins: min(mins // config.VOICE_BLOCK_MINUTES * r("voice_per_10min"), r("voice_cap"))
            active = 20 * avg + r("daily_bonus") + voice(60)
            casual = 10 * avg + r("daily_bonus") + voice(30)
            e.description = (f"**Messages:** {r('msg_min')}–{r('msg_max')} XP, once per {r('cooldown')}s, "
                             f"real content only (≥ {r('min_chars')} letters/digits)\n"
                             f"**Daily bonus:** +{r('daily_bonus')} XP (first message of the day, server time)\n"
                             f"**Voice:** {r('voice_per_10min')} XP per 10 minutes, max {r('voice_cap'):,}/day\n\n"
                             f"**Pace to Eternal True God (level {MAX_LEVEL}):**\n"
                             f"Active (20 msgs + 1h voice a day): {months_to_max(active)}\n"
                             f"Casual (10 msgs + 30 min voice a day): {months_to_max(casual)}")
            e.set_footer(text="The level curve and rank roles are fixed. Dropdowns below set XP exclusions.")
        elif self.page == "features":
            e.description = "Anything you un-tick is switched off immediately. Changes apply to the whole server."
        elif self.page == "welcome":
            wc = find_welcome_channel(self.bot, g)
            e.description = (f"Channel: {wc.mention if wc else '*none: set one under Channels*'}\n\n"
                             "**Edit texts**: welcome message, DM and leave message "
                             "(`{mention}` `{user}` `{server}` `{count}`).\n"
                             "**Preview**: see the card with your own avatar.\n\n"
                             "Banner: `/welcome banner` (upload image) · `/welcome banner-reset`\n"
                             "DM banner and design: open the **Welcome DM** section.\n"
                             "Card positions: `/welcome layout` · `/welcome layout-reset`")
        elif self.page == "dm":
            col = db.get("dm_color")
            e.description = (
                f"Sent to every new member in their DMs ({'✅ on' if db.get('dm_enabled', True) else '❌ off: turn it on under Features'}).\n\n"
                f"**Banner:** {'uploaded' if DM_BANNER.exists() else '*none yet*'} · "
                f"{DM_POSITIONS.get(db.get('dm_banner_position', 'top'))}\n"
                f"**Title:** {db.get('dm_title') or '*none*'}\n"
                f"**Colour:** {f'#{col:06X}' if isinstance(col, int) else 'default purple'}\n"
                f"**Footer:** {db.get('dm_footer') or '*none*'}\n\n"
                "**Edit DM**: title, message, footer and colour.\n"
                "**Preview**: see exactly what a new member gets.\n"
                "Upload or replace the banner with `/welcome dm-banner` (attach the image).")
        elif self.page == "channels":
            e.description = ("Pick where the welcome card, the 'member left' messages and the level-up announcements are posted. "
                             "**Staff alerts** is where Orion reports problems, such as role changes it couldn't make.")
        elif self.page == "experimental":
            on = bool(db.get(AI_HELPER_KEY, False))
            e.description = (
                "**🤖 AI ticket handler**\n"
                f"Status: {'✅ **ON**' if on else '❌ **OFF**'}\n"
                f"AI service: {'✅ key set' if ai.is_configured() else '❌ no `AI_API_KEY` in `.env`'}"
                f" · `{config.AI_PROVIDER}` · `{db.get('ai_model', '') or config.AI_MODEL}`\n\n"
                "When ON, Orion greets new tickets and gives first-line help from the approved **knowledge base**. "
                "Anything sensitive, uncertain or unanswerable is handed to staff (pinged), and Orion steps back the moment "
                "a staff member replies or claims the ticket. Reports, staff applications and partnerships are never answered "
                "by the AI. Routine tickets can be closed automatically once the member says they're solved; members can "
                "reopen them.\n\n"
                "Everything else (tone, length, confidence, knowledge base, logs, test) is in **`/ai`**. "
                "Turn it off per ticket with `/ticket ai off`. If the AI is down, tickets keep working normally.")
        elif self.page == "staff":
            staff = " ".join(f"<@&{x}>" for x in sorted(staff_role_ids(db))) or "none"
            e.description = (f"**{config.ADMIN_ROLE_NAME}** always has full access.\n"
                             f"Staff roles (the always-on ones from `STAFF_ROLE_IDS` plus any picked here): {staff}\n\n"
                             "Staff roles can use `/admin`, `/announce`, `/welcome`, `/giveaway`, `/poll` and the "
                             "staff `/ticket` commands, and handle every ticket type that has no roles of its own. "
                             f"Only **{config.ADMIN_ROLE_NAME}** can run `/setup` or change this list. "
                             "Roles from `STAFF_ROLE_IDS` stay staff even if they are unselected here.")
        if self.notice:
            e.add_field(name="\u200b", value=self.notice, inline=False)
        return e

    @staticmethod
    def _jump(channel_id, message_id) -> str:
        return f"https://discord.com/channels/{config.GUILD_ID}/{channel_id}/{message_id}"

    async def _load(self):
        """Fetch what the current page lists (kept out of the sync embed builder)."""
        self.data = {}
        try:
            if self.page in ("overview", "giveaways"):
                self.data["gw"] = await self.db.gw_list(25)
            if self.page in ("overview", "polls"):
                self.data["polls"] = await self.db.poll_list(25)
            if self.page in ("overview", "tickets"):
                self.data["tickets"] = await self.db.ticket_open_list(25)
            if self.page == "announce":
                self.data["templates"] = await self.db.list_templates()
        except Exception:
            log.exception("Dashboard could not load page data")

    async def render(self, notice: str | None = None):
        self.notice = notice
        await self._load()
        self._build()
        try:
            await self.origin.edit_original_response(embed=self.embed(), view=self)
        except discord.HTTPException:
            log.exception("Dashboard render failed")

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        try:
            await self.origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass

    # ----------------------------------------------------------- callbacks
    async def _nav(self, i, sel):
        await i.response.defer()
        self.page = sel.values[0]
        await self.render()

    async def _msg_modal(self, i):
        await i.response.send_modal(RulesModal(self, "Message XP", MSG_SPECS))

    async def _voice_modal(self, i):
        await i.response.send_modal(RulesModal(self, "Voice XP", VOICE_SPECS))

    async def _reset(self, i):
        await i.response.defer()
        await self.db.reset_rules()
        await refresh_panel(self.bot)
        await self.render("♻️ XP rules are back to the defaults.")

    async def _ex_channels(self, i, sel):
        await i.response.defer()
        g = self.origin.guild
        shown = set(sorted(c for c in self.db.excluded_channels if g.get_channel(c))[:25])
        new = {v.id for v in sel.values}
        for cid in shown - new:
            await self.db.set_exclusion("channel", cid, False)
        for cid in new - shown:
            await self.db.set_exclusion("channel", cid, True)
        await self.render("✅ Channel exclusions updated.")

    async def _ex_roles(self, i, sel):
        await i.response.defer()
        g = self.origin.guild
        shown = set(sorted(r for r in self.db.excluded_roles if g.get_role(r))[:25])
        new = {v.id for v in sel.values}
        for rid in shown - new:
            await self.db.set_exclusion("role", rid, False)
        for rid in new - shown:
            await self.db.set_exclusion("role", rid, True)
        await self.render("✅ Role exclusions updated.")

    async def _features(self, i, sel):
        await i.response.defer()
        on = set(sel.values)
        for key, _ in FEATURES:
            await self.db.set(key, key in on)
        await self.render("✅ Features updated.")

    async def _ai_toggle(self, i):
        await i.response.defer()
        if not is_admin(i.user) and not is_staff(i.user, self.db):
            return await self.render("⚠️ You can't change this.")
        turning_on = not self.db.get(AI_HELPER_KEY, False)
        if turning_on and not ai.is_configured():
            return await self.render("⚠️ It can't be turned on yet: add `AI_API_KEY` to `.env` and restart the bot.")
        await self.db.set(AI_HELPER_KEY, turning_on)
        log.info("AI ticket helper turned %s by %s (%d)", "ON" if turning_on else "OFF", i.user, i.user.id)
        await self.render("✅ AI ticket handler is now **ON**." if turning_on else "⏸️ AI ticket handler is now **OFF**.")

    async def _ai_test(self, i):
        await i.response.defer(ephemeral=True, thinking=True)
        if not ai.is_configured():
            return await i.followup.send("❌ No `AI_API_KEY` in `.env`.", ephemeral=True)
        try:
            text = await ai.complete("You are Orion, a friendly assistant of the Blue Heaven Sect Discord. Reply in one short sentence.",
                                     "Say hello and confirm you are connected.", model=self.db.get("ai_model", "") or None)
        except ai.AIError as e:
            return await i.followup.send(f"❌ The AI service failed: {e}", ephemeral=True)
        await i.followup.send(f"✅ Connected. A sample reply:\n>>> {text}"[:1900], ephemeral=True,
                              allowed_mentions=discord.AllowedMentions.none())

    async def _edit_texts(self, i):
        await i.response.send_modal(TextModal(self.db))

    async def _preview(self, i):
        await i.response.defer(ephemeral=True, thinking=True)
        cog = self.bot.get_cog("welcome")
        embed, file = await cog.build(i.user)
        await i.followup.send(content=i.user.mention, embed=embed, file=file, ephemeral=True)

    async def _dm_edit(self, i):
        await i.response.send_modal(DMModal(self))

    async def _dm_position(self, i, sel):
        await i.response.defer()
        await self.db.set("dm_banner_position", sel.values[0])
        note = None if DM_BANNER.exists() or sel.values[0] == "off" else "ℹ️ No banner uploaded yet: use `/welcome dm-banner`."
        await self.render(note or "✅ Banner position saved.")

    async def _dm_preview(self, i):
        await i.response.defer(ephemeral=True, thinking=True)
        embeds, file = self.bot.get_cog("welcome").build_dm(i.user)
        await i.followup.send(embeds=embeds, **({"file": file} if file else {}), ephemeral=True)

    async def _dm_remove(self, i):
        await i.response.defer()
        DM_BANNER.unlink(missing_ok=True)
        await self.render("🗑️ DM banner removed.")

    async def _set_channel(self, i, sel, key):
        await i.response.defer()
        if sel.values:
            await self.db.set(key, sel.values[0].id)
            await self.render("✅ Channel saved.")
        else:
            await self.render()

    async def _staff(self, i, sel):
        await i.response.defer()
        if not is_admin(i.user):
            return await self.render(f"⚠️ Only **{config.ADMIN_ROLE_NAME}** can change staff access.")
        ids = [r.id for r in sel.values if not r.is_default() and not r.managed]
        await self.db.set("staff_role_ids", ids)
        await self.render("✅ Staff roles updated.")

    # ------------------------------------------- open the other builders
    async def _launch(self, i: discord.Interaction, cog_name: str, method: str):
        cog = self.bot.get_cog(cog_name)
        if cog is None:
            return await i.response.send_message("That feature isn't loaded.", ephemeral=True)
        # thinking=True makes a NEW private message for the builder; without it a button press would edit this panel.
        await i.response.defer(ephemeral=True, thinking=True)
        await getattr(cog, method)(i)

    async def _gw_new(self, i):
        await self._launch(i, "giveaway", "open_builder")

    async def _gw_manage(self, i):
        await self._launch(i, "giveaway", "open_manage")

    async def _poll_new(self, i):
        await self._launch(i, "poll", "open_builder")

    async def _poll_manage(self, i):
        await self._launch(i, "poll", "open_manage")

    # ------------------------------------------------------------- tickets
    @property
    def _tickets(self):
        return self.bot.get_cog("ticket")

    async def _tk_setup(self, i):
        await i.response.defer()
        if not is_admin(i.user):
            return await self.render(f"⚠️ Only **{config.ADMIN_ROLE_NAME}** can run the ticket setup.")
        try:
            text = await self._tickets.setup_tickets(i.guild)
        except discord.HTTPException as e:
            text = f"⚠️ Discord refused part of the setup: {e.text or e}"
        await self.render(text[:1000])

    async def _tk_panel(self, i):
        await i.response.defer()
        cog = self._tickets
        ch = self.bot.get_channel(self.db.get("ticket_panel_channel_id") or 0)
        if ch is None:
            return await self.render("ℹ️ No panel yet: press **Run setup**, or use `/ticket panel #channel`.")
        note = await cog.refresh_panel()
        if note and "deleted" in note:
            note = await cog.post_panel(ch)
        await self.render(note or "✅ Panel refreshed.")

    async def _tk_text(self, i):
        await i.response.send_modal(PanelTextModal(self))

    async def _tk_settings(self, i):
        await i.response.send_modal(TicketSettingsModal(self))

    async def _tk_types(self, i):
        await self._launch(i, "ticket", "open_types")

    async def _tk_stats(self, i):
        await i.response.defer(ephemeral=True, thinking=True)
        await i.followup.send(embed=await self._tickets.stats(30), ephemeral=True)

    async def _tk_blacklist(self, i):
        rows = await self.db.bl_list(50)
        text = "\n".join(f"<@{r['user_id']}> · {r['reason'] or 'no reason'}" for r in rows)
        await i.response.send_message(
            (("🚫 **Ticket blacklist**\n" + text) if rows else "Nobody is blacklisted.")[:1850]
            + "\n-# Add or remove people with `/ticket blacklist add` and `/ticket blacklist remove`.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def _tk_ping(self, i, sel):
        await i.response.defer()
        ids = [r.id for r in sel.values if not r.is_default()][:MAX_PING_ROLES]
        await self.db.set("ticket_ping_role_ids", ids)
        await self.db.set("ticket_ping_role_id", None)          # the old single-role setting is replaced by the list
        await self.render(f"✅ {len(ids)} ping role{'s' if len(ids) != 1 else ''} saved." if ids
                          else "✅ New tickets won't ping a role.")

    async def _bn_link(self, i, which):
        await i.response.send_modal(BannerLinkModal(self, which))

    async def _bn_remove(self, i, which):
        await i.response.defer()
        await self.render(await self._tickets.set_banner(which, None, None, remove=True))

    async def _bn_preview_panel(self, i):
        await i.response.defer(ephemeral=True, thinking=True)
        cog = self._tickets
        url, files = await cog.banner_file("panel")
        await i.followup.send(embed=ticket_panel_embed(self.db, cog.enabled_types(), url), files=files, ephemeral=True)

    async def _bn_preview_ticket(self, i):
        await i.response.defer(ephemeral=True, thinking=True)
        url, files = await self._tickets.banner_file("ticket")
        sample = {"id": 42, "type_name": "Support", "answers": [{"q": "What do you need help with?", "a": "Example answer"}],
                  "claimed_by": None, "priority": "normal"}
        await i.followup.send(embed=ticket_embed(sample, i.user.mention, banner_url=url), files=files, ephemeral=True)

    async def _ann_new(self, i):
        await i.response.defer(ephemeral=True, thinking=True)
        await AnnounceBuilder(self.bot, i, AnnounceDraft(), channel_id=None).render()

    async def _ann_history(self, i):
        cog = self.bot.get_cog("announce")
        await cog.history.callback(cog, i)

    async def _ann_templates(self, i):
        cog = self.bot.get_cog("announce")
        await cog.templates.callback(cog, i)


class DashboardCog(commands.Cog, name="Dashboard"):
    def __init__(self, bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_staff(interaction)

    @app_commands.command(name="admin", description="Open the Orion control panel")
    async def admin(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        await Dashboard(self.bot, i).render()


async def setup(bot):
    await bot.add_cog(DashboardCog(bot))
