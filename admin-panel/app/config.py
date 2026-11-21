"""Environment configuration for the admin panel.

The panel never receives the Discord bot token. It gets its own Discord OAuth
application credentials and the same read/write PostgreSQL credentials the bot
uses. All values are read once at startup.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    session_secret: str
    discord_client_id: str
    discord_client_secret: str
    discord_redirect_uri: str
    guild_id: str
    owner_id: str
    admin_role_ids: tuple[str, ...]
    staff_role_ids: tuple[str, ...]
    database_url: str
    docker_socket: str
    compose_project: str
    backups_dir: str
    api_token: str
    bot_rpc_url: str
    bot_rpc_token: str
    session_hours: int
    secure_cookies: bool
    allow_writes: bool
    allowed_origins: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Config":
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            user = quote(os.getenv("DB_USER", "orion"))
            password = quote(os.getenv("DB_PASSWORD", ""))
            host = os.getenv("DB_HOST", "db")
            port = os.getenv("DB_PORT", "5432")
            name = os.getenv("DB_NAME", "orion")
            if not password:
                raise RuntimeError("Set DATABASE_URL or DB_PASSWORD for the admin panel")
            database_url = f"postgresql://{user}:{password}@{host}:{port}/{name}"

        secret = os.getenv("SESSION_SECRET", "").strip()
        if len(secret) < 32:
            raise RuntimeError("SESSION_SECRET must be at least 32 characters")

        return cls(
            host=os.getenv("ADMIN_HOST", "127.0.0.1"),
            port=_int("ADMIN_PORT", 8080),
            session_secret=secret,
            discord_client_id=os.getenv("DISCORD_CLIENT_ID", "").strip(),
            discord_client_secret=os.getenv("DISCORD_CLIENT_SECRET", "").strip(),
            discord_redirect_uri=os.getenv(
                "DISCORD_REDIRECT_URI", "http://127.0.0.1:8081/auth/callback"
            ).strip(),
            guild_id=os.getenv("ADMIN_GUILD_ID", "1562411368458485820").strip(),
            owner_id=os.getenv("ADMIN_OWNER_ID", "985637303844225104").strip(),
            admin_role_ids=_list("ADMIN_ROLE_IDS") or ("1570857006884065290",),
            staff_role_ids=_list("STAFF_ROLE_IDS") or ("1562412567547224125",),
            database_url=database_url,
            docker_socket=os.getenv("DOCKER_SOCKET", "/var/run/docker.sock"),
            compose_project=os.getenv("COMPOSE_PROJECT", "orion"),
            backups_dir=os.getenv("BACKUPS_DIR", "/backups"),
            api_token=os.getenv("ADMIN_API_TOKEN", "").strip(),
            bot_rpc_url=os.getenv("BOT_RPC_URL", "http://bot:8082").strip().rstrip("/"),
            bot_rpc_token=os.getenv("BOT_RPC_TOKEN", "").strip(),
            session_hours=_int("SESSION_HOURS", 8),
            secure_cookies=_bool("COOKIE_SECURE"),
            allow_writes=_bool("ADMIN_ALLOW_WRITES", True),
            allowed_origins=_list("ALLOWED_ORIGINS"),
        )

    @property
    def oauth_scopes(self) -> str:
        return "identify guilds guilds.members.read"
