"""/profile: the member's Sect profile (cultivation, activity, achievements, events)."""
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import checks
from ..profile_ui import profile_embed
from ..services import profiles

log = logging.getLogger("orion.profile")


class Profile(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="profile", description="Show your Sect profile (or another member's)")
    @app_commands.describe(member="Whose profile to show (default: you)")
    async def profile(self, i: discord.Interaction, member: discord.Member | None = None):
        target = member or i.user
        # Staff looking at somebody else also see the progression history, so that reply is private.
        staff_view = target.id != i.user.id and checks.is_staff(i.user, self.bot.db)
        await i.response.defer(ephemeral=staff_view)
        data = await profiles.get_profile(self.bot.db, target.id, joined_at=getattr(target, "joined_at", None),
                                          include_staff=staff_view)
        await i.followup.send(embed=profile_embed(data, target, staff_view=staff_view))


async def setup(bot):
    await bot.add_cog(Profile(bot))
