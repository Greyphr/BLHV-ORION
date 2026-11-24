"""/build: describe a feature, the AI writes it, you read the code in Discord and approve it. Sect Patriarch only.

Flow: /build new -> the AI writes a cog -> a private review message with the code attached and Approve / Request changes /
Reject buttons -> on Approve the harness (builder_runtime.py) snapshots the database if the feature needs new tables, loads
the code live and, if anything fails, puts the previous version back. Every version is kept: /build rollback.
"""
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import ai, builder, config
from ..builder import DraftError, module_name, schema_tables
from ..checks import is_patriarch, require_patriarch

log = logging.getLogger("orion.builder.cog")

STATUS_ICON = {"pending": "🕓", "active": "✅", "superseded": "📦", "rejected": "🚫", "failed": "❌"}


# ------------------------------------------------------------------ helpers
def runtime(bot):
    return getattr(bot, "builder", None)


def existing_command_names(bot, exclude_module: str | None = None) -> list[str]:
    """Top-level slash command names already in use (a feature being modified may keep its own)."""
    return [c.name for c in bot.tree.get_commands() if exclude_module is None or getattr(c, "module", None) != exclude_module]


def review_embed(b, *, pending: bool) -> discord.Embed:
    e = discord.Embed(title=f"🛠️ {b['title']} · v{b['version']}", description=b["summary"] or "No summary given.",
                      color=config.COLOR_ACCENT)
    e.add_field(name="Feature", value=f"`{b['slug']}` · build #{b['id']} · {STATUS_ICON.get(b['status'], '')} {b['status']}",
                inline=False)
    if b["notes"]:
        e.add_field(name="Check these (the AI's assumptions)", value=b["notes"][:1000], inline=False)
    if b["schema_sql"]:
        tables = ", ".join(f"`{t}`" for t in schema_tables(b["schema_sql"])) or "indexes only"
        e.add_field(name="Database changes", inline=False,
                    value=f"New: {tables}\nA data snapshot of the whole database is taken before they are created.")
    else:
        e.add_field(name="Database changes", value="None", inline=False)
    e.add_field(name="Asked for", value=b["request"][:500], inline=False)
    if b["error"]:
        e.add_field(name="Last error", value=f"`{b['error'][:500]}`", inline=False)
    e.set_footer(text="Read the attached file first. Approve loads it into the live bot straight away." if pending
                 else "Review copy. This build is no longer waiting for a decision.")
    return e


def review_files(b) -> list[discord.File]:
    files = [discord.File(io.BytesIO(b["code"].encode()), filename=f"{b['slug']}_v{b['version']}.py")]
    if b["schema_sql"]:
        files.append(discord.File(io.BytesIO(b["schema_sql"].encode()), filename=f"{b['slug']}_v{b['version']}_schema.sql"))
    return files


def review_payload(b) -> dict:
    pending = b["status"] == "pending"
    kw = {"embed": review_embed(b, pending=pending), "files": review_files(b)}
    if pending:
        view = discord.ui.View(timeout=None)
        for item in (ApproveButton(b["id"]), ChangesButton(b["id"]), RejectButton(b["id"])):
            view.add_item(item)
        kw["view"] = view
    return kw


async def create_build(bot, user_id: int, request: str, *, base=None, feedback: str | None = None):
    """Ask the AI for a new feature (base=None) or a new version of one, store it as pending, return the stored row."""
    db = bot.db
    draft = await builder.generate(
        request, existing_commands=existing_command_names(bot, module_name(base["slug"]) if base else None),
        taken_slugs=() if base else await db.bb_slugs(), slug=base["slug"] if base else None,
        previous_code=base["code"] if base else None, previous_schema=base["schema_sql"] if base else None,
        feedback=feedback, model=config.BUILDER_MODEL or None,
        server=builder.format_server(bot.get_guild(config.GUILD_ID)))
    shown = (f"Changes: {feedback}" if feedback and base is not None and request == base["request"] else request)
    bid, _ = await db.bb_add(draft.slug, draft.title, shown, draft.summary, draft.notes, draft.code, draft.schema_sql,
                             config.BUILDER_MODEL or config.AI_MODEL, user_id)
    await db.record_audit(user_id, "build.draft", "builder_build", bid, slug=draft.slug)
    return await db.bb_get(bid)


def plain_error(e: Exception) -> str:
    if isinstance(e, ai.AIError):
        return f"❌ The AI service failed: {e}"
    if isinstance(e, DraftError):
        return f"⚠️ I couldn't get usable code from the AI: {e}\nTry rewording the request, or giving more detail."
    log.exception("Build generation crashed", exc_info=e)
    return "❌ Something went wrong while generating. Check the bot log."


# ------------------------------------------------------------------ review buttons
class _PatriarchOnly:
    """Mixin: a review button answers only to the Sect Patriarch, even if the review message leaks."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_patriarch(interaction.user):
            return True
        await interaction.response.send_message("Only the Sect Patriarch can use the build tools.", ephemeral=True)
        return False


class ApproveButton(_PatriarchOnly, discord.ui.DynamicItem[discord.ui.Button], template=r"build:approve:(?P<id>[0-9]+)"):
    def __init__(self, bid: int):
        super().__init__(discord.ui.Button(label="Approve & load", emoji="✅", style=discord.ButtonStyle.success,
                                           custom_id=f"build:approve:{bid}"))
        self.bid = bid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, i: discord.Interaction):
        bot, rt = i.client, runtime(i.client)
        await i.response.edit_message(view=None)             # buttons vanish at once: no double approval
        b = await bot.db.bb_get(self.bid)
        if rt is None or b is None or b["status"] != "pending":
            return await i.followup.send(f"Build #{self.bid} was already decided"
                                         f"{' (' + b['status'] + ')' if b else ''}.", ephemeral=True)
        await i.followup.send("⏳ Loading it into the bot…", ephemeral=True)
        res = await rt.activate(self.bid, i.user.id)
        text = res.message
        if not res.ok:
            text = f"❌ **Not loaded.** {text}\nUse `/build modify feature:{b['slug']}` to have the AI fix it."
        await i.followup.send(text[:1900], ephemeral=True)


class RejectButton(_PatriarchOnly, discord.ui.DynamicItem[discord.ui.Button], template=r"build:reject:(?P<id>[0-9]+)"):
    def __init__(self, bid: int):
        super().__init__(discord.ui.Button(label="Reject", emoji="🚫", style=discord.ButtonStyle.danger,
                                           custom_id=f"build:reject:{bid}"))
        self.bid = bid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, i: discord.Interaction):
        await i.response.edit_message(view=None)
        ok = await i.client.db.bb_claim_pending(self.bid, "rejected")
        if ok:
            await i.client.db.record_audit(i.user.id, "build.reject", "builder_build", self.bid)
        await i.followup.send("🚫 Rejected. Nothing was loaded." if ok else f"Build #{self.bid} was already decided.",
                              ephemeral=True)


class ChangesModal(discord.ui.Modal, title="What should change?"):
    text = discord.ui.TextInput(label="Tell the AI what to change", style=discord.TextStyle.paragraph, max_length=1500)

    def __init__(self, bid: int):
        super().__init__()
        self.bid = bid

    async def on_submit(self, i: discord.Interaction):
        bot, rt = i.client, runtime(i.client)
        base = await bot.db.bb_get(self.bid)
        if rt is None or base is None or base["status"] != "pending":
            return await i.response.send_message("That build was already decided.", ephemeral=True)
        if rt.gen_lock.locked():
            return await i.response.send_message("I'm already writing another build. Try again in a minute.", ephemeral=True)
        await i.response.edit_message(view=None)
        await i.followup.send("⏳ Asking the AI for a revised version…", ephemeral=True)
        try:
            async with rt.gen_lock:
                new = await create_build(bot, i.user.id, base["request"], base=base, feedback=self.text.value.strip())
        except Exception as e:
            return await i.followup.send(plain_error(e) + f"\nThe draft is still waiting: `/build show build:{self.bid}`.",
                                         ephemeral=True)
        await bot.db.bb_claim_pending(self.bid, "rejected")        # the old draft is replaced
        await i.followup.send("Here is the revised version:", ephemeral=True, **review_payload(new))


class ChangesButton(_PatriarchOnly, discord.ui.DynamicItem[discord.ui.Button], template=r"build:changes:(?P<id>[0-9]+)"):
    def __init__(self, bid: int):
        super().__init__(discord.ui.Button(label="Request changes", emoji="✏️", style=discord.ButtonStyle.primary,
                                           custom_id=f"build:changes:{bid}"))
        self.bid = bid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, i: discord.Interaction):
        await i.response.send_modal(ChangesModal(self.bid))


# ------------------------------------------------------------------ the commands
class Build(commands.GroupCog, name="build", description="Have the AI build new bot features (Sect Patriarch only)"):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(ApproveButton, ChangesButton, RejectButton)

    async def cog_unload(self):
        self.bot.remove_dynamic_items(ApproveButton, ChangesButton, RejectButton)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_patriarch(interaction)

    @property
    def db(self):
        return self.bot.db

    async def _ready(self, i: discord.Interaction) -> bool:
        if runtime(self.bot) is None:
            await i.response.send_message("The build tools failed to start. Check the bot log.", ephemeral=True)
            return False
        return True

    async def _features(self, current: str):
        rows = await self.db.bf_list()
        cur = current.lower()
        return [app_commands.Choice(name=f"{r['title']} ({r['slug']})"[:100], value=r["slug"]) for r in rows
                if cur in r["slug"] or cur in r["title"].lower()][:25]

    async def _generate(self, i: discord.Interaction, request: str, *, base=None, feedback: str | None = None,
                        intro: str = ""):
        rt = runtime(self.bot)
        if not ai.is_configured():
            return await i.response.send_message("❌ Add `AI_API_KEY` to `.env` and restart the bot first.", ephemeral=True)
        if rt.gen_lock.locked():
            return await i.response.send_message("I'm already writing another build. Try again in a minute.", ephemeral=True)
        await i.response.defer(ephemeral=True, thinking=True)
        try:
            async with rt.gen_lock:
                b = await create_build(self.bot, i.user.id, request, base=base, feedback=feedback)
        except Exception as e:
            return await i.followup.send(plain_error(e), ephemeral=True)
        if base is not None and base["status"] == "pending":
            await self.db.bb_claim_pending(base["id"], "rejected")
        await i.followup.send(intro or "Here is what the AI wrote. Nothing is loaded until you press **Approve**.",
                              ephemeral=True, **review_payload(b))

    # ---- create / change
    @app_commands.command(name="new", description="Describe a feature; the AI writes it for you to review")
    @app_commands.describe(request="What should it do? Be specific (channels, roles, commands, who can use them)")
    async def new(self, i: discord.Interaction, request: app_commands.Range[str, 10, builder.MAX_REQUEST_CHARS]):
        if await self._ready(i):
            await self._generate(i, request.strip())

    @app_commands.command(name="modify", description="Ask the AI to change a feature that already exists")
    @app_commands.describe(feature="The feature to change", request="What should be different?")
    async def modify(self, i: discord.Interaction, feature: str,
                     request: app_commands.Range[str, 5, builder.MAX_REQUEST_CHARS]):
        if not await self._ready(i):
            return
        builds = await self.db.bb_list(feature, 20)
        base = next((b for b in builds if b["status"] != "rejected"), builds[0] if builds else None)
        if base is None:
            return await i.response.send_message("There is no feature with that name.", ephemeral=True)
        text = request.strip()
        if base["status"] == "failed" and base["error"]:
            text += f"\n\n(Note: the previous version failed to load with: {base['error'][:400]})"
        extra = " The draft that was waiting is replaced by this one." if base["status"] == "pending" else ""
        await self._generate(i, base["request"], base=base, feedback=text,
                             intro=f"Here is v{base['version'] + 1} of **{base['title']}**.{extra} Nothing is loaded until you "
                                   "press **Approve**.")

    @modify.autocomplete("feature")
    async def _modify_ac(self, i: discord.Interaction, current: str):
        return await self._features(current)

    # ---- look
    @app_commands.command(name="list", description="Features built so far, and drafts waiting for your decision")
    async def list_(self, i: discord.Interaction):
        feats, pending = await self.db.bf_list(), await self.db.bb_pending()
        e = discord.Embed(title="🛠️ Built features", color=config.COLOR_MAIN)
        for f in feats:
            b = await self.db.bb_get(f["active_build_id"]) if f["active_build_id"] else None
            state = "✅ on" if f["enabled"] and runtime(self.bot) and runtime(self.bot).loaded(f["slug"]) else (
                "⚠️ should be on but isn't loaded" if f["enabled"] else "⏸️ off")
            e.add_field(name=f"{f['title']} (`{f['slug']}`)", inline=False,
                        value=f"{state} · running v{b['version'] if b else '—'}")
        if not feats:
            e.description = "Nothing has been approved yet. Start with `/build new`."
        if pending:
            e.add_field(name="Waiting for your decision", inline=False,
                        value="\n".join(f"🕓 #{p['id']} {p['title']} v{p['version']}: `/build show build:{p['id']}`"
                                        for p in pending)[:1000])
        await i.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="history", description="Every version of a feature")
    @app_commands.describe(feature="The feature")
    async def history(self, i: discord.Interaction, feature: str):
        rows = await self.db.bb_list(feature, 15)
        if not rows:
            return await i.response.send_message("There is no feature with that name.", ephemeral=True)
        lines = [f"{STATUS_ICON.get(r['status'], '•')} **v{r['version']}** (build #{r['id']}) <t:{int(r['created_at'].timestamp())}:R>"
                 f" · {r['status']}\n> {r['request'][:90].replace(chr(10), ' ')}" for r in rows]
        await i.response.send_message("\n".join(lines)[:1950], ephemeral=True)

    @history.autocomplete("feature")
    async def _history_ac(self, i: discord.Interaction, current: str):
        return await self._features(current)

    @app_commands.command(name="show", description="Open a build's code (with the buttons if it's still waiting)")
    @app_commands.describe(build="The build number")
    async def show(self, i: discord.Interaction, build: int):
        b = await self.db.bb_get(build)
        if b is None:
            return await i.response.send_message("There is no build with that number.", ephemeral=True)
        await i.response.send_message(ephemeral=True, **review_payload(b))

    # ---- switch / roll back
    @app_commands.command(name="rollback", description="Go back to an earlier working version of a feature")
    @app_commands.describe(feature="The feature", version="Which version (blank = the one before the current)")
    async def rollback(self, i: discord.Interaction, feature: str, version: int | None = None):
        if not await self._ready(i):
            return
        f = await self.db.bf_get(feature)
        if f is None:
            return await i.response.send_message("There is no feature with that name.", ephemeral=True)
        current = await self.db.bb_get(f["active_build_id"]) if f["active_build_id"] else None
        target = (await self.db.bb_find(feature, version) if version is not None
                  else await self.db.bb_previous_working(feature, current["version"] if current else 10 ** 9))
        if target is None or target["status"] not in ("active", "superseded"):
            return await i.response.send_message("There is no earlier version that has run before to go back to. "
                                                 "See `/build history`.", ephemeral=True)
        await i.response.defer(ephemeral=True, thinking=True)
        res = await runtime(self.bot).activate(target["id"], i.user.id, run_schema=False)
        await self.db.record_audit(i.user.id, "build.rollback", "builder_build", target["id"], slug=feature, ok=res.ok)
        await i.followup.send((f"↩️ Rolled back to v{target['version']}. " if res.ok else "") + res.message, ephemeral=True)

    @rollback.autocomplete("feature")
    async def _rollback_ac(self, i: discord.Interaction, current: str):
        return await self._features(current)

    @app_commands.command(name="disable", description="Switch a built feature off (its data is kept)")
    @app_commands.describe(feature="The feature")
    async def disable(self, i: discord.Interaction, feature: str):
        if not await self._ready(i):
            return
        await i.response.defer(ephemeral=True, thinking=True)
        res = await runtime(self.bot).disable(feature, i.user.id)
        await i.followup.send(res.message, ephemeral=True)

    @disable.autocomplete("feature")
    async def _disable_ac(self, i: discord.Interaction, current: str):
        return await self._features(current)

    @app_commands.command(name="enable", description="Switch a built feature back on")
    @app_commands.describe(feature="The feature")
    async def enable(self, i: discord.Interaction, feature: str):
        if not await self._ready(i):
            return
        await i.response.defer(ephemeral=True, thinking=True)
        res = await runtime(self.bot).enable(feature, i.user.id)
        await i.followup.send(res.message, ephemeral=True)

    @enable.autocomplete("feature")
    async def _enable_ac(self, i: discord.Interaction, current: str):
        return await self._features(current)


async def setup(bot):
    await bot.add_cog(Build(bot))
