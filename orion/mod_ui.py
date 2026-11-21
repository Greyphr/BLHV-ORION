"""Moderation embeds and member-facing messages (pure: no database, no Discord I/O)."""
import discord

from . import config
from .services.moderation import format_duration

META = {  # action: (emoji, colour, past tense)
    "warn": ("⚠️", 0xF59E0B, "warned"), "note": ("📝", 0x64748B, "noted"), "timeout": ("🔇", 0xF97316, "timed out"),
    "untimeout": ("🔊", 0x10B981, "un-timed-out"), "kick": ("👢", 0xEF4444, "kicked"), "ban": ("🔨", 0xB91C1C, "banned"),
    "unban": ("🕊️", 0x10B981, "unbanned"),
}
SOURCE_NOTE = {"automod": "Automatic (auto-moderation)", "escalation": "Automatic (warning escalation)",
               "system": "Automatic (system)"}


def _ts(dt, style="F") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"


def who(uid) -> str:
    return f"<@{uid}> (`{uid}`)" if uid else "—"


def case_embed(c, *, show_moderator: bool = True) -> discord.Embed:
    emoji, color, _ = META.get(c["action"], ("•", config.COLOR_MAIN, c["action"]))
    e = discord.Embed(title=f"{emoji} Case #{c['id']} · {c['action'].title()}", color=color,
                      description=c["reason"] or "*No reason given.*", timestamp=c["created_at"])
    e.add_field(name="Member", value=who(c["target_id"]), inline=True)
    if show_moderator:
        e.add_field(name="Moderator", value=who(c["moderator_id"]) if c["moderator_id"]
                    else SOURCE_NOTE.get(c["source"], "Automatic"), inline=True)
    if c["duration_seconds"]:
        end = f" · ends {_ts(c['expires_at'], 'R')}" if c["expires_at"] else ""
        e.add_field(name="Duration", value=format_duration(c["duration_seconds"]) + end, inline=True)
    if c["revoked_at"]:
        e.add_field(name="Revoked", inline=False, value=f"{_ts(c['revoked_at'])}"
                    + (f" by {who(c['revoked_by'])}" if c["revoked_by"] else "")
                    + (f": {c['revoke_reason']}" if c["revoke_reason"] else ""))
    if c["source"] not in ("manual",) and c["moderator_id"]:
        e.set_footer(text=f"Source: {c['source']}")
    return e


def case_line(c, *, show_moderator: bool = True) -> str:
    emoji = META.get(c["action"], ("•",))[0]
    state = " ~~revoked~~" if c["revoked_at"] else ""
    by = f" · by <@{c['moderator_id']}>" if show_moderator and c["moderator_id"] else (" · auto" if show_moderator else "")
    dur = f" ({format_duration(c['duration_seconds'])})" if c["duration_seconds"] else ""
    reason = (c["reason"] or "—").replace("\n", " ")[:90]
    return f"`#{c['id']}` {emoji} **{c['action']}**{dur}{state} · {_ts(c['created_at'], 'd')}{by}\n└ {reason}"


def dm_text(action: str, guild_name: str, case_id: int | None, reason: str, duration_seconds: int | None = None,
            expires_at=None) -> str:
    _, _, past = META[action]
    head = f"You have been **{past}** in **{guild_name}**" + (f" (case #{case_id})." if case_id else ".")
    lines = [head, f"**Reason:** {reason or 'No reason given.'}"]
    if duration_seconds:
        lines.append(f"**Duration:** {format_duration(duration_seconds)}"
                     + (f" (until {_ts(expires_at)})" if expires_at else ""))
    if action in ("warn", "timeout", "kick", "ban"):
        lines.append("If you think this was a mistake, open a **General Support** ticket in the server and the staff will review it."
                     if action != "kick" else "You may rejoin, but please follow the Sect rules.")
    return "\n".join(lines)[:1900]


def report_embed(r, status_note: str | None = None) -> discord.Embed:
    color = {"open": 0xF59E0B, "reviewing": 0x38BDF8, "closed": 0x10B981, "dismissed": 0x64748B}[r["status"]]
    e = discord.Embed(title=f"🚨 Report #{r['id']} · {r['status'].title()}", color=color, description=r["reason"],
                      timestamp=r["created_at"])
    e.add_field(name="Reported member", value=who(r["target_id"]), inline=True)
    e.add_field(name="Reported by", value=who(r["reporter_id"]), inline=True)
    if r["message_link"]:
        e.add_field(name="Message", value=r["message_link"], inline=False)
    if r["handled_by"]:
        e.add_field(name="Handled by", value=who(r["handled_by"]), inline=True)
    if status_note:
        e.set_footer(text=status_note)
    return e
