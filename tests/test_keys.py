"""Beta keys. The helper tests always run; the SQL tests need a real PostgreSQL (TEST_DATABASE_URL), e.g.

    TEST_DATABASE_URL=postgresql://user:pw@localhost/orion_test python -m pytest tests/test_keys.py

The SQL tests cover what unit tests can't: two people claiming at the same moment can never get the same code, a claim
is all-or-nothing, and a tester can only self-claim once.
"""
import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

import asyncio

import pytest

from orion.keys import INLINE_MAX_CODES, KeysError, mask, parse_csv, render_codes

DSN = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run the PostgreSQL tests")


# ----------------------------------------------------------------- CSV parsing
def test_plain_list_one_code_per_row():
    p = parse_csv(b"AAA-111\nBBB-222\nCCC-333\n")
    assert p.codes == ["AAA-111", "BBB-222", "CCC-333"] and not p.header_found and p.column == 0


@pytest.mark.parametrize("header", ["code", "Code", "CODES", "Beta Key", "beta_key", "Redeem Code", "keys"])
def test_header_row_is_detected_and_skipped(header):
    p = parse_csv(f"{header}\nAAA-111\nBBB-222\n".encode())
    assert p.codes == ["AAA-111", "BBB-222"] and p.header_found


def test_code_column_is_found_by_its_header():
    p = parse_csv(b"id,code,status\n1,AAA-111,unused\n2,BBB-222,unused\n")
    assert p.codes == ["AAA-111", "BBB-222"] and p.column == 1 and p.header_found


def test_first_column_is_used_without_a_header():
    p = parse_csv(b"AAA-111,extra\nBBB-222,extra\n")
    assert p.codes == ["AAA-111", "BBB-222"] and p.column == 0


def test_a_code_that_merely_contains_key_is_not_mistaken_for_a_header():
    p = parse_csv(b"KEY-8F3A-92B1\nKEY-1111-2222\n")
    assert p.codes == ["KEY-8F3A-92B1", "KEY-1111-2222"] and not p.header_found


def test_excel_bom_spaces_blank_lines_and_windows_newlines():
    p = parse_csv("\ufeffcode\r\n  AAA-111 \r\n\r\nBBB-222\r\n".encode("utf-8"))
    assert p.codes == ["AAA-111", "BBB-222"]


def test_duplicates_invalid_and_blank_are_counted_not_loaded():
    p = parse_csv(b"AAA-111\nAAA-111\nhas space\n,orphan cell\nBBB-222\n" + b"x" * 300 + b"\n")
    assert p.codes == ["AAA-111", "BBB-222"]
    assert (p.duplicates, p.invalid, p.blank) == (1, 2, 1)


def test_codes_keep_their_exact_case():
    assert parse_csv(b"AbC-123\nabc-123\n").codes == ["AbC-123", "abc-123"]


def test_empty_file_gives_no_codes():
    assert parse_csv(b"").codes == [] and parse_csv(b"\n\n").codes == []


def test_a_binary_file_is_refused():
    with pytest.raises(KeysError):
        parse_csv(b"PK\x03\x04\x00\x00binary")


# --------------------------------------------------------------------- display
def test_mask_hides_most_of_the_code():
    assert mask("ABCDEF-123456") == "AB" + "•" * 10 and mask("A") == "A" and "CDEF" not in mask("ABCDEF-123456")


def test_ten_codes_go_inline_and_a_big_batch_becomes_a_file():
    text, inline = render_codes([f"CODE-{n:04d}" for n in range(10)])
    assert inline and text.count("\n") == 9
    assert not render_codes([f"CODE-{n:05d}" for n in range(INLINE_MAX_CODES + 1)])[1]
    assert not render_codes(["x" * 100] * 20)[1]          # few codes, but too long for one message


# ------------------------------------------------------------------- database
def run(coro_fn):
    """Fresh pool + empty key tables for each test."""
    from orion.db import Database

    async def go():
        db = Database(DSN)
        await db.connect()
        await db.pool.execute("TRUNCATE beta_keys, beta_key_claims RESTART IDENTITY CASCADE")
        try:
            return await coro_fn(db)
        finally:
            await db.close()
    return asyncio.run(go())


async def load(db, n: int, start: int = 0) -> int:
    return await db.keys_import([(f"hash{k}", f"enc{k}".encode()) for k in range(start, start + n)], 99)


@needs_db
def test_import_skips_codes_already_stored():
    async def t(db):
        assert await load(db, 5) == 5
        assert await load(db, 8) == 3                      # 0-4 already there, 5-7 are new
        assert (await db.keys_stats())["total"] == 8
    run(t)


@needs_db
def test_tester_claim_assigns_exactly_the_amount_in_order():
    async def t(db):
        await load(db, 25)
        r = await db.keys_claim(1, 10, "self")
        assert r.status == "ok" and r.remaining == 15 and r.claim_id
        assert r.codes == [f"enc{k}".encode() for k in range(10)]
        assert await db.keys_codes_of(1) == r.codes
    run(t)


@needs_db
def test_nobody_can_self_claim_twice():
    async def t(db):
        await load(db, 25)
        first = await db.keys_claim(1, 10, "self")
        again = await db.keys_claim(1, 10, "self")
        assert first.status == "ok" and again.status == "already" and again.codes == []
        assert (await db.keys_stats())["free"] == 15       # the second try took nothing
        assert await db.keys_owned_count(1) == 10
    run(t)


@needs_db
def test_not_enough_codes_gives_nothing_and_records_nothing():
    async def t(db):
        await load(db, 6)
        r = await db.keys_claim(1, 10, "self")
        assert r.status == "short" and r.remaining == 6
        assert (await db.keys_stats())["free"] == 6 and await db.keys_owned_count(1) == 0
        assert await db.pool.fetchval("SELECT COUNT(*) FROM beta_key_claims") == 0
        assert (await db.keys_claim(1, 6, "self")).status == "ok"      # and they can still claim once stock is there
    run(t)


@needs_db
def test_grants_can_be_big_and_stack_on_a_tester_claim():
    async def t(db):
        await load(db, 5100)
        assert (await db.keys_claim(7, 10, "self")).status == "ok"
        g = await db.keys_claim(7, 5000, "grant", granted_by=2)
        assert g.status == "ok" and len(g.codes) == 5000 and g.remaining == 90
        assert await db.keys_owned_count(7) == 5010
        row = (await db.keys_claimants())[0]
        assert (row["user_id"], row["tester"], row["granted"]) == (7, 10, 5000)
    run(t)


@needs_db
def test_a_creator_with_no_self_claim_can_still_be_granted_and_then_claim():
    async def t(db):
        await load(db, 30)
        assert (await db.keys_claim(5, 3, "grant", granted_by=2)).status == "ok"
        assert (await db.keys_claim(5, 10, "self")).status == "ok"     # a grant doesn't use up the one tester claim
    run(t)


@needs_db
def test_many_people_claiming_at_once_never_share_a_code():
    async def t(db):
        await load(db, 400)
        results = await asyncio.gather(*(db.keys_claim(u, 10, "self") for u in range(1, 61)))   # 60 people, 600 wanted
        ok = [r for r in results if r.status == "ok"]
        assert len(ok) == 40 and all(r.status == "short" for r in results if r not in ok)
        handed = [c for r in ok for c in r.codes]
        assert len(handed) == 400 and len(set(handed)) == 400             # every code went to exactly one person
        assert (await db.keys_stats())["free"] == 0
    run(t)


@needs_db
def test_one_person_pressing_twice_at_once_gets_one_batch():
    async def t(db):
        await load(db, 50)
        results = await asyncio.gather(*(db.keys_claim(1, 10, "self") for _ in range(8)))
        assert sorted(r.status for r in results) == ["already"] * 7 + ["ok"]
        assert await db.keys_owned_count(1) == 10
    run(t)


@needs_db
def test_stats_and_claimants():
    async def t(db):
        await load(db, 40)
        await db.keys_claim(1, 10, "self")
        await db.keys_claim(2, 15, "grant", granted_by=9)
        s = await db.keys_stats()
        assert (s["total"], s["free"], s["assigned"]) == (40, 15, 25)
        assert (s["testers"], s["tester_keys"], s["grantees"], s["grant_keys"]) == (1, 10, 1, 15)
        assert {r["user_id"] for r in await db.keys_claimants()} == {1, 2}
    run(t)


@needs_db
def test_creator_stock_is_separate_and_reset_keeps_claimed():
    async def t(db):
        assert await load(db, 6) == 6                                                     # regular stock
        assert await db.keys_import([(f"c{k}", b"x") for k in range(4)], 99, "creator") == 4
        s = await db.keys_stats()
        assert (s["free_regular"], s["free_creator"]) == (6, 4)
        assert (await db.keys_claim(1, 5, "grant", 9, "creator")).status == "short"       # only 4 creator codes
        assert (await db.keys_claim(1, 4, "grant", 9, "creator")).status == "ok"
        assert (await db.keys_claim(2, 3, "self")).status == "ok"                         # tester claim: regular only
        s = await db.keys_stats()
        assert (s["free_regular"], s["free_creator"]) == (3, 0)
        assert await db.keys_reset("regular") == 3                                        # only unclaimed regular codes
        s = await db.keys_stats()
        assert s["total"] == 7 and s["free"] == 0 and s["assigned"] == 7                 # claimed codes all kept
        assert await db.keys_owned_count(1) == 4 and await db.keys_owned_count(2) == 3
        assert len(await db.keys_claimants()) == 2                                        # claim records kept
    run(t)
