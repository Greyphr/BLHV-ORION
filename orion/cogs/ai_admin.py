"""/ai: manage Orion's AI ticket handler (Sect Admin only): switch, settings, knowledge base, core brief, gaps, logs, test."""
import asyncio
import io
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from .. import ai, ai_brief, config
from ..checks import require_admin
from ..ticket_ai import AI_HELPER_KEY, LENGTHS, TONES, build_system_prompt, load_settings, parse_model_json, \
    parse_setting, select_knowledge
from ..ticket_ai_runner import gather_facts

log = logging.getLogger("orion.ai.admin")

SETTING_CHOICES = ["tone", "length", "personality", "confidence", "auto-close", "max-replies", "ping-staff",
                   "model", "human-only-types"]
KIND_ICON = {"reply": "💬", "escalate": "🙋", "close": "✅", "error": "⚠️", "skip": "⏭️", "handoff": "🤝", "gap": "❓"}
MAX_IMPORT_BYTES = 300_000


def _ago(seconds: float) -> str:
    return f"{int(seconds)}s" if seconds < 90 else (f"{int(seconds // 60)}m" if seconds < 5400 else f"{int(seconds // 3600)}h")


class KnowledgeModal(discord.ui.Modal):
    def __init__(self, cog: "AIAdmin", entry=None):
        super().__init__(title="Edit knowledge entry" if entry else "Add knowledge entry")
        self.cog, self.entry = cog, entry
        self.t = discord.ui.TextInput(label="Title (what it is about)", max_length=100,
                                      default=entry["title"] if entry else None)
        self.c = discord.ui.TextInput(label="Approved answer / information", style=discord.TextStyle.paragraph,
                                      max_length=1500, default=entry["content"] if entry else None)
        self.add_item(self.t)
        self.add_item(self.c)

    async def on_submit(self, i: discord.Interaction):
        db = self.cog.bot.db
        if self.entry:
            await db.kb_update(self.entry["id"], self.t.value.strip(), self.c.value.strip())
            eid = self.entry["id"]
        else:
            eid = await db.kb_add(self.t.value.strip(), self.c.value.strip(), i.user.id)
        await db.record_audit(i.user.id, "ai.kb.edit" if self.entry else "ai.kb.add", "ai_knowledge", eid,
                              title=self.t.value.strip())
        await i.response.send_message(f"✅ Knowledge entry **#{eid}** saved.", ephemeral=True)


class BriefModal(discord.ui.Modal):
    def __init__(self, cog: "AIAdmin", current: str):
        super().__init__(title="Core brief: who Blue Heaven is")
        self.cog = cog
        self.c = discord.ui.TextInput(label="What the AI always knows about Blue Heaven", style=discord.TextStyle.paragraph,
                                      max_length=ai_brief.BRIEF_MAX, default=current[:ai_brief.BRIEF_MAX] or None,
                                      placeholder="What the Sect is, what it stands for, its games and teams, how to join, who to ask...")
        self.add_item(self.c)

    async def on_submit(self, i: discord.Interaction):
        text = self.c.value.strip()
        await self.cog.db.set(ai_brief.BRIEF_KEY, text)
        await self.cog.db.record_audit(i.user.id, "ai.brief.set", "config", ai_brief.BRIEF_KEY, chars=len(text))
        await i.response.send_message(f"✅ Core brief saved ({len(text):,} characters)." if text else "🗑️ Core brief cleared.",
                                      ephemeral=True)


class AIAdmin(commands.GroupCog, name="ai", description="Orion's AI ticket handler (Sect Admin)"):
    kb = app_commands.Group(name="kb", description="The approved knowledge base the AI may answer from")
    brief = app_commands.Group(name="brief", description="The overview of Blue Heaven the AI always has in front of it")
    provider = app_commands.Group(name="provider", description="Which AI service answers, and the backups if it is down")

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        """Restore the provider pinned with /ai provider use, and log any fallback in .env that was skipped."""
        for problem in ai.chain()[1]:
            log.warning("AI config: %s", problem)
        try:
            pinned = self.bot.db.get(ai.PREFERRED_KEY, None)
            if pinned:
                ai.set_preferred(pinned)
        except ValueError:
            log.warning("The pinned AI provider '%s' is no longer in .env, so the normal order is used.", pinned)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await require_admin(interaction)

    @property
    def db(self):
        return self.bot.db

    # ------------------------------------------------------------- status
    @app_commands.command(name="status", description="Show the AI helper's state and settings")
    async def status(self, i: discord.Interaction):
        s = load_settings(self.db.get)
        stats = await self.db.ai_stats(7)
        kb_n = len(await self.db.kb_list(True))
        e = discord.Embed(title="🤖 AI ticket handler", color=config.COLOR_MAIN,
                          description=f"**{'✅ ON' if s.enabled else '❌ OFF'}** · experimental · "
                                      f"{'API key set' if ai.is_configured() else '⚠️ no `AI_API_KEY` in `.env`'}")
        providers, _ = ai.chain()
        nxt = ai.active_provider()
        if nxt is None:
            model_line = f"`{config.AI_PROVIDER}` · `{s.model or config.AI_MODEL}`"
        else:
            model_line = f"`{nxt.name}` · `{(s.model or nxt.model) if nxt.primary else nxt.model}`"
            if len(providers) > 1:
                model_line += f" (+{len(providers) - 1} backup{'s' if len(providers) > 2 else ''}: `/ai provider list`)"
        e.add_field(name="Model", value=model_line, inline=False)
        e.add_field(name="Tone / length", value=f"{s.tone} / {s.length} (max {s.max_chars} chars)", inline=True)
        e.add_field(name="Confidence needed", value=f"{s.confidence:.0%}", inline=True)
        e.add_field(name="Auto-close routine tickets", value="on" if s.auto_close else "off", inline=True)
        e.add_field(name="Max AI replies per ticket", value=str(s.max_replies), inline=True)
        e.add_field(name="Ping staff on handover", value="yes" if s.ping_staff else "no", inline=True)
        e.add_field(name="Knowledge entries", value=str(kb_n), inline=True)
        brief_n = len(str(self.db.get(ai_brief.BRIEF_KEY, "") or ""))
        e.add_field(name="Core brief", value=f"{brief_n:,} characters" if brief_n else "⚠️ not written (`/ai brief set`)",
                    inline=True)
        e.add_field(name="Human-only ticket types (AI never answers)", value=", ".join(sorted(s.human_only)) or "none",
                    inline=False)
        if s.personality:
            e.add_field(name="Personality notes", value=s.personality[:300], inline=False)
        e.add_field(name="Last 7 days", inline=False,
                    value=" · ".join(f"{KIND_ICON.get(k, '')} {k}: {n}" for k, n in sorted(stats.items())) or "no activity")
        await i.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="switch", description="Turn the AI helper on or off for every ticket")
    @app_commands.choices(state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
    async def switch(self, i: discord.Interaction, state: app_commands.Choice[str]):
        on = state.value == "on"
        if on and not ai.is_configured():
            return await i.response.send_message("⚠️ Add `AI_API_KEY` to `.env` and restart the bot first.", ephemeral=True)
        await self.db.set(AI_HELPER_KEY, on)
        await self.db.record_audit(i.user.id, "ai.switch", "config", AI_HELPER_KEY, enabled=on)
        await i.response.send_message("✅ AI helper is now **ON**. New tickets get first-line help; tickets work normally "
                                      "if it fails." if on else "⏸️ AI helper is now **OFF**. Tickets work normally without it.",
                                      ephemeral=True)

    @app_commands.command(name="set", description="Change an AI setting")
    @app_commands.describe(setting="What to change", value="New value (tone: %s; length: %s; on/off; 0-100%%; ...)"
                           % ("/".join(TONES), "/".join(LENGTHS)))
    @app_commands.choices(setting=[app_commands.Choice(name=n, value=n) for n in SETTING_CHOICES])
    async def set_(self, i: discord.Interaction, setting: app_commands.Choice[str], value: str):
        try:
            key, parsed = parse_setting(setting.value, value)
        except ValueError as e:
            return await i.response.send_message(f"⚠️ {e}", ephemeral=True)
        await self.db.set(key, parsed)
        await self.db.record_audit(i.user.id, "ai.set", "config", key, value=parsed)
        await i.response.send_message(f"✅ **{setting.value}** is now `{parsed}`.", ephemeral=True)

    # ---------------------------------------------------------- AI providers
    @provider.command(name="list", description="The AI services, which one is tried first, and which are having trouble")
    async def provider_list(self, i: discord.Interaction):
        providers, problems = ai.chain()
        if not providers:
            return await i.response.send_message("⚠️ No AI provider is set up. Add `AI_API_KEY` to `.env` and restart the bot.\n"
                                                 + "\n".join(problems), ephemeral=True)
        pinned = ai.get_preferred()
        lines = []
        for st in ai.provider_status():
            mark = "▶️" if st["next"] else "▫️"
            if st["cooling_for"] > 0:
                state = f"⚠️ having trouble, skipped for {int(st['cooling_for'])}s more: {st['last_error'][:110]}"
            elif st["last_ok_ago"] is not None:
                state = f"✅ last worked {_ago(st['last_ok_ago'])} ago"
            else:
                state = "no requests yet"
            tags = " 📌pinned" if st["pinned"] else ""
            lines.append(f"{mark} **{st['name']}**{tags} · {st['kind']} · `{st['model']}`\n　{state}")
        e = discord.Embed(title="🤖 AI providers", color=config.COLOR_MAIN, description="\n".join(lines))
        e.add_field(name="Order", inline=False,
                    value=(f"Pinned to **{pinned}** first, then the others." if pinned else "Automatic: the order in `.env`.")
                    + " If the first one fails, the next answers. A failing one is skipped for a minute (ten for a bad key).")
        if problems:
            e.add_field(name="Skipped (fix in .env)", value="\n".join(problems)[:1000], inline=False)
        await i.response.send_message(embed=e, ephemeral=True)

    @provider.command(name="use", description="Pin one AI service to be tried first, or go back to automatic")
    @app_commands.describe(name="A provider from /ai provider list, or 'auto'")
    async def provider_use(self, i: discord.Interaction, name: str):
        name = name.strip().lower()
        try:
            ai.set_preferred(name)
        except ValueError as e:
            return await i.response.send_message(f"⚠️ {e} See `/ai provider list`.", ephemeral=True)
        await self.db.set(ai.PREFERRED_KEY, name)        # "auto" is stored as is: the config table has no NULLs
        await self.db.record_audit(i.user.id, "ai.provider.use", "config", ai.PREFERRED_KEY, provider=name)
        await i.response.send_message("✅ Back to the automatic order." if name == "auto" else
                                      f"✅ **{name}** is tried first now. If it fails, the others still step in. "
                                      "`/ai provider use auto` undoes this.", ephemeral=True)

    @provider_use.autocomplete("name")
    async def _provider_ac(self, i: discord.Interaction, current: str):
        names = ["auto"] + [p.name for p in ai.chain()[0]]
        return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n][:25]

    @provider.command(name="test", description="Send a tiny test message to one AI service (or all) to check the key works")
    @app_commands.describe(name="Which one (blank = all of them)")
    async def provider_test(self, i: discord.Interaction, name: str | None = None):
        providers, _ = ai.chain()
        targets = [p.name for p in providers if not name or p.name == name.strip().lower()]
        if not targets:
            return await i.response.send_message("⚠️ There is no AI provider with that name. See `/ai provider list`.",
                                                 ephemeral=True)
        await i.response.defer(ephemeral=True, thinking=True)

        async def one(n: str) -> str:
            t0 = time.monotonic()
            try:
                out = await ai.complete("Reply with the single word OK.", "ping", max_tokens=20, only=n)
                return f"✅ **{n}** answered in {time.monotonic() - t0:.1f}s: `{out[:40]}`"
            except ai.AIError as e:
                return f"❌ **{n}**: {e}"

        await i.followup.send("\n".join(await asyncio.gather(*(one(n) for n in targets))), ephemeral=True)

    @provider_test.autocomplete("name")
    async def _provider_test_ac(self, i: discord.Interaction, current: str):
        return [app_commands.Choice(name=p.name, value=p.name) for p in ai.chain()[0] if current.lower() in p.name][:25]

    # ---------------------------------------------------------- knowledge base
    @kb.command(name="add", description="Add an approved fact or answer")
    async def kb_add(self, i: discord.Interaction):
        await i.response.send_modal(KnowledgeModal(self))

    @kb.command(name="edit", description="Edit a knowledge entry")
    async def kb_edit(self, i: discord.Interaction, id: int):
        e = await self.db.kb_get(id)
        if e is None:
            return await i.response.send_message("No entry with that number.", ephemeral=True)
        await i.response.send_modal(KnowledgeModal(self, e))

    @kb.command(name="list", description="List the knowledge entries")
    async def kb_list(self, i: discord.Interaction):
        rows = await self.db.kb_list()
        if not rows:
            return await i.response.send_message("The knowledge base is empty. Add facts with `/ai kb add`: the AI only "
                                                 "answers from what is written here.", ephemeral=True)
        lines = [f"`#{r['id']}` {'✅' if r['enabled'] else '⏸️'} **{r['title']}**: {r['content'][:80].replace(chr(10), ' ')}…"
                 for r in rows]
        await i.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    @kb.command(name="toggle", description="Enable or disable an entry without deleting it")
    async def kb_toggle(self, i: discord.Interaction, id: int, enabled: bool):
        ok = await self.db.kb_set_enabled(id, enabled)
        if ok:
            await self.db.record_audit(i.user.id, "ai.kb.toggle", "ai_knowledge", id, enabled=enabled)
        await i.response.send_message("✅ Updated." if ok else "No entry with that number.", ephemeral=True)

    @kb.command(name="remove", description="Delete a knowledge entry")
    async def kb_remove(self, i: discord.Interaction, id: int):
        ok = await self.db.kb_remove(id)
        if ok:
            await self.db.record_audit(i.user.id, "ai.kb.remove", "ai_knowledge", id)
        await i.response.send_message("🗑️ Removed." if ok else "No entry with that number.", ephemeral=True)

    @kb.command(name="import", description="Load the core brief and knowledge entries from a .md or .txt file")
    @app_commands.describe(file="A file of `## Title` sections. A section called CORE BRIEF becomes the core brief")
    async def kb_import(self, i: discord.Interaction, file: discord.Attachment):
        if not file.filename.lower().endswith((".md", ".txt", ".markdown")):
            return await i.response.send_message("⚠️ Attach a `.md` or `.txt` file.", ephemeral=True)
        if file.size > MAX_IMPORT_BYTES:
            return await i.response.send_message(f"⚠️ That file is too big (the limit is {MAX_IMPORT_BYTES // 1000} KB).",
                                                 ephemeral=True)
        await i.response.defer(ephemeral=True, thinking=True)
        parsed = ai_brief.parse_knowledge_file((await file.read()).decode("utf-8-sig", errors="replace"))
        if parsed.brief is None and not parsed.entries:
            return await i.followup.send("⚠️ Nothing to import: no filled-in sections were found. Each entry starts with a line "
                                         "like `## Title`.\n" + "\n".join(f"• {x}" for x in parsed.skipped[:15]), ephemeral=True)
        existing = {r["title"].strip().lower(): r for r in await self.db.kb_list()}
        added = updated = 0
        for title, content in parsed.entries:                 # same title = update, so re-importing an edited file is safe
            row = existing.get(title.strip().lower())
            if row is None:
                existing[title.strip().lower()] = {"id": await self.db.kb_add(title, content, i.user.id), "title": title}
                added += 1
            else:
                await self.db.kb_update(row["id"], title, content)
                updated += 1
        if parsed.brief is not None:
            await self.db.set(ai_brief.BRIEF_KEY, parsed.brief)
        await self.db.record_audit(i.user.id, "ai.kb.import", "ai_knowledge", None, file=file.filename, added=added,
                                   updated=updated, brief=parsed.brief is not None, skipped=len(parsed.skipped))
        lines = [f"✅ Imported **{file.filename}**: {added} added, {updated} updated"
                 + (", core brief saved." if parsed.brief is not None else ".")]
        if parsed.skipped:
            lines.append(f"⏭️ Skipped {len(parsed.skipped)} (fix these and import again):")
            lines += [f"• {x}" for x in parsed.skipped[:12]]
        await i.followup.send("\n".join(lines)[:1900], ephemeral=True)

    @kb.command(name="export", description="Download the core brief and all knowledge entries as one editable file")
    async def kb_export(self, i: discord.Interaction):
        rows = await self.db.kb_list()
        text = ai_brief.render_knowledge_file(str(self.db.get(ai_brief.BRIEF_KEY, "") or ""),
                                              [(r["title"], r["content"]) for r in rows])
        await i.response.send_message(f"📄 {len(rows)} entries. Edit the file, then load it back with `/ai kb import`.",
                                      file=discord.File(io.BytesIO(text.encode("utf-8")), filename="blue-heaven-knowledge.md"),
                                      ephemeral=True)

    # ------------------------------------------------------------ core brief
    @brief.command(name="set", description="Write or replace the overview of Blue Heaven the AI always knows")
    async def brief_set(self, i: discord.Interaction):
        await i.response.send_modal(BriefModal(self, str(self.db.get(ai_brief.BRIEF_KEY, "") or "")))

    @brief.command(name="show", description="Show the current core brief")
    async def brief_show(self, i: discord.Interaction):
        text = str(self.db.get(ai_brief.BRIEF_KEY, "") or "")
        await i.response.send_message(text[:1900] if text else "No core brief yet. Write one with `/ai brief set`, or load a "
                                      "file with `/ai kb import`.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ what Orion could not answer
    @app_commands.command(name="gaps", description="Questions the AI couldn't answer: what to add to its knowledge next")
    async def gaps(self, i: discord.Interaction):
        rows = await self.db.ai_gap_logs(15)
        if not rows:
            return await i.response.send_message("No gaps recorded: every question so far was covered by what Orion knows.",
                                                 ephemeral=True)
        lines = [f"❓ <t:{int(r['created_at'].timestamp())}:R> ticket #{r['ticket_id'] or '—'}"
                 f"{chr(10) + '> ' + r['summary'][:200] if r['summary'] else ''}" for r in rows]
        await i.response.send_message("**Not covered by Orion's knowledge, newest first.** Add the answers with `/ai kb add`, "
                                      "or `/ai kb export`, edit and `/ai kb import`.\n" + "\n".join(lines)[:1750],
                                      ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # -------------------------------------------------------------- logs / test
    @app_commands.command(name="logs", description="Recent AI activity (optionally for one ticket)")
    async def logs(self, i: discord.Interaction, ticket: app_commands.Range[int, 1, 10_000_000] | None = None):
        rows = await self.db.ai_logs(ticket, 15)
        if not rows:
            return await i.response.send_message("No AI activity recorded yet.", ephemeral=True)
        lines = []
        for r in rows:
            conf = f" {r['confidence']:.0%}" if r["confidence"] is not None else ""
            lines.append(f"{KIND_ICON.get(r['kind'], '•')} <t:{int(r['created_at'].timestamp())}:R> "
                         f"ticket #{r['ticket_id'] or '—'} **{r['kind']}**{conf}"
                         f"{' · ' + r['reason'] if r['reason'] else ''}"
                         f"{chr(10) + '> ' + r['summary'][:160] if r['summary'] else ''}")
        await i.response.send_message("\n".join(lines)[:1950], ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="test", description="Try a question exactly as a member in a General Support ticket")
    async def test(self, i: discord.Interaction, question: app_commands.Range[str, 1, 500]):
        if not ai.is_configured():
            return await i.response.send_message("❌ No `AI_API_KEY` in `.env`.", ephemeral=True)
        await i.response.defer(ephemeral=True, thinking=True)
        s = load_settings(self.db.get)
        kb = select_knowledge(await self.db.kb_list(True), question)
        types = [dict(t) for t in await self.db.tt_list()]
        system = build_system_prompt(s, type_name="General Support", type_names=["General Support", "Other"], kb=kb,
                                     guild_name=i.guild.name if i.guild else "Blue Heaven",
                                     brief=str(self.db.get(ai_brief.BRIEF_KEY, "") or ""), facts=gather_facts(self.db, types))
        from ..ticket_ai import build_user_message
        try:
            text = await ai.complete(system, build_user_message([{"q": "Question", "a": question}], []), max_tokens=700,
                                     model=s.model or None, json_mode=True, raw=True)
        except ai.AIError as e:
            return await i.followup.send(f"❌ The AI service failed: {e}", ephemeral=True)
        raw = parse_model_json(text)
        if raw is None:
            return await i.followup.send("⚠️ The model's answer wasn't readable JSON:\n>>> " + text[:900], ephemeral=True)
        used = ", ".join(e["title"] for e in kb) or "none"
        await i.followup.send(
            f">>> {ai.clean_reply(raw['reply'], s.max_chars)}\n\n"
            f"confidence **{raw['confidence']:.0%}** (needs {s.confidence:.0%}) · human needed: **{raw['needs_human']}** · "
            f"sensitive: **{raw['sensitive']}**\nknowledge used: {used}", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(AIAdmin(bot))
