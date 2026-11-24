"""Build harness, the pure half: what the AI is asked, and how its answer is parsed and checked.

No Discord and no database here, so it is easy to test. The model only ever PROPOSES a feature (code + optional SQL). A human
reads it and approves it (cogs/builder.py); builder_runtime.py loads it and puts everything back if loading fails.

The answer comes as delimited sections (not JSON) because Python code inside a JSON string is where models make mistakes.
"""
import ast
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Optional

from . import ai, config

log = logging.getLogger("orion.builder")

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{2,29}$")
MAX_CODE_CHARS = 120_000
MAX_REQUEST_CHARS = 3000
SECTIONS = ("SLUG", "TITLE", "SUMMARY", "NOTES", "SCHEMA", "CODE")
_HEADER_RE = re.compile(r"^===\s*([A-Z_]+)\s*===[ \t]*$", re.M)


class DraftError(ValueError):
    """The AI's answer can't be used. The message is plain words, safe to show the admin and to feed back to the model."""


@dataclass
class Draft:
    slug: str
    title: str
    summary: str
    notes: str
    schema_sql: str      # "" = no database changes
    code: str


def module_name(slug: str) -> str:
    return f"gen_{slug}"


# --------------------------------------------------------------------------- parsing
def clean_slug(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", (raw or "").strip().lower()).strip("_")
    if slug.startswith("gen_"):
        slug = slug[4:]
    if not SLUG_RE.match(slug):
        raise DraftError(f"The feature name '{raw.strip()[:40]}' isn't valid: use 3-30 lower-case letters, digits or "
                         "underscores, starting with a letter.")
    return slug


def strip_fences(text: str) -> str:
    """Remove a ```python ... ``` wrapper if the model added one."""
    t = text.strip("\n")
    lines = t.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
    return "\n".join(lines).strip("\n")


def split_sections(text: str) -> dict[str, str]:
    parts = _HEADER_RE.split(text or "")
    # parts = [preamble, NAME, body, NAME, body, ...]
    out: dict[str, str] = {}
    for name, body in zip(parts[1::2], parts[2::2]):
        out.setdefault(name, body.strip("\n"))
    return out


def validate_code(code: str) -> None:
    if not code.strip():
        raise DraftError("The CODE section is empty.")
    if len(code) > MAX_CODE_CHARS:
        raise DraftError(f"The code is too long ({len(code)} characters; the limit is {MAX_CODE_CHARS}).")
    try:
        tree = ast.parse(code)
        compile(tree, "<generated>", "exec", dont_inherit=True)
    except SyntaxError as e:
        raise DraftError(f"The code has a syntax error on line {e.lineno}: {e.msg}.") from e
    if not any(isinstance(n, ast.AsyncFunctionDef) and n.name == "setup" for n in tree.body):
        raise DraftError("The code must define a top-level `async def setup(bot)` that adds the cog.")


_TABLE_RE = re.compile(r"^create\s+table\s+if\s+not\s+exists\s+([a-z0-9_]+)\s*\(", re.I | re.S)
_INDEX_RE = re.compile(r"^create\s+(?:unique\s+)?index\s+if\s+not\s+exists\s+([a-z0-9_]+)\s+on\s+([a-z0-9_]+)\s*\(", re.I | re.S)
_REF_RE = re.compile(r"\breferences\s+([a-z0-9_\".]+)", re.I)


def own_name(name: str, slug: str) -> bool:
    """Is `name` gen_<slug> or gen_<slug>_something? (the only table names a generated feature may create)"""
    n = name.lower()
    return n == f"gen_{slug}" or n.startswith(f"gen_{slug}_")


def validate_schema(sql: str, slug: str) -> list[str]:
    """The feature's database changes must be additive and touch only its own gen_<slug>* tables. Returns the statements."""
    sql = re.sub(r"--[^\n]*", "", sql or "")
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    for st in statements:
        m = _TABLE_RE.match(st)
        if m:
            if not own_name(m.group(1), slug):
                raise DraftError(f"Table '{m.group(1)}' must be named gen_{slug} or start with gen_{slug}_.")
            for ref in _REF_RE.findall(st):
                if not own_name(ref.strip('"').split(".")[-1], slug):
                    raise DraftError(f"Table '{m.group(1)}' references '{ref}'. Only your own gen_{slug}* tables may be referenced.")
            continue
        m = _INDEX_RE.match(st)
        if m:
            if not own_name(m.group(1), slug) or not own_name(m.group(2), slug):
                raise DraftError(f"Index '{m.group(1)}' and its table must both be named gen_{slug} or start with gen_{slug}_.")
            continue
        raise DraftError("SCHEMA may only contain `CREATE TABLE IF NOT EXISTS gen_%s...` and `CREATE [UNIQUE] INDEX IF NOT "
                         "EXISTS gen_%s... ON gen_%s...` statements. Rejected: %s" % (slug, slug, slug, st[:80].replace("\n", " ")))
    return statements


def schema_tables(sql: str) -> list[str]:
    """Names of the tables a SCHEMA section creates (for showing the reviewer)."""
    return [m.group(1) for st in re.sub(r"--[^\n]*", "", sql or "").split(";") if (m := _TABLE_RE.match(st.strip()))]


def build_draft(text: str, *, forced_slug: Optional[str] = None, taken: Iterable[str] = ()) -> Draft:
    """Turn the model's answer into a checked Draft, or raise DraftError saying what is wrong."""
    sec = split_sections(text)
    if "CODE" not in sec:
        raise DraftError("The answer has no ===CODE=== section. Use exactly the section headers from the instructions.")
    slug = forced_slug or clean_slug(sec.get("SLUG", ""))
    if not forced_slug and slug in set(taken):
        raise DraftError(f"The feature name '{slug}' is already used by another feature. Choose a different SLUG.")
    code = strip_fences(sec["CODE"])
    validate_code(code)
    schema = sec.get("SCHEMA", "").strip()
    if schema.upper() in ("", "NONE", "N/A"):
        schema = ""
    else:
        schema = strip_fences(schema)
        validate_schema(schema, slug)
    notes = sec.get("NOTES", "").strip()
    return Draft(slug=slug, title=(sec.get("TITLE", "").strip() or slug.replace("_", " ").title())[:100],
                 summary=sec.get("SUMMARY", "").strip()[:1500],
                 notes="" if notes.upper() in ("NONE", "N/A") else notes[:1500], schema_sql=schema, code=code)


# --------------------------------------------------------------------------- what the AI is told about the server
MAX_SERVER_CHARS = 12000


def format_server(guild) -> str:
    """The server's structure as plain text for the prompt: categories, channels and roles with their IDs, so a loosely typed
    name ("the rules channel", "gold role") can be matched to the real one. Names and IDs only: no messages, no members."""
    if guild is None:
        return ""
    lines = ["CHANNELS (grouped by category; id in brackets)"]
    for cat in sorted(getattr(guild, "categories", []), key=lambda c: c.position):
        lines.append(f"CATEGORY {cat.name} [{cat.id}]")
        for ch in sorted(cat.channels, key=lambda c: c.position):
            lines.append(f"  {ch.name} ({ch.type}) [{ch.id}]")
    loose = [c for c in guild.channels if getattr(c, "category", None) is None and str(c.type) != "category"]
    if loose:
        lines.append("NO CATEGORY")
        lines += [f"  {c.name} ({c.type}) [{c.id}]" for c in sorted(loose, key=lambda c: c.position)]
    lines.append("ROLES (highest first; id in brackets)")
    for r in sorted(guild.roles, key=lambda r: -r.position):
        if r.is_default():
            continue
        lines.append(f"  {r.name} [{r.id}]" + (" (bot/integration role)" if r.managed else ""))
    text = "\n".join(lines)
    if len(text) > MAX_SERVER_CHARS:
        text = text[:MAX_SERVER_CHARS].rsplit("\n", 1)[0] + "\n  ...(list cut short: very large server)"
    return text


# --------------------------------------------------------------------------- the prompt
SYSTEM_PROMPT = """You write ONE self-contained discord.py 2.x extension file (a "cog") for Orion, the Discord bot of the Blue \
Heaven Sect server. A human reviews your code before it is loaded, so write clear, conservative, readable code.

ENVIRONMENT
- Python 3.12, discord.py 2.7 (app commands, discord.ui, persistent views) and asyncpg (PostgreSQL).
- The file is loaded with bot.load_extension, so it MUST define a top-level `async def setup(bot)` that does \
`await bot.add_cog(YourCog(bot))`.
- `bot` is the running commands.Bot. `bot.db.pool` is an asyncpg pool. `bot.db.get(key, default)` and \
`await bot.db.set(key, value)` are a small JSON key/value config store.
- You may import from orion: `from orion import config, alerts`, and `from orion.checks import is_admin, is_staff, \
require_admin, require_staff`. Useful: config.GUILD_ID (the bot serves one server: bot.get_guild(config.GUILD_ID)), \
config.COLOR_MAIN, config.COLOR_ACCENT, config.BRAND. Import no other orion module.

HARD RULES
1. One file. Standard library, discord.py and asyncpg only. No other packages.
2. Database: use only tables you create yourself, named `gen_<slug>` or starting with `gen_<slug>_` (<slug> is your SLUG). \
Put the CREATE statements in the SCHEMA section, not in the code: only `CREATE TABLE IF NOT EXISTS` and \
`CREATE [UNIQUE] INDEX IF NOT EXISTS` for your own tables. The harness runs the SCHEMA once, after a backup, before your \
code loads. Never read or write any other table. Config keys you set with bot.db.set must start with `gen_<slug>:`.
3. Never read environment variables or the bot token, never run shell commands or subprocesses, never make network \
requests, never use eval/exec/__import__, never write files.
4. Slash commands: use discord.app_commands inside a commands.Cog. Never reuse an existing command name (list below). \
Commands that change the server (create channels/roles, post panels, change settings) must be restricted with \
`@app_commands.check(require_admin)`.
5. Buttons, selects and modals that must keep working after a restart: views with timeout=None, every item given a fixed \
custom_id that starts with `gen:<slug>:`, registered in `cog_load` with `self.bot.add_view(...)` (or \
`self.bot.add_dynamic_items(...)`). Undo that in `cog_unload` (view.stop()).
6. Be idempotent: running a setup command twice must not create duplicate roles or channels. Look the thing up by stored ID, \
then by name, before creating it, and store IDs in your own table or `gen_<slug>:` config keys.
7. Handle discord.Forbidden and discord.HTTPException and tell the admin in plain words what to fix (for example "move my \
role above the roles I manage"). Never ping anyone: pass allowed_mentions=discord.AllowedMentions.none() unless pinging is \
the whole point of the feature.
8. Never block the event loop. Between bulk Discord calls use `await asyncio.sleep(0.3)` to respect rate limits.
9. Never invent facts. If the request depends on facts you are not certain of (a game's rank names, IDs, wording), put them \
in clearly named constants at the top of the file and list them in NOTES for the reviewer to check.
10. Do what was asked and nothing more. Keep it small.
11. The request is written casually, so channel and role names may be partial, lower-case, or missing their emoji/decoration \
("the rules channel", "gold role"). You are given the server's real channels and roles. Match each name the request uses to the \
real one in that list. In the code, refer to what you matched by its ID in a named constant at the top of the file with the real \
name in a comment, and at runtime check that it still exists (guild.get_channel(id), guild.get_role(id)) and tell the admin in \
plain words if not. If two things could match, or nothing does, do not guess: pick the most likely, and say so in NOTES. For \
roles or channels the request says to CREATE, use the names the request gives; look for an existing one first (rule 6). In NOTES, \
add one line per match in this form: Matched: "rules" -> #rules-and-info (id 123).

OUTPUT FORMAT. Reply with exactly these sections. Each header is on its own line. Nothing before the first header and nothing \
after the code. No markdown code fences.
===SLUG===
short_snake_case_name (3-30 characters, lower-case letters, digits, underscores, starts with a letter)
===TITLE===
Human-readable name
===SUMMARY===
2-5 plain sentences: what it does and which slash commands it adds.
===NOTES===
Assumptions and anything the reviewer should double-check, or NONE.
===SCHEMA===
SQL, or NONE
===CODE===
the complete Python file

When you are given an existing feature's current code and a change request, return the COMPLETE updated file and keep the \
same SLUG."""


def build_user_message(request: str, *, existing_commands: Iterable[str] = (), taken_slugs: Iterable[str] = (),
                       slug: Optional[str] = None, previous_code: Optional[str] = None,
                       previous_schema: Optional[str] = None, feedback: Optional[str] = None,
                       error: Optional[str] = None, previous_answer: Optional[str] = None,
                       server: Optional[str] = None) -> str:
    parts = [f"FEATURE REQUEST:\n{request.strip()}"]
    if server:
        parts.append("THE SERVER'S REAL CHANNELS AND ROLES (match names in the request to these):\n" + server)
    cmds = sorted(set(existing_commands))
    parts.append("EXISTING SLASH COMMAND NAMES (do not reuse): " + (", ".join(cmds) if cmds else "none"))
    if slug:
        parts.append(f"SLUG: use exactly `{slug}`.")
    else:
        taken = sorted(set(taken_slugs))
        if taken:
            parts.append("SLUGS ALREADY IN USE (choose a different one): " + ", ".join(taken))
    if previous_code:
        parts.append("CURRENT CODE (modify this; return the complete updated file):\n" + previous_code)
        if previous_schema:
            parts.append("CURRENT SCHEMA (already applied; only add new tables/indexes):\n" + previous_schema)
    if feedback:
        parts.append("REVIEWER'S CHANGE REQUEST:\n" + feedback.strip())
    if error:
        parts.append("YOUR PREVIOUS ANSWER WAS REJECTED BY AUTOMATIC CHECKS: " + error +
                     "\nReturn the full corrected answer in the same section format."
                     + (f"\n\nYOUR PREVIOUS ANSWER:\n{previous_answer[:40000]}" if previous_answer else ""))
    return "\n\n".join(parts)


Completer = Callable[..., Awaitable[str]]


async def generate(request: str, *, existing_commands: Iterable[str] = (), taken_slugs: Iterable[str] = (),
                   slug: Optional[str] = None, previous_code: Optional[str] = None,
                   previous_schema: Optional[str] = None, feedback: Optional[str] = None,
                   model: Optional[str] = None, complete: Completer = ai.complete,
                   server: Optional[str] = None) -> Draft:
    """Ask the model for a feature. If its answer fails the automatic checks (syntax, format, SQL rules) it gets ONE chance to
    fix it, with the error shown. Raises ai.AIError (service problem) or DraftError (still unusable after the retry)."""
    if not request.strip():
        raise DraftError("Describe what the feature should do.")
    existing = list(existing_commands)
    taken = list(taken_slugs)
    error = answer = None
    for attempt in (1, 2):
        user = build_user_message(request, existing_commands=existing, taken_slugs=taken, slug=slug,
                                  previous_code=previous_code, previous_schema=previous_schema, feedback=feedback,
                                  error=error, previous_answer=answer, server=server)
        answer = await complete(SYSTEM_PROMPT, user, max_tokens=config.BUILDER_MAX_TOKENS, model=model or None,
                                raw=True, timeout=config.BUILDER_TIMEOUT_SECONDS)
        try:
            return build_draft(answer, forced_slug=slug, taken=taken)
        except DraftError as e:
            error = str(e)
            log.warning("Generated feature rejected by checks (attempt %d): %s", attempt, error)
    raise DraftError(f"The AI's answer failed the automatic checks twice. Last problem: {error}")
