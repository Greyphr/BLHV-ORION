"""Build harness, the running half: put an approved feature into the live bot, and put everything back if that fails.

The safety net, in the order it matters:
  1. Before any database change a generated feature asks for, a data-only snapshot of every table is written to disk. If the
     snapshot can't be taken, nothing is changed.
  2. The feature file is written and loaded. If loading, or registering its slash commands with Discord, fails, the previous
     version is loaded again (or the feature is left unloaded if there was none). The rest of the bot never stops.
  3. Every version stays in the database. `rollback` makes an older one active again.
  4. At startup, a feature that no longer loads falls back to its previous working version, or is switched off, and staff
     are told in the alerts channel. A broken feature can't stop the bot from starting.
"""
import asyncio
import gzip
import importlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

import discord

from . import config
from .builder import DraftError, module_name, validate_schema

log = logging.getLogger("orion.builder.runtime")


@dataclass
class Result:
    ok: bool
    message: str
    snapshot: Optional[Path] = None


async def resync_commands(bot) -> None:
    """Make Discord's command list for the server match what is loaded right now (including removing unloaded commands)."""
    guild = discord.Object(config.GUILD_ID)
    bot.tree.clear_commands(guild=guild)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)


async def snapshot_database(pool, dest: Path) -> dict[str, int]:
    """Write every table in the public schema to dest/<table>.csv.gz plus a manifest. Data only: the table structures come from
    the migrations. One read-only transaction, so all the tables are from the same moment. Returns {table: rows}."""
    dest.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            tables = [r["table_name"] for r in await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' "
                "ORDER BY table_name")]
            for t in tables:
                with gzip.open(dest / f"{t}.csv.gz", "wb") as f:
                    status = await conn.copy_from_table(t, output=f, format="csv", header=True, schema_name="public")
                counts[t] = int(status.rsplit(" ", 1)[-1])
    (dest / "manifest.json").write_text(json.dumps(
        {"taken_at": datetime.now(timezone.utc).isoformat(), "tables": counts,
         "restore": "Data only, one CSV per table with a header row. Restore a table with "
                    "\\copy <table> FROM PROGRAM 'gunzip -c <table>.csv.gz' CSV HEADER (after emptying it)."}, indent=2))
    return counts


def prune_snapshots(root: Path, keep: int) -> None:
    dirs = sorted(d for d in root.glob("prebuild-*") if d.is_dir())
    for d in dirs[:-keep] if keep > 0 else dirs:
        for f in d.iterdir():
            f.unlink(missing_ok=True)
        d.rmdir()


class BuildRuntime:
    def __init__(self, bot, *, generated_dir: Optional[Path] = None, snapshot_dir: Optional[Path] = None,
                 resync: Optional[Callable[[object], Awaitable[None]]] = None):
        self.bot = bot
        self.dir = Path(generated_dir or config.GENERATED_DIR)
        self.snap_dir = Path(snapshot_dir or config.SNAPSHOT_DIR)
        self._resync = resync or resync_commands
        self.lock = asyncio.Lock()            # one load/unload at a time
        self.gen_lock = asyncio.Lock()        # one AI generation at a time (they cost money)

    # ------------------------------------------------------------------ files
    @property
    def db(self):
        return self.bot.db

    def _ensure_path(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        p = str(self.dir)
        if p not in sys.path:
            sys.path.insert(0, p)

    def write_module(self, slug: str, code: str) -> Path:
        self._ensure_path()
        path = self.dir / f"{module_name(slug)}.py"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(code, encoding="utf-8")
        os.replace(tmp, path)
        # A stale compiled copy (same size, same second) must never win over the code we just wrote.
        for pyc in (self.dir / "__pycache__").glob(f"{module_name(slug)}.*.pyc"):
            pyc.unlink(missing_ok=True)
        importlib.invalidate_caches()
        return path

    def loaded(self, slug: str) -> bool:
        return module_name(slug) in self.bot.extensions

    def _cleanup_ui(self, name: str):
        """Stop persistent views / dynamic buttons that the module registered, so an unloaded feature's old buttons stop
        answering. (A well-written cog does this in cog_unload; this is the backstop.)"""
        try:
            for v in list(self.bot.persistent_views):
                if type(v).__module__ == name:
                    v.stop()
            mod = sys.modules.get(name)
            if mod is not None:
                items = [o for o in vars(mod).values() if isinstance(o, type) and issubclass(o, discord.ui.DynamicItem)
                         and o.__module__ == name]
                if items:
                    self.bot.remove_dynamic_items(*items)
        except Exception:
            log.exception("Could not clean up the buttons of %s", name)

    async def _unload(self, name: str):
        if name in self.bot.extensions:
            self._cleanup_ui(name)
            await self.bot.unload_extension(name)

    async def _resync_quietly(self):
        try:
            await self._resync(self.bot)
        except Exception:
            log.exception("Could not re-sync slash commands")

    async def _load(self, build) -> None:
        """Write the build's code and load it (reloading if a version is already loaded). Raises on any failure."""
        name = module_name(build["slug"])
        self.write_module(build["slug"], build["code"])
        if name in self.bot.extensions:
            self._cleanup_ui(name)
            await self.bot.reload_extension(name)
        else:
            await self.bot.load_extension(name)

    # --------------------------------------------------------------- snapshots
    async def take_snapshot(self, build_id: int) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = self.snap_dir / f"prebuild-{stamp}-b{build_id}"
        await snapshot_database(self.db.pool, dest)
        prune_snapshots(self.snap_dir, config.BUILDER_SNAPSHOTS_KEEP)
        return dest

    async def _mark_failed(self, build, error: str) -> None:
        """Only a draft that was never loaded becomes 'failed'. A version that already ran (rollback / enable) keeps its status:
        it is a known-good rollback target, and a hiccup now (say Discord rejecting the command sync) must not strike it off."""
        if build["status"] == "pending":
            await self.db.bb_set_status(build["id"], "failed", error)
        else:
            log.warning("Build #%s (%s) could not be loaded this time, but it ran before so its status is unchanged: %s",
                        build["id"], build["status"], error)

    # --------------------------------------------------------------- activate
    async def activate(self, build_id: int, actor_id: Optional[int], *, run_schema: bool = True) -> Result:
        """Make a build the live version of its feature. Used for approving a new draft and for rolling back.
        On any failure the previous state is restored and the build is marked failed."""
        async with self.lock:
            db = self.db
            build = await db.bb_get(build_id)
            if build is None:
                return Result(False, "I can't find that build.")
            if build["status"] not in ("pending", "superseded", "active"):
                return Result(False, f"Build #{build_id} is **{build['status']}**, so it can't be loaded. "
                                     "Ask for a new version instead.")
            slug, name = build["slug"], module_name(build["slug"])
            feature = await db.bf_get(slug)
            prev = await db.bb_get(feature["active_build_id"]) if feature and feature["active_build_id"] else None
            if prev is not None and prev["id"] == build_id and self.loaded(slug):
                return Result(True, f"**{build['title']}** v{build['version']} is already running.")
            was_loaded = self.loaded(slug)
            snapshot: Optional[Path] = None

            # 1. database changes: backup first, then the feature's own additive SQL
            if run_schema and build["schema_sql"]:
                try:
                    statements = validate_schema(build["schema_sql"], slug)
                except DraftError as e:
                    await self._mark_failed(build, str(e))
                    return Result(False, f"The database changes were refused, so nothing was changed: {e}")
                try:
                    snapshot = await self.take_snapshot(build_id)
                except Exception as e:
                    log.exception("Pre-build database snapshot failed")
                    return Result(False, "I couldn't take the database backup, so I changed **nothing** (the build is still "
                                         f"waiting for your decision). Reason: {type(e).__name__}: {e}"[:1500])
                try:
                    async with db.pool.acquire() as c, c.transaction():
                        for st in statements:
                            await c.execute(st)
                except Exception as e:
                    log.exception("Generated schema failed")
                    await self._mark_failed(build, f"database: {e}"[:1500])
                    return Result(False, f"The database changes failed and were undone. Nothing else was touched. "
                                         f"Reason: {e}"[:1500], snapshot)

            # 2. code
            try:
                await self._load(build)
                await self._resync(self.bot)
            except Exception as e:
                log.exception("Loading generated feature %s failed", name)
                err = f"{type(e).__name__}: {e}"
                outcome = await self._restore(slug, prev, was_loaded)
                await self._mark_failed(build, err[:1500])
                return Result(False, f"It failed to load: `{err[:600]}`\n{outcome}", snapshot)

            # 3. bookkeeping
            if prev is not None and prev["id"] != build_id and prev["status"] == "active":
                await db.bb_set_status(prev["id"], "superseded")
            await db.bb_set_status(build_id, "active")
            await db.bf_upsert(slug, build["title"], True, build_id, actor_id)
            await db.record_audit(actor_id, "build.activate", "builder_build", build_id, slug=slug, version=build["version"])
            msg = f"✅ **{build['title']}** v{build['version']} is live."
            if snapshot:
                msg += f"\nDatabase snapshot taken first: `{snapshot.name}`"
            return Result(True, msg, snapshot)

    async def _restore(self, slug: str, prev, was_loaded: bool) -> str:
        """Put the feature back how it was before a failed activation."""
        name = module_name(slug)
        try:
            if was_loaded and prev is not None:
                await self._load(prev)                 # reload the previous code
                await self._resync_quietly()
                return f"✅ The previous version (v{prev['version']}) is still running. Nothing else was affected."
            await self._unload(name)
            await self._resync_quietly()
            return "The feature is not loaded. Nothing else was affected."
        except Exception:
            log.exception("Could not restore the previous version of %s", name)
            try:
                await self._unload(name)
                await self.db.bf_set_enabled(slug, False)
                await self._resync_quietly()
            except Exception:
                log.exception("Could not even unload %s", name)
            return ("⚠️ I couldn't bring the previous version back either, so the feature is switched **off**. "
                    "The rest of the bot is fine. Use `/build rollback` or `/build enable` when ready.")

    # ------------------------------------------------------------ off / on
    async def disable(self, slug: str, actor_id: Optional[int]) -> Result:
        async with self.lock:
            feature = await self.db.bf_get(slug)
            if feature is None:
                return Result(False, "There is no feature with that name.")
            try:
                await self._unload(module_name(slug))
                await self._resync(self.bot)
            except Exception as e:
                log.exception("Disabling %s failed", slug)
                await self.db.bf_set_enabled(slug, False)
                return Result(False, f"It's off in the database but unloading hit a problem ({type(e).__name__}). "
                                     "Restart the bot to be sure.")
            await self.db.bf_set_enabled(slug, False)
            await self.db.record_audit(actor_id, "build.disable", "builder_feature", slug)
            return Result(True, f"⏸️ **{feature['title']}** is switched off. Its data is kept; `/build enable` turns it back on.")

    async def enable(self, slug: str, actor_id: Optional[int]) -> Result:
        async with self.lock:
            feature = await self.db.bf_get(slug)
            if feature is None:
                return Result(False, "There is no feature with that name.")
            build = await self.db.bb_get(feature["active_build_id"]) if feature["active_build_id"] else None
            if build is None:
                return Result(False, "It has no approved version yet. Approve a build first.")
            if self.loaded(slug):
                await self.db.bf_set_enabled(slug, True)
                return Result(True, "It's already on.")
            try:
                await self._load(build)
                await self._resync(self.bot)
            except Exception as e:
                log.exception("Enabling %s failed", slug)
                await self._unload(module_name(slug))
                await self._resync_quietly()
                return Result(False, f"It failed to load, so it stays off: `{type(e).__name__}: {e}`"[:1500])
            await self.db.bf_set_enabled(slug, True)
            await self.db.record_audit(actor_id, "build.enable", "builder_feature", slug)
            return Result(True, f"✅ **{feature['title']}** v{build['version']} is on.")

    # ---------------------------------------------------------------- startup
    async def load_all(self) -> list[str]:
        """Load every enabled feature when the bot starts. Never raises. Returns plain-words problems for the staff alert.
        (Commands are registered by the normal sync at the end of setup_hook, so no re-sync here.)"""
        problems: list[str] = []
        try:
            features = [f for f in await self.db.bf_list() if f["enabled"]]
        except Exception:
            log.exception("Could not read the generated features")
            return ["I couldn't read the list of generated features, so none of them were loaded."]
        for f in features:
            slug = f["slug"]
            try:
                await self._load_with_fallback(f, problems)
            except Exception:
                log.exception("Unexpected problem loading generated feature %s", slug)
                problems.append(f"Generated feature **{slug}** hit an unexpected problem and was not loaded.")
        return problems

    async def _load_with_fallback(self, feature, problems: list[str]):
        db, slug = self.db, feature["slug"]
        active = await db.bb_get(feature["active_build_id"]) if feature["active_build_id"] else None
        if active is None:
            await db.bf_set_enabled(slug, False)
            problems.append(f"Generated feature **{slug}** has no approved version, so it was switched off.")
            return
        try:
            await self._load(active)
            return
        except Exception as e:
            log.exception("Generated feature %s v%s failed to load at startup", slug, active["version"])
            err = f"{type(e).__name__}: {e}"
            await self._unload(module_name(slug))
            await db.bb_set_status(active["id"], "failed", f"startup: {err}"[:1500])
        older = await db.bb_previous_working(slug, active["version"])
        if older is not None:
            try:
                await self._load(older)
                await db.bb_set_status(older["id"], "active")
                await db.bf_set_active(slug, older["id"])
                problems.append(f"**{feature['title']}** v{active['version']} failed to load (`{err[:150]}`), so I went back to "
                                f"v{older['version']}.")
                return
            except Exception:
                log.exception("Fallback version of %s also failed", slug)
                await self._unload(module_name(slug))
        await db.bf_set_enabled(slug, False)
        problems.append(f"**{feature['title']}** failed to load (`{err[:150]}`) and has no working older version, so it was "
                        "switched off. The rest of the bot is running normally.")
