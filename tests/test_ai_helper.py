"""The AI client (transport), tested against a local fake AI server."""
import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

import asyncio
from types import SimpleNamespace

import discord
import pytest
from aiohttp import web

from orion import ai, config


# ------------------------------------------------------------- the client
def test_clean_reply_strips_pings_and_caps_length():
    assert ai.clean_reply("hi @everyone and @here <@123> <@&456> ok") == "hi and ok"
    long = ai.clean_reply("word " * 500)
    assert len(long) <= ai.MAX_REPLY_CHARS + 1 and long.endswith("…")
    assert ai.clean_reply(None) == ""


@pytest.fixture
def fake_ai(monkeypatch):
    """A local web server pretending to be the AI service. `state` lets a test change what it answers."""
    state = {"status": 200, "seen": []}

    async def anthropic(request):
        state["seen"].append((dict(request.headers), await request.json()))
        if state["status"] != 200:
            return web.Response(status=state["status"], text="boom sk-secret-key")
        return web.json_response({"content": [{"type": "text", "text": "Welcome aboard! @everyone"}]})

    async def gemini(request):
        state["seen"].append((dict(request.headers), await request.json()))
        return web.json_response({"candidates": [{"content": {"parts": [{"text": "Hi from "}, {"text": "Gemini @here"}]}}]})

    async def openai(request):
        state["seen"].append((dict(request.headers), await request.json()))
        return web.json_response({"choices": [{"message": {"content": "Hello from openai"}}]})

    async def run():
        app = web.Application()
        app.router.add_post("/anthropic", anthropic)
        app.router.add_post("/openai", openai)
        app.router.add_post("/gemini", gemini)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return runner, site._server.sockets[0].getsockname()[1]

    loop = asyncio.new_event_loop()
    runner, port = loop.run_until_complete(run())
    monkeypatch.setattr(config, "AI_API_KEY", "sk-secret-key")
    state["port"], state["loop"] = port, loop
    yield state
    loop.run_until_complete(runner.cleanup())
    loop.close()


def test_anthropic_call_and_reply_is_cleaned(fake_ai, monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "AI_MODEL", "some-model")
    monkeypatch.setattr(config, "AI_BASE_URL", f"http://127.0.0.1:{fake_ai['port']}/anthropic")
    text = fake_ai["loop"].run_until_complete(ai.complete("You are Orion", "I want the beta role"))
    assert text == "Welcome aboard!"                                      # the @everyone was removed
    headers, body = fake_ai["seen"][0]
    assert headers["x-api-key"] == "sk-secret-key" and body["model"] == "some-model"
    assert "Orion" in body["system"] and "I want the beta role" in body["messages"][0]["content"]


def test_gemini_call(fake_ai, monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "gemini")
    monkeypatch.setattr(config, "AI_MODEL", "gemini-x")
    monkeypatch.setattr(config, "AI_BASE_URL", f"http://127.0.0.1:{fake_ai['port']}/gemini")
    assert fake_ai["loop"].run_until_complete(ai.complete("You are Orion", "beta please")) == "Hi from Gemini"
    headers, body = fake_ai["seen"][0]
    assert headers["x-goog-api-key"] == "sk-secret-key"
    assert "Orion" in body["systemInstruction"]["parts"][0]["text"]
    assert "beta please" in body["contents"][0]["parts"][0]["text"]


def test_default_provider_is_gemini():
    assert config.AI_PROVIDER == "gemini" and config.AI_MODEL.startswith("gemini")


def test_openai_call(fake_ai, monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "openai")
    monkeypatch.setattr(config, "AI_MODEL", "m")
    monkeypatch.setattr(config, "AI_BASE_URL", f"http://127.0.0.1:{fake_ai['port']}/openai")
    assert fake_ai["loop"].run_until_complete(ai.complete("You are Orion", "a")) == "Hello from openai"
    assert fake_ai["seen"][0][0]["Authorization"] == "Bearer sk-secret-key"


def test_http_error_becomes_aierror_without_leaking_the_key(fake_ai, monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "AI_BASE_URL", f"http://127.0.0.1:{fake_ai['port']}/anthropic")
    fake_ai["status"] = 500
    with pytest.raises(ai.AIError) as e:
        fake_ai["loop"].run_until_complete(ai.complete("You are Orion", "a"))
    assert "500" in str(e.value) and "sk-secret-key" not in str(e.value)


def test_not_configured_and_bad_provider(monkeypatch):
    monkeypatch.setattr(config, "AI_API_KEY", "")
    assert not ai.is_configured()
    with pytest.raises(ai.AIError):
        asyncio.run(ai.complete("s", "u"))
    monkeypatch.setattr(config, "AI_API_KEY", "k")
    monkeypatch.setattr(config, "AI_PROVIDER", "nope")
    with pytest.raises(ai.AIError):
        asyncio.run(ai.complete("s", "u"))


def test_model_override_json_mode_and_raw(fake_ai, monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "gemini")
    monkeypatch.setattr(config, "AI_MODEL", "default-model")
    monkeypatch.setattr(config, "AI_BASE_URL", f"http://127.0.0.1:{fake_ai['port']}/gemini")
    out = fake_ai["loop"].run_until_complete(ai.complete("s", "u", model="other", json_mode=True, raw=True))
    assert out == "Hi from Gemini @here"                                  # raw: nothing stripped
    assert fake_ai["seen"][0][1]["generationConfig"]["responseMimeType"] == "application/json"
    monkeypatch.setattr(config, "AI_PROVIDER", "openai")
    monkeypatch.setattr(config, "AI_BASE_URL", f"http://127.0.0.1:{fake_ai['port']}/openai")
    fake_ai["loop"].run_until_complete(ai.complete("s", "u", model="gpt-x", json_mode=True))
    body = fake_ai["seen"][1][1]
    assert body["model"] == "gpt-x" and body["response_format"] == {"type": "json_object"}


def test_clean_reply_limit_parameter():
    assert len(ai.clean_reply("word " * 500, 100)) <= 101
