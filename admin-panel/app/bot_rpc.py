"""Client for the bot's internal RPC service."""
from __future__ import annotations

import aiohttp
from aiohttp import web

from .config import Config

TOKEN_HEADER = "X-Orion-RPC-Token"


class BotRPC:
    def __init__(self, config: Config, client: aiohttp.ClientSession) -> None:
        self.url = config.bot_rpc_url
        self.token = config.bot_rpc_token
        self.client = client

    def _headers(self) -> dict[str, str]:
        return {TOKEN_HEADER: self.token, "Content-Type": "application/json"}

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            async with self.client.post(
                f"{self.url}{path}", json=payload, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 401:
                    raise web.HTTPForbidden(reason="Bot RPC token rejected")
                if response.status == 503:
                    raise web.HTTPServiceUnavailable(reason="Orion bot is not ready")
                if response.status == 404:
                    raise web.HTTPBadRequest(reason=(await response.text())[:300])
                if response.status >= 400:
                    text = (await response.text())[:300]
                    raise web.HTTPBadGateway(reason=f"Bot RPC failed ({response.status}): {text}")
                data = await response.json()
        except aiohttp.ClientError as exc:
            raise web.HTTPBadGateway(reason=f"Cannot reach Orion bot RPC: {exc}") from exc
        if not data.get("ok"):
            raise web.HTTPBadGateway(reason=str(data.get("error", "Bot RPC action failed")))
        return data.get("result") or {}

    async def capabilities(self) -> list[dict]:
        try:
            async with self.client.get(
                f"{self.url}/rpc/capabilities", headers=self._headers(), timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status >= 400:
                    return []
                data = await response.json()
        except aiohttp.ClientError:
            return []
        return data.get("actions", [])

    async def snapshot(self, actor_id: str) -> dict:
        return await self._post("/rpc/action", {"action": "dashboard.snapshot", "actor_id": int(actor_id), "params": {}})

    async def action(self, name: str, params: dict, actor_id: str) -> dict:
        return await self._post(
            "/rpc/action", {"action": name, "actor_id": int(actor_id), "params": params}
        )
