"""/build harness. Pure parsing/validation tests always run; the load / rollback / snapshot tests drive a REAL discord.py Bot
object (never connected) against a real PostgreSQL (TEST_DATABASE_URL), like the other database tests."""
import asyncio
import gzip
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import discord
import pytest
from discord.ext import commands

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from orion import ai, builder
from orion.builder import DraftError, build_draft, validate_code, validate_schema
from orion.builder_runtime import BuildRuntime, prune_snapshots, snapshot_database
from orion.checks import is_patriarch
from orion.db import Database

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run the PostgreSQL tests")


def cog_source(name: str, version: int = 1, *, boom: bool = False, view: bool = False) -> str:
    """A tiny valid generated cog. boom=True makes setup() raise (a feature that can't load)."""
    return f'''
import discord
from discord import app_commands
from discord.ext import commands

VERSION = {version}

class {name.title().replace("_", "")}(commands.Cog):
    version = VERSION
    def __init__(self, bot):
        self.bot = bot
{"    async def cog_load(self):" + chr(10) + "        self.bot.add_view(MyView())" if view else ""}
    @app_commands.command(name="{name}")
    async def hello(self, i: discord.Interaction):
        await i.response.send_message("v{version}")

{"class MyView(discord.ui.View):" + chr(10) + "    def __init__(self):" + chr(10) + "        super().__init__(timeout=None)" + chr(10) + "        self.add_item(discord.ui.Button(label='x', custom_id='gen:" + name + ":x'))" if view else ""}

async def setup(bot):
    {"raise RuntimeError('boom')" if boom else "await bot.add_cog(" + name.title().replace("_", "") + "(bot))"}
'''


def answer(slug="role_picker", code=None, schema="NONE", notes="NONE") -> str:
    return (f"===SLUG===\n{slug}\n===TITLE===\nRole picker\n===SUMMARY===\nPosts a role picker.\n===NOTES===\n{notes}\n"
            f"===SCHEMA===\n{schema}\n===CODE===\n{code if code is not None else cog_source(slug)}")


# ------------------------------------------------------------------ pure: parsing and checks
def test_patriarch_is_only_the_owner_id():
    from orion import config
    assert is_patriarch(NS(id=config.OWNER_ID)) and not is_patriarch(NS(id=config.OWNER_ID + 1))


def test_good_answer_parses():
    d = build_draft(answer())
    assert (d.slug, d.title, d.schema_sql, d.notes) == ("role_picker", "Role picker", "", "")
    assert "async def setup" in d.code and d.summary == "Posts a role picker."


def test_code_fences_are_stripped():
    d = build_draft(answer(code="```python\n" + cog_source("role_picker") + "\n```"))
    assert not d.code.startswith("```") and "```" not in d.code


@pytest.mark.parametrize("bad, why", [
    ("===SLUG===\nx_y\n===TITLE===\nT\n", "no ===CODE=== section"),
    (answer(slug="Bad Name!!", code=cog_source("bad_name")), None),                      # cleaned to bad_name, still valid
    (answer(slug="1abc", code=cog_source("abc")), "isn't valid"),
    (answer(code="def setup(bot):\n    pass\n"), "async def setup"),
    (answer(code="async def setup(bot:\n"), "syntax error"),
    (answer(code="   "), "empty"),
])
def test_bad_answers_are_rejected_in_plain_words(bad, why):
    if why is None:
        assert build_draft(bad).slug == "bad_name"
        return
    with pytest.raises(DraftError, match=why):
        build_draft(bad)


def test_a_new_feature_cannot_reuse_a_slug_but_a_modification_can():
    with pytest.raises(DraftError, match="already used"):
        build_draft(answer(), taken={"role_picker"})
    assert build_draft(answer(slug="whatever"), forced_slug="role_picker", taken={"role_picker"}).slug == "role_picker"


def test_gen_prefix_is_not_doubled():
    assert build_draft(answer(slug="gen_role_picker")).slug == "role_picker"


GOOD_SQL = ("CREATE TABLE IF NOT EXISTS gen_role_picker_panels (id SERIAL PRIMARY KEY, msg BIGINT);\n"
            "CREATE TABLE IF NOT EXISTS gen_role_picker (k TEXT PRIMARY KEY, panel INT REFERENCES gen_role_picker_panels(id));\n"
            "-- comment\nCREATE UNIQUE INDEX IF NOT EXISTS gen_role_picker_idx ON gen_role_picker_panels (msg);")


def test_schema_accepts_only_own_additive_tables():
    assert len(validate_schema(GOOD_SQL, "role_picker")) == 3
    assert builder.schema_tables(GOOD_SQL) == ["gen_role_picker_panels", "gen_role_picker"]


@pytest.mark.parametrize("sql", [
    "DROP TABLE users",
    "ALTER TABLE users ADD COLUMN x INT",
    "DELETE FROM users",
    "CREATE TABLE users (id INT)",                                   # not IF NOT EXISTS and not gen_
    "CREATE TABLE IF NOT EXISTS users (id INT)",                     # other feature's / core table
    "CREATE TABLE IF NOT EXISTS gen_other_t (id INT)",               # another feature's namespace
    "CREATE TABLE IF NOT EXISTS gen_role_pickerX (id INT)",          # prefix trick
    "CREATE TABLE IF NOT EXISTS gen_role_picker_t (u BIGINT REFERENCES users(user_id))",
    "CREATE INDEX IF NOT EXISTS gen_role_picker_i ON users (user_id)",
    "CREATE TABLE IF NOT EXISTS gen_role_picker_t (id INT); DROP TABLE users",
])
def test_schema_rejects_anything_else(sql):
    with pytest.raises(DraftError):
        validate_schema(sql, "role_picker")


def test_a_bad_schema_makes_the_whole_answer_unusable():
    with pytest.raises(DraftError):
        build_draft(answer(schema="DROP TABLE users"))


def test_generate_gives_the_model_one_chance_to_fix_its_answer():
    calls = []

    async def fake(system, user, **kw):
        calls.append((user, kw))
        return "===SLUG===\nrole_picker\n===CODE===\nasync def setup(bot:\n" if len(calls) == 1 else answer()

    d = asyncio.run(builder.generate("make a role picker", existing_commands=["rank", "ticket"], complete=fake))
    assert d.slug == "role_picker" and len(calls) == 2
    assert "syntax error" in calls[1][0] and "YOUR PREVIOUS ANSWER" in calls[1][0]
    assert "rank, ticket" in calls[0][0]
    assert calls[0][1]["raw"] is True and calls[0][1]["timeout"] >= 120


def test_generate_gives_up_after_two_bad_answers_and_ai_errors_pass_through():
    async def bad(system, user, **kw):
        return "nonsense"
    with pytest.raises(DraftError, match="failed the automatic checks twice"):
        asyncio.run(builder.generate("x" * 20, complete=bad))

    async def down(system, user, **kw):
        raise ai.AIError("couldn't reach the AI service")
    with pytest.raises(ai.AIError):
        asyncio.run(builder.generate("x" * 20, complete=down))


def test_modify_prompt_carries_current_code_slug_and_feedback():
    msg = builder.build_user_message("orig", slug="role_picker", previous_code="CODE_HERE", feedback="add a Celestial role",
                                     taken_slugs=["ignored"])
    assert "CODE_HERE" in msg and "add a Celestial role" in msg and "`role_picker`" in msg and "ignored" not in msg


# ------------------------------------------------------------------ runtime against a real bot + PostgreSQL
class Harness:
    def __init__(self, tmp: Path, db: Database, resync=None):
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        self.bot.db = db
        self.synced = 0
        self.fail_sync = False

        async def fake_sync(bot):
            self.synced += 1
            if self.fail_sync:
                raise discord.HTTPException(NS(status=500, reason="x"), "sync failed")
        self.rt = BuildRuntime(self.bot, generated_dir=tmp / "generated", snapshot_dir=tmp / "backups", resync=resync or fake_sync)
        self.bot.builder = self.rt

    async def add(self, slug, code, schema=None, title="Test feature", status="pending"):
        bid, ver = await self.bot.db.bb_add(slug, title, "req", "sum", "", code, schema or "", "m", 1)
        if status != "pending":
            await self.bot.db.bb_set_status(bid, status)
        return bid

    def version_running(self, slug):
        cog = self.bot.get_cog(slug.title().replace("_", ""))
        return None if cog is None else cog.version


def run_rt(fn):
    async def go(tmp: Path):
        db = Database(DSN)
        await db.connect()
        try:
            await db.pool.execute("DELETE FROM builder_builds"); await db.pool.execute("DELETE FROM builder_features")
            for r in await db.pool.fetch("SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'gen\\_%'"):
                await db.pool.execute(f'DROP TABLE IF EXISTS "{r["table_name"]}" CASCADE')
            h = Harness(tmp, db)
            await fn(h)
        finally:
            await db.close()
    return go


@pytest.fixture
def tmp(tmp_path):
    yield tmp_path
    for m in [m for m in sys.modules if m.startswith("gen_")]:
        del sys.modules[m]


def runner(tmp, fn):
    asyncio.run(run_rt(fn)(tmp))


@needs_db
def test_approve_loads_the_feature_and_a_second_version_replaces_it(tmp):
    async def t(h):
        db = h.bot.db
        b1 = await h.add("alpha", cog_source("alpha", 1))
        r = await h.rt.activate(b1, 7)
        assert r.ok and h.version_running("alpha") == 1 and "alpha" in [c.name for c in h.bot.tree.get_commands()]
        f = await db.bf_get("alpha")
        assert f["enabled"] and f["active_build_id"] == b1 and (await db.bb_get(b1))["status"] == "active"
        assert (tmp / "generated" / "gen_alpha.py").read_text() == cog_source("alpha", 1)

        b2 = await h.add("alpha", cog_source("alpha", 2))
        assert (await h.rt.activate(b2, 7)).ok
        assert h.version_running("alpha") == 2
        assert [(await db.bb_get(x))["status"] for x in (b1, b2)] == ["superseded", "active"]
        assert (await db.bf_get("alpha"))["active_build_id"] == b2
        assert any(a["action"] == "build.activate" for a in await db.recent_audit_log(5))
    runner(tmp, t)


@needs_db
def test_a_version_that_crashes_on_load_is_undone_and_the_old_one_keeps_running(tmp):
    async def t(h):
        db = h.bot.db
        b1 = await h.add("beta", cog_source("beta", 1))
        assert (await h.rt.activate(b1, 7)).ok
        bad = await h.add("beta", cog_source("beta", 2, boom=True))
        r = await h.rt.activate(bad, 7)
        assert not r.ok and "RuntimeError: boom" in r.message and "previous version (v1) is still running" in r.message
        assert h.version_running("beta") == 1                                  # in memory: still v1
        assert (tmp / "generated" / "gen_beta.py").read_text() == cog_source("beta", 1)   # on disk: v1 restored
        row = await db.bb_get(bad)
        assert row["status"] == "failed" and "boom" in row["error"]
        f = await db.bf_get("beta")
        assert f["enabled"] and f["active_build_id"] == b1 and (await db.bb_get(b1))["status"] == "active"
    runner(tmp, t)


@needs_db
def test_a_syntax_error_that_slipped_past_the_checks_is_also_undone(tmp):
    async def t(h):
        b1 = await h.add("gamma", cog_source("gamma", 1))
        assert (await h.rt.activate(b1, 7)).ok
        bad = await h.add("gamma", "async def setup(bot:\n")
        r = await h.rt.activate(bad, 7)
        assert not r.ok and h.version_running("gamma") == 1
        assert (await h.bot.db.bb_get(bad))["status"] == "failed"
    runner(tmp, t)


@needs_db
def test_a_first_version_that_fails_leaves_nothing_loaded(tmp):
    async def t(h):
        bad = await h.add("delta", cog_source("delta", 1, boom=True))
        r = await h.rt.activate(bad, 7)
        assert not r.ok and "not loaded" in r.message and "gen_delta" not in h.bot.extensions
        assert await h.bot.db.bf_get("delta") is None
    runner(tmp, t)


@needs_db
def test_if_registering_the_slash_commands_fails_the_previous_version_comes_back(tmp):
    async def t(h):
        b1 = await h.add("epsilon", cog_source("epsilon", 1))
        assert (await h.rt.activate(b1, 7)).ok
        b2 = await h.add("epsilon", cog_source("epsilon", 2))
        h.fail_sync = True                       # Discord refuses the sync while v2 is loading...
        calls = h.synced

        orig = h.rt._resync_quietly

        async def quiet():                       # ...but the cleanup sync afterwards works
            h.fail_sync = False
            await orig()
        h.rt._resync_quietly = quiet
        r = await h.rt.activate(b2, 7)
        assert not r.ok and h.version_running("epsilon") == 1 and h.synced > calls
        assert (await h.bot.db.bb_get(b2))["status"] == "failed" and (await h.bot.db.bb_get(b1))["status"] == "active"
    runner(tmp, t)


@needs_db
def test_rollback_goes_to_the_last_version_that_actually_ran(tmp):
    async def t(h):
        db = h.bot.db
        b1 = await h.add("zeta", cog_source("zeta", 1)); assert (await h.rt.activate(b1, 7)).ok
        bad = await h.add("zeta", cog_source("zeta", 2, boom=True)); assert not (await h.rt.activate(bad, 7)).ok
        rej = await h.add("zeta", cog_source("zeta", 3), status="rejected")
        b4 = await h.add("zeta", cog_source("zeta", 4)); assert (await h.rt.activate(b4, 7)).ok
        assert h.version_running("zeta") == 4
        prev = await db.bb_previous_working("zeta", 4)
        assert prev["id"] == b1                                              # skips the failed and the rejected ones
        assert (await h.rt.activate(prev["id"], 7, run_schema=False)).ok
        assert h.version_running("zeta") == 1
        assert [(await db.bb_get(x))["status"] for x in (b1, b4)] == ["active", "superseded"]
        assert (await h.rt.activate(bad, 7)).ok is False                     # a failed build can't be loaded again
        assert (await h.rt.activate(rej, 7)).ok is False
    runner(tmp, t)


@needs_db
def test_double_approval_is_harmless(tmp):
    async def t(h):
        b = await h.add("eta", cog_source("eta", 1))
        r1, r2 = await asyncio.gather(h.rt.activate(b, 7), h.rt.activate(b, 7))
        assert r1.ok and r2.ok and "already running" in r2.message + r1.message
        assert h.version_running("eta") == 1
    runner(tmp, t)


@needs_db
def test_disable_unloads_and_stops_its_buttons_and_enable_brings_it_back(tmp):
    async def t(h):
        b = await h.add("theta", cog_source("theta", 1, view=True)); assert (await h.rt.activate(b, 7)).ok
        assert any(type(v).__module__ == "gen_theta" for v in h.bot.persistent_views)
        r = await h.rt.disable("theta", 7)
        assert r.ok and "gen_theta" not in h.bot.extensions and not (await h.bot.db.bf_get("theta"))["enabled"]
        assert not any(type(v).__module__ == "gen_theta" for v in h.bot.persistent_views)     # old buttons no longer answer
        assert "theta" not in [c.name for c in h.bot.tree.get_commands()]
        r = await h.rt.enable("theta", 7)
        assert r.ok and h.version_running("theta") == 1 and (await h.bot.db.bf_get("theta"))["enabled"]
        assert not (await h.rt.disable("nope", 7)).ok and not (await h.rt.enable("nope", 7)).ok
    runner(tmp, t)


# ---- database changes: backup first
SQL_OK = "CREATE TABLE IF NOT EXISTS gen_iota_items (id SERIAL PRIMARY KEY, name TEXT);"


@needs_db
def test_database_changes_snapshot_everything_first_then_create_the_tables(tmp):
    async def t(h):
        db = h.bot.db
        await db.pool.execute("INSERT INTO users(user_id) VALUES(9001) ON CONFLICT DO NOTHING")
        b = await h.add("iota", cog_source("iota", 1), SQL_OK)
        r = await h.rt.activate(b, 7)
        assert r.ok and r.snapshot and r.snapshot.parent == tmp / "backups" and "snapshot" in r.message
        assert await db.pool.fetchval("SELECT to_regclass('gen_iota_items')") is not None
        files = {p.name for p in r.snapshot.iterdir()}
        assert {"manifest.json", "users.csv.gz", "config.csv.gz"} <= files
        assert "9001" in gzip.open(r.snapshot / "users.csv.gz", "rt").read()
        assert "gen_iota_items" not in " ".join(files)                    # the snapshot is of what existed BEFORE the change
    runner(tmp, t)


@needs_db
def test_no_snapshot_means_no_change(tmp):
    async def t(h):
        b = await h.add("kappa", cog_source("kappa", 1), "CREATE TABLE IF NOT EXISTS gen_kappa_t (id INT);")

        async def boom(_): raise OSError("disk full")
        h.rt.take_snapshot = boom
        r = await h.rt.activate(b, 7)
        assert not r.ok and "changed **nothing**" in r.message and "disk full" in r.message
        assert await h.bot.db.pool.fetchval("SELECT to_regclass('gen_kappa_t')") is None
        assert "gen_kappa" not in h.bot.extensions and (await h.bot.db.bb_get(b))["status"] == "pending"   # still reviewable
    runner(tmp, t)


@needs_db
def test_failing_sql_is_rolled_back_and_nothing_loads(tmp):
    async def t(h):
        b = await h.add("lambda_", cog_source("lambda_", 1),
                        "CREATE TABLE IF NOT EXISTS gen_lambda__a (id INT);\nCREATE TABLE IF NOT EXISTS gen_lambda__b (x notatype);")
        r = await h.rt.activate(b, 7)
        assert not r.ok and "undone" in r.message
        assert await h.bot.db.pool.fetchval("SELECT to_regclass('gen_lambda__a')") is None     # first statement undone too
        assert (await h.bot.db.bb_get(b))["status"] == "failed" and not h.rt.loaded("lambda_")
    runner(tmp, t)


@needs_db
def test_sql_that_touches_other_tables_is_refused_even_if_it_reached_the_database(tmp):
    async def t(h):
        b = await h.add("mu", cog_source("mu", 1), "DROP TABLE users;")
        r = await h.rt.activate(b, 7)
        assert not r.ok and "refused" in r.message
        assert await h.bot.db.pool.fetchval("SELECT to_regclass('users')") is not None
    runner(tmp, t)


@needs_db
def test_rollback_does_not_run_the_sql_or_take_another_snapshot(tmp):
    async def t(h):
        b1 = await h.add("nu", cog_source("nu", 1), "CREATE TABLE IF NOT EXISTS gen_nu_t (id INT);")
        assert (await h.rt.activate(b1, 7)).ok
        b2 = await h.add("nu", cog_source("nu", 2)); assert (await h.rt.activate(b2, 7)).ok
        n = len(list((tmp / "backups").iterdir()))
        r = await h.rt.activate(b1, 7, run_schema=False)
        assert r.ok and r.snapshot is None and len(list((tmp / "backups").iterdir())) == n
    runner(tmp, t)


@needs_db
def test_snapshot_counts_and_pruning(tmp):
    async def t(h):
        pool = h.bot.db.pool
        await pool.execute("INSERT INTO users(user_id) VALUES(8001),(8002) ON CONFLICT DO NOTHING")
        counts = await snapshot_database(pool, tmp / "s")
        assert counts["users"] >= 2 and "schema_migrations" in counts and "builder_builds" in counts
        for n in range(4):
            (tmp / "p" / f"prebuild-2026010{n}-b{n}").mkdir(parents=True)
            (tmp / "p" / f"prebuild-2026010{n}-b{n}" / "manifest.json").write_text("{}")
        prune_snapshots(tmp / "p", 2)
        assert sorted(d.name for d in (tmp / "p").iterdir()) == ["prebuild-20260102-b2", "prebuild-20260103-b3"]
    runner(tmp, t)


# ---- startup
@needs_db
def test_startup_loads_enabled_features_and_skips_disabled_ones(tmp):
    async def t(h):
        a = await h.add("xi", cog_source("xi", 1)); assert (await h.rt.activate(a, 7)).ok
        b = await h.add("omicron", cog_source("omicron", 1)); assert (await h.rt.activate(b, 7)).ok
        assert (await h.rt.disable("omicron", 7)).ok
        # a "restart": a brand-new bot and runtime over the same database, with the generated folder wiped
        for m in [m for m in sys.modules if m.startswith("gen_")]:
            del sys.modules[m]
        import shutil; shutil.rmtree(tmp / "generated")
        h2 = Harness(tmp / "second", h.bot.db)
        problems = await h2.rt.load_all()
        assert problems == [] and h2.version_running("xi") == 1 and not h2.rt.loaded("omicron")
        assert (tmp / "second" / "generated" / "gen_xi.py").exists()             # the files are rebuilt from the database
    runner(tmp, t)


@needs_db
def test_startup_falls_back_to_the_previous_version_when_the_active_one_no_longer_loads(tmp):
    async def t(h):
        db = h.bot.db
        b1 = await h.add("pi", cog_source("pi", 1)); assert (await h.rt.activate(b1, 7)).ok
        b2 = await h.add("pi", cog_source("pi", 2)); assert (await h.rt.activate(b2, 7)).ok
        await db.pool.execute("UPDATE builder_builds SET code=$2 WHERE id=$1", b2, cog_source("pi", 2, boom=True))   # it broke
        for m in [m for m in sys.modules if m.startswith("gen_")]:
            del sys.modules[m]
        h2 = Harness(tmp / "second", db)
        problems = await h2.rt.load_all()
        assert len(problems) == 1 and "went back to v1" in problems[0]
        assert h2.version_running("pi") == 1
        assert (await db.bb_get(b2))["status"] == "failed" and (await db.bb_get(b1))["status"] == "active"
        assert (await db.bf_get("pi"))["active_build_id"] == b1 and (await db.bf_get("pi"))["enabled"]
    runner(tmp, t)


@needs_db
def test_startup_switches_a_broken_feature_off_when_there_is_nothing_to_fall_back_to(tmp):
    async def t(h):
        db = h.bot.db
        b1 = await h.add("rho", cog_source("rho", 1)); assert (await h.rt.activate(b1, 7)).ok
        await db.pool.execute("UPDATE builder_builds SET code=$2 WHERE id=$1", b1, cog_source("rho", 1, boom=True))
        for m in [m for m in sys.modules if m.startswith("gen_")]:
            del sys.modules[m]
        h2 = Harness(tmp / "second", db)
        problems = await h2.rt.load_all()
        assert len(problems) == 1 and "switched off" in problems[0] and "rest of the bot" in problems[0]
        assert not h2.rt.loaded("rho") and not (await db.bf_get("rho"))["enabled"]
    runner(tmp, t)


@needs_db
def test_startup_never_raises_even_when_the_database_is_unreadable(tmp):
    async def t(h):
        async def broken(): raise RuntimeError("db down")
        h.bot.db.bf_list = broken
        problems = await h.rt.load_all()
        assert len(problems) == 1 and "couldn't read" in problems[0]
    runner(tmp, t)


@needs_db
def test_store_versions_slugs_and_atomic_reject(tmp):
    async def t(h):
        db = h.bot.db
        a = await db.bb_add("sigma", "S", "r", "s", "", "code", "", "m", 1)
        b = await db.bb_add("sigma", "S", "r", "s", "", "code2", "", "m", 1)
        assert (a[1], b[1]) == (1, 2) and "sigma" in await db.bb_slugs()
        assert [r["id"] for r in await db.bb_pending()] == [a[0], b[0]]
        assert await db.bb_claim_pending(a[0], "rejected") and not await db.bb_claim_pending(a[0], "rejected")
        assert (await db.bb_find("sigma", 2))["id"] == b[0] and await db.bb_find("sigma", 9) is None
    runner(tmp, t)


# ------------------------------------------------------------------ the /build cog and its buttons (fake interactions)
from unittest.mock import AsyncMock

from discord import app_commands

from orion import config
from orion.cogs import builder as bcog


def fake_i(bot, user_id=None):
    return NS(user=NS(id=config.OWNER_ID if user_id is None else user_id), client=bot,
              response=NS(edit_message=AsyncMock(), send_message=AsyncMock(), defer=AsyncMock(), send_modal=AsyncMock()),
              followup=NS(send=AsyncMock()))


def sent(i) -> str:
    return " | ".join(str(c.args[0]) if c.args else "" for c in i.followup.send.call_args_list)


def fake_generate(slug="theta2", version=1, boom=False, log=None):
    async def gen(request, **kw):
        if log is not None:
            log.append(kw)
        s = kw.get("slug") or slug
        return builder.Draft(slug=s, title="Fake feature", summary="It does things.", notes="Check the rank names.",
                             schema_sql="", code=cog_source(s, version, boom=boom))
    return gen


@needs_db
def test_cog_loads_with_its_commands_and_only_the_patriarch_may_use_them(tmp):
    async def t(h, cog):
        names = {c.name for c in h.bot.tree.get_commands()}
        assert names == {"build"}
        assert {c.name for c in h.bot.tree.get_command("build").commands} == {
            "new", "modify", "list", "history", "show", "rollback", "disable", "enable"}
        assert await cog.interaction_check(fake_i(h.bot)) is True
        with pytest.raises(app_commands.CheckFailure, match="Sect Patriarch"):
            await cog.interaction_check(fake_i(h.bot, user_id=config.OWNER_ID + 1))
        # the review buttons refuse everyone else too
        other = fake_i(h.bot, user_id=config.OWNER_ID + 1)
        assert await bcog.ApproveButton(1).interaction_check(other) is False
        other.response.send_message.assert_awaited_once()
    runner(tmp, with_cog_wrapper(t))


def with_cog_wrapper(fn):
    """Run `fn(h, cog)` with the /build cog loaded into the test bot."""
    async def inner(h):
        await h.bot.load_extension("orion.cogs.builder")
        try:
            await fn(h, h.bot.get_cog("build"))
        finally:
            await h.bot.unload_extension("orion.cogs.builder")
    return inner


@needs_db
def test_review_message_has_the_code_attached_and_buttons_only_while_pending(tmp):
    async def t(h):
        b = await h.add("upsilon", cog_source("upsilon", 1), SQL_OK.replace("iota", "upsilon"))
        row = await h.bot.db.bb_get(b)
        kw = bcog.review_payload(row)
        assert [f.filename for f in kw["files"]] == ["upsilon_v1.py", "upsilon_v1_schema.sql"]
        assert [c.custom_id for c in kw["view"].children] == [f"build:approve:{b}", f"build:changes:{b}", f"build:reject:{b}"]
        text = " ".join(f.value for f in kw["embed"].fields)
        assert "gen_upsilon_items" in text and "snapshot" in text and "Check the rank names" not in text
        await h.bot.db.bb_set_status(b, "rejected")
        assert "view" not in bcog.review_payload(await h.bot.db.bb_get(b))
    runner(tmp, t)


@needs_db
def test_new_then_approve_goes_live_and_a_second_click_is_ignored(tmp):
    async def t(h, cog):
        i = fake_i(h.bot)
        orig = (ai.is_configured, builder.generate)
        ai.is_configured, builder.generate = (lambda: True), fake_generate("phi")
        try:
            await cog.new.callback(cog, i, "make a thing please")
        finally:
            ai.is_configured, builder.generate = orig
        i.response.defer.assert_awaited_once()
        kw = i.followup.send.call_args.kwargs
        assert kw["ephemeral"] is True and kw["view"] and kw["files"][0].filename == "phi_v1.py"
        bid = (await h.bot.db.bb_pending())[0]["id"]
        assert not h.rt.loaded("phi")                                   # nothing is loaded before approval

        click = fake_i(h.bot)
        await bcog.ApproveButton(bid).callback(click)
        click.response.edit_message.assert_awaited_once_with(view=None)
        assert h.rt.loaded("phi") and h.version_running("phi") == 1 and "is live" in sent(click)

        again = fake_i(h.bot)
        await bcog.ApproveButton(bid).callback(again)
        assert "already decided" in sent(again)
    runner(tmp, with_cog_wrapper(t))


@needs_db
def test_approving_a_broken_build_reports_it_and_leaves_the_bot_untouched(tmp):
    async def t(h, cog):
        good = await h.add("chi", cog_source("chi", 1)); assert (await h.rt.activate(good, 1)).ok
        bad = await h.add("chi", cog_source("chi", 2, boom=True))
        click = fake_i(h.bot)
        await bcog.ApproveButton(bad).callback(click)
        msg = sent(click)
        assert "Not loaded" in msg and "boom" in msg and "/build modify feature:chi" in msg and h.version_running("chi") == 1
    runner(tmp, with_cog_wrapper(t))


@needs_db
def test_reject_changes_nothing_and_cannot_be_undone_by_a_late_approve(tmp):
    async def t(h, cog):
        b = await h.add("psi", cog_source("psi", 1))
        click = fake_i(h.bot)
        await bcog.RejectButton(b).callback(click)
        assert "Rejected" in sent(click) and not h.rt.loaded("psi")
        late = fake_i(h.bot)
        await bcog.ApproveButton(b).callback(late)
        assert "already decided" in sent(late) and not h.rt.loaded("psi")
    runner(tmp, with_cog_wrapper(t))


@needs_db
def test_modify_builds_on_the_latest_non_rejected_version_and_replaces_a_waiting_draft(tmp):
    async def t(h, cog):
        log = []
        b1 = await h.add("omega", cog_source("omega", 1)); assert (await h.rt.activate(b1, 1)).ok
        waiting = await h.add("omega", cog_source("omega", 2))                  # a draft nobody decided on
        orig = (ai.is_configured, builder.generate)
        ai.is_configured, builder.generate = (lambda: True), fake_generate("omega", 3, log=log)
        try:
            i = fake_i(h.bot)
            await cog.modify.callback(cog, i, "omega", "add a Celestial role")
        finally:
            ai.is_configured, builder.generate = orig
        kw = log[0]
        assert kw["slug"] == "omega" and kw["previous_code"] == cog_source("omega", 2) and "Celestial" in kw["feedback"]
        assert (await h.bot.db.bb_get(waiting))["status"] == "rejected"            # replaced by the new draft
        new = (await h.bot.db.bb_pending())[0]
        assert new["version"] == 3 and "Celestial" in new["change_note"] and new["request"] == "req"
        missing = fake_i(h.bot)
        await cog.modify.callback(cog, missing, "nothing_here", "change it")
        assert "no feature" in missing.response.send_message.call_args.args[0]
    runner(tmp, with_cog_wrapper(t))


@needs_db
def test_request_changes_modal_creates_the_next_version_and_keeps_the_old_one_if_the_ai_fails(tmp):
    async def t(h, cog):
        b = await h.add("tau", cog_source("tau", 1))
        modal = bcog.ChangesModal(b)
        modal.text._value = "use buttons instead of a dropdown"
        orig = builder.generate
        builder.generate = fake_generate("tau", 2)
        try:
            i = fake_i(h.bot)
            await modal.on_submit(i)
        finally:
            builder.generate = orig
        assert (await h.bot.db.bb_get(b))["status"] == "rejected"
        new = (await h.bot.db.bb_pending())[0]
        assert new["version"] == 2 and new["change_note"] == "use buttons instead of a dropdown" and new["request"] == "req"
        assert i.followup.send.call_args.kwargs["view"]                              # fresh buttons on the new draft

        keep = await h.add("tau2", cog_source("tau2", 1))
        async def down(*a, **k): raise ai.AIError("couldn't reach the AI service")
        builder.generate = down
        try:
            m2 = bcog.ChangesModal(keep); m2.text._value = "anything"
            j = fake_i(h.bot)
            await m2.on_submit(j)
        finally:
            builder.generate = orig
        assert (await h.bot.db.bb_get(keep))["status"] == "pending" and f"/build show build:{keep}" in sent(j)
    runner(tmp, with_cog_wrapper(t))


@needs_db
def test_rollback_disable_enable_commands(tmp):
    async def t(h, cog):
        b1 = await h.add("up", cog_source("up", 1)); assert (await h.rt.activate(b1, 1)).ok
        b2 = await h.add("up", cog_source("up", 2)); assert (await h.rt.activate(b2, 1)).ok
        i = fake_i(h.bot)
        await cog.rollback.callback(cog, i, "up", None)
        assert "Rolled back to v1" in sent(i) and h.version_running("up") == 1
        none = fake_i(h.bot)
        await cog.rollback.callback(cog, none, "up", None)                          # v1 has nothing older
        assert "no earlier version" in none.response.send_message.call_args.args[0]
        j = fake_i(h.bot)
        await cog.rollback.callback(cog, j, "up", 2)                                 # explicit version
        assert h.version_running("up") == 2
        d = fake_i(h.bot); await cog.disable.callback(cog, d, "up")
        assert "switched off" in sent(d) and not h.rt.loaded("up")
        e = fake_i(h.bot); await cog.enable.callback(cog, e, "up")
        assert "is on" in sent(e) and h.version_running("up") == 2
        lst = fake_i(h.bot); await cog.list_.callback(cog, lst)
        assert "up" in str(lst.response.send_message.call_args.kwargs["embed"].fields[0].name)
        hist = fake_i(h.bot); await cog.history.callback(cog, hist, "up")
        assert "v2" in hist.response.send_message.call_args.args[0] and "v1" in hist.response.send_message.call_args.args[0]
    runner(tmp, with_cog_wrapper(t))


@needs_db
def test_new_refuses_without_an_api_key_and_while_another_build_is_being_written(tmp):
    async def t(h, cog):
        i = fake_i(h.bot)
        await cog.new.callback(cog, i, "make a thing please")
        assert "AI_API_KEY" in i.response.send_message.call_args.args[0]
        orig = ai.is_configured
        ai.is_configured = lambda: True
        try:
            async with h.rt.gen_lock:
                j = fake_i(h.bot)
                await cog.new.callback(cog, j, "make a thing please")
            assert "already writing" in j.response.send_message.call_args.args[0]
        finally:
            ai.is_configured = orig
    runner(tmp, with_cog_wrapper(t))


def test_buttons_round_trip_their_custom_ids():
    for cls, kind in ((bcog.ApproveButton, "approve"), (bcog.RejectButton, "reject"), (bcog.ChangesButton, "changes")):
        m = cls.__discord_ui_compiled_template__.fullmatch(f"build:{kind}:42")
        assert m and m["id"] == "42"
        assert cls(42).item.custom_id == f"build:{kind}:42"


# --------------------------------------------------------------------------- server snapshot
class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fake_guild(n_extra=0):
    cat = _Obj(id=10, name="INFO", position=0, type="category", channels=[])
    rules = _Obj(id=11, name="rules-and-info", position=0, type="text", category=cat)
    news = _Obj(id=12, name="news", position=1, type="text", category=cat)
    cat.channels = [news, rules]
    loose = _Obj(id=13, name="general", position=0, type="text", category=None)
    extra = [_Obj(id=100 + i, name=f"c{i}", position=i, type="text", category=None) for i in range(n_extra)]
    roles = [
        _Obj(id=1, name="@everyone", position=0, managed=False, is_default=lambda: True),
        _Obj(id=2, name="Gold", position=1, managed=False, is_default=lambda: False),
        _Obj(id=3, name="Orion", position=5, managed=True, is_default=lambda: False),
    ]
    return _Obj(categories=[cat], channels=[cat, rules, news, loose, *extra], roles=roles)


def test_format_server_none_is_empty():
    assert builder.format_server(None) == ""


def test_format_server_content_and_order():
    t = builder.format_server(_fake_guild())
    assert "CATEGORY INFO [10]" in t
    assert t.index("rules-and-info") < t.index("news")
    assert "NO CATEGORY" in t and "general" in t
    assert "@everyone" not in t
    assert t.index("Orion") < t.index("Gold")
    assert "bot/integration" in t


def test_format_server_truncates():
    t = builder.format_server(_fake_guild(n_extra=2000))
    assert len(t) <= builder.MAX_SERVER_CHARS + 100
    assert "cut short" in t


def test_user_message_includes_server_block():
    m = builder.build_user_message("make rules thing", server="CHANNELS x")
    assert "REAL CHANNELS AND ROLES" in m and "CHANNELS x" in m
    assert "REAL CHANNELS" not in builder.build_user_message("x")


# ------------------------------------------------------------------ the bot's API is checked before a human reads the code
def _cog(extra: str) -> str:
    return ("from discord.ext import commands\nfrom orion import config, alerts\nfrom orion.checks import require_admin\n"
            + extra + "\nasync def setup(bot):\n    pass\n")


@pytest.mark.parametrize("body, why", [
    ("async def f(bot):\n    return await bot.db.get('gen_x:a', None)\n", "not async"),
    ("async def f(bot):\n    bot.db.set('gen_x:a', 1)\n", "must be awaited"),
    ("async def f(bot):\n    await alerts.send_admin_alert('x')\n", "does not exist in orion.alerts"),
    ("X = config.NO_SUCH_THING\n", "does not exist in orion.config"),
    ("async def f(bot):\n    await bot.db.set('gen:x:a', 1)\n", "must start with `gen_x:`"),
    ("K = 'wrong:key'\nasync def f(bot):\n    return bot.db.get(K)\n", "must start with `gen_x:`"),
    ("from orion import ai\n", "not allowed"),
    ("from orion.db import Database\n", "not allowed"),
    ("from orion.checks import require_nobody\n", "has no"),
    ("import orion\n", "not `import orion`"),
])
def test_invented_or_misused_bot_api_is_caught_before_a_human_sees_it(body, why):
    with pytest.raises(DraftError, match=why):
        validate_code(_cog(body), "x")


def test_correct_use_of_the_bot_api_passes():
    validate_code(_cog("K = 'gen_x:chan'\n@app_commands_placeholder\nasync def f(bot, guild):\n    v = bot.db.get(K, None)\n"
                       "    w = bot.db.get(f'gen_x:{guild.id}')\n    await bot.db.set(K, 1)\n"
                       "    await alerts.alert_staff(bot, guild, 'hi', key='gen_x:a')\n    return config.COLOR_MAIN, require_admin\n"
                       ).replace("@app_commands_placeholder\n", ""), "x")


def test_the_rankpanel_mistakes_would_have_been_rejected():
    """The three real mistakes in the first generated /rankpanel cog: awaiting db.get, a made-up alerts function, bad keys."""
    cog = ("import discord\nfrom discord.ext import commands\nfrom orion import config, alerts\n"
           "CONFIG_CHANNEL_ID = 'gen:rank_roles:channel_id'\n"
           "class C(commands.Cog):\n    def __init__(self, bot):\n        self.bot = bot\n"
           "    async def ensure(self, guild):\n        cid = await self.bot.db.get(CONFIG_CHANNEL_ID, None)\n"
           "        await alerts.send_admin_alert('no category')\n"
           "async def setup(bot):\n    await bot.add_cog(C(bot))\n")
    with pytest.raises(DraftError):
        build_draft(answer(slug="rank_roles", code=cog))
    fixed = (cog.replace("await self.bot.db.get", "self.bot.db.get").replace("alerts.send_admin_alert('no category')",
             "alerts.alert_staff(self.bot, guild, 'no category', key='gen_rank_roles:cat')")
             .replace("gen:rank_roles:", "gen_rank_roles:"))
    assert build_draft(answer(slug="rank_roles", code=fixed)).slug == "rank_roles"


# ------------------------------------------------------------------ /build fixes found in review
@needs_db
def test_modify_after_a_rollback_builds_on_the_version_that_is_running(tmp):
    async def t(h, cog):
        log = []
        b1 = await h.add("rb", cog_source("rb", 1)); assert (await h.rt.activate(b1, 1)).ok
        b2 = await h.add("rb", cog_source("rb", 2)); assert (await h.rt.activate(b2, 1)).ok
        assert (await h.rt.activate(b1, 1, run_schema=False)).ok            # rolled back: v2 is 'superseded', v1 runs
        assert (await h.bot.db.bb_get(b2))["status"] == "superseded"
        orig = (ai.is_configured, builder.generate)
        ai.is_configured, builder.generate = (lambda: True), fake_generate("rb", 3, log=log)
        try:
            i = fake_i(h.bot)
            await cog.modify.callback(cog, i, "rb", "tweak it")
        finally:
            ai.is_configured, builder.generate = orig
        assert log[0]["previous_code"] == cog_source("rb", 1)               # NOT the rolled-back v2
        assert (await h.bot.db.bb_pending())[0]["version"] == 3
        assert "Here is v3" in sent(i) and "built on v1" in sent(i)
    runner(tmp, with_cog_wrapper(t))


@needs_db
def test_the_original_request_survives_repeated_changes(tmp):
    async def t(h, cog):
        b1 = await h.add("orq", cog_source("orq", 1)); assert (await h.rt.activate(b1, 1)).ok
        seen = []

        async def gen(request, **kw):
            seen.append((request, kw.get("feedback")))
            return builder.Draft(slug=kw["slug"], title="T", summary="s", notes="", schema_sql="",
                                 code=cog_source(kw["slug"], len(seen) + 1))
        orig = (ai.is_configured, builder.generate)
        ai.is_configured, builder.generate = (lambda: True), gen
        try:
            await cog.modify.callback(cog, fake_i(h.bot), "orq", "make it blue")
            await cog.modify.callback(cog, fake_i(h.bot), "orq", "now red")
        finally:
            ai.is_configured, builder.generate = orig
        assert [s[0] for s in seen] == ["req", "req"]                       # the AI always sees the original request
        assert "make it blue" in seen[0][1] and "now red" in seen[1][1]
        hist = fake_i(h.bot); await cog.history.callback(cog, hist, "orq")
        assert "Changes: now red" in hist.response.send_message.call_args.args[0]
        embed = bcog.review_embed(await h.bot.db.bb_get((await h.bot.db.bb_pending())[0]["id"]), pending=True)
        assert {f.name for f in embed.fields} >= {"Changes asked for", "Original request"}
    runner(tmp, with_cog_wrapper(t))


@needs_db
def test_a_rollback_that_hits_a_discord_error_does_not_strike_the_old_version_off(tmp):
    async def t(h, cog):
        b1 = await h.add("poison", cog_source("poison", 1)); assert (await h.rt.activate(b1, 1)).ok
        b2 = await h.add("poison", cog_source("poison", 2)); assert (await h.rt.activate(b2, 1)).ok
        h.fail_sync = True                                                  # Discord refuses the command sync
        r = await h.rt.activate(b1, 1, run_schema=False)
        assert not r.ok and h.version_running("poison") == 2                # v2 is still what runs
        assert (await h.bot.db.bb_get(b1))["status"] == "superseded"        # v1 is still a valid rollback target
        assert (await h.bot.db.bb_previous_working("poison", 2))["id"] == b1
        h.fail_sync = False
        assert (await h.rt.activate(b1, 1, run_schema=False)).ok and h.version_running("poison") == 1
    runner(tmp, with_cog_wrapper(t))


@needs_db
def test_modify_and_history_autocomplete_include_features_that_never_loaded(tmp):
    async def t(h, cog):
        await h.add("never", cog_source("never", 1, boom=True))             # a draft: no builder_features row yet
        ok = await h.add("shown", cog_source("shown", 1)); assert (await h.rt.activate(ok, 1)).ok
        i = fake_i(h.bot)
        assert {c.value for c in await cog._modify_ac(i, "")} == {"never", "shown"}
        assert {c.value for c in await cog._history_ac(i, "nev")} == {"never"}
        assert {c.value for c in await cog._rollback_ac(i, "")} == {"shown"}   # rollback/enable/disable need an approved one
    runner(tmp, with_cog_wrapper(t))


@needs_db
def test_an_approve_that_changed_nothing_hands_the_buttons_back(tmp):
    async def t(h, cog):
        b = await h.add("kap", cog_source("kap", 1), "CREATE TABLE IF NOT EXISTS gen_kap_t (id INT);")

        async def boom(_): raise OSError("disk full")
        h.rt.take_snapshot = boom
        click = fake_i(h.bot)
        await bcog.ApproveButton(b).callback(click)
        last = click.followup.send.call_args_list[-1]
        assert "still waiting" in last.args[0] and "disk full" in last.args[0] and last.kwargs["view"]
        assert (await h.bot.db.bb_get(b))["status"] == "pending"
    runner(tmp, with_cog_wrapper(t))


@needs_db
def test_build_list_survives_more_than_25_features(tmp):
    async def t(h, cog):
        for n in range(26):
            b = await h.add(f"lf{n:02d}", cog_source(f"lf{n:02d}", 1)); assert (await h.rt.activate(b, 1)).ok
        i = fake_i(h.bot)
        await cog.list_.callback(cog, i)
        embed = i.response.send_message.call_args.kwargs["embed"]
        assert len(embed.fields) <= 25 and "Showing 24 of 26" in embed.description
    runner(tmp, with_cog_wrapper(t))
