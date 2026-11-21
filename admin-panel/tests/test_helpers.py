import time
from pathlib import Path
from types import SimpleNamespace

from app.database import coerce, jsonable, quote_ident
from app.docker_ctl import demux_docker_stream
from app.security import csrf_ok, permissions_for, sign, unsign


def test_quote_ident_escapes_quotes():
    assert quote_ident('weird"name') == '"weird""name"'


def test_jsonable_handles_common_types():
    assert jsonable(3) == 3
    assert jsonable(None) is None
    assert jsonable({"a": 1}) == {"a": 1}
    assert jsonable(b"\x01") == "01"


def test_coerce_types():
    assert coerce("12", "integer") == (12, "")
    assert coerce("true", "boolean") == (True, "")
    assert coerce({"a": 1}, "jsonb") == ('{"a": 1}', "::jsonb")
    assert coerce("2026-01-01", "date") == ("2026-01-01", "::date")


def test_coerce_rejects_unsupported():
    try:
        coerce("x", "bytea")
    except ValueError:
        return
    raise AssertionError("bytea should be rejected")


def test_demux_strips_headers():
    payload = b"\x01\x00\x00\x00\x00\x00\x00\x05hello\x02\x00\x00\x00\x00\x00\x00\x01!"
    assert demux_docker_stream(payload) == "hello!"


def test_signed_tokens_roundtrip_and_expire():
    secret = "x" * 40
    token = sign({"sub": "1", "exp": time.time() + 60}, secret)
    assert unsign(token, secret)["sub"] == "1"
    assert unsign(token, "wrong-" + secret) is None
    assert unsign(sign({"exp": time.time() - 1}, secret), secret) is None


def test_csrf_requires_exact_match():
    request = SimpleNamespace(headers={"X-CSRF-Token": "abc"})
    assert csrf_ok(request, {"csrf": "abc"})
    assert not csrf_ok(request, {"csrf": "xyz"})


def test_permissions_follow_discord_identity():
    cfg = SimpleNamespace(
        owner_id="1", admin_role_ids=("10",), staff_role_ids=("20",)
    )
    assert permissions_for("1", set(), cfg) == frozenset({"read", "write", "control", "owner"})
    assert permissions_for("2", {"10"}, cfg) == frozenset({"read", "write", "control"})
    assert permissions_for("3", {"20"}, cfg) == frozenset({"read"})
    assert permissions_for("4", set(), cfg) == frozenset()


def test_discord_nav_has_a_renderer():
    static = Path(__file__).resolve().parents[1] / "app" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'data-section="discord"' in html
    assert "discord: renderDiscordAdmin" in js
