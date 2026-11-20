"""Cloud LLM client (transport only). It has no Discord or database code, so it is easy to test.

`complete()` sends one system prompt and one user message to the configured provider and returns the text. What the model is
asked, and what is done with its answer, lives in ticket_ai.py. The model never decides anything that matters on its own.

Set in .env:  AI_API_KEY (required), AI_PROVIDER (gemini | anthropic | openai, default gemini), AI_MODEL, AI_BASE_URL (optional,
for any OpenAI-compatible service). The model can also be changed at runtime with `/ai set model`.
"""
import logging
import re

import aiohttp

from . import config

log = logging.getLogger("orion.ai")

TIMEOUT_SECONDS = 25
MAX_REPLY_CHARS = 900            # the answer is one short paragraph; Discord allows 2000 per message
MAX_INPUT_CHARS = 3000

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

_MENTION_RE = re.compile(r"@(everyone|here)|<@[!&]?\d+>", re.I)


class AIError(Exception):
    """A plain-words reason the model couldn't be used (never contains the API key)."""


def is_configured() -> bool:
    return bool(config.AI_API_KEY)


def clean_reply(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    """Make a model reply safe to post: no pings, trimmed, never longer than `limit`."""
    text = _MENTION_RE.sub("", text or "").replace("\r", "").strip()
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


async def complete(system: str, user: str, max_tokens: int = 400, session: aiohttp.ClientSession | None = None, *,
                   model: str | None = None, json_mode: bool = False, raw: bool = False) -> str:
    """Ask the configured model. Raises AIError with a short reason on any failure.
    json_mode asks the provider for JSON where it supports that; raw=True returns the text untouched (for JSON),
    otherwise it is cleaned and capped like a chat message."""
    model = (model or config.AI_MODEL).strip()
    if not config.AI_API_KEY:
        raise AIError("AI_API_KEY is not set in .env.")
    provider = config.AI_PROVIDER
    if provider == "gemini":
        url = config.AI_BASE_URL or GEMINI_URL.format(model=model)
        headers = {"x-goog-api-key": config.AI_API_KEY, "content-type": "application/json"}
        # room for "thinking" tokens on Gemini 2.5+ models, which count against the output limit
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": max(max_tokens, 1024),
                                     **({"responseMimeType": "application/json"} if json_mode else {})}}
    elif provider == "anthropic":
        url = config.AI_BASE_URL or ANTHROPIC_URL
        headers = {"x-api-key": config.AI_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        body = {"model": model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
    elif provider == "openai":
        url = config.AI_BASE_URL or OPENAI_URL
        headers = {"Authorization": f"Bearer {config.AI_API_KEY}", "content-type": "application/json"}
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
    else:
        raise AIError(f"AI_PROVIDER must be 'gemini', 'anthropic' or 'openai' (it is '{provider}').")

    own = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.post(url, json=body, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as r:
            if r.status != 200:
                detail = (await r.text())[:200].replace(config.AI_API_KEY, "***")
                raise AIError(f"the AI service answered HTTP {r.status}: {detail}")
            data = await r.json(content_type=None)
    except AIError:
        raise
    except (aiohttp.ClientError, TimeoutError) as e:      # asyncio.TimeoutError is TimeoutError on 3.11+
        raise AIError(f"couldn't reach the AI service ({type(e).__name__}).") from e
    finally:
        if own:
            await session.close()

    try:
        if provider == "gemini":
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        elif provider == "anthropic":
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        else:
            text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise AIError("the AI service sent a reply in a shape I didn't expect.") from e
    text = (text or "").strip() if raw else clean_reply(text)
    if not text:
        raise AIError("the AI service sent an empty reply.")
    return text
