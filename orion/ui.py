"""Shared embeds + views: profile card, leaderboard, rank ladder, persistent panel."""
import asyncio
import math

import discord

from . import config
from .images import LEVEL_BANNER, render_rank
from .levels import (LEVEL_ROLES, MAX_LEVEL, next_milestone, progress, rank_name, realm_of,
                     role_name_for_level, total_xp_for_level)

PAGE = 10
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _embed(**kw) -> discord.Embed:
    return discord.Embed(color=config.COLOR_MAIN, **kw)


# ---------------------------------------------------------------- profile
async def rank_card(bot, member: discord.Member):
    """Renders the member's rank card image from their current XP.
    Returns (png_buffer, row, xp, level, into, need, position, rank_title)."""
    row = await bot.db.get_user(member.id)
    xp = row["xp"] if row else 0
    lvl, into, need = progress(xp)
    pos = await bot.db.get_rank(member.id) if row else None
    title = role_name_for_level(lvl)
    avatar = await member.display_avatar.replace(size=256, format="png").read()
    buf = await asyncio.to_thread(render_rank, avatar, member.display_name, lvl, title, pos, into, need, xp)
    return buf, row, xp, lvl, into, need, pos, title


async def build_profile(bot, member: discord.Member):
    buf, row, xp, lvl, into, need, pos, title = await rank_card(bot, member)

    e = _embed(title=f"{member.display_name}'s profile")
    e.set_image(url="attachment://rank.png")
    e.add_field(name="Realm", value=f"**{realm_of(lvl)}**", inline=True)
    e.add_field(name="Rank", value=f"**{rank_name(lvl)}**", inline=True)
    e.add_field(name="Sect position", value=f"**#{pos}**" if pos else "Unranked", inline=True)
    e.add_field(name="Total XP", value=f"**{xp:,}**", inline=True)
    if need:
        pct = int(into * 100 / need)
        e.add_field(name="Progress to next rank",
                    value=f"**{need - into:,}** XP to **{rank_name(lvl + 1)}** ({pct}%)", inline=False)
    else:
        e.add_field(name="Progress to next rank", value="Highest rank reached 👑", inline=False)
    nxt = next_milestone(lvl)
    if nxt:
        e.add_field(name="Next realm",
                    value=f"**{nxt[1]}** — {total_xp_for_level(nxt[0]) - xp:,} XP away", inline=False)
    if row:
        e.set_footer(text=f"{row['messages']:,} XP-earning messages · {row['voice_minutes'] / 60:.1f}h voice")
    return e, discord.File(buf, "rank.png")


# ----------------------------------------------------------------- ladder
def ladder_embed() -> discord.Embed:
    lines = [f"**{name}** — from {name} 1 · {total_xp_for_level(lv):,} XP" for lv, name in LEVEL_ROLES]
    e = _embed(title="Cultivation realms", description="\n".join(lines))
    e.set_footer(text="Each realm has 9 ranks. Your realm role changes when you break into the next realm.")
    return e


def ladder_message() -> dict:
    """kwargs for send_message: the ladder with the Level Roles banner on top (a slim image-only embed
    first, since an embed's own image slot sits at the bottom). No banner file in assets/ = ladder alone."""
    e = ladder_embed()
    if not LEVEL_BANNER.exists():
        return {"embeds": [e]}
    head = _embed()
    head.set_image(url="attachment://level_roles.png")
    return {"embeds": [head, e], "file": discord.File(LEVEL_BANNER, "level_roles.png")}


# ------------------------------------------------------------ leaderboard
class LeaderboardView(discord.ui.View):
    TITLES = {"all": "All-time", "week": "This week", "month": "This month"}

    def __init__(self, bot, guild: discord.Guild, kind="all"):
        super().__init__(timeout=180)
        self.bot, self.guild, self.kind, self.page, self.pages = bot, guild, kind, 0, 1
        self._style()

    def _style(self):
        for b, k in ((self.b_all, "all"), (self.b_week, "week"), (self.b_month, "month")):
            b.style = discord.ButtonStyle.primary if k == self.kind else discord.ButtonStyle.secondary
        self.b_prev.disabled = self.page <= 0
        self.b_next.disabled = self.page >= self.pages - 1

    async def embed(self) -> discord.Embed:
        rows, total = await self.bot.db.leaderboard(self.kind, PAGE, self.page * PAGE)
        self.pages = max(1, math.ceil(total / PAGE))
        if self.page >= self.pages:
            self.page = self.pages - 1
        self._style()
        e = _embed(title=f"🏆 Sect leaderboard — {self.TITLES[self.kind]}")
        if not rows:
            e.description = "Nobody has cultivated here yet."
        else:
            out = []
            for i, r in enumerate(rows, start=self.page * PAGE + 1):
                m = self.guild.get_member(r["user_id"])
                who = m.mention if m else "*former member*"
                if self.kind == "all":
                    from .levels import level_from_xp
                    extra = f"Level {level_from_xp(r['xp'])} · {r['xp']:,} XP"
                else:
                    extra = f"{r['xp']:,} XP"
                out.append(f"{MEDALS.get(i, f'`#{i}`')} {who} — {extra}")
            e.description = "\n".join(out)
        foot = f"Page {self.page + 1}/{self.pages}"
        if self.kind == "week":
            foot += " · resets Monday 00:00 server time"
        elif self.kind == "month":
            foot += " · resets on the 1st, 00:00 server time"
        e.set_footer(text=foot)
        return e

    async def _refresh(self, i: discord.Interaction):
        await i.response.edit_message(embed=await self.embed(), view=self)

    @discord.ui.button(label="All-time", row=0)
    async def b_all(self, i, _):
        self.kind, self.page = "all", 0
        await self._refresh(i)

    @discord.ui.button(label="Weekly", row=0)
    async def b_week(self, i, _):
        self.kind, self.page = "week", 0
        await self._refresh(i)

    @discord.ui.button(label="Monthly", row=0)
    async def b_month(self, i, _):
        self.kind, self.page = "month", 0
        await self._refresh(i)

    @discord.ui.button(label="◀ Prev", row=1)
    async def b_prev(self, i, _):
        self.page -= 1
        await self._refresh(i)

    @discord.ui.button(label="Next ▶", row=1)
    async def b_next(self, i, _):
        self.page += 1
        await self._refresh(i)


async def send_leaderboard(bot, interaction: discord.Interaction, ephemeral: bool):
    view = LeaderboardView(bot, interaction.guild)
    await interaction.response.send_message(embed=await view.embed(), view=view, ephemeral=ephemeral)


# ------------------------------------------------------------------ panel
def panel_embed(db) -> discord.Embed:
    e = _embed(
        title=f"{config.BRAND} · Levelling",
        description=(
            "Chat and hang out in voice to earn XP and climb the ranks.\n"
            "Use the buttons below to see **your profile**, the **leaderboard** and the **rank ladder**."))
    e.add_field(name="💬 Messages",
                value=f"{db.rule('msg_min')}–{db.rule('msg_max')} XP, once per {db.rule('cooldown')}s", inline=True)
    e.add_field(name="🎙️ Voice",
                value=f"{db.rule('voice_per_10min')} XP / 10 min · max {db.rule('voice_cap'):,}/day\n"
                      "Unmuted, not deafened, with at least 1 other person", inline=True)
    e.add_field(name="🌅 Daily bonus", value=f"+{db.rule('daily_bonus')} XP on your first message each day (server time)",
                inline=False)
    return e


class PanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)  # persistent: survives restarts
        self.bot = bot

    @discord.ui.button(label="My Profile", emoji="👤", style=discord.ButtonStyle.primary,
                       custom_id="orion:profile")
    async def profile(self, i: discord.Interaction, _):
        await i.response.defer(ephemeral=True)
        embed, file = await build_profile(self.bot, i.user)
        await i.followup.send(embed=embed, file=file, ephemeral=True)

    @discord.ui.button(label="Leaderboard", emoji="🏆", style=discord.ButtonStyle.secondary,
                       custom_id="orion:leaderboard")
    async def leaderboard(self, i: discord.Interaction, _):
        await send_leaderboard(self.bot, i, ephemeral=True)

    @discord.ui.button(label="Rank Ladder", emoji="🎖️", style=discord.ButtonStyle.secondary,
                       custom_id="orion:ladder")
    async def ladder(self, i: discord.Interaction, _):
        await i.response.defer(ephemeral=True)       # answer inside Discord's 3 s window; the banner upload can take longer
        await i.followup.send(**ladder_message(), ephemeral=True)


async def refresh_panel(bot):
    """Re-render the public rank panel so it shows the current XP rules."""
    guild = bot.get_guild(config.GUILD_ID)
    ch = guild.get_channel(bot.db.get("panel_channel_id", 0)) if guild else None
    mid = bot.db.get("panel_message_id")
    if not ch or not mid:
        return
    try:
        msg = await ch.fetch_message(mid)
        await msg.edit(embed=panel_embed(bot.db))
    except discord.HTTPException:
        pass
