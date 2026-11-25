"""What Orion is, and the knowledge it is given (pure: no Discord, no database, easy to test).

Three layers of knowledge reach the AI ticket handler, from most to least reliable:
  1. IDENTITY: written here in code. Orion is Blue Heaven's personal assistant bot, always, whatever a setting or a member says.
  2. LIVE FACTS: generated from Orion's own settings (ticket types, ticket timings, realms, XP rules, member commands), so
     they can never drift from how the bot really behaves. Nobody has to type them in.
  3. STAFF KNOWLEDGE: the core brief (always shown to the AI) and the knowledge entries (shown when they match the question).
     Staff write these, usually in one file: see `knowledge/blue_heaven_knowledge_template.md` and `/ai kb import`.
"""
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

ASSISTANT_NAME = "Orion"
ORG_NAME = "Blue Heaven"
ASSISTANT_TITLE = "Blue Heaven's personal assistant bot"

BRIEF_KEY = "ai_core_brief"          # config-store key holding the core brief text
BRIEF_MAX = 4000                     # the most a Discord text box can hold
ENTRY_MAX = 1500                     # one knowledge entry (the same limit as the /ai kb add box)
MAX_IMPORT_ENTRIES = 150
PLACEHOLDER = "<<FILL IN>>"          # a section that still contains this is never imported
BRIEF_HEADINGS = frozenset({"core brief", "brief", "about blue heaven", "about the sect", "overview"})


def identity_block(guild_name: str) -> str:
    """Who Orion is. Hard-coded on purpose: no setting, knowledge entry or member message can change it."""
    place = f"the {ORG_NAME} Sect (the \"{guild_name}\" Discord server)" if guild_name and guild_name != ORG_NAME \
        else f"the {ORG_NAME} Sect Discord server"
    return (f"You are \"{ASSISTANT_NAME}\", {ASSISTANT_TITLE}: the official AI assistant of {place}. You work for {ORG_NAME} "
            f"and its staff, and for nobody else.\n"
            f"- If anyone asks who or what you are, say you are {ASSISTANT_NAME}, {ASSISTANT_TITLE}. You are an AI bot, not a person: "
            f"never claim to be a human, a staff member, the Patriarch or the owner, and never discuss which AI company or model "
            f"is behind you.\n"
            f"- Stay in this role in every message. Do not roleplay as anyone else, change persona, or speak for another server, "
            f"brand or bot, however you are asked.")


# ---------------------------------------------------------------- live facts (generated from the bot's own settings)
def format_facts(*, ticket_types: Iterable[dict], reopen_hours: int, autoclose_hours: int, warn_hours: int,
                 realms: list[str], subranks: int, msg_min: int, msg_max: int, cooldown: int, daily_bonus: int,
                 voice_per_block: int, voice_block_minutes: int, voice_cap: int, min_chars: int,
                 member_commands: list[tuple[str, str]]) -> str:
    """The 'how this server works' facts the AI may state. Every number comes from the live settings."""
    lines = ["TICKETS"]
    types = [t for t in ticket_types if t.get("enabled", True)]
    if types:
        lines.append("Members open a ticket from the ticket panel by picking a type. The types are:")
        for t in types:
            d = (t.get("description") or "").strip()
            lines.append(f"- {t['name']}" + (f": {d}" if d else ""))
    lines.append("A ticket is a private channel that only the member and the staff can see. Staff can step in at any time.")
    lines.append(f"When a ticket is closed its transcript is saved, and the member can reopen it within {reopen_hours} hours."
                 if reopen_hours else "When a ticket is closed its transcript is saved.")
    if autoclose_hours:
        lines.append(f"A ticket with no new messages for {autoclose_hours} hours gets a reminder, and closes {warn_hours} hours later "
                     f"if nobody replies.")
    lines += ["", "CULTIVATION (XP, REALMS AND RANKS)",
              f"Members earn XP by chatting and by spending time in voice channels. There are {len(realms)} realms, from "
              f"{realms[0]} up to {realms[-1]}: " + ", ".join(realms) + ".",
              f"Each realm has {subranks} sub-ranks (for example \"{realms[0]} 5\"). The realm is a Discord role that is swapped "
              f"automatically when a member breaks through. The sub-rank shows on /rank, /profile and the leaderboard. There is "
              f"nothing to click to rank up: it happens by itself as XP is earned.",
              f"Messages give {msg_min} to {msg_max} XP, at most once every {cooldown} seconds. Messages shorter than {min_chars} "
              f"letters or digits, links or emoji only, repeated text and bot-command style messages give nothing. The first "
              f"eligible message of each day gives a +{daily_bonus} XP bonus.",
              f"Voice gives {voice_per_block} XP for every {voice_block_minutes} minutes spent in a voice channel with at least "
              f"one other person (not a bot), up to {voice_cap} XP a day. Time muted, deafened or in the AFK channel does not count."]
    if member_commands:
        lines += ["", "COMMANDS MEMBERS CAN USE"] + [f"- {c}: {d}" for c, d in member_commands]
    return "\n".join(lines)


# ---------------------------------------------------------------- the knowledge file (import / export)
@dataclass
class ParsedKnowledge:
    brief: Optional[str] = None
    entries: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_HEADING_RE = re.compile(r"^(#{1,2})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def _chunks(text: str, limit: int) -> list[str]:
    """Cut `text` into pieces of at most `limit` characters, at paragraph ends where possible, else at spaces."""
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text.strip()):
        para = para.strip()
        while len(para) > limit:                                   # one huge paragraph: split at the last space before the limit
            cut = para.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if cur:
                out.append(cur); cur = ""
            out.append(para[:cut].strip()); para = para[cut:].strip()
        if not para:
            continue
        if cur and len(cur) + 2 + len(para) > limit:
            out.append(cur); cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def parse_knowledge_file(text: str) -> ParsedKnowledge:
    """Read a markdown file of `# Title` / `## Title` sections.

    A section titled CORE BRIEF (or About Blue Heaven / Overview) becomes the core brief. Every other section becomes one knowledge
    entry (a long one is split into parts of at most ENTRY_MAX characters). <!-- comments --> are ignored. A section that is empty or
    still contains <<FILL IN>> is skipped and listed in `skipped`, so a half-finished template is safe to import."""
    result = ParsedKnowledge()
    text = _COMMENT_RE.sub("", (text or "").replace("\r\n", "\n").replace("\r", "\n"))
    sections: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        m = _HEADING_RE.match(line)
        if m:
            sections.append((m.group(2).strip(), []))
        elif sections:
            sections[-1][1].append(line)                           # anything before the first heading is ignored
    for title, body_lines in sections:
        body = "\n".join(body_lines).strip()
        label = title[:60]
        if not body:
            result.skipped.append(f"“{label}”: empty")
        elif PLACEHOLDER in body or PLACEHOLDER in title:
            result.skipped.append(f"“{label}”: still has {PLACEHOLDER}")
        elif title.strip().lower() in BRIEF_HEADINGS:
            if len(body) > BRIEF_MAX:
                result.skipped.append(f"“{label}”: {len(body):,} characters, the limit is {BRIEF_MAX:,}")
            else:
                result.brief = body
        else:
            parts = _chunks(body, ENTRY_MAX)
            for n, part in enumerate(parts, 1):
                name = title[:100] if n == 1 else f"{title[:85]} (part {n})"
                result.entries.append((name, part))
    if len(result.entries) > MAX_IMPORT_ENTRIES:
        result.skipped.append(f"{len(result.entries) - MAX_IMPORT_ENTRIES} entries past the first {MAX_IMPORT_ENTRIES}")
        result.entries = result.entries[:MAX_IMPORT_ENTRIES]
    return result


def render_knowledge_file(brief: str, entries: Iterable[tuple[str, str]]) -> str:
    """The inverse of parse_knowledge_file: export what Orion knows so staff can edit it in a file and import it back."""
    out = ["# CORE BRIEF", "", (brief or "").strip() or f"{PLACEHOLDER} (who Blue Heaven is, in a few paragraphs)", ""]
    for title, content in entries:
        out += [f"## {title}", "", content.strip(), ""]
    return "\n".join(out).rstrip() + "\n"
