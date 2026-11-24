"""Cloud LLM client (transport only). It has no Discord or database code, so it is easy to test.

`complete()` sends one system prompt and one user message to an AI provider and returns the text. What the model is asked, and
what is done with its answer, lives in ticket_ai.py. The model never decides anything that matters on its own.

Providers. The main one is set in .env:  AI_API_KEY, AI_PROVIDER (gemini | anthropic | openai, default gemini), AI_MODEL,
AI_BASE_URL (optional, for any OpenAI-compatible service). Up to six more are fallbacks: AI_FALLBACK_1_NAME / _KIND / _KEY /
_MODEL / _BASE_URL ... AI_FALLBACK_6_*. If the provider being used fails (Gemini's "high demand" 503, rate limits, timeouts, a
bad key) the next one answers instead. A provider that just failed is skipped for a while so every request doesn't wait for it
again. `/ai provider use` pins one provider first (`set_preferred()` is what it calls). `/ai set model` and BUILDER_MODEL name a
model for the MAIN provider only: each fallback has its own model in .env.
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

from . import config

log = logging.getLogger("orion.ai")

TIMEOUT_SECONDS = 25
MAX_REPLY_CHARS = 900            # the answer is one short paragraph; Discord allows 2000 per message
MAX_INPUT_CHARS = 3000
KINDS = ("gemini", "anthropic", "openai")
COOLDOWN_BUSY = 60.0             # seconds to skip a provider after 429 / 5xx / timeout / unreachable
COOLDOWN_BAD_KEY = 600.0         # ... after 401 / 403 / 404 (a key or model problem doesn't fix itself in a minute)
RETRY_DELAY = 1.5                # with only ONE provider configured, a busy answer is retried once after this many seconds
PREFERRED_KEY = "ai_provider"    # where /ai provider use keeps the pinned provider (the bot's config store)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

_MENTION_RE = re.compile(r"@(everyone|here)|<@[!&]?\d+>", re.I)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
_now = time.monotonic            # tests replace this to move time forward


class AIError(Exception):
    """A plain-words reason the model couldn't be used (never contains an API key)."""


class ProviderError(AIError):
    """One provider failed. `retryable` = busy/overloaded (worth another try), `cooldown` = seconds to skip it afterwards."""

    def __init__(self, message: str, *, status: Optional[int] = None, retryable: bool = False, cooldown: float = 0.0):
        super().__init__(message)
        self.status, self.retryable, self.cooldown = status, retryable, cooldown


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str            # gemini | anthropic | openai (openai = any OpenAI-compatible service)
    key: str
    model: str
    base_url: str = ""
    primary: bool = False


@dataclass
class _Health:
    down_until: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0
    last_ok_at: float = 0.0
    ok: int = 0
    failed: int = 0


_health: dict[str, _Health] = {}
_preferred: Optional[str] = None
_last_answered_by: Optional[str] = None


def reset_state() -> None:
    """Forget all provider health and the pinned provider (used by tests)."""
    global _preferred, _last_answered_by
    _health.clear()
    _preferred = _last_answered_by = None


# --------------------------------------------------------------------------- the provider list
def chain() -> tuple[list[Provider], list[str]]:
    """The configured providers in .env order (main first), and plain-words problems for any that were skipped.
    Read from config on every call, so a changed setting takes effect at once."""
    out: list[Provider] = []
    problems: list[str] = []
    seen: set[str] = set()
    if config.AI_API_KEY:
        if config.AI_PROVIDER in KINDS:
            out.append(Provider(config.AI_PROVIDER, config.AI_PROVIDER, config.AI_API_KEY, config.AI_MODEL,
                                config.AI_BASE_URL, primary=True))
            seen.add(config.AI_PROVIDER)
        else:
            problems.append(f"AI_PROVIDER must be 'gemini', 'anthropic' or 'openai' (it is '{config.AI_PROVIDER}').")
    for raw in getattr(config, "AI_FALLBACKS", []):
        n = raw.get("index", "?")
        pre = f"AI_FALLBACK_{n}"
        name, kind = raw.get("name", ""), raw.get("kind", "openai")
        if not raw.get("key"):
            problems.append(f"{pre}_KEY is empty, so that fallback was skipped.")
        elif not _NAME_RE.match(name) or name == "auto":
            problems.append(f"{pre}_NAME '{name}' isn't usable (1-24 lower-case letters, digits, - or _; not 'auto'). Skipped.")
        elif name in seen:
            problems.append(f"{pre}_NAME '{name}' is already used by another provider. Skipped.")
        elif kind not in KINDS:
            problems.append(f"{pre}_KIND must be gemini, anthropic or openai (it is '{kind}'). Skipped.")
        elif not raw.get("model"):
            problems.append(f"{pre}_MODEL is empty. Name the model to use (see the provider's model list). Skipped.")
        elif kind == "openai" and not raw.get("base_url"):
            problems.append(f"{pre}_BASE_URL is empty (for example https://api.groq.com/openai/v1). Skipped.")
        elif raw.get("base_url") and not raw["base_url"].startswith(("http://", "https://")):
            problems.append(f"{pre}_BASE_URL must start with http:// or https://. Skipped.")
        else:
            base = raw.get("base_url", "")
            if kind == "openai" and not base.rstrip("/").endswith("/chat/completions"):
                base = base.rstrip("/") + "/chat/completions"        # a base like .../v1 is fine
            out.append(Provider(name, kind, raw["key"], raw["model"], base))
            seen.add(name)
    return out, problems


def is_configured() -> bool:
    return bool(chain()[0])


def get_preferred() -> Optional[str]:
    return _preferred


def set_preferred(name: Optional[str]) -> None:
    """Pin a provider to be tried first. None or 'auto' goes back to the .env order. Raises ValueError for an unknown name."""
    global _preferred
    if name in (None, "", "auto"):
        _preferred = None
        return
    if name not in [p.name for p in chain()[0]]:
        raise ValueError(f"There is no AI provider called '{name}'.")
    _preferred = name


def _ordered(providers: list[Provider]) -> list[Provider]:
    """Pinned provider first, then .env order. Providers that failed recently go to the back (still tried if nothing else works)."""
    order = sorted(providers, key=lambda p: (p.name != _preferred,))        # stable: keeps .env order otherwise
    now = _now()
    ready = [p for p in order if _health.get(p.name, _Health()).down_until <= now]
    return ready + [p for p in order if p not in ready]


def active_provider() -> Optional[Provider]:
    """The provider the next request will try first."""
    order = _ordered(chain()[0])
    return order[0] if order else None


def provider_status() -> list[dict]:
    """For /ai provider list: every provider in the order the next request would try them."""
    now = _now()
    out = []
    for i, p in enumerate(_ordered(chain()[0])):
        h = _health.get(p.name, _Health())
        out.append({"name": p.name, "kind": p.kind, "model": p.model, "primary": p.primary, "next": i == 0,
                    "pinned": p.name == _preferred, "cooling_for": max(0.0, h.down_until - now),
                    "last_error": h.last_error, "last_error_ago": (now - h.last_error_at) if h.last_error_at else None,
                    "last_ok_ago": (now - h.last_ok_at) if h.last_ok_at else None, "ok": h.ok, "failed": h.failed,
                    "answered_last": p.name == _last_answered_by})
    return out


def _note_ok(p: Provider) -> None:
    global _last_answered_by
    h = _health.setdefault(p.name, _Health())
    h.down_until, h.last_ok_at, h.ok = 0.0, _now(), h.ok + 1
    _last_answered_by = p.name


def _note_fail(p: Provider, e: AIError) -> None:
    h = _health.setdefault(p.name, _Health())
    h.failed += 1
    h.last_error, h.last_error_at = str(e)[:200], _now()
    cooldown = getattr(e, "cooldown", 0.0)
    if cooldown:
        h.down_until = _now() + cooldown


def clean_reply(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    """Make a model reply safe to post: no pings, trimmed, never longer than `limit`."""
    text = _MENTION_RE.sub("", text or "").replace("\r", "").strip()
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


# --------------------------------------------------------------------------- one provider
async def _post(session: aiohttp.ClientSession, p: Provider, url: str, headers: dict, body: dict, timeout: float):
    try:
        async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status != 200:
                detail = (await r.text())[:200]
                for k in {p.key, *(q.key for q in chain()[0])}:      # never echo any configured key, even another provider's
                    detail = detail.replace(k, "***")
                busy = r.status in (408, 429) or r.status >= 500
                cool = COOLDOWN_BUSY if busy else (COOLDOWN_BAD_KEY if r.status in (401, 403, 404) else 0.0)
                raise ProviderError(f"the AI service answered HTTP {r.status}: {detail}", status=r.status,
                                    retryable=busy, cooldown=cool)
            return await r.json(content_type=None)
    except AIError:
        raise
    except (aiohttp.ClientError, TimeoutError) as e:      # asyncio.TimeoutError is TimeoutError on 3.11+
        raise ProviderError(f"couldn't reach the AI service ({type(e).__name__}).", retryable=True,
                            cooldown=COOLDOWN_BUSY) from e


async def _call(session, p: Provider, system: str, user: str, max_tokens: int, model: str, json_mode: bool,
                timeout: float) -> str:
    """Ask one provider. Returns its raw text. Raises ProviderError."""
    if p.kind == "gemini":
        url = p.base_url or GEMINI_URL.format(model=model)
        headers = {"x-goog-api-key": p.key, "content-type": "application/json"}
        # room for "thinking" tokens on Gemini 2.5+ models, which count against the output limit
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": max(max_tokens, 1024),
                                     **({"responseMimeType": "application/json"} if json_mode else {})}}
        data = await _post(session, p, url, headers, body, timeout)
    elif p.kind == "anthropic":
        url = p.base_url or ANTHROPIC_URL
        headers = {"x-api-key": p.key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        body = {"model": model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        data = await _post(session, p, url, headers, body, timeout)
    else:
        url = p.base_url or OPENAI_URL
        headers = {"Authorization": f"Bearer {p.key}", "content-type": "application/json"}
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            data = await _post(session, p, url, headers, body, timeout)
        except ProviderError as e:
            if e.status == 400 and json_mode:
                # some OpenAI-compatible services reject response_format; the prompt already asks for JSON, so ask again plainly
                body.pop("response_format")
                data = await _post(session, p, url, headers, body, timeout)
            else:
                raise
    try:
        if p.kind == "gemini":
            return "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
        if p.kind == "anthropic":
            return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise ProviderError("the AI service sent a reply in a shape I didn't expect.") from e


# --------------------------------------------------------------------------- the public call
async def complete(system: str, user: str, max_tokens: int = 400, session: aiohttp.ClientSession | None = None, *,
                   model: str | None = None, json_mode: bool = False, raw: bool = False,
                   timeout: float | None = None, only: str | None = None) -> str:
    """Ask the AI. Tries the providers in order and returns the first answer. Raises AIError with a short reason if none works.

    `model` names a model for the MAIN provider only (fallbacks use their own). json_mode asks the provider for JSON where it
    supports that; raw=True returns the text untouched (for JSON), otherwise it is cleaned and capped like a chat message.
    `timeout` overrides the 25 s default (code writing needs longer). `only` asks just that provider, with no fallback and no
    retry (for /ai provider test)."""
    providers, problems = chain()
    if only is not None:
        order = [p for p in providers if p.name == only]
        if not order:
            raise AIError(f"There is no AI provider called '{only}'.")
    else:
        order = _ordered(providers)
    if not order:
        raise AIError(problems[0] if problems else "AI_API_KEY is not set in .env.")
    tries = 2 if len(order) == 1 and only is None else 1        # with nothing to fall back to, a busy answer is worth a retry

    own = session is None
    session = session or aiohttp.ClientSession()
    failures: list[tuple[str, AIError]] = []
    try:
        for p in order:
            use_model = (model or p.model).strip() if p.primary else p.model
            for attempt in range(tries):
                try:
                    text = await _call(session, p, system, user, max_tokens, use_model, json_mode, timeout or TIMEOUT_SECONDS)
                    text = (text or "").strip() if raw else clean_reply(text)
                    if not text:
                        raise ProviderError("the AI service sent an empty reply.")
                except ProviderError as e:
                    _note_fail(p, e)
                    if attempt + 1 < tries and e.retryable:
                        await asyncio.sleep(RETRY_DELAY)
                        continue
                    failures.append((p.name, e))
                    break
                _note_ok(p)
                if failures:
                    log.warning("AI: %s answered after %s", p.name,
                                "; ".join(f"{n} failed ({str(e)[:80]})" for n, e in failures))
                return text
    finally:
        if own:
            await session.close()
    if len(failures) == 1:
        raise failures[0][1]                                        # one provider: the plain reason, as before
    raise AIError("Every AI provider failed. " + " | ".join(f"{n}: {str(e)[:140]}" for n, e in failures))
