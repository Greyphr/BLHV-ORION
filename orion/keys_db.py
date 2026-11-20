"""Beta key tables + queries (mixed into Database).

Design rules that keep the keys honest:
  * A code lives in exactly ONE row of beta_keys and that row is only ever handed out once: the claim is a single
    UPDATE ... WHERE owner_id IS NULL, inside a transaction, behind a lock that serialises all claims.
  * "Not enough codes left" rolls the whole claim back, so nobody ever gets a part-batch and no code is half assigned.
  * A tester can only self-claim once: enforced by the lock AND by a unique index, so it holds even if the code above
    is changed later.
  * Codes arrive here ALREADY ENCRYPTED (see vault.py). code_hash is a keyed fingerprint (vault.blind_index) so the
    same code can't be loaded twice without storing anything readable. This layer never sees a plain-text code.
  * beta_key_claims is the permanent record: who, how many, when, and who granted it.
"""
from datetime import datetime
from typing import NamedTuple, Optional

KEYS_SCHEMA = """
CREATE TABLE IF NOT EXISTS beta_key_claims (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    amount     INT NOT NULL CHECK (amount > 0),
    kind       TEXT NOT NULL,                     -- self (/keys) | grant (/givekeys); later maybe 'giveaway'
    granted_by BIGINT,                            -- the admin, for grants
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS beta_key_self_once ON beta_key_claims (user_id) WHERE kind = 'self';
CREATE INDEX IF NOT EXISTS beta_key_claims_user_idx ON beta_key_claims (user_id);
CREATE TABLE IF NOT EXISTS beta_keys (
    id          BIGSERIAL PRIMARY KEY,
    code_hash   TEXT NOT NULL UNIQUE,             -- keyed fingerprint of the code (never the code itself)
    code_enc    BYTEA NOT NULL,                   -- the code, encrypted
    claim_id    BIGINT REFERENCES beta_key_claims(id),
    owner_id    BIGINT,                           -- NULL = still in stock
    assigned_at TIMESTAMPTZ,
    imported_by BIGINT,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((owner_id IS NULL) = (claim_id IS NULL))
);
CREATE INDEX IF NOT EXISTS beta_keys_free_idx ON beta_keys (id) WHERE owner_id IS NULL;
-- two separate stocks: 'regular' (testers) and 'creator' (content creators). Older databases get the column added,
-- and everything already loaded stays 'regular'.
ALTER TABLE beta_keys ADD COLUMN IF NOT EXISTS pool TEXT NOT NULL DEFAULT 'regular';
ALTER TABLE beta_key_claims ADD COLUMN IF NOT EXISTS pool TEXT NOT NULL DEFAULT 'regular';
CREATE INDEX IF NOT EXISTS beta_keys_free_pool_idx ON beta_keys (pool, id) WHERE owner_id IS NULL;
CREATE INDEX IF NOT EXISTS beta_keys_owner_idx ON beta_keys (owner_id) WHERE owner_id IS NOT NULL;
"""

POOLS = ("regular", "creator")
KEYS_LOCK = 7_240_001        # advisory-lock id: every claim takes it, so claims run one at a time
IMPORT_CHUNK = 2000


class ClaimResult(NamedTuple):
    status: str                   # ok | already | short
    claim_id: Optional[int]
    codes: list[bytes]            # the ENCRYPTED codes just assigned (empty unless status == 'ok')
    remaining: int                # codes still in stock after this call
    at: Optional[datetime]        # when the claim was recorded


class KeyStore:
    """Mixin: expects `self.pool` (asyncpg pool)."""

    async def keys_import(self, pairs: list[tuple[str, bytes]], imported_by: int, pool: str = "regular") -> int:
        """Store (code_hash, encrypted_code) pairs into `pool`. Codes already stored (in any pool, claimed or not) are
        skipped. Returns how many were new."""
        assert pool in POOLS
        added = 0
        for n in range(0, len(pairs), IMPORT_CHUNK):
            chunk = pairs[n:n + IMPORT_CHUNK]
            res = await self.pool.execute(
                "INSERT INTO beta_keys(code_hash, code_enc, imported_by, pool) "
                "SELECT t.h, t.e, $3::bigint, $4::text FROM unnest($1::text[], $2::bytea[]) AS t(h, e) "
                "ON CONFLICT (code_hash) DO NOTHING",
                [p[0] for p in chunk], [p[1] for p in chunk], imported_by, pool)
            added += int(res.split()[-1])           # "INSERT 0 <n>"
        return added

    async def keys_claim(self, user_id: int, amount: int, kind: str, granted_by: Optional[int] = None,
                         pool: str = "regular") -> ClaimResult:
        """Give `amount` unused codes to `user_id`, all or nothing.
        status 'ok'      -> assigned; `codes` holds them (encrypted)
        status 'already' -> kind == 'self' and this person already claimed: nothing changed
        status 'short'   -> fewer than `amount` codes in that pool's stock: nothing changed"""
        assert pool in POOLS
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock($1)", KEYS_LOCK)
            free = await c.fetchval("SELECT COUNT(*) FROM beta_keys WHERE owner_id IS NULL AND pool = $1", pool)
            if kind == "self" and await c.fetchval(
                    "SELECT 1 FROM beta_key_claims WHERE user_id = $1 AND kind = 'self'", user_id):
                return ClaimResult("already", None, [], free, None)
            if free < amount:
                return ClaimResult("short", None, [], free, None)
            claim = await c.fetchrow(
                "INSERT INTO beta_key_claims(user_id, amount, kind, granted_by, pool) VALUES($1,$2,$3,$4,$5) "
                "RETURNING id, created_at", user_id, amount, kind, granted_by, pool)
            rows = await c.fetch(
                "UPDATE beta_keys SET owner_id = $1, claim_id = $2, assigned_at = now() "
                "WHERE id IN (SELECT id FROM beta_keys WHERE owner_id IS NULL AND pool = $4 ORDER BY id LIMIT $3) "
                "RETURNING id, code_enc", user_id, claim["id"], amount, pool)
            if len(rows) != amount:                 # can't happen under the lock; if it ever does, undo everything
                raise RuntimeError(f"beta key claim assigned {len(rows)} of {amount} codes")
            rows = sorted(rows, key=lambda r: r["id"])
            return ClaimResult("ok", claim["id"], [bytes(r["code_enc"]) for r in rows], free - amount, claim["created_at"])

    async def keys_reset(self, pool: Optional[str] = None) -> int:
        """Delete the UNCLAIMED codes (of one pool, or of both when pool is None). Codes that were already claimed, and
        the claim records, are never touched. Returns how many codes were deleted. Takes the claim lock."""
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock($1)", KEYS_LOCK)
            if pool is None:
                res = await c.execute("DELETE FROM beta_keys WHERE owner_id IS NULL")
            else:
                res = await c.execute("DELETE FROM beta_keys WHERE owner_id IS NULL AND pool = $1", pool)
            return int(res.split()[-1])

    async def keys_codes_of(self, user_id: int) -> list[bytes]:
        """Every (encrypted) code this person holds, oldest first."""
        rows = await self.pool.fetch("SELECT code_enc FROM beta_keys WHERE owner_id = $1 ORDER BY id", user_id)
        return [bytes(r["code_enc"]) for r in rows]

    async def keys_owned_count(self, user_id: int) -> int:
        return await self.pool.fetchval("SELECT COUNT(*) FROM beta_keys WHERE owner_id = $1", user_id)

    async def keys_stats(self) -> dict:
        row = await self.pool.fetchrow(
            "SELECT (SELECT COUNT(*) FROM beta_keys) AS total, "
            "(SELECT COUNT(*) FROM beta_keys WHERE owner_id IS NULL) AS free, "
            "(SELECT COUNT(*) FROM beta_keys WHERE owner_id IS NULL AND pool = 'regular') AS free_regular, "
            "(SELECT COUNT(*) FROM beta_keys WHERE owner_id IS NULL AND pool = 'creator') AS free_creator, "
            "(SELECT COUNT(*) FROM beta_keys WHERE pool = 'regular') AS total_regular, "
            "(SELECT COUNT(*) FROM beta_keys WHERE pool = 'creator') AS total_creator, "
            "(SELECT COUNT(DISTINCT user_id) FROM beta_key_claims WHERE kind = 'self') AS testers, "
            "(SELECT COALESCE(SUM(amount), 0) FROM beta_key_claims WHERE kind = 'self') AS tester_keys, "
            "(SELECT COUNT(DISTINCT user_id) FROM beta_key_claims WHERE kind = 'grant') AS grantees, "
            "(SELECT COALESCE(SUM(amount), 0) FROM beta_key_claims WHERE kind = 'grant') AS grant_keys")
        s = dict(row)
        s["assigned"] = s["total"] - s["free"]
        return s

    async def keys_claimants(self) -> list:
        """One row per person who holds codes, most recent first: user_id, tester, granted, first_at, last_at."""
        return await self.pool.fetch(
            "SELECT user_id, "
            "COALESCE(SUM(amount) FILTER (WHERE kind = 'self'), 0)::bigint AS tester, "
            "COALESCE(SUM(amount) FILTER (WHERE kind <> 'self'), 0)::bigint AS granted, "
            "COALESCE(SUM(amount) FILTER (WHERE pool = 'creator'), 0)::bigint AS creator, "
            "MIN(created_at) AS first_at, MAX(created_at) AS last_at "
            "FROM beta_key_claims GROUP BY user_id ORDER BY last_at DESC, user_id")
