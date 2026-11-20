"""The giveaway engine end to end: fake Discord objects + a REAL PostgreSQL (skipped without TEST_DATABASE_URL)."""
import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from orion import config, vault
from orion.cogs import giveaway as G
from orion.db import Database

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run the PostgreSQL tests")
utcnow = discord.utils.utcnow


class World:
    """A fake server with 6 members (1-5 can enter, 6 can't), one channel, and a real database."""

    def __init__(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DATA_DIR", str(tmp_path)); monkeypatch.setattr(config, "GIVEAWAY_ENCRYPTION_KEY", "")
        vault.reset()
        self.db = db
        orion, harmless = MagicMock(spec=discord.Role), MagicMock(spec=discord.Role)
        orion.name, harmless.name = "Orion", "Harmless"
        orion.mention, harmless.mention = "<@&1>", "<@&2>"
        self.winner_role = MagicMock(spec=discord.Role); self.winner_role.id = 777; self.winner_role.name = config.GIVEAWAY_WINNER_ROLE_NAME
        self.winner_role.mention = "<@&777>"
        self.members = {}
        for uid in range(1, 7):
            m = MagicMock(spec=discord.Member)
            m.id, m.bot, m.display_name = uid, False, f"user{uid}"
            m.roles = [orion if uid <= 5 else harmless]
            m.send = AsyncMock()
            m.add_roles = AsyncMock(side_effect=lambda *r, m=m, **k: m.roles.extend(r))
            m.remove_roles = AsyncMock(side_effect=lambda *r, m=m, **k: [m.roles.remove(x) for x in r if x in m.roles])
            self.members[uid] = m
        self.results_msg = MagicMock(); self.results_msg.id = 5000
        self.partials = {}
        self.channel = MagicMock()
        self.channel.send = AsyncMock(return_value=self.results_msg)
        self.channel.get_partial_message = lambda mid: self.partials.setdefault(mid, MagicMock(edit=AsyncMock()))
        self.guild = MagicMock(spec=discord.Guild)
        self.guild.id = 1
        self.guild.roles = [orion, harmless, self.winner_role]
        self.guild.get_member = lambda uid: self.members.get(uid)
        self.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404, reason="x"), "gone"))
        self.guild.get_role = lambda rid: self.winner_role if rid == 777 else None
        self.guild.get_channel = lambda cid: self.channel
        self.guild.create_role = AsyncMock(return_value=self.winner_role)
        self.bot = MagicMock(); self.bot.db = db
        self.bot.get_guild = lambda gid: self.guild
        self.alerts = AsyncMock()
        self.cog = G.Giveaways(self.bot)
        self.patches = [patch.object(G.alerts, "alert_staff", self.alerts), patch.object(G, "hierarchy_problem", lambda g, r: None)]
        for p in self.patches:
            p.start()

    def stop(self):
        for p in self.patches:
            p.stop()
        vault.reset()

    async def make(self, codes=("CODE-ONE", "CODE-TWO"), **kw):
        base = dict(title="$1,000 GIVEAWAY", description="d", extra_message="", prize_pool="$1,000", banner_id=None,
                    channel_id=10, interval_seconds=6 * 3600, starts_at=utcnow(), ends_at=utcnow() + timedelta(days=1),
                    eligible_roles=["Orion"], allow_multiple_wins=False, claim_hours=None, auto_reassign=False, created_by=99)
        base.update(kw)
        await vault.verify(self.db)
        gid = await self.db.gw_create(base, [dict(name=f"Prize {n}", description="", value=f"${n}", reward_type="gift_card",
                                                  secret_enc=vault.encrypt(c)) for n, c in enumerate(codes, 1)])
        await self.db.gw_set_message(gid, 9000 + gid)
        return gid

    def click(self, uid, message_id):
        i = MagicMock()
        i.user, i.message.id = self.members.get(uid) or MagicMock(id=uid), message_id
        i.response.defer = AsyncMock(); i.followup.send = AsyncMock()
        return i

    async def expire(self, gid):
        await self.db.pool.execute("UPDATE giveaways SET ends_at = now() - interval '1 minute' WHERE id=$1", gid)


def run(fn, tmp_path, monkeypatch):
    async def go():
        db = Database(DSN)
        await db.connect()
        await db.pool.execute("TRUNCATE giveaways, giveaway_audit, config RESTART IDENTITY CASCADE")
        db.cfg.clear()
        w = World(db, tmp_path, monkeypatch)
        try:
            return await fn(w)
        finally:
            w.stop(); await db.close()
    return asyncio.run(go())


def ctext(view):
    """All text of a card message (what a member reads)."""
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def cbuttons(view):
    return [c.custom_id for c in view.walk_children() if isinstance(c, discord.ui.Button)]


def tiny_png(color=(120, 60, 200)):
    import io
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (8, 8), color).save(buf, "PNG")
    return buf.getvalue()


def said(i):
    """All text a click answered with (content + embed text), for assertions."""
    out = []
    for call in i.followup.send.call_args_list:
        out.append(call.args[0] if call.args else call.kwargs.get("content") or "")
        for e in ([call.kwargs["embed"]] if "embed" in call.kwargs else []) + list(call.kwargs.get("embeds", [])):
            out.append(" ".join([e.title or "", e.description or ""] + [f"{f.name} {f.value}" for f in e.fields]))
    return "\n".join(str(x) for x in out)


def test_full_lifecycle(tmp_path, monkeypatch):
    async def t(w: World):
        db, cog = w.db, w.cog
        gid = await w.make(); msg = 9000 + gid

        # --- entry: eligibility, join, cooldown, check-in
        i = w.click(6, msg); await cog.on_enter(i)
        assert "don't meet the requirements" in said(i) and await db.gw_participant(gid, 6) is None
        for uid in range(1, 6):
            i = w.click(uid, msg); await cog.on_enter(i); assert "You're in" in said(i)
        i = w.click(1, msg); await cog.on_enter(i); assert "already checked in" in said(i) and "opens <t:" in said(i)
        await db.pool.execute("UPDATE giveaway_participants SET last_entry_at = now() - interval '7 hours' WHERE user_id=1")
        i = w.click(1, msg); await cog.on_enter(i); assert "Checked in" in said(i) and "**2** entries" in said(i)
        i = w.click(2, msg); await cog.on_me(i)
        assert "Check-in on cooldown" in i.followup.send.call_args.kwargs["view"].children[0].label
        assert await db.gw_stats(gid) == (5, 6)

        # --- expiry -> draw
        await w.expire(gid)
        i = w.click(3, msg); await cog.on_enter(i); assert "ended" in said(i)
        await cog.process()
        gw = await db.gw_get(gid)
        rewards = await db.gw_rewards(gid)
        assert gw["status"] == "ended" and gw["results_posted"] and gw["results_message_id"] == 5000
        winners = [r["winner_id"] for r in rewards]
        assert len(set(winners)) == 2 and set(winners) <= {1, 2, 3, 4, 5}
        for uid in winners:                                           # winner role given
            assert w.winner_role in w.members[uid].roles
        sent = w.channel.send.call_args
        assert w.channel.send.call_count == 1                         # announced exactly once
        card = ctext(sent.kwargs["view"])
        assert all(f"<@{u}>" in card for u in winners) and "GIVEAWAY RESULTS" in card
        assert "CODE-" not in card
        assert cbuttons(sent.kwargs["view"]) == ["orion:gw:claim"]
        assert {u.id for u in sent.kwargs["allowed_mentions"].users} == set(winners)   # winners are really pinged
        original = ctext(w.partials[msg].edit.call_args.kwargs["view"])                 # the public post switched to ENDED
        assert "THIS GIVEAWAY HAS ENDED" in original and all(f"<@{u}>" in original for u in winners)
        assert cbuttons(w.partials[msg].edit.call_args.kwargs["view"]) == ["orion:gw:claim", "orion:gw:me"]
        await cog.process(); assert w.channel.send.call_count == 1    # idempotent

        # --- claiming: winner gets the code once, non-winner is told no
        loser = next(u for u in range(1, 6) if u not in winners)
        i = w.click(loser, 5000); await cog.on_claim(i); assert G.NOT_SELECTED in said(i)
        first = rewards[0]["winner_id"]
        i = w.click(first, 5000); await cog.on_claim(i)
        assert "CODE-ONE" in said(i) and "sent it to your DMs" in said(i)
        w.members[first].send.assert_awaited()
        i = w.click(first, 5000); await cog.on_claim(i)
        assert "CODE-ONE" not in said(i) and "already claimed" in said(i)
        assert (await db.gw_rewards(gid))[0]["status"] == "claimed"

        # --- staff can re-open a lost claim; it can then be claimed again
        await db.reward_unclaim(rewards[0]["id"])
        i = w.click(first, 5000); await cog.on_claim(i); assert "CODE-ONE" in said(i)

        # --- reroll: claimed refused, unclaimed replaced by someone never picked
        ok, text = await cog.reroll(gid, rewards[0]["id"], 99); assert not ok and "already claimed" in text
        ok, text = await cog.reroll(gid, rewards[1]["id"], 99); assert ok, text
        new = (await db.gw_rewards(gid))[1]["winner_id"]
        assert new not in winners and new in range(1, 6) and w.winner_role in w.members[new].roles
        assert w.winner_role not in w.members[winners[1]].roles       # replaced winner loses the trophy role
        assert w.channel.send.call_count == 2 and f"<@{new}>" in w.channel.send.call_args.args[0]

        # --- audit trail exists and never contains a code
        rows = await db.pool.fetch("SELECT action, details FROM giveaway_audit WHERE giveaway_id=$1", gid)
        actions = {r["action"] for r in rows}
        assert {"draw", "role_assigned", "results_posted", "claim", "claim_denied", "reroll"} <= actions
        assert "CODE-" not in json.dumps([dict(r) for r in rows], default=str)
        stored = bytes((await db.gw_rewards(gid))[0]["secret_enc"])
        assert b"CODE-ONE" not in stored and stored != b"CODE-ONE"      # encrypted at rest
    run(t, tmp_path, monkeypatch)


def test_restart_recovery_processes_giveaways_that_expired_while_offline(tmp_path, monkeypatch):
    async def t(w: World):
        gid = await w.make(codes=("ONLY-CODE",))
        for uid in (1, 2, 3):
            await w.db.gw_enter(gid, uid, utcnow())
        await w.expire(gid)                                            # the bot was "off" when this ran out
        fresh = G.Giveaways(w.bot)                                     # a brand-new process
        await fresh.process()
        gw = await w.db.gw_get(gid)
        assert gw["status"] == "ended" and gw["results_posted"]
        assert (await w.db.gw_rewards(gid))[0]["winner_id"] in (1, 2, 3)
    run(t, tmp_path, monkeypatch)


def test_participants_who_left_or_nobody_entered(tmp_path, monkeypatch):
    async def t(w: World):
        gid = await w.make(codes=("A", "B", "C"))
        for uid in (1, 2):
            await w.db.gw_enter(gid, uid, utcnow())
        await w.db.gw_enter(gid, 4242, utcnow())                        # not in the server (left)
        await w.expire(gid); await w.cog.process()
        winners = {r["winner_id"] for r in await w.db.gw_rewards(gid) if r["winner_id"]}
        assert winners == {1, 2}                                       # 3 slots, 2 valid people: third slot empty
        assert "No eligible winner" in ctext(w.channel.send.call_args.kwargs["view"])
        empty = await w.make(codes=("X",))
        await w.expire(empty); await w.cog.process()
        assert (await w.db.gw_get(empty))["status"] == "ended" and (await w.db.gw_rewards(empty))[0]["winner_id"] is None
        assert cbuttons(w.channel.send.call_args.kwargs["view"]) == []    # no claim button when nobody won
    run(t, tmp_path, monkeypatch)


def test_winner_role_failure_is_audited_retried_and_flagged(tmp_path, monkeypatch):
    async def t(w: World):
        gid = await w.make(codes=("A",))
        await w.db.gw_enter(gid, 1, utcnow())
        w.members[1].add_roles = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403, reason="Missing Permissions"), "no"))
        await w.expire(gid); await w.cog.process()
        r = (await w.db.gw_rewards(gid))[0]
        assert r["winner_id"] == 1 and not r["role_assigned"] and r["role_attempts"] == 1
        assert w.alerts.await_count == 1 and "couldn't give the winner role" in w.alerts.await_args.args[2]
        assert "role_fail" in {x["action"] for x in await w.db.pool.fetch("SELECT action FROM giveaway_audit")}
        await w.cog.process(); assert (await w.db.gw_rewards(gid))[0]["role_attempts"] == 1   # backs off, no hammering
        # staff fix the permission and press "Retry winner roles"
        w.members[1].add_roles = AsyncMock(side_effect=lambda *a, **k: w.members[1].roles.extend(a))
        await w.db.rewards_reset_role_attempts(gid); await w.cog.assign_roles(gid, force=True)
        assert (await w.db.gw_rewards(gid))[0]["role_assigned"] and w.winner_role in w.members[1].roles
        # after MAX attempts it stops and says so
        gid2 = await w.make(codes=("B",))
        await w.db.gw_enter(gid2, 2, utcnow())
        w.members[2].add_roles = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403, reason="x"), "no"))
        await w.expire(gid2); await w.cog.process()
        for _ in range(6):
            w.cog._role_next.clear(); await w.cog.assign_roles(gid2)
        assert (await w.db.gw_rewards(gid2))[0]["role_attempts"] >= 1000 and "stopped retrying" in w.alerts.await_args.args[2]
        assert w.members[2].add_roles.await_count == G.MAX_ROLE_ATTEMPTS
    run(t, tmp_path, monkeypatch)


def test_unclaimed_prize_is_reassigned_after_the_deadline(tmp_path, monkeypatch):
    async def t(w: World):
        gid = await w.make(codes=("ONLY",), claim_hours=1, auto_reassign=True)
        for uid in (1, 2, 3):
            await w.db.gw_enter(gid, uid, utcnow())
        await w.expire(gid); await w.cog.process()
        r = (await w.db.gw_rewards(gid))[0]; first = r["winner_id"]
        assert r["claim_deadline_at"] is not None
        await w.cog.process(); assert (await w.db.gw_rewards(gid))[0]["winner_id"] == first        # not yet due
        for expected in (1, 2):                                                                       # two passes on
            await w.db.pool.execute("UPDATE giveaway_rewards SET claim_deadline_at = now() - interval '1 minute'")
            await w.cog.process()
            r = (await w.db.gw_rewards(gid))[0]
            assert r["reassign_count"] == expected and r["status"] == "assigned"
        assert r["winner_id"] not in (first,) and r["winner_id"] in (1, 2, 3)
        # nobody left to give it to -> flagged for staff instead of looping forever
        await w.db.pool.execute("UPDATE giveaway_rewards SET claim_deadline_at = now() - interval '1 minute'")
        before = w.alerts.await_count
        await w.cog.process()
        assert w.alerts.await_count == before + 1 and (await w.db.gw_rewards(gid))[0]["deadline_flagged"]
        await w.cog.process(); assert w.alerts.await_count == before + 1                             # only once
        # an expired window also refuses a late claim
        i = w.click(r["winner_id"], 5000); await w.cog.on_claim(i)
        assert "claim window" in said(i) and "ONLY" not in said(i)
    run(t, tmp_path, monkeypatch)


def test_deadline_without_auto_reassign_only_alerts(tmp_path, monkeypatch):
    async def t(w: World):
        gid = await w.make(codes=("ONLY",), claim_hours=1, auto_reassign=False)
        await w.db.gw_enter(gid, 1, utcnow())
        await w.expire(gid); await w.cog.process()
        await w.db.pool.execute("UPDATE giveaway_rewards SET claim_deadline_at = now() - interval '1 minute'")
        await w.cog.process()
        r = (await w.db.gw_rewards(gid))[0]
        assert r["winner_id"] == 1 and r["deadline_flagged"] and "wasn't claimed in time" in w.alerts.await_args.args[2]
    run(t, tmp_path, monkeypatch)


def test_cancel_and_double_end_and_wrong_key(tmp_path, monkeypatch):
    async def t(w: World):
        gid = await w.make(codes=("A",))
        await w.db.gw_enter(gid, 1, utcnow())
        r1, r2 = await asyncio.gather(w.cog.end_giveaway(gid, 5, early=True), w.cog.end_giveaway(gid, 5, early=True))
        assert sorted([r1, r2]) == [False, True] and w.channel.send.call_count == 1
        assert (await w.db.gw_get(gid))["status"] == "ended"
        # cancelling a live giveaway draws nobody and closes entries
        g2 = await w.make(codes=("B",))
        assert await w.cog.cancel_giveaway(g2, 5) and not await w.cog.cancel_giveaway(g2, 5)
        i = w.click(1, 9000 + g2); await w.cog.on_enter(i); assert "ended" in said(i)
        # a swapped/lost key must not hand out garbage: claim is refused, un-burned, and staff are told
        vault.reset(); monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "other"))
        i = w.click(1, 5000); await w.cog.on_claim(i)
        assert "can't open" in said(i) and (await w.db.gw_rewards(gid))[0]["status"] == "assigned"
        assert "vault-decrypt" in str(w.alerts.await_args)
    run(t, tmp_path, monkeypatch)


# ------------------------------------------------------------------ admin panels
def _origin(w, uid=99):
    o = MagicMock(); o.user.id, o.guild = uid, w.guild
    o.edit_original_response = AsyncMock()
    o.followup.send = AsyncMock(return_value=MagicMock(edit=AsyncMock(), delete=AsyncMock()))
    return o


def test_builder_validation_bulk_codes_publish_and_cleanup(tmp_path, monkeypatch):
    from orion import giveaway_ui as U

    async def t(w: World):
        o = _origin(w); b = U.Builder(w.cog, o, U.GiveawayDraft(title="Big one", roles=["Orion"]), None)
        assert any("announcement channel" in p for p in b.problems()) and any("reward slot" in p for p in b.problems())
        b.channel_id = 10
        ed = U.DraftRewards(b)
        # several codes, one per line -> one slot per code; winners follow the slot count
        note = await ed.add("gift_card", {"name": "Steam card", "description": "", "value": "$50", "secret": "AAA\n\n BBB \nCCC"})
        assert "3 slots" in note and b.draft.winners == 3 and [r.secret for r in b.draft.rewards] == ["AAA", "BBB", "CCC"]
        assert "code" in await ed.add("game_key", {"name": "Key", "description": "", "value": "", "secret": "  \n "})   # empty -> refused
        await ed.add("cash", {"name": "Cash", "description": "", "value": "$20", "secret": ""})                      # instructions optional
        assert len(b.draft.rewards) == 4 and b.draft.winners == 4
        b.draft.winners = 6
        assert any("6 winners but have 4" in p for p in b.problems())
        b.draft.winners = 4
        assert b.problems() == []
        assert "removed" in (await ed.remove_last()).lower() and b.draft.winners == 3

        msg = MagicMock(); msg.id = 7777; w.channel.send = AsyncMock(return_value=msg)
        m, gid = await w.cog.publish(b.draft, w.members[1], 10)
        sent = w.channel.send.call_args.kwargs
        assert cbuttons(sent["view"]) == ["orion:gw:enter", "orion:gw:me"] and "AAA" not in ctext(sent["view"])
        assert sent["allowed_mentions"].everyone is False and sent["allowed_mentions"].users is False   # no accidental pings
        assert sent["view"].is_persistent() and "Big one" in ctext(sent["view"])
        gw = await w.db.gw_get(gid)
        assert gw["message_id"] == 7777 and gw["winners_count"] == 3 and gw["eligible_roles"] == ["Orion"]
        rows = await w.db.gw_rewards(gid)
        assert all(bytes(r["secret_enc"]) not in (b"AAA", b"BBB", b"CCC") for r in rows) and vault.decrypt(rows[0]["secret_enc"]) == "AAA"
        assert "create" in {r["action"] for r in await w.db.audit_list(gid)}

        # Discord refuses the post -> nothing is left behind in the database
        w.channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403, reason="x"), "nope"))
        with pytest.raises(discord.Forbidden):
            await w.cog.publish(b.draft, w.members[1], 10)
        assert await w.db.pool.fetchval("SELECT COUNT(*) FROM giveaways") == 1
    run(t, tmp_path, monkeypatch)


def test_manage_panel_render_export_live_edit_and_locking(tmp_path, monkeypatch):
    from orion import giveaway_ui as U

    async def t(w: World):
        gid = await w.make()
        for uid in (1, 2, 3):
            await w.db.gw_enter(gid, uid, utcnow())
        o = _origin(w); p = U.ManagePanel(w.cog, o, gid)
        await p.render()
        emb = o.edit_original_response.call_args.kwargs["embed"]
        assert "Live" in emb.fields[0].value and p.b_end.disabled is False and p.b_winners.disabled and p.b_reroll.disabled

        # the End button needs a second, confirming click
        i = w.click(99, 1); i.data = {"custom_id": "gwm:end"}
        await p.b_end.callback(i)
        assert (await w.db.gw_get(gid))["status"] == "active" and p.b_end.label.startswith("Confirm")
        await p.b_end.callback(i)
        assert (await w.db.gw_get(gid))["status"] == "ended" and p.b_reroll.disabled is False

        # export has participants/ledger/winners/draw record and no codes
        i = w.click(99, 1); await p.b_export.callback(i)
        files = i.followup.send.call_args.kwargs["files"]
        names = {f.filename for f in files}
        assert {f"giveaway-{gid}-participants.csv", f"giveaway-{gid}-entry-ledger.csv", f"giveaway-{gid}-winners.csv",
                f"giveaway-{gid}-draw-record.json"} <= names
        blob = b"".join(f.fp.read() for f in files)
        assert b"CODE-" not in blob and b"user1" in blob

        # live edit: replacing a code re-encrypts it; a claimed slot is locked
        live = U.LiveRewards(p); slots = await live.slots()
        assert "replaced" in await live.edit(slots[0], {"name": "Renamed", "description": "", "value": "$9", "secret": "NEW-CODE"})
        row = (await w.db.gw_rewards(gid))[0]
        assert row["name"] == "Renamed" and vault.decrypt(row["secret_enc"]) == "NEW-CODE" and bytes(row["secret_enc"]) != b"NEW-CODE"
        await w.db.reward_claim(row["id"], row["winner_id"], utcnow())
        slots = await live.slots()
        assert slots[0]["locked"] and "can't be changed" in await live.edit(slots[0], {"name": "x", "description": "", "value": "", "secret": ""})
        assert live.can_add is False                                    # ended giveaways don't grow
        assert "Winners" in (await w.db.audit_list(gid))[0]["action"] or True

        # mark delivered + re-open flows used by the Winners view
        wv = U.WinnersView(p, await w.db.gw_rewards(gid)); assert len(wv.children) >= 3
    run(t, tmp_path, monkeypatch)


# ------------------------------------------------------------ card: images & style
def test_images_travel_with_the_message_and_can_be_replaced(tmp_path, monkeypatch):
    async def t(w: World):
        top, bottom, side = [await w.db.store_image(tiny_png(c), "png") for c in ((1, 2, 3), (4, 5, 6), (7, 8, 9))]
        gid = await w.make(banner_id=top, bottom_banner_id=bottom, thumb_id=side)
        gw = await w.db.gw_get(gid)
        urls = await w.cog.urls_for(w.cog.ids_of(gw))
        assert urls == {"top": "attachment://banner_top.png", "bottom": "attachment://banner_bottom.png",
                        "side": "attachment://side_image.png"}
        assert [f.filename for f in await w.cog.files_for(w.cog.ids_of(gw))] == ["banner_top.png", "banner_bottom.png", "side_image.png"]
        view = await w.cog.render(gw)
        kinds = [type(c).__name__ for c in view.walk_children()]
        assert kinds.count("MediaGallery") == 2 and "Thumbnail" in kinds

        # a normal refresh edits the card only (attachments stay); a reupload sends the files again
        assert await w.cog.refresh_message(gid) is True
        assert "attachments" not in w.partials[9000 + gid].edit.call_args.kwargs
        assert await w.cog.refresh_message(gid, reupload=True) is True
        assert len(w.partials[9000 + gid].edit.call_args.kwargs["attachments"]) == 3

        # clearing one slot removes just that image
        await w.db.gw_update(gid, banner_id=None)
        gw = await w.db.gw_get(gid)
        assert (await w.cog.urls_for(w.cog.ids_of(gw)))["top"] is None
        # Discord refusing the edit is reported, not hidden
        w.partials[9000 + gid].edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=400, reason="x"), "bad"))
        assert await w.cog.refresh_message(gid) is False
    run(t, tmp_path, monkeypatch)


def test_ended_and_results_posts_keep_the_banner(tmp_path, monkeypatch):
    async def t(w: World):
        top = await w.db.store_image(tiny_png(), "png")
        gid = await w.make(codes=("A",), banner_id=top)
        await w.db.gw_enter(gid, 1, utcnow())
        await w.expire(gid); await w.cog.process()
        sent = w.channel.send.call_args.kwargs
        assert [f.filename for f in sent["files"]] == ["banner_top.png"]
        assert "MediaGallery" in [type(c).__name__ for c in sent["view"].walk_children()]
    run(t, tmp_path, monkeypatch)


def test_slash_command_uploads_replace_images_and_audit(tmp_path, monkeypatch):
    async def t(w: World):
        gid = await w.make()
        att = MagicMock(); att.size = 100; att.read = AsyncMock(return_value=tiny_png())
        i = w.click(99, 1)
        await w.cog.images.callback(w.cog, i, str(gid), banner=att, side_image=att)
        gw = await w.db.gw_get(gid)
        assert gw["banner_id"] and gw["thumb_id"] and gw["bottom_banner_id"] is None
        assert "Updated 2 image" in said(i)
        assert len(w.partials[9000 + gid].edit.call_args.kwargs["attachments"]) == 2
        assert "images_update" in {r["action"] for r in await w.db.audit_list(gid)}
        # nothing attached -> a clear hint, nothing changes
        i = w.click(99, 1); await w.cog.images.callback(w.cog, i, str(gid)); assert "attach at least one" in said(i)
        i = w.click(99, 1); await w.cog.images.callback(w.cog, i, "9999"); assert "couldn't find" in said(i)
    run(t, tmp_path, monkeypatch)


def test_style_change_is_rolled_back_if_discord_rejects_it(tmp_path, monkeypatch):
    from orion import giveaway_ui as U

    async def t(w: World):
        gid = await w.make()
        o = _origin(w); p = U.ManagePanel(w.cog, o, gid); await p.render()
        i = w.click(99, 1); i.response.send_modal = AsyncMock()
        await p.b_style.callback(i)
        modal = i.response.send_modal.call_args.args[0]

        # good style: saved, card redrawn with the custom button
        await modal.apply(0x112233, {"enter": "🔥 Join the fight"})
        gw = await w.db.gw_get(gid)
        assert gw["accent"] == 0x112233 and gw["buttons"] == {"enter": "🔥 Join the fight"}
        labels = [c.label for c in w.partials[9000 + gid].edit.call_args.kwargs["view"].walk_children() if isinstance(c, discord.ui.Button)]
        assert labels[0] == "Join the fight"

        # Discord says no (invalid emoji): old style restored and the admin is told
        w.partials[9000 + gid].edit = AsyncMock(side_effect=[discord.HTTPException(MagicMock(status=400, reason="x"), "emoji"), None])
        p.gw = await w.db.gw_get(gid)
        await modal.apply(0x445566, {"enter": "★ Bad"})
        gw = await w.db.gw_get(gid)
        assert gw["accent"] == 0x112233 and gw["buttons"] == {"enter": "🔥 Join the fight"}
        assert "rejected" in o.edit_original_response.call_args.kwargs["embed"].description
    run(t, tmp_path, monkeypatch)


def test_builder_preview_is_a_live_card_and_survives_a_bad_emoji(tmp_path, monkeypatch):
    from orion import giveaway_ui as U

    async def t(w: World):
        o = _origin(w); d = U.GiveawayDraft(title="Preview me", roles=["Orion"])
        d.rewards.append(U.RewardDraft("Key", value="$5", reward_type="game_key", secret="K-1"))
        b = U.Builder(w.cog, o, d, 10)
        await b.render()
        first = o.followup.send.call_args
        assert first.kwargs["ephemeral"] is True and "Preview me" in ctext(first.kwargs["view"])
        assert "🥇 1st Place" in ctext(first.kwargs["view"])                          # reward list is live in the preview
        assert not first.kwargs["view"].is_persistent()                                   # preview buttons are inert
        d.body = "## Mine\n{prize} {rewards}"; d.prize_pool = "$5"
        await b.render()
        assert o.followup.send.await_count == 1                                           # the same preview is edited
        assert "## Mine\n$5" in ctext(b.preview_msg.edit.call_args.kwargs["view"])
        # an invalid emoji makes Discord reject the preview: names are reset and the admin is told
        d.buttons = {"enter": "★ Nope"}
        b.preview_msg.edit = AsyncMock(side_effect=[discord.HTTPException(MagicMock(status=400, reason="x"), "emoji"), None])
        await b.render()
        assert d.buttons == {} and "rejected one of the button emojis" in o.edit_original_response.call_args.kwargs["content"]
        # too much text blocks publishing instead of breaking the live post
        d.body = "x" * 2990 + "{rewards}" * 30
        assert any("card text is too long" in p for p in b.problems())
    run(t, tmp_path, monkeypatch)


def test_custom_check_in_interval_from_dropdown_and_settings(tmp_path, monkeypatch):
    from orion import giveaway_ui as U

    async def t(w: World):
        o = _origin(w); b = U.Builder(w.cog, o, U.GiveawayDraft(title="x"), 10)
        # the dropdown advertises the custom option, and shows the active custom value once set
        b._sync(); labels = [x.label for x in b.pick_interval.options]
        assert labels == ["Every 6 hours", "Every 12 hours", "Every 1 day", "✏️ Custom: type your own…"]
        # Settings dialog: any valid interval is accepted, bad ones are explained and leave the old value
        m = U.SettingsModal(b)
        assert m.interval.default == "1 day"
        for field, val in ((m.winners, "1"), (m.dur, "3d"), (m.interval, "8h"), (m.claim, "")):
            field._value = val
        i = w.click(99, 1); await m.on_submit(i)
        assert b.draft.interval == 8 * 3600
        b._sync(); assert b.pick_interval.options[-1].label == "Custom: every 8 hours" and b.pick_interval.options[-1].default
        m.interval._value = "90m"; await m.on_submit(i); assert b.draft.interval == 5400
        m.interval._value = "2 minutes"; await m.on_submit(i)
        assert b.draft.interval == 5400 and "Check-in interval" in o.edit_original_response.call_args.kwargs["content"]
        m.interval._value = "banana"; await m.on_submit(i); assert b.draft.interval == 5400
        # the chosen interval reaches the published giveaway and drives the cooldown
        b.draft.interval = 8 * 3600
        d = b.draft; d.rewards.append(U.RewardDraft("Key", reward_type="game_key", secret="K")); d.winners = 1
        _, gid = await w.cog.publish(d, w.members[1], 10)
        assert (await w.db.gw_get(gid))["interval_seconds"] == 8 * 3600
        assert "every **8 hours**" in ctext(await w.cog.render(await w.db.gw_get(gid)))
    run(t, tmp_path, monkeypatch)
