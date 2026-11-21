"""Profile embed (pure: takes the dict from services.profiles.get_profile)."""
import discord

from . import config
from .levels import rank_name

RARITY_MARK = {"legendary": "🟡", "epic": "🟣", "rare": "🔵", "uncommon": "🟢", "common": "⚪"}
KIND_ICON = {"event": "🎉", "scrim": "⚔️", "tournament": "🏆"}
MAX_ACHIEVEMENTS_SHOWN = 12


def _ts(dt, style="D") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"


def profile_embed(data: dict, member: discord.abc.User, *, staff_view: bool = False) -> discord.Embed:
    e = discord.Embed(title=f"{member.display_name}'s profile", color=config.COLOR_MAIN)
    e.set_thumbnail(url=member.display_avatar.url)

    pos = f"#{data['position']}" if data["position"] else "Unranked"
    e.add_field(name="Realm", value=f"**{data['realm']}**", inline=True)
    e.add_field(name="Rank", value=f"**{data['rank']}**", inline=True)
    e.add_field(name="Sect position", value=f"**{pos}**", inline=True)
    if data["need"]:
        nxt = rank_name(data["level"] + 1)
        e.add_field(name="Total XP · progress", inline=False,
                    value=f"**{data['xp']:,}** XP · {data['percent']}% of the way to **{nxt}** "
                          f"({data['need'] - data['into']:,} XP to go)")
    else:
        e.add_field(name="Total XP", value=f"**{data['xp']:,}** · highest rank reached 👑", inline=False)

    a = data["activity"]
    when = []
    if data["joined_at"]:
        when.append(f"Joined the Sect {_ts(data['joined_at'])}")
    if data["first_seen"]:
        when.append(f"First cultivated {_ts(data['first_seen'])}")
    e.add_field(name="Activity", inline=False, value="\n".join(when + [
        f"{a['messages']:,} XP-earning messages · {a['voice_hours']:,.1f}h voice",
        f"This week **{a['week_xp']:,}** XP · this month **{a['month_xp']:,}** XP"]))

    ach = data["achievements"]
    if ach:
        shown = [f"{x['icon']} {x['name']} {RARITY_MARK.get(x['rarity'], '')}" for x in ach[:MAX_ACHIEVEMENTS_SHOWN]]
        more = f"\n…and {len(ach) - MAX_ACHIEVEMENTS_SHOWN} more" if len(ach) > MAX_ACHIEVEMENTS_SHOWN else ""
        e.add_field(name=f"Achievements ({len(ach)})", value="\n".join(shown) + more, inline=False)
    else:
        e.add_field(name="Achievements", value="*None yet.*", inline=False)

    ev = data["events"]
    lines = [f"**{ev['registered']}** joined · **{ev['attended']}** attended"]
    for r in ev["recent"]:
        tag = " ⏳ waitlist" if r["waitlisted"] else (" ✅ attended" if r["attended"] else "")
        lines.append(f"{KIND_ICON.get(r['kind'], '🎉')} {r['title']} · {_ts(r['starts_at'])}{tag}")
    e.add_field(name="Events", value="\n".join(lines)[:1000], inline=False)

    if staff_view and "staff" in data:
        hist = data["staff"]["progression"]
        text = "\n".join(
            f"{_ts(h['at'], 'd')} {rank_name(h['old_level'])} → **{rank_name(h['new_level'])}** "
            f"({h['source']}{' by <@%s>' % h['actor_id'] if h['actor_id'] else ''})" for h in hist) or "*No progression recorded.*"
        e.add_field(name="🛡️ Staff: recent progression", value=text[:1000], inline=False)
        mod = data["staff"].get("moderation")
        if mod is not None:
            parts = [f"{v['total']} {k}" for k, v in sorted(mod["by_action"].items())]
            e.add_field(name="🛡️ Staff: moderation record", inline=False,
                        value=(f"**{mod['active_warns']}** active warning(s) · " + ", ".join(parts)
                               + f"\nUse `/cases` for details.") if mod["total"] else "Clean record.")
        e.set_footer(text=f"User ID {data['member_id']}")
    else:
        e.set_footer(text=config.BRAND)
    return e
