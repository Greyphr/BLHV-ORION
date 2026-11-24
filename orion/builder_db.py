"""Build harness storage: features, and every version (build) of them. The database is the source of truth for the code;
the files under data/generated are only a copy the bot rewrites from it at startup."""
from typing import Optional

STATUSES = ("pending", "active", "superseded", "rejected", "failed")
RUNNABLE = ("active", "superseded")      # statuses of versions that ran before, so they are safe to roll back to


class BuilderStore:
    # ---- features
    async def bf_get(self, slug: str):
        return await self.pool.fetchrow("SELECT * FROM builder_features WHERE slug=$1", slug)

    async def bf_list(self):
        return await self.pool.fetch("SELECT * FROM builder_features ORDER BY slug")

    async def bf_upsert(self, slug: str, title: str, enabled: bool, active_build_id: Optional[int],
                        created_by: Optional[int] = None):
        await self.pool.execute(
            "INSERT INTO builder_features(slug, title, enabled, active_build_id, created_by) VALUES($1,$2,$3,$4,$5) "
            "ON CONFLICT (slug) DO UPDATE SET title=EXCLUDED.title, enabled=EXCLUDED.enabled, "
            "active_build_id=EXCLUDED.active_build_id, updated_at=now()",
            slug, title, enabled, active_build_id, created_by)

    async def bf_set_enabled(self, slug: str, enabled: bool):
        await self.pool.execute("UPDATE builder_features SET enabled=$2, updated_at=now() WHERE slug=$1", slug, enabled)

    async def bf_set_active(self, slug: str, build_id: Optional[int]):
        await self.pool.execute("UPDATE builder_features SET active_build_id=$2, updated_at=now() WHERE slug=$1",
                                slug, build_id)

    # ---- builds
    async def bb_add(self, slug: str, title: str, request: str, summary: str, notes: str, code: str,
                     schema_sql: str, model: str, created_by: Optional[int],
                     change_note: Optional[str] = None) -> tuple[int, int]:
        """Store a new pending version. Returns (build_id, version). `request` is always the ORIGINAL feature request (it is
        what the AI is shown on every later change); what was asked of this particular version goes in `change_note`."""
        row = await self.pool.fetchrow(
            "INSERT INTO builder_builds(slug, version, title, request, summary, notes, code, schema_sql, model, created_by, "
            "change_note) "
            "SELECT $1, COALESCE(MAX(version), 0) + 1, $2, $3, $4, $5, $6, $7, $8, $9, $10 FROM builder_builds WHERE slug=$1 "
            "RETURNING id, version",
            slug, title, request, summary, notes, code, schema_sql or None, model, created_by, change_note or None)
        return row["id"], row["version"]

    async def bb_get(self, build_id: int):
        return await self.pool.fetchrow("SELECT * FROM builder_builds WHERE id=$1", build_id)

    async def bb_find(self, slug: str, version: int):
        return await self.pool.fetchrow("SELECT * FROM builder_builds WHERE slug=$1 AND version=$2", slug, version)

    async def bb_list(self, slug: str, limit: int = 15):
        return await self.pool.fetch("SELECT * FROM builder_builds WHERE slug=$1 ORDER BY version DESC LIMIT $2",
                                     slug, limit)

    async def bb_pending(self):
        return await self.pool.fetch("SELECT * FROM builder_builds WHERE status='pending' ORDER BY id")

    async def bb_features(self):
        """Every feature name that has any build at all (newest title each), including ones never approved."""
        return await self.pool.fetch(
            "SELECT DISTINCT ON (slug) slug, title FROM builder_builds ORDER BY slug, version DESC")

    async def bb_slugs(self) -> set[str]:
        """Every slug ever used (even by a rejected draft): a new feature must not reuse one."""
        return {r["slug"] for r in await self.pool.fetch("SELECT DISTINCT slug FROM builder_builds")}

    async def bb_set_status(self, build_id: int, status: str, error: Optional[str] = None) -> bool:
        assert status in STATUSES
        res = await self.pool.execute(
            "UPDATE builder_builds SET status=$2, error=$3, decided_at=now() WHERE id=$1", build_id, status, error)
        return res.endswith("1")

    async def bb_claim_pending(self, build_id: int, status: str) -> bool:
        """Atomically move a build out of 'pending' (so a double click can't approve and reject it at once)."""
        assert status in STATUSES and status != "pending"
        res = await self.pool.execute(
            "UPDATE builder_builds SET status=$2, decided_at=now() WHERE id=$1 AND status='pending'", build_id, status)
        return res.endswith("1")

    async def bb_previous_working(self, slug: str, below_version: int):
        """The newest version older than `below_version` that actually ran before (so it is safe to go back to)."""
        return await self.pool.fetchrow(
            "SELECT * FROM builder_builds WHERE slug=$1 AND version < $2 AND status = ANY($3::text[]) "
            "ORDER BY version DESC LIMIT 1", slug, below_version, list(RUNNABLE))
