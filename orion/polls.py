"""Poll helpers that need no database: option parsing and the card embed."""
from typing import Mapping, Optional, Sequence

import discord

from . import config

MIN_OPTIONS, MAX_OPTIONS, MAX_OPTION_LEN, MAX_QUESTION_LEN = 2, 10, 80, 250
MIN_DURATION, MAX_DURATION = 60, 30 * 86400


class PollError(ValueError):
    """A problem with the poll the admin asked for, phrased for the admin."""


def parse_options(text: str) -> list[str]:
    """'Yes | No | Maybe' (or one option per line) -> ['Yes', 'No', 'Maybe']."""
    out: list[str] = []
    for raw in (text or "").replace("\n", "|").split("|"):
        raw = raw.strip()
        if raw:
            out.append(raw)
    if len(out) < MIN_OPTIONS:
        raise PollError(f"Give at least {MIN_OPTIONS} options, separated by `|` (for example `Yes | No`).")
    if len(out) > MAX_OPTIONS:
        raise PollError(f"A poll can have at most {MAX_OPTIONS} options (you gave {len(out)}).")
    for o in out:
        if len(o) > MAX_OPTION_LEN:
            raise PollError(f"Each option can be at most {MAX_OPTION_LEN} characters (“{o[:30]}…” is {len(o)}).")
    if len({o.casefold() for o in out}) != len(out):
        raise PollError("Two of the options are the same. Every option must be different.")
    return out


def bar(share: float, width: int = 12) -> str:
    filled = max(0, min(width, round(share * width)))
    return "▓" * filled + "░" * (width - filled)


def build_embed(poll: Mapping, counts: Mapping[int, int], *, ended: bool = False,
                banner_url: Optional[str] = None) -> discord.Embed:
    options: Sequence[str] = poll["options"]
    total = sum(counts.get(i, 0) for i in range(len(options)))
    top = max((counts.get(i, 0) for i in range(len(options))), default=0)
    lines = []
    for i, name in enumerate(options):
        n = counts.get(i, 0)
        share = n / total if total else 0
        crown = " 🏆" if ended and top > 0 and n == top else ""
        lines.append(f"**{i + 1}. {name}**{crown}\n{bar(share)}  **{n}** ({share:.0%})")
    e = discord.Embed(color=config.COLOR_MAIN, title=f"📊 {poll['question']}"[:256], description="\n\n".join(lines))
    private = poll["anonymous"]
    kind = ("🔒 Private poll: nobody can see who voted, only staff" if private
            else "🌐 Public poll: anyone can see who voted (press **Who voted**)")
    bits = [f"{total} vote{'s' if total != 1 else ''}"]
    if ended:
        bits.insert(0, "Final results")
    elif poll["ends_at"]:
        bits.append(f"ends <t:{int(poll['ends_at'].timestamp())}:R>")
    else:
        bits.append("open until staff close it")
    if not ended and not poll["allow_change"]:
        bits.append("votes are final")
    roles = poll.get("eligible_role_ids") or []
    if roles:
        bits.append("vote needs " + " or ".join(f"<@&{r}>" for r in roles))
    e.add_field(name="​", value=" · ".join(bits), inline=False)
    if banner_url:
        e.set_image(url=banner_url)
    e.set_footer(text=kind + (f" · Poll #{poll['id']}" if poll.get("id") else ""))
    return e
