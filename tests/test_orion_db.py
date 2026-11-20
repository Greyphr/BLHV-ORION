"""Migrations, progression history and the audit log (real PostgreSQL; skipped without TEST_DATABASE_URL)."""
import asyncio
import os

import pytest

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from orion import migrations
from orion.db import Database

DSN = os.getenv("TEST_DATABASE_URL")


def test_no_method_name_is_defined_by_two_stores():
    """A store silently shadowing another store's method is how an `audit` clash broke giveaways once."""
    seen: dict[str, str] = {}
    for cls in Database.__mro__[1:]:
        if cls is object:
            continue
        for name, val in vars(cls).items():
            if name.startswith("__") or not callable(val):
                continue
            assert name not in seen, f"{name} defined in both {seen[name]} and {cls.__name__}"
            seen[name] = cls.__name__


def test_migration_files_are_numbered_and_ordered():
    names = [f.stem for f in migrations.pending_files()]
    assert names == sorted(names) and names[0].startswith("0001")
    assert len({n[:4] for n in names}) == len(names)


pytestmark_db = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run the PostgreSQL tests")


@pytestmark_db
def test_migrations_apply_once_and_are_idempotent():
    async def go():
        db = Database(DSN)
        await db.connect()
        try:
            async with db.pool.acquire() as c:
                assert await migrations.apply(c) == []                       # connect() already applied everything
                n = await c.fetchval("SELECT count(*) FROM schema_migrations")
                assert n == len(migrations.pending_files())
        finally:
            await db.close()
    asyncio.run(go())


@pytestmark_db
def test_progression_history_and_audit_log():
    async def go():
        db = Database(DSN)
        await db.connect()
        try:
            uid = 424242
            await db.pool.execute("DELETE FROM progression_history WHERE user_id=$1", uid)
            await db.log_progression(uid, 0, 120, 0, 1, "activity")
            await db.log_progression(uid, 120, 9000, 1, 12, "admin", actor_id=7)
            rows = await db.progression_history(uid)
            assert [r["new_level"] for r in rows] == [12, 1]               # newest first
            assert rows[0]["source"] == "admin" and rows[0]["actor_id"] == 7

            await db.record_audit(7, "xp.add", "member", uid, amount=500)
            last = (await db.recent_audit_log(1))[0]
            assert last["action"] == "xp.add" and last["target_id"] == str(uid) and last["details"]["amount"] == 500
            assert (await db.recent_audit_log(5, actor_id=7))[0]["actor_id"] == 7
        finally:
            await db.close()
    asyncio.run(go())
