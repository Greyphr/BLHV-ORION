import discord
from discord import app_commands

from . import config


def _has_role(member, role_id: int, role_name: str) -> bool:
    """By ID when one is configured, otherwise by name."""
    if role_id:
        return any(r.id == role_id for r in member.roles)
    return any(r.name == role_name for r in member.roles)


def admin_role(guild: discord.Guild):
    """The admin role object (by ID, falling back to the name), or None."""
    if config.ADMIN_ROLE_ID:
        return guild.get_role(config.ADMIN_ROLE_ID)
    return discord.utils.get(guild.roles, name=config.ADMIN_ROLE_NAME)


def staff_role_ids(db) -> set[int]:
    """Always-on staff roles from the config plus the ones picked in /admin -> Staff access."""
    return set(config.STAFF_ROLE_IDS) | set(db.get("staff_role_ids", []))


def is_admin(member) -> bool:
    return isinstance(member, discord.Member) and (
        member.id == config.OWNER_ID or member.guild.owner_id == member.id or _has_role(member, config.ADMIN_ROLE_ID, config.ADMIN_ROLE_NAME))


def is_xp_manager(member) -> bool:
    """Sect Patriarch and the Sect Admin role manage XP."""
    return is_admin(member)


def is_staff(member, db) -> bool:
    if is_admin(member):
        return True
    ids = staff_role_ids(db)
    return isinstance(member, discord.Member) and any(r.id in ids for r in member.roles)


async def require_admin(interaction: discord.Interaction) -> bool:
    if is_admin(interaction.user):
        return True
    raise app_commands.CheckFailure(f"Only members with the **{config.ADMIN_ROLE_NAME}** role can use this.")


async def require_staff(interaction: discord.Interaction) -> bool:
    if is_staff(interaction.user, interaction.client.db):
        return True
    raise app_commands.CheckFailure(
        f"Only **{config.ADMIN_ROLE_NAME}** and approved staff roles can use this.")


async def require_xp_manager(interaction: discord.Interaction) -> bool:
    if is_xp_manager(interaction.user):
        return True
    raise app_commands.CheckFailure("Only the Sect Patriarch and **%s** can manage XP." % config.ADMIN_ROLE_NAME)


def is_keys_manager(member) -> bool:
    """Sect Admin plus the roles in BETA_KEYS_MANAGER_ROLE_IDS: may run the beta key admin commands."""
    return is_admin(member) or (isinstance(member, discord.Member) and any(
        r.id in config.BETA_KEYS_MANAGER_ROLE_IDS for r in member.roles))


async def require_keys_manager(interaction: discord.Interaction) -> bool:
    if is_keys_manager(interaction.user):
        return True
    raise app_commands.CheckFailure(f"Only **{config.ADMIN_ROLE_NAME}** and the beta key manager roles can use this.")
