"""Application entrypoint: OAuth login, middleware, static UI and API wiring."""
from __future__ import annotations

import hmac
import logging
import secrets
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from .api import setup_api
from .config import Config
from .database import AdminDB
from .discord_oauth import DiscordOAuth
from .docker_ctl import DockerControl
from .security import (
    COOKIE_NAME,
    Principal,
    RateLimiter,
    clear_session,
    csrf_ok,
    new_csrf,
    permissions_for,
    set_session,
    sign,
    unsign,
)

log = logging.getLogger("orion.admin")
STATIC = Path(__file__).resolve().parent / "static"
STATE_COOKIE = "orion_oauth_state"


@web.middleware
async def security_middleware(request: web.Request, handler: web.Handler) -> web.Response:
    config: Config = request.app["config"]
    limiter: RateLimiter = request.app["limiter"]
    remote = request.remote or "unknown"

    if not limiter.allow(f"{remote}:{request.path.startswith('/auth/')}"):
        raise web.HTTPTooManyRequests(reason="Rate limit exceeded")

    if request.path.startswith("/auth/"):
        return await handler(request)

    if request.path.startswith("/api/") and request.path != "/api/health":
        principal = await _authenticate(request, config)
        if principal is None:
            raise web.HTTPUnauthorized(reason="Authentication required")
        request["principal"] = principal
        if request.method not in ("GET", "HEAD", "OPTIONS") and principal.via != "bearer":
            # CSRF protects cookie sessions only. Bearer tokens are sent by scripts,
            # which are not vulnerable to cross-site request forgery.
            origin = request.headers.get("Origin")
            if origin and config.allowed_origins and origin not in config.allowed_origins:
                raise web.HTTPForbidden(reason="Origin not allowed")
            if not csrf_ok(request, {"csrf": request.get("csrf", "")}):
                raise web.HTTPForbidden(reason="Invalid CSRF token")

    return await handler(request)


async def _authenticate(request: web.Request, config: Config) -> Principal | None:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer ") and config.api_token:
        if hmac.compare_digest(header[7:], config.api_token):
            return Principal("api", "api-token", frozenset({"read", "write", "control"}), "bearer")

    token = request.cookies.get(COOKIE_NAME, "")
    payload = unsign(token, config.session_secret)
    if payload is None:
        return None
    request["csrf"] = payload.get("csrf", "")
    return Principal(
        str(payload.get("sub", "")),
        str(payload.get("name", "unknown")),
        frozenset(payload.get("permissions", [])),
        "discord",
    )


async def login(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    if not config.discord_client_id or not config.discord_client_secret:
        raise web.HTTPServiceUnavailable(reason="Discord OAuth is not configured")
    state = secrets.token_urlsafe(24)
    response = web.HTTPFound(request.app["oauth"].authorize_url(state))
    response.set_cookie(
        STATE_COOKIE,
        sign({"state": state, "exp": time.time() + 600}, config.session_secret),
        httponly=True,
        samesite="Lax",
        secure=config.secure_cookies,
        path="/",
        max_age=600,
    )
    raise response


async def callback(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    state = request.query.get("state", "")
    stored = unsign(request.cookies.get(STATE_COOKIE, ""), config.session_secret)
    if not stored or not secrets.compare_digest(str(stored.get("state", "")), state):
        raise web.HTTPBadRequest(reason="Invalid OAuth state")

    code = request.query.get("code", "")
    if not code:
        raise web.HTTPBadRequest(reason="Missing authorization code")

    try:
        token = await request.app["oauth"].exchange_code(code)
        profile, roles = await request.app["oauth"].identity(token)
    except PermissionError as exc:
        raise web.HTTPForbidden(reason=str(exc)) from exc
    except RuntimeError as exc:
        log.warning("OAuth failure: %s", exc)
        raise web.HTTPBadGateway(reason="Discord authentication failed") from exc

    permissions = permissions_for(profile["id"], roles, config)
    if not permissions:
        raise web.HTTPForbidden(reason="Your Discord roles do not grant panel access")

    session = sign(
        {
            "sub": profile["id"],
            "name": profile["display_name"],
            "permissions": sorted(permissions),
            "csrf": new_csrf(),
            "exp": time.time() + config.session_hours * 3600,
        },
        config.session_secret,
    )
    log.info("AUDIT login user=%s permissions=%s", profile["id"], sorted(permissions))
    response = web.HTTPFound("/")
    set_session(response, session, secure=config.secure_cookies)
    response.del_cookie(STATE_COOKIE, path="/")
    raise response


async def logout(request: web.Request) -> web.Response:
    response = web.HTTPFound("/")
    clear_session(response)
    raise response


async def index(_request: web.Request) -> web.Response:
    return web.FileResponse(STATIC / "index.html")


async def startup(app: web.Application) -> None:
    config: Config = app["config"]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    app["http"] = aiohttp.ClientSession()
    app["docker_http"] = aiohttp.ClientSession(
        connector=aiohttp.UnixConnector(path=config.docker_socket)
    )
    app["oauth"] = DiscordOAuth(config, app["http"])
    app["docker"] = DockerControl(config.docker_socket, config.compose_project, app["docker_http"])
    app["db"] = AdminDB(config.database_url)
    await app["db"].connect()
    log.info("Admin panel ready on %s:%s", config.host, config.port)


async def cleanup(app: web.Application) -> None:
    await app["db"].close()
    await app["http"].close()
    await app["docker_http"].close()


def create_app(config: Config | None = None) -> web.Application:
    config = config or Config.from_env()
    app = web.Application(middlewares=[security_middleware])
    app["config"] = config
    app["limiter"] = RateLimiter(limit=180, window=60)

    setup_api(app)
    app.router.add_get("/auth/login", login)
    app.router.add_get("/auth/callback", callback)
    app.router.add_post("/auth/logout", logout)
    app.router.add_static("/static", STATIC, show_index=False)
    app.router.add_get("/{tail:.*}", index)

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def main() -> None:
    config = Config.from_env()
    web.run_app(create_app(config), host=config.host, port=config.port, print=None)


if __name__ == "__main__":
    main()
