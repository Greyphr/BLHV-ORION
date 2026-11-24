"""AI provider switcher: fallback when a provider is overloaded, cooldowns, pinning, validation and the /ai provider commands.
Uses small local web servers pretending to be the AI services, so nothing leaves the machine."""
import asyncio
import os
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from orion import ai, config

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run the PostgreSQL tests")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    ai.reset_state()
    monkeypatch.setattr(ai, "RETRY_DELAY", 0)
    monkeypatch.setattr(config, "AI_PROVIDER", "openai")
    monkeypatch.setattr(config, "AI_API_KEY", "sk-main")
    monkeypatch.setattr(config, "AI_MODEL", "main-model")
    monkeypatch.setattr(config, "AI_BASE_URL", "")
    monkeypatch.setattr(config, "AI_FALLBACKS", [])
    yield
    ai.reset_state()


class Fake:
    """Fake AI services on one local port. Each route has a queue of statuses (then `status`), an optional delay, and records hits."""

    def __init__(self):
        self.routes: dict[str, dict] = {}

    def route(self, path, reply="ok", status=200, delay=0.0, kind="openai"):
        self.routes[path] = {"reply": reply, "status": status, "delay": delay, "kind": kind, "queue": [], "hits": []}
        return self.routes[path]

    async def __aenter__(self):
        app = web.Application()
        for path, st in self.routes.items():
            async def handler(request, st=st):
                st["hits"].append((dict(request.headers), await request.json()))
                if st["delay"]:
                    await asyncio.sleep(st["delay"])
                code = st["queue"].pop(0) if st["queue"] else st["status"]
                if code != 200:
                    return web.Response(status=code, text="no sk-main gk-groq here")
                if st["kind"] == "gemini":
                    return web.json_response({"candidates": [{"content": {"parts": [{"text": st["reply"]}]}}]})
                if st["kind"] == "anthropic":
                    return web.json_response({"content": [{"type": "text", "text": st["reply"]}]})
                return web.json_response({"choices": [{"message": {"content": st["reply"]}}]})
            app.router.add_post(path, handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *a):
        await self.runner.cleanup()


def fallback(n=1, name="groq", base="", key="gk-groq", model="groq-model", kind="openai"):
    return {"index": n, "name": name, "kind": kind, "key": key, "model": model, "base_url": base}


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ fallback
def test_overloaded_main_provider_hands_over_to_the_next(monkeypatch):
    async def t():
        f = Fake()
        f.route("/main/chat/completions", status=503); f.route("/groq/v1/chat/completions", reply="from groq")
        async with f:
            monkeypatch.setattr(config, "AI_BASE_URL", f.base + "/main/chat/completions")
            monkeypatch.setattr(config, "AI_FALLBACKS", [fallback(base=f.base + "/groq/v1")])
            out = await ai.complete("sys", "hi", model="override")
            assert out == "from groq"
            main, groq = f.routes["/main/chat/completions"], f.routes["/groq/v1/chat/completions"]
            assert len(main["hits"]) == 1 and len(groq["hits"]) == 1            # no retry on main: the next provider is faster
            assert main["hits"][0][1]["model"] == "override"                     # a model override is for the MAIN provider only
            assert groq["hits"][0][1]["model"] == "groq-model"
            assert groq["hits"][0][0]["Authorization"] == "Bearer gk-groq"       # each provider gets its own key
    run(t())


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504, 401, 403, 404])
def test_any_provider_failure_falls_back(monkeypatch, code):
    async def t():
        f = Fake()
        f.route("/main/chat/completions", status=code); f.route("/groq/v1/chat/completions", reply="from groq")
        async with f:
            monkeypatch.setattr(config, "AI_BASE_URL", f.base + "/main/chat/completions")
            monkeypatch.setattr(config, "AI_FALLBACKS", [fallback(base=f.base + "/groq/v1")])
            assert await ai.complete("s", "u") == "from groq"
    run(t())


def test_timeout_and_empty_replies_fall_back_too(monkeypatch):
    async def t():
        f = Fake()
        f.route("/main/chat/completions", delay=1.0); f.route("/groq/v1/chat/completions", reply="from groq")
        async with f:
            monkeypatch.setattr(config, "AI_BASE_URL", f.base + "/main/chat/completions")
            monkeypatch.setattr(config, "AI_FALLBACKS", [fallback(base=f.base + "/groq/v1")])
            assert await ai.complete("s", "u", timeout=0.2) == "from groq"
            f.routes["/main/chat/completions"].update(delay=0, reply="@everyone")      # nothing left after cleaning
            ai.reset_state()
            assert await ai.complete("s", "u") == "from groq"
    run(t())


def test_a_failing_provider_is_skipped_for_a_while_then_tried_again(monkeypatch):
    async def t():
        f = Fake()
        main = f.route("/main/chat/completions", status=503, reply="from main")
        groq = f.route("/groq/v1/chat/completions", reply="from groq")
        async with f:
            monkeypatch.setattr(config, "AI_BASE_URL", f.base + "/main/chat/completions")
            monkeypatch.setattr(config, "AI_FALLBACKS", [fallback(base=f.base + "/groq/v1")])
            clock = [1000.0]
            monkeypatch.setattr(ai, "_now", lambda: clock[0])
            assert await ai.complete("s", "u") == "from groq" and len(main["hits"]) == 1
            assert await ai.complete("s", "u") == "from groq" and len(main["hits"]) == 1      # main not even contacted
            st = ai.provider_status()
            assert [s["name"] for s in st] == ["groq", "openai"] and 0 < st[1]["cooling_for"] <= 60
            main["status"] = 200
            clock[0] += 61                                                                    # cooldown over
            assert await ai.complete("s", "u") == "from main" and len(main["hits"]) == 2
            assert [s["name"] for s in ai.provider_status()][0] == "openai"
    run(t())


def test_a_bad_key_is_skipped_for_ten_minutes(monkeypatch):
    async def t():
        f = Fake()
        main = f.route("/main/chat/completions", status=401, reply="from main"); f.route("/groq/v1/chat/completions", reply="from groq")
        async with f:
            monkeypatch.setattr(config, "AI_BASE_URL", f.base + "/main/chat/completions")
            monkeypatch.setattr(config, "AI_FALLBACKS", [fallback(base=f.base + "/groq/v1")])
            clock = [0.0]
            monkeypatch.setattr(ai, "_now", lambda: clock[0])
            await ai.complete("s", "u")
            main["status"] = 200
            clock[0] += 300
            assert await ai.complete("s", "u") == "from groq"             # still skipped after 5 minutes
            clock[0] += 400
            assert await ai.complete("s", "u") == "from main"
    run(t())


def test_when_everyone_fails_the_error_names_each_provider_and_leaks_no_key(monkeypatch):
    async def t():
        f = Fake()
        f.route("/main/chat/completions", status=503); f.route("/groq/v1/chat/completions", status=429)
        async with f:
            monkeypatch.setattr(config, "AI_BASE_URL", f.base + "/main/chat/completions")
            monkeypatch.setattr(config, "AI_FALLBACKS", [fallback(base=f.base + "/groq/v1")])
            with pytest.raises(ai.AIError) as e:
                await ai.complete("s", "u")
            msg = str(e.value)
            assert "Every AI provider failed" in msg and "openai:" in msg and "groq:" in msg and "503" in msg and "429" in msg
            assert "sk-main" not in msg and "gk-groq" not in msg
    run(t())


def test_providers_that_are_cooling_down_are_still_tried_when_nothing_else_works(monkeypatch):
    async def t():
        f = Fake()
        main = f.route("/main/chat/completions", status=503, reply="from main"); groq = f.route("/groq/v1/chat/completions", status=503)
        async with f:
            monkeypatch.setattr(config, "AI_BASE_URL", f.base + "/main/chat/completions")
            monkeypatch.setattr(config, "AI_FALLBACKS", [fallback(base=f.base + "/groq/v1")])
            with pytest.raises(ai.AIError):
                await ai.complete("s", "u")
            main["status"] = 200
            assert await ai.complete("s", "u") == "from main"        # both are cooling, yet the call still goes out
            assert len(main["hits"]) == 2
    run(t())


# ------------------------------------------------------------------ one provider only
def test_with_a_single_provider_a_busy_answer_is_retried_once(monkeypatch):
    async def t():
        f = Fake()
        main = f.route("/main/chat/completions", reply="finally")
        main["queue"] = [503]
        async with f:
            monkeypatch.setattr(config, "AI_BASE_URL", f.base + "/main/chat/completions")
            assert await ai.complete("s", "u") == "finally" and len(main["hits"]) == 2
            main["queue"] = [503, 503]
            with pytest.raises(ai.AIError, match="HTTP 503"):                 # the plain reason, not the multi-provider summary
                await ai.complete("s", "u")
            main["queue"] = [401]
            hits = len(main["hits"])
            with pytest.raises(ai.AIError, match="HTTP 401"):
                await ai.complete("s", "u")
            assert len(main["hits"]) == hits + 1                              # a bad key is not retried
    run(t())


def test_json_mode_is_asked_again_without_response_format_if_the_service_rejects_it(monkeypatch):
    async def t():
        f = Fake()
        main = f.route("/main/chat/completions", reply='{"a":1}')
        main["queue"] = [400]
        async with f:
            monkeypatch.setattr(config, "AI_BASE_URL", f.base + "/main/chat/completions")
            assert await ai.complete("s", "u", json_mode=True, raw=True) == '{"a":1}'
            assert "response_format" in main["hits"][0][1] and "response_format" not in main["hits"][1][1]
    run(t())


# ------------------------------------------------------------------ pinning (the manual switch)
def test_pinning_a_provider_puts_it_first_and_auto_goes_back(monkeypatch):
    async def t():
        f = Fake()
        main, groq = f.route("/main/chat/completions", reply="from main"), f.route("/groq/v1/chat/completions", reply="from groq")
        async with f:
            monkeypatch.setattr(config, "AI_BASE_URL", f.base + "/main/chat/completions")
            monkeypatch.setattr(config, "AI_FALLBACKS", [fallback(base=f.base + "/groq/v1")])
            assert await ai.complete("s", "u") == "from main"
            ai.set_preferred("groq")
            assert ai.get_preferred() == "groq" and ai.active_provider().name == "groq"
            assert await ai.complete("s", "u") == "from groq"
            groq["status"] = 503                                              # the pinned one goes down: the other steps in
            assert await ai.complete("s", "u") == "from main"
            ai.set_preferred("auto")
            assert ai.get_preferred() is None
            with pytest.raises(ValueError, match="no AI provider called 'nope'"):
                ai.set_preferred("nope")
    run(t())


def test_only_asks_exactly_one_provider_with_no_fallback_and_no_retry(monkeypatch):
    async def t():
        f = Fake()
        main, groq = f.route("/main/chat/completions", status=503), f.route("/groq/v1/chat/completions", reply="from groq")
        async with f:
            monkeypatch.setattr(config, "AI_BASE_URL", f.base + "/main/chat/completions")
            monkeypatch.setattr(config, "AI_FALLBACKS", [fallback(base=f.base + "/groq/v1")])
            with pytest.raises(ai.AIError, match="503"):
                await ai.complete("s", "u", only="openai")
            assert len(main["hits"]) == 1 and not groq["hits"]
            assert await ai.complete("s", "u", only="groq") == "from groq"
            with pytest.raises(ai.AIError, match="no AI provider called 'x'"):
                await ai.complete("s", "u", only="x")
    run(t())


def test_other_provider_kinds_work_as_fallbacks(monkeypatch):
    async def t():
        f = Fake()
        f.route("/main/chat/completions", status=503)
        gem = f.route("/gem", reply="from gemini2", kind="gemini"); cl = f.route("/cl", reply="from claude", kind="anthropic")
        async with f:
            monkeypatch.setattr(config, "AI_BASE_URL", f.base + "/main/chat/completions")
            monkeypatch.setattr(config, "AI_FALLBACKS", [
                fallback(1, "gemini2", f.base + "/gem", "gk2", "gemini-lite", "gemini"),
                fallback(2, "claude", f.base + "/cl", "ak", "claude-x", "anthropic")])
            assert await ai.complete("s", "u") == "from gemini2"
            assert gem["hits"][0][0]["x-goog-api-key"] == "gk2"
            gem["status"] = 500
            ai.reset_state()
            assert await ai.complete("s", "u") == "from claude" and cl["hits"][0][1]["model"] == "claude-x"
    run(t())


# ------------------------------------------------------------------ configuration
def test_fallback_blocks_are_read_from_the_environment(monkeypatch):
    for k in list(os.environ):
        if k.startswith("AI_FALLBACK_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("AI_FALLBACK_1_NAME", "Groq")
    monkeypatch.setenv("AI_FALLBACK_1_KEY", " gk ")
    monkeypatch.setenv("AI_FALLBACK_1_MODEL", "m1")
    monkeypatch.setenv("AI_FALLBACK_1_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("AI_FALLBACK_3_MODEL", "only-a-model")            # half filled: kept, so the bot can say what's missing
    got = config._load_ai_fallbacks()
    assert got[0] == {"index": 1, "name": "groq", "kind": "openai", "key": "gk", "model": "m1",
                      "base_url": "https://api.groq.com/openai/v1"}
    assert [g["index"] for g in got] == [1, 3] and got[1]["name"] == "fallback3"


@pytest.mark.parametrize("raw, problem", [
    (fallback(key=""), "AI_FALLBACK_1_KEY is empty"),
    (fallback(model="", base="https://x/v1"), "AI_FALLBACK_1_MODEL is empty"),
    (fallback(base=""), "AI_FALLBACK_1_BASE_URL is empty"),
    (fallback(base="api.groq.com/v1"), "must start with http"),
    (fallback(kind="llama", base="https://x"), "KIND must be"),
    (fallback(name="auto", base="https://x/v1"), "isn't usable"),
    (fallback(name="Bad Name!", base="https://x/v1"), "isn't usable"),
    (fallback(name="openai", base="https://x/v1"), "already used"),      # same as the main provider's name
])
def test_a_misconfigured_fallback_is_skipped_with_a_reason_and_never_breaks_the_rest(monkeypatch, raw, problem):
    good = fallback(2, "ok", "https://ok.example/v1")
    monkeypatch.setattr(config, "AI_FALLBACKS", [raw, good])
    providers, problems = ai.chain()
    assert [p.name for p in providers] == ["openai", "ok"]
    assert len(problems) == 1 and problem in problems[0]


def test_base_url_may_be_a_base_or_the_full_endpoint(monkeypatch):
    monkeypatch.setattr(config, "AI_FALLBACKS", [fallback(1, "a", "https://api.groq.com/openai/v1"),
                                                 fallback(2, "b", "https://x.example/v1/"),
                                                 fallback(3, "c", "https://y.example/v1/chat/completions")])
    assert [p.base_url for p in ai.chain()[0][1:]] == ["https://api.groq.com/openai/v1/chat/completions",
                                                        "https://x.example/v1/chat/completions",
                                                        "https://y.example/v1/chat/completions"]


def test_a_fallback_alone_is_enough_and_the_main_key_can_be_empty(monkeypatch):
    async def t():
        f = Fake()
        f.route("/g/v1/chat/completions", reply="from groq")
        async with f:
            monkeypatch.setattr(config, "AI_API_KEY", "")
            assert not ai.is_configured()
            monkeypatch.setattr(config, "AI_FALLBACKS", [fallback(base=f.base + "/g/v1")])
            assert ai.is_configured()
            assert await ai.complete("s", "u", model="ignored-for-fallbacks") == "from groq"
            assert f.routes["/g/v1/chat/completions"]["hits"][0][1]["model"] == "groq-model"
    run(t())


def test_nothing_configured_says_so(monkeypatch):
    monkeypatch.setattr(config, "AI_API_KEY", "")
    with pytest.raises(ai.AIError, match="AI_API_KEY is not set"):
        run(ai.complete("s", "u"))


# ------------------------------------------------------------------ the /ai provider commands
def fake_i(user_id=None):
    return NS(user=NS(id=config.OWNER_ID if user_id is None else user_id),
              response=NS(send_message=AsyncMock(), defer=AsyncMock()), followup=NS(send=AsyncMock()))


def with_cog(fn):
    async def go():
        import discord
        from discord.ext import commands
        from orion.db import Database
        db = Database(DSN)
        await db.connect()
        try:
            await db.pool.execute("DELETE FROM config WHERE key = $1", ai.PREFERRED_KEY)
            bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
            bot.db = db
            await bot.load_extension("orion.cogs.ai_admin")
            try:
                await fn(bot, bot.get_cog("ai"))
            finally:
                await bot.unload_extension("orion.cogs.ai_admin")
        finally:
            await db.pool.execute("DELETE FROM config WHERE key = $1", ai.PREFERRED_KEY)
            await db.close()
    asyncio.run(go())


@needs_db
def test_provider_list_use_and_test_commands(monkeypatch):
    async def t(bot, cog):
        f = Fake()
        f.route("/main/chat/completions", status=503); f.route("/groq/v1/chat/completions", reply="OK")
        async with f:
            monkeypatch.setattr(config, "AI_BASE_URL", f.base + "/main/chat/completions")
            monkeypatch.setattr(config, "AI_FALLBACKS", [fallback(base=f.base + "/groq/v1"), fallback(2, "dead", key="")])
            # the list shows the order, the skipped fallback, and who is having trouble
            await ai.complete("s", "u")
            i = fake_i()
            await cog.provider_list.callback(cog, i)
            e = i.response.send_message.call_args.kwargs["embed"]
            assert "groq" in e.description and "having trouble" in e.description and "AI_FALLBACK_2_KEY" in e.fields[-1].value
            # use: pins, persists, and a stranger's name is refused
            i = fake_i()
            await cog.provider_use.callback(cog, i, "Groq")
            assert ai.get_preferred() == "groq" and bot.db.get(ai.PREFERRED_KEY, None) == "groq"
            i = fake_i()
            await cog.provider_use.callback(cog, i, "nope")
            assert "no AI provider" in i.response.send_message.call_args.args[0] and ai.get_preferred() == "groq"
            # test: asks every provider on its own, no fallback, and reports each result
            i = fake_i()
            await cog.provider_test.callback(cog, i, None)
            report = i.followup.send.call_args.args[0]
            assert "❌ **openai**" in report and "503" in report and "✅ **groq**" in report
            i = fake_i()
            await cog.provider_test.callback(cog, i, "zzz")
            assert "no AI provider" in i.response.send_message.call_args.args[0]
            # auto: unpins
            await cog.provider_use.callback(cog, fake_i(), "auto")
            assert ai.get_preferred() is None and bot.db.get(ai.PREFERRED_KEY, None) == "auto"
            # every command is for Sect Admins and above only, like the rest of /ai
            assert cog.interaction_check.__func__ is type(cog).interaction_check
    with_cog(t)


@needs_db
def test_the_pinned_provider_is_restored_when_the_bot_restarts(monkeypatch):
    async def go():
        import discord
        from discord.ext import commands
        from orion.db import Database
        db = Database(DSN)
        await db.connect()
        try:
            monkeypatch.setattr(config, "AI_FALLBACKS", [fallback(base="https://g.example/v1")])
            await db.set(ai.PREFERRED_KEY, "groq")
            bot = commands.Bot(command_prefix="!", intents=discord.Intents.none()); bot.db = db
            ai.reset_state()
            await bot.load_extension("orion.cogs.ai_admin")
            assert ai.get_preferred() == "groq"
            await bot.unload_extension("orion.cogs.ai_admin")
            await db.set(ai.PREFERRED_KEY, "removed-from-env")              # .env changed while the bot was off
            ai.reset_state()
            await bot.load_extension("orion.cogs.ai_admin")
            assert ai.get_preferred() is None                                # falls back to the normal order, no crash
            await bot.unload_extension("orion.cogs.ai_admin")
            await db.set(ai.PREFERRED_KEY, "auto")                           # what /ai provider use auto leaves behind
            ai.reset_state()
            await bot.load_extension("orion.cogs.ai_admin")
            assert ai.get_preferred() is None
            await bot.unload_extension("orion.cogs.ai_admin")
        finally:
            await db.pool.execute("DELETE FROM config WHERE key = $1", ai.PREFERRED_KEY)
            await db.close()
    asyncio.run(go())
