"""Achievements: staff create them and hand them out; members see them on /profile.
(Automatic unlock conditions and notifications arrive with Phase 7.)"""
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from ..achievement_db import RARITIES
from ..checks import require_admin, require_staff

log = logging.getLogger("orion.achievements")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


class Achievements(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    achievement = app_commands.Group(name="achievement", description="Achievements (staff)")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_staff(interaction)

    async def _keys(self, i: discord.Interaction, current: str):
        rows = await self.bot.db.list_achievements(active_only=False)
        cur = current.lower()
        return [app_commands.Choice(name=f"{r['icon']} {r['name']} ({r['key']})"[:100], value=r["key"])
                for r in rows if cur in r["key"] or cur in r["name"].lower()][:25]

    @achievement.command(name="create", description="Create an achievement (Sect Admin)")
    @app_commands.describe(key="Short id: lowercase letters, digits, dashes", name="Display name",
                           description="What it is for", icon="An emoji", category="e.g. cultivation, events, community",
                           rarity="How rare it is")
    @app_commands.choices(rarity=[app_commands.Choice(name=r.title(), value=r) for r in RARITIES])
    @app_commands.check(require_admin)
    async def create(self, i: discord.Interaction, key: str, name: app_commands.Range[str, 1, 60],
                     description: app_commands.Range[str, 0, 200] = "", icon: app_commands.Range[str, 1, 8] = "🏅",
                     category: app_commands.Range[str, 1, 30] = "general",
                     rarity: app_commands.Choice[str] | None = None):
        key = key.strip().lower()
        if not KEY_RE.match(key):
            return await i.response.send_message(
                "⚠️ The key must be 2 to 40 characters: lowercase letters, digits and dashes.", ephemeral=True)
        db = self.bot.db
        new_id = await db.create_achievement(key, name.strip(), description.strip(), icon.strip(),
                                             category.strip().lower(), rarity.value if rarity else "common", i.user.id)
        if new_id is None:
            return await i.response.send_message(f"⚠️ An achievement with the key `{key}` already exists.", ephemeral=True)
        await db.record_audit(i.user.id, "achievement.create", "achievement", key, name=name)
        await i.response.send_message(f"✅ Created {icon} **{name}** (`{key}`).", ephemeral=True)

    @achievement.command(name="list", description="List the achievements")
    async def list_(self, i: discord.Interaction):
        rows = await self.bot.db.list_achievements(active_only=False)
        if not rows:
            return await i.response.send_message("No achievements yet. Create one with `/achievement create`.", ephemeral=True)
        lines = [f"{r['icon']} **{r['name']}** `{r['key']}` · {r['rarity']} · {r['category']} · "
                 f"{r['holders']} holder(s){'' if r['active'] else ' · *disabled*'}" for r in rows]
        await i.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    @achievement.command(name="grant", description="Give a member an achievement")
    @app_commands.autocomplete(key=_keys)
    async def grant(self, i: discord.Interaction, member: discord.Member, key: str):
        a = await self.bot.db.grant_achievement(member.id, key, i.user.id)
        if a is None:
            return await i.response.send_message(
                "⚠️ Either that achievement doesn't exist / is disabled, or they already have it.", ephemeral=True)
        await self.bot.db.record_audit(i.user.id, "achievement.grant", "member", member.id, key=key)
        await i.response.send_message(f"🏅 {member.mention} received {a['icon']} **{a['name']}**.", ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none())

    @achievement.command(name="revoke", description="Take an achievement back (Sect Admin)")
    @app_commands.autocomplete(key=_keys)
    @app_commands.check(require_admin)
    async def revoke(self, i: discord.Interaction, member: discord.Member, key: str):
        ok = await self.bot.db.revoke_achievement(member.id, key)
        if ok:
            await self.bot.db.record_audit(i.user.id, "achievement.revoke", "member", member.id, key=key)
        await i.response.send_message("✅ Removed." if ok else "They don't have that achievement.", ephemeral=True)

    @achievement.command(name="disable", description="Hide an achievement from being granted (Sect Admin)")
    @app_commands.autocomplete(key=_keys)
    @app_commands.check(require_admin)
    async def disable(self, i: discord.Interaction, key: str, disabled: bool = True):
        ok = await self.bot.db.set_achievement_active(key, not disabled)
        if ok:
            await self.bot.db.record_audit(i.user.id, "achievement.disable" if disabled else "achievement.enable",
                                           "achievement", key)
        await i.response.send_message(("✅ Updated." if ok else "No such achievement."), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Achievements(bot))
