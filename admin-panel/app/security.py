"""Signed sessions, CSRF tokens, rate limiting and permission helpers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from aiohttp import web

COOKIE_NAME = "orion_admin"


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def sign(payload: dict[str, Any], secret: str) -> str:
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64encode(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def unsign(token: str, secret: str) -> dict[str, Any] | None:
    try:
        body, signature = token.split(".", 1)
    except (ValueError, AttributeError):
        return None
    expected = _b64encode(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or float(payload.get("exp", 0)) <= time.time():
        return None
    return payload


def new_csrf() -> str:
    return secrets.token_urlsafe(24)


def csrf_ok(request: web.Request, principal: dict[str, Any]) -> bool:
    supplied = request.headers.get("X-CSRF-Token", "")
    expected = principal.get("csrf", "")
    return bool(supplied) and hmac.compare_digest(supplied, expected)


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    permissions: frozenset[str]
    via: str

    def has(self, permission: str) -> bool:
        return permission in self.permissions


def permissions_for(user_id: str, roles: set[str], config: Any) -> frozenset[str]:
    if user_id == config.owner_id:
        return frozenset({"read", "write", "control", "owner"})
    if roles & set(config.admin_role_ids):
        return frozenset({"read", "write", "control"})
    if roles & set(config.staff_role_ids):
        return frozenset({"read"})
    return frozenset()


class RateLimiter:
    """Small in-memory sliding-window limiter; adequate for a host-only panel."""

    def __init__(self, limit: int = 120, window: int = 60) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


def set_session(response: web.Response, token: str, *, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="Lax",
        secure=secure,
        path="/",
        max_age=8 * 60 * 60,
    )


def clear_session(response: web.Response) -> None:
    response.del_cookie(COOKIE_NAME, path="/")
