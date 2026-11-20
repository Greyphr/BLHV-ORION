import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

import asyncio
import random
from collections import Counter
from datetime import datetime, timedelta, timezone

import discord
import pytest

from orion import vault
from orion.giveaway_card import (DEFAULT_BODY, PLACEHOLDERS, SAFE_LIMIT, ButtonSpec, CardSpec, CardView, ended_spec,
                                 live_spec, make_context, parse_button, render_text, resolve_body, results_spec)
from orion.giveaways import (DEFAULT_ELIGIBLE, MAX_DURATION, MIN_DURATION, DurationError, csv_bytes,
                             evaluate_entry, format_duration, ordinal, parse_duration, place_label, reward_lines,
                             weighted_sample)

UTC = timezone.utc
T0 = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)
END = T0 + timedelta(days=3)
SIX_H = 6 * 3600


# ------------------------------------------------------------------ durations
@pytest.mark.parametrize("text,secs", [
    ("6h", 21600), ("6 hours", 21600), ("1d 6h", 108000), ("90m", 5400), ("2 weeks", 1209600),
    ("1h30m", 5400), ("1 day and 12 hours", 129600), (" 3D ", 259200), ("1d, 2h, 3m", 93780)])
def test_parse_duration_ok(text, secs):
    assert parse_duration(text, 60, 10**9) == secs


@pytest.mark.parametrize("bad", ["", "soon", "6", "h6", "6 parsecs", "6h banana", "-5h", "1.5h"])
def test_parse_duration_rejects_garbage(bad):
    with pytest.raises(DurationError):
        parse_duration(bad, 60, 10**9)


def test_parse_duration_bounds():
    with pytest.raises(DurationError, match="short"):
        parse_duration("1m", MIN_DURATION, MAX_DURATION)
    with pytest.raises(DurationError, match="long"):
        parse_duration("365d", MIN_DURATION, MAX_DURATION)


def test_format_and_ordinals():
    assert format_duration(21600) == "6 hours"
    assert format_duration(108000) == "1 day 6 hours"
    assert format_duration(86400 * 8) == "1 week 1 day"
    assert [ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 101, 111)] == \
        ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "101st", "111th"]
    assert place_label(1) == "🥇 1st Place" and place_label(4) == "🎖️ 4th Place"


def test_default_eligible_roles_are_everything_above_the_first_realm():
    assert DEFAULT_ELIGIBLE[0] == "Planetary" and DEFAULT_ELIGIBLE[-1] == "Eternal True God"
    assert len(DEFAULT_ELIGIBLE) == 9


# ----------------------------------------------------------------- entry rule
def test_spec_six_hour_example():
    """The table from the spec: 08:00 join, 10:00 too early, 14:00 / 20:00 / 02:00 each +1."""
    end = T0 + timedelta(days=2)
    at = lambda h: T0 + timedelta(hours=h)                                   # noqa: E731
    state, nxt = evaluate_entry(None, at(0), end, SIX_H)
    assert (state, nxt) == ("joined", at(6))
    entries, last = 1, at(0)
    for hours, want in ((2, "cooldown"), (6, "checked_in"), (12, "checked_in"), (18, "checked_in")):
        state, nxt = evaluate_entry(last, at(hours), end, SIX_H)
        assert state == want
        if state == "checked_in":
            entries, last = entries + 1, at(hours)
    assert entries == 4


def test_cooldown_reports_when_next_entry_opens_and_does_not_reset():
    state, nxt = evaluate_entry(T0, T0 + timedelta(hours=5, minutes=59), END, SIX_H)
    assert state == "cooldown" and nxt == T0 + timedelta(hours=6)
    # the failed attempt must not push the timer back: exactly 6h after the LAST ENTRY it opens
    assert evaluate_entry(T0, T0 + timedelta(hours=6), END, SIX_H)[0] == "checked_in"


def test_missing_a_checkin_keeps_entries_and_does_not_stack():
    # 3 intervals pass without a visit: still only ONE new entry when they come back
    state, _ = evaluate_entry(T0, T0 + timedelta(hours=20), END, SIX_H)
    assert state == "checked_in"
    assert evaluate_entry(T0 + timedelta(hours=20), T0 + timedelta(hours=21), END, SIX_H)[0] == "cooldown"


def test_closed_after_end_and_no_next_entry_past_the_end():
    assert evaluate_entry(None, END, END, SIX_H)[0] == "closed"
    assert evaluate_entry(T0, END + timedelta(hours=1), END, SIX_H)[0] == "closed"
    # last entry 1h before the end: the next one would land after the end -> None
    state, nxt = evaluate_entry(None, END - timedelta(hours=1), END, SIX_H)
    assert state == "joined" and nxt is None


# --------------------------------------------------------------------- draw
def test_weighted_sample_unique_and_bounded():
    w = {1: 10, 2: 5, 3: 2, 4: 1}
    for seed in range(50):
        picks = weighted_sample(w, 3, rng=random.Random(seed))
        assert len(picks) == 3 and len(set(picks)) == 3 and set(picks) <= set(w)
    assert sorted(weighted_sample(w, 10)) == [1, 2, 3, 4]          # k > participants: everybody, once
    assert weighted_sample({}, 3) == []
    assert weighted_sample({1: 0, 2: 0}, 2) == []                    # zero entries never win


def test_weighted_sample_odds_follow_entries():
    """Player A 10 entries, B 5, C 2: first pick probability should be ~ 10/17, 5/17, 2/17."""
    rng, n = random.Random(1234), 20000
    first = Counter(weighted_sample({1: 10, 2: 5, 3: 2}, 1, rng=rng)[0] for _ in range(n))
    for user, expect in ((1, 10 / 17), (2, 5 / 17), (3, 2 / 17)):
        assert abs(first[user] / n - expect) < 0.015, (user, first[user] / n, expect)
    # ...and nobody is guaranteed: the 2-entry player still wins sometimes
    assert first[3] > 0


def test_weighted_sample_repeat_mode_allows_multiple_wins():
    picks = weighted_sample({1: 1000, 2: 1}, 5, allow_repeat=True, rng=random.Random(0))
    assert len(picks) == 5 and picks.count(1) >= 4
    assert len(weighted_sample({1: 3}, 4, allow_repeat=True)) == 4


def test_default_rng_is_secure_system_random():
    import secrets
    from orion.giveaways import _RNG
    assert isinstance(_RNG, secrets.SystemRandom)


# ---------------------------------------------------------------------- CSV
def test_csv_neutralises_formula_injection():
    raw = csv_bytes(["name", "n"], [["=HYPERLINK(\"http://x\")", 1], ["+cmd", 2], ["@sum", 3], ["fine, name", 4]])
    text = raw.decode("utf-8-sig")
    assert "'=HYPERLINK" in text and "'+cmd" in text and "'@sum" in text
    assert '"fine, name"' in text and raw.startswith(b"\xef\xbb\xbf")


# --------------------------------------------------------------- card text
def _gw(**kw):
    base = dict(id=7, title="$1,000 GIVEAWAY", body="", description="", extra_message="", prize_pool="$1,000",
                winners_count=3, starts_at=T0, ends_at=END, interval_seconds=SIX_H, status="active", claim_hours=None,
                accent=None, buttons={})
    return {**base, **kw}


def _rewards():
    rows = []
    for pos, (name, value) in enumerate([("Gift card", "$500"), ("Gift card", "$300"), ("Gift card", "$200")], 1):
        rows.append(dict(position=pos, name=name, value=value, reward_type="gift_card", winner_id=100 + pos,
                         secret_enc=b"SUPER-SECRET-CODE", description="private description with CODE-1234"))
    return rows


def _kw(**kw):
    return {**dict(participants=3, entries=9, eligible_text="<@&1>", urls={}), **kw}


def _texts(view):
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def _buttons(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]


H = {k: (lambda i: None) for k in ("enter", "me", "claim")}


def test_placeholders_fill_in_and_unknown_ones_survive():
    ctx = make_context(_gw(), _rewards(), participants=1234, entries=5678, eligible_text="<@&9>")
    out = render_text("{title} | {prize} | {winners} | {interval} | {participants}/{entries} | {roles} | {nope} | {id}", ctx)
    assert out == "$1,000 GIVEAWAY | $1,000 | 3 | 6 hours | 1,234/5,678 | <@&9> | {nope} | 7"
    ts = int(END.timestamp())
    assert render_text("{ends} {ends_date}", ctx) == f"<t:{ts}:R> <t:{ts}:f>"
    assert "🥇 1st Place" in render_text("{rewards}", ctx) and "$500" in render_text("{rewards}", ctx)
    # a title that itself contains a placeholder is NOT expanded a second time
    ctx = make_context(_gw(title="{prize}"), _rewards(), participants=0, entries=0, eligible_text="")
    assert render_text("{title}", ctx) == "{prize}"
    assert {p[0].strip("{}") for p in PLACEHOLDERS} <= set(ctx)            # every documented placeholder exists


def test_default_template_is_premium_and_fills_every_placeholder():
    spec = live_spec(_gw(), _rewards(), **_kw())
    assert spec.body.startswith("## 🎁 $1,000 GIVEAWAY") and "{" not in spec.body
    for heading in ("📅 Giveaway Dates", "🎟️ How to Enter", "🏆 Rewards", "✅ Who Can Enter"):
        assert heading in spec.body
    assert "Come back every **6 hours** to earn **+1 entry**" in spec.body
    assert "3 participants" in spec.footer and "9 entries" in spec.footer and "Giveaway #7" in spec.footer


def test_admin_text_replaces_the_template_and_legacy_rows_still_work():
    spec = live_spec(_gw(body="## Custom!\n{prize} up for grabs, ends {ends}"), _rewards(), **_kw())
    assert spec.body.startswith("## Custom!\n$1,000 up for grabs, ends <t:")
    legacy = resolve_body(_gw(description="old text", extra_message="old rules"))
    assert "old text" in legacy and "old rules" in legacy and "{title}" in legacy
    assert resolve_body(_gw()) == DEFAULT_BODY


def test_text_never_exceeds_discords_limit_even_with_hostile_input():
    huge = "x" * 3000 + "{rewards}" * 40
    many = [dict(position=i, name="N" * 100, value="V" * 50, reward_type="custom", winner_id=10**17 + i, secret_enc=None,
                 description="") for i in range(1, 51)]
    for spec in (live_spec(_gw(body=huge, winners_count=50), many, **_kw()),
                 ended_spec(_gw(body=huge, status="ended", winners_count=50), many, **_kw()),
                 results_spec(_gw(status="ended", winners_count=50), many, urls={}, ping="🎉 " + " ".join(f"<@{10**17 + i}>" for i in range(60)))):
        assert spec.text_len() <= 4000, spec.text_len()


def test_live_card_has_images_buttons_and_never_secrets():
    urls = {"top": "attachment://banner_top.png", "bottom": "attachment://banner_bottom.png", "side": "attachment://side_image.png"}
    spec = live_spec(_gw(), _rewards(), **_kw(urls=urls))
    view = CardView(spec, H)
    kinds = [type(c).__name__ for c in view.walk_children()]
    assert kinds.count("MediaGallery") == 2 and "Thumbnail" in kinds and "Section" in kinds   # top + bottom banner, side image
    assert [b.custom_id for b in _buttons(view)] == ["orion:gw:enter", "orion:gw:me"]
    assert view.is_persistent()                                           # survives a restart
    text = _texts(view)
    assert "SUPER-SECRET" not in text and "CODE-1234" not in text
    # no images -> plain text card, no gallery / section
    plain = [type(c).__name__ for c in CardView(live_spec(_gw(), _rewards(), **_kw()), H).walk_children()]
    assert "MediaGallery" not in plain and "Section" not in plain


def test_ended_card_keeps_your_text_adds_winners_and_a_claim_button():
    spec = ended_spec(_gw(status="ended", body="## Hello {title}"), _rewards(), **_kw())
    text = _texts(CardView(spec, H))
    assert "## Hello $1,000 GIVEAWAY" in text and "THIS GIVEAWAY HAS ENDED" in text
    assert "🥇 **1st Place:** <@101> — $500" in text and "SUPER-SECRET" not in text
    assert [b.custom_id for b in _buttons(CardView(spec, H))] == ["orion:gw:claim", "orion:gw:me"]
    # nobody won -> no claim button; cancelled -> no winners, no buttons
    nobody = [dict(r, winner_id=None) for r in _rewards()]
    assert _buttons(CardView(ended_spec(_gw(status="ended"), nobody, **_kw()), H)) == []
    cancelled = ended_spec(_gw(status="cancelled"), _rewards(), **_kw())
    assert "CANCELLED" in cancelled.extra and "Winners" not in cancelled.extra and cancelled.buttons == []


def test_results_card_matches_the_spec_wording():
    rows = _rewards(); rows[2]["winner_id"] = None
    spec = results_spec(_gw(status="ended", title="$1,000 GIVEAWAY"), rows, urls={}, ping="🎉 <@101> <@102>")
    view = CardView(spec, H); text = _texts(view)
    assert text.startswith("🎉 <@101> <@102>")                                # the ping line sits above the card
    for want in ("## 🎉 GIVEAWAY RESULTS — $1,000 GIVEAWAY", "officially ended", "🥇 **1st Place:** <@101> — $500",
                 "🥈 **2nd Place:** <@102> — $300", "*No eligible winner*", "CLAIM REWARD"):
        assert want in text, want
    assert [b.custom_id for b in _buttons(view)] == ["orion:gw:claim"]


def test_custom_button_names_emojis_and_colour():
    assert parse_button("", ("Enter", "🎟️")) == ("Enter", "🎟️")
    assert parse_button("🔥 Join the fight", ("x", "y")) == ("Join the fight", "🔥")
    assert parse_button("🔥", ("x", "y")) == (None, "🔥")                    # emoji-only button
    assert parse_button("<:orion:123456789> Enter", ("x", "y")) == ("Enter", "<:orion:123456789>")
    assert parse_button("<a:spin:42> Go", ("x", "y")) == ("Go", "<a:spin:42>")
    assert parse_button("Join now", ("x", "y")) == ("Join now", None)
    assert parse_button("Éntrer maintenant", ("x", "y")) == ("Éntrer maintenant", None)   # letters are not an emoji
    assert parse_button("— dash", ("x", "y")) == ("— dash", None)
    assert parse_button("1️⃣ First", ("x", "y")) == ("First", "1️⃣")
    assert len(parse_button("x" * 200, ("a", "b"))[0]) == 80
    spec = live_spec(_gw(accent=0x112233, buttons={"enter": "🔥 Join the fight", "me": "🧾 Status"}), _rewards(), **_kw())
    assert spec.accent == 0x112233
    assert [(b.label, b.emoji) for b in spec.buttons] == [("Join the fight", "🔥"), ("Status", "🧾")]
    view = CardView(spec, H)
    assert [b.label for b in _buttons(view)] == ["Join the fight", "Status"]


def test_preview_cards_use_throwaway_ids():
    view = CardView(live_spec(_gw(), _rewards(), **_kw()), H, preview=True)
    assert not view.is_persistent() and all(b.custom_id != "orion:gw:enter" for b in _buttons(view))


def test_public_text_helpers_never_leak():
    assert "Gift card" in " ".join(reward_lines(_rewards()))
    assert "SUPER" not in " ".join(reward_lines(_rewards()))


# -------------------------------------------------------------------- vault
def test_vault_roundtrip_and_key_file(tmp_path, monkeypatch):
    from orion import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path)); monkeypatch.setattr(config, "GIVEAWAY_ENCRYPTION_KEY", "")
    vault.reset()
    blob = vault.encrypt("ABCD-1234-SECRET")
    assert b"ABCD" not in blob and vault.decrypt(blob) == "ABCD-1234-SECRET"
    key_file = tmp_path / "giveaway.key"
    assert key_file.exists()
    if os.name != "nt":                                            # POSIX permissions; Windows uses ACLs instead
        assert oct(key_file.stat().st_mode)[-3:] == "600"
    vault.reset()                                                  # a restart re-reads the SAME key
    assert vault.decrypt(blob) == "ABCD-1234-SECRET"
    assert vault.decrypt(None) == ""


def test_vault_detects_a_changed_key(tmp_path, monkeypatch):
    from orion import config

    class FakeDb:
        def __init__(self): self.cfg = {}
        def get(self, k): return self.cfg.get(k)
        async def set(self, k, v): self.cfg[k] = v

    db = FakeDb()
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "a")); monkeypatch.setattr(config, "GIVEAWAY_ENCRYPTION_KEY", "")
    vault.reset()
    blob = vault.encrypt("code")
    asyncio.run(vault.verify(db))                                  # first run stores the fingerprint
    asyncio.run(vault.verify(db))                                  # same key: fine
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "b"))   # e.g. data folder wiped: NEW key generated
    vault.reset()
    with pytest.raises(vault.VaultError, match="doesn't match"):
        asyncio.run(vault.verify(db))
    with pytest.raises(vault.VaultError, match="key has changed"):
        vault.decrypt(blob)
    vault.reset()


def test_vault_bad_env_key_is_a_clear_error(monkeypatch):
    from orion import config
    monkeypatch.setattr(config, "GIVEAWAY_ENCRYPTION_KEY", "not-a-valid-key")
    vault.reset()
    with pytest.raises(vault.VaultError, match="isn't valid"):
        vault.encrypt("x")
    vault.reset()
