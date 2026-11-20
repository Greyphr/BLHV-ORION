"""Giveaway SQL against a REAL PostgreSQL (skipped unless TEST_DATABASE_URL is set), e.g.

    TEST_DATABASE_URL=postgresql://user:pw@localhost/orion_test python -m pytest tests/test_giveaway_db.py

These cover what unit tests can't: concurrency (double clicks), atomic claims and the draw transaction.
"""
import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

import asyncio
import random
from datetime import datetime, timedelta, timezone

import pytest

from orion.db import Database
from orion.giveaways import weighted_sample

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run the PostgreSQL tests")

UTC = timezone.utc
T0 = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)
SIX_H = 6 * 3600


def run(coro_fn):
    """Fresh pool + empty giveaway tables for each test."""
    async def go():
        db = Database(DSN)
        await db.connect()
        await db.pool.execute("TRUNCATE giveaways, giveaway_audit RESTART IDENTITY CASCADE")
        try:
            return await coro_fn(db)
        finally:
            await db.close()
    return asyncio.run(go())


def gw_args(**kw):
    base = dict(title="Test", description="d", extra_message="", prize_pool="$1,000", banner_id=None,
                channel_id=10, interval_seconds=SIX_H, starts_at=T0, ends_at=T0 + timedelta(days=2),
                eligible_roles=["Orion"], allow_multiple_wins=False, claim_hours=None, auto_reassign=False,
                created_by=1)
    return {**base, **kw}


def reward(name="Gift card", value="$100", secret=b"enc"):
    return dict(name=name, description="", value=value, reward_type="gift_card", secret_enc=secret)


async def make(db, n_rewards=3, **kw):
    gid = await db.gw_create(gw_args(**kw), [reward(f"R{i}", f"${i}") for i in range(1, n_rewards + 1)])
    await db.gw_set_message(gid, 1000 + gid)
    return gid


# --------------------------------------------------------------------- entries
def test_spec_example_through_the_database():
    async def t(db):
        gid = await make(db)
        at = lambda h: T0 + timedelta(hours=h)                                       # noqa: E731
        seen = []
        for h in (0, 2, 6, 12, 18):
            o = await db.gw_enter(gid, 42, at(h))
            seen.append((o.state, o.entries))
        assert seen == [("joined", 1), ("cooldown", 1), ("checked_in", 2), ("checked_in", 3), ("checked_in", 4)]
        assert await db.gw_weights(gid) == {42: 4}
        assert await db.gw_stats(gid) == (1, 4)
        # cooldown tells you when
        o = await db.gw_enter(gid, 42, at(19))
        assert o.state == "cooldown" and o.next_at == at(24)
    run(t)


def test_double_click_cannot_double_enter():
    async def t(db):
        gid = await make(db)
        outcomes = await asyncio.gather(*[db.gw_enter(gid, 7, T0) for _ in range(30)])
        states = sorted(o.state for o in outcomes)
        assert states.count("joined") == 1 and states.count("cooldown") == 29
        assert await db.gw_weights(gid) == {7: 1}
        # same race again at the check-in boundary
        later = T0 + timedelta(hours=6)
        outcomes = await asyncio.gather(*[db.gw_enter(gid, 7, later) for _ in range(30)])
        assert sorted(o.state for o in outcomes).count("checked_in") == 1
        assert await db.gw_weights(gid) == {7: 2}
        assert await db.gw_stats(gid) == (1, 2)
    run(t)


def test_unique_participants_and_isolated_counts():
    async def t(db):
        a, b = await make(db), await make(db)
        await asyncio.gather(*[db.gw_enter(a, u, T0) for u in range(100, 120)])
        await db.gw_enter(b, 100, T0)
        assert (await db.gw_stats(a))[0] == 20 and (await db.gw_stats(b))[0] == 1
    run(t)


def test_entries_closed_after_end_and_when_not_active():
    async def t(db):
        gid = await make(db)
        assert (await db.gw_enter(gid, 1, T0 + timedelta(days=2))).state == "closed"
        assert (await db.gw_enter(gid, 1, T0 + timedelta(days=5))).state == "closed"
        assert (await db.gw_enter(999999, 1, T0)).state == "closed"            # unknown giveaway
        await db.gw_cancel(gid, T0)
        assert (await db.gw_enter(gid, 2, T0)).state == "closed"
    run(t)


def test_close_entries_and_extend():
    async def t(db):
        gid = await make(db)
        await db.gw_enter(gid, 1, T0)
        new_end = await db.gw_extend(gid, 3600, T0 + timedelta(hours=1))
        assert new_end == T0 + timedelta(days=2, hours=1)
        await db.gw_close_entries(gid, T0 + timedelta(hours=3))                # "end early"
        assert (await db.gw_enter(gid, 2, T0 + timedelta(hours=3))).state == "closed"
        assert await db.gw_extend(gid, 3600, T0 + timedelta(hours=4)) is None   # already closed: can't extend
    run(t)


def test_draw_weights_come_from_ledger_not_the_cached_counter():
    async def t(db):
        gid = await make(db)
        for h in (0, 6, 12):
            await db.gw_enter(gid, 1, T0 + timedelta(hours=h))
        await db.gw_enter(gid, 2, T0)
        # someone tampers with the display counter: the ledger still says 3 and 1
        await db.pool.execute("UPDATE giveaway_participants SET entries = 9999 WHERE user_id = 2")
        assert await db.gw_weights(gid) == {1: 3, 2: 1}
    run(t)


# --------------------------------------------------------------- finalize / claim
async def draw(db, gid, users, now=None):
    rewards = await db.gw_rewards(gid)
    picks = list(zip([r["id"] for r in rewards], users))
    return await db.gw_finalize(gid, picks, {"method": "test"}, now or T0 + timedelta(days=2), None, 99)


def test_only_one_finalize_wins_a_race():
    async def t(db):
        gid = await make(db)
        results = await asyncio.gather(*[draw(db, gid, [11, 12, 13]) for _ in range(8)])
        assert results.count(True) == 1
        rows = await db.gw_rewards(gid)
        assert [r["winner_id"] for r in rows] == [11, 12, 13] and {r["status"] for r in rows} == {"assigned"}
        assert (await db.gw_get(gid))["status"] == "ended"
        assert len(await db.pool.fetch("SELECT 1 FROM giveaway_draws")) == 1
    run(t)


def test_claim_is_atomic_winner_only_and_once():
    async def t(db):
        gid = await make(db)
        await draw(db, gid, [11, 12, 13])
        r1 = (await db.gw_rewards(gid))[0]
        now = T0 + timedelta(days=3)
        assert await db.reward_claim(r1["id"], 999, now) is None               # not the winner
        got = await asyncio.gather(*[db.reward_claim(r1["id"], 11, now) for _ in range(20)])
        assert len([g for g in got if g]) == 1                                 # exactly one succeeds
        assert await db.reward_claim(r1["id"], 11, now) is None                # never twice
        assert (await db.reward_get(r1["id"]))["status"] == "claimed"
        # another winner can't claim 11's reward either
        assert await db.reward_claim(r1["id"], 12, now) is None
    run(t)


def test_claim_deadline_and_unclaim():
    async def t(db):
        gid = await make(db, claim_hours=24)
        rewards = await db.gw_rewards(gid)
        end = T0 + timedelta(days=2)
        await db.gw_finalize(gid, [(rewards[0]["id"], 11)], {}, end, 24, None)
        rid = rewards[0]["id"]
        assert (await db.reward_get(rid))["claim_deadline_at"] == end + timedelta(hours=24)
        assert await db.reward_claim(rid, 11, end + timedelta(hours=25)) is None     # window closed
        assert await db.reward_claim(rid, 11, end + timedelta(hours=2)) is not None
        await db.reward_unclaim(rid)                                                  # delivery failed: retry allowed
        assert (await db.reward_get(rid))["status"] == "assigned"
        assert await db.reward_claim(rid, 11, end + timedelta(hours=3)) is not None
    run(t)


def test_deliver_reroll_and_edit_rules():
    async def t(db):
        gid = await make(db)
        await draw(db, gid, [11, 12, 13])
        r = await db.gw_rewards(gid)
        now = T0 + timedelta(days=3)
        # unclaimed reward can be replaced / edited
        assert await db.reward_update(r[0]["id"], name="New name", secret_enc=b"new") is True
        assert (await db.reward_set_winner(r[0]["id"], 77, now, None)) ["winner_id"] == 77
        # claimed reward can NOT be rerolled or edited
        await db.reward_claim(r[1]["id"], 12, now)
        assert await db.reward_set_winner(r[1]["id"], 88, now, None) is None
        assert await db.reward_update(r[1]["id"], name="hack") is False
        # delivered flow
        assert (await db.reward_mark_delivered(r[1]["id"], 1, now))["status"] == "delivered"
        assert await db.reward_mark_delivered(r[1]["id"], 1, now) is None
        # an unclaimed reward can be marked delivered by hand (e.g. cash sent manually) and then can't be claimed
        await db.reward_mark_delivered(r[2]["id"], 1, now)
        assert await db.reward_claim(r[2]["id"], 13, now) is None
        # replaced winners are remembered so they're never re-picked
        await db.draw_add(gid, "reroll", {}, [{"reward_id": r[0]["id"], "user_id": 77}], 1)
        assert await db.draw_picked_users(gid) == {11, 12, 13, 77}
        assert await db.user_has_other_wins(11, r[0]["id"]) is False
        assert await db.user_has_other_wins(12, r[0]["id"]) is True
    run(t)


def test_role_retry_bookkeeping_and_deadline_query():
    async def t(db):
        gid = await make(db, claim_hours=1, auto_reassign=True)
        r = await db.gw_rewards(gid)
        end = T0 + timedelta(days=2)
        await db.gw_finalize(gid, [(r[0]["id"], 11), (r[1]["id"], 12)], {}, end, 1, None)
        need = await db.rewards_needing_role(5)
        assert {x["winner_id"] for x in need} == {11, 12}
        await db.rewards_role_done([r[0]["id"]])
        await db.rewards_role_failed([r[1]["id"]])
        assert [x["winner_id"] for x in await db.rewards_needing_role(5)] == [12]
        await db.rewards_role_failed([r[1]["id"]], gone=True)
        assert await db.rewards_needing_role(5) == []
        late = await db.rewards_past_deadline(end + timedelta(hours=2))
        assert {x["winner_id"] for x in late} == {11, 12} and late[0]["auto_reassign"] is True
        assert await db.rewards_past_deadline(end + timedelta(minutes=30)) == []
        await db.reward_flag_deadline(r[0]["id"])
        assert [x["winner_id"] for x in await db.rewards_past_deadline(end + timedelta(hours=2))] == [12]
    run(t)


def test_slots_can_be_added_and_removed_while_live_only():
    async def t(db):
        gid = await make(db, n_rewards=2)
        assert await db.reward_add(gid, reward("Extra", "$5")) is not None
        g = await db.gw_get(gid)
        assert g["winners_count"] == 3 and len(await db.gw_rewards(gid)) == 3
        assert await db.reward_remove_last(gid) is True and (await db.gw_get(gid))["winners_count"] == 2
        assert await db.reward_remove_last(gid) is True and await db.reward_remove_last(gid) is False  # keeps >= 1
        await db.gw_cancel(gid, T0)
        assert await db.reward_add(gid, reward()) is None
    run(t)


def test_secret_blob_is_stored_as_given_and_audit_never_needs_it():
    async def t(db):
        gid = await make(db, n_rewards=1)
        await db.reward_update((await db.gw_rewards(gid))[0]["id"], secret_enc=b"\x00cipher\xff")
        assert bytes((await db.gw_rewards(gid))[0]["secret_enc"]) == b"\x00cipher\xff"
        await db.audit(gid, 5, "create", {"slots": 1})
        rows = await db.audit_list(gid)
        assert rows[0]["action"] == "create" and rows[0]["details"] == {"slots": 1}
    run(t)


def test_full_flow_with_weighted_draw_over_the_ledger():
    async def t(db):
        gid = await make(db, n_rewards=2)
        for u, visits in ((1, 5), (2, 3), (3, 1), (4, 1)):
            for h in range(visits):
                await db.gw_enter(gid, u, T0 + timedelta(hours=6 * h))
        w = await db.gw_weights(gid)
        assert w == {1: 5, 2: 3, 3: 1, 4: 1}
        picks = weighted_sample(w, 2, rng=random.Random(5))
        rewards = await db.gw_rewards(gid)
        assert await db.gw_finalize(gid, list(zip([r["id"] for r in rewards], picks)),
                                    {"weights": {str(k): v for k, v in w.items()}}, T0 + timedelta(days=2), None, None)
        assert len({r["winner_id"] for r in await db.gw_rewards(gid)}) == 2
        rec = await db.pool.fetchval("SELECT record FROM giveaway_draws")
        assert rec["weights"]["1"] == 5                                         # verifiable record is kept
    run(t)


def test_card_columns_roundtrip_and_are_editable():
    async def t(db):
        gid = await db.gw_create(gw_args(body="## Hi {title}", banner_id=None, bottom_banner_id=None, thumb_id=None,
                                         accent=0x112233, buttons={"enter": "🔥 Join", "claim": "🎁 Get it"}),
                                 [reward()])
        g = await db.gw_get(gid)
        assert g["body"] == "## Hi {title}" and g["accent"] == 0x112233
        assert g["buttons"] == {"enter": "🔥 Join", "claim": "🎁 Get it"}
        img = await db.store_image(b"x", "png")
        await db.gw_update(gid, body="new", bottom_banner_id=img, thumb_id=img, accent=None, buttons={})
        g = await db.gw_get(gid)
        assert (g["body"], g["bottom_banner_id"], g["thumb_id"], g["accent"], g["buttons"]) == ("new", img, img, None, {})
        assert await db.image_ext(img) == "png" and await db.image_ext(99999) is None
        # giveaways saved by the first version (no card columns) still load with sane defaults
        gid2 = await db.gw_create(gw_args(), [reward()])
        g2 = await db.gw_get(gid2)
        assert g2["body"] == "" and g2["accent"] is None and g2["buttons"] == {}
        with pytest.raises(ValueError):
            await db.gw_update(gid, winners_count=99)                              # still guarded
    run(t)
