"""Discord OAuth2 login and server-side guild/role verification."""
from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlencode

from aiohttp import ClientSession, web

from .config import Config

API = "https://discord.com/api/v10"
AUTHORIZE = "https://discord.com/oauth2/authorize"


class DiscordOAuth:
    def __init__(self, config: Config, client: ClientSession) -> None:
        self.config = config
        self.client = client

    def authorize_url(self, state: str) -> str:
        params = {
            "client_id": self.config.discord_client_id,
            "redirect_uri": self.config.discord_redirect_uri,
            "response_type": "code",
            "scope": self.config.oauth_scopes,
            "state": state,
            "prompt": "consent",
        }
        return f"{AUTHORIZE}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> str:
        data = {
            "client_id": self.config.discord_client_id,
            "client_secret": self.config.discord_client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.config.discord_redirect_uri,
        }
        async with self.client.post(f"{API}/oauth2/token", data=data) as response:
            if response.status != 200:
                raise RuntimeError(f"Discord token exchange failed ({response.status})")
            payload = await response.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("Discord token exchange returned no access token")
        return str(token)

    async def _get(self, path: str, token: str) -> Any:
        async with self.client.get(f"{API}{path}", headers={"Authorization": f"Bearer {token}"}) as response:
            if response.status != 200:
                raise RuntimeError(f"Discord API call failed ({response.status}) for {path}")
            return await response.json()

    async def identity(self, token: str) -> tuple[dict[str, Any], set[str]]:
        user = await self._get("/users/@me", token)
        guild_id = self.config.guild_id

        guilds = await self._get("/users/@me/guilds", token)
        in_guild = any(str(guild.get("id")) == guild_id for guild in guilds)
        if not in_guild:
            raise PermissionError("You are not a member of the configured Discord server")

        member = await self._get(f"/users/@me/guilds/{guild_id}/member", token)
        roles = {str(role) for role in member.get("roles", [])}

        avatar = user.get("avatar")
        avatar_url = (
            f"https://cdn.discordapp.com/avatars/{user['id']}/{avatar}.png?size=64"
            if avatar
            else "https://cdn.discordapp.com/embed/avatars/0.png"
        )
        profile = {
            "id": str(user.get("id")),
            "username": str(user.get("username", "unknown")),
            "display_name": str(user.get("global_name") or user.get("username", "unknown")),
            "avatar_url": avatar_url,
        }
        return profile, roles
