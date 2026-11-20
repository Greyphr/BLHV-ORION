import asyncio
import logging

import discord

from . import alerts
from .levels import LEVEL_ROLE_NAMES, LEVEL_ROLES, ROLE_COLORS, level_from_xp, role_name_for_level

log = logging.getLogger("orion.roles")


class RoleSyncError(Exception):
    """Raised (only in strict mode) when Discord refuses a role change."""


def hierarchy_problem(guild: discord.Guild, role: discord.Role) -> str | None:
    """Why the bot can't hand out `role`, in plain words (None = it can)."""
    me = guild.me
    if not me.guild_permissions.manage_roles:
        return "I'm missing the **Manage Roles** permission."
    if role.position >= me.top_role.position:
        return (f"my role (**{me.top_role.name}**) is not above **{role.name}**. "
                "Drag my role above the level roles in Server Settings → Roles.")
    return None


async def get_or_create_role(guild: discord.Guild, name: str) -> discord.Role:
    role = next((x for x in guild.roles if x.name == name and not x.managed), None)
    if role is None:
        role = await guild.create_role(
            name=name, colour=discord.Colour(ROLE_COLORS.get(name, 0x7C3AED)),
            reason="Orion level role")
    return role


async def ensure_level_roles(guild: discord.Guild) -> dict[str, discord.Role]:
    return {name: await get_or_create_role(guild, name) for _, name in LEVEL_ROLES}


async def sync_member_role(member: discord.Member, level: int, strict: bool = False) -> bool:
    """Make the member wear exactly one level role. Returns True if anything changed.
    strict=True raises RoleSyncError instead of only logging, so commands can tell the admin."""
    target = await get_or_create_role(member.guild, role_name_for_level(level))
    stale = [r for r in member.roles if r.name in LEVEL_ROLE_NAMES and not r.managed and r != target]
    changed = False
    try:
        if target not in member.roles:
            await member.add_roles(target, reason="Orion level role")
            changed = True
        if stale:
            await member.remove_roles(*stale, reason="Orion level role replaced")
            changed = True
    except discord.Forbidden:
        log.warning("Missing permission / role hierarchy problem while updating %s. "
                    "Move the bot's role ABOVE the level roles.", member)
        if strict:
            raise RoleSyncError(hierarchy_problem(member.guild, target) or "Discord refused (Missing Permissions).")
    except discord.HTTPException as e:
        log.exception("Failed updating roles for %s", member)
        if strict:
            raise RoleSyncError(f"Discord error: {e.text or e}")
    return changed


async def sync_and_alert(bot, member: discord.Member, level: int) -> bool:
    """Like sync_member_role, but if Discord refuses, tell the staff alerts channel."""
    try:
        return await sync_member_role(member, level, strict=True)
    except RoleSyncError as e:
        await alerts.alert_staff(bot, member.guild,
                                 f"I couldn't update the level role for {member.mention}: {e}", key=str(e))
        return False


async def resync_all(guild: discord.Guild, db) -> tuple[int, str | None]:
    """Give every human member the level role that matches their stored XP (no row = base role).
    Stops at the first refusal. Returns (members_changed, problem_or_None)."""
    changed = 0
    for m in list(guild.members):
        if m.bot:
            continue
        row = await db.get_user(m.id)
        try:
            if await sync_member_role(m, level_from_xp(row["xp"]) if row else 0, strict=True):
                changed += 1
                await asyncio.sleep(0.3)  # be gentle with Discord's rate limits
        except RoleSyncError as e:
            return changed, str(e)
    return changed, None


async def assign_base_role(guild: discord.Guild) -> tuple[int, int, str | None]:
    """Give every human member who has NO level role yet the base role (Earth King).
    Members who already wear a level role are left alone. Returns (given, failed, problem)."""
    base = await get_or_create_role(guild, role_name_for_level(0))
    problem = hierarchy_problem(guild, base)
    if problem:
        return 0, 0, problem
    given = failed = 0
    for m in list(guild.members):
        if m.bot or any(x.name in LEVEL_ROLE_NAMES and not x.managed for x in m.roles):
            continue
        try:
            await m.add_roles(base, reason="Orion setup: base role")
            given += 1
        except discord.HTTPException:
            failed += 1
        await asyncio.sleep(0.3)  # be gentle with Discord's rate limits
    return given, failed, None
