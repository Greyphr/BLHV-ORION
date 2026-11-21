"""Minimal read-only health endpoint for Docker and WUD.

The endpoint stays on the loopback interface by default. It reports ready only
after Discord is connected and the PostgreSQL pool is alive, so a container that
cannot log in or cannot reach the database never becomes healthy.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from .main import OrionBot

log = logging.getLogger("orion.health")


class HealthServer:
    def __init__(self, bot: "OrionBot", *, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.bot = bot
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None

    async def _healthz(self, _request: web.Request) -> web.Response:
        db = getattr(self.bot, "db", None)
        pool = getattr(db, "pool", None)
        database_up = pool is not None and not pool.is_closed()
        discord_ready = self.bot.is_ready() and not self.bot.is_closed()
        ready = bool(database_up and discord_ready)

        return web.json_response(
            {
                "status": "ok" if ready else "unavailable",
                "ready": ready,
                "database": "up" if database_up else "down",
                "discord": "ready" if discord_ready else "connecting",
            },
            status=200 if ready else 503,
        )

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/healthz", self._healthz)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        log.info("Health endpoint listening on http://%s:%s/healthz", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
