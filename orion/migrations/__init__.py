"""Versioned SQL migrations: orion/migrations/NNNN_name.sql, applied once each, in order.

Deliberately dependency-free. The files are plain SQL so Alembic (or a future API service) can adopt them unchanged.
"""
import logging
from pathlib import Path

log = logging.getLogger("orion.migrations")
_DIR = Path(__file__).resolve().parent


def pending_files() -> list[Path]:
    return sorted(_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))


async def apply(conn) -> list[str]:
    """Apply every migration not yet recorded. Each runs in its own transaction. Returns the versions applied."""
    await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                       "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    await conn.execute("SELECT pg_advisory_lock(7102026)")          # two processes starting together must not race
    try:
        done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
        applied = []
        for f in pending_files():
            if f.stem in done:
                continue
            async with conn.transaction():
                await conn.execute(f.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations(version) VALUES($1)", f.stem)
            log.info("Applied migration %s", f.stem)
            applied.append(f.stem)
        return applied
    finally:
        await conn.execute("SELECT pg_advisory_unlock(7102026)")
