import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from orion import images
from orion.images import DEFAULT_LAYOUT, DEFAULT_WELCOME_TEMPLATE, WELCOME_TEMPLATE, render_welcome

pytestmark = pytest.mark.skipif(not WELCOME_TEMPLATE.exists(), reason="no welcome banner in assets/")


def _avatar():
    b = BytesIO(); Image.new("RGB", (64, 64), (230, 90, 60)).save(b, "PNG"); return b.getvalue()


def _ink(name, count):
    """Pixels the bot changed compared with the clean banner."""
    out = np.array(Image.open(render_welcome(_avatar(), name, count, DEFAULT_LAYOUT)).convert("RGB")).astype(int)
    base = np.array(Image.open(WELCOME_TEMPLATE).convert("RGB")).astype(int)
    return np.abs(out - base).sum(axis=2) > 60, base.shape[1], base.shape[0]


def _bbox(mask, x0, x1, y0, y1):
    ys, xs = np.where(mask[y0:y1, x0:x1])
    return xs.min() + x0, ys.min() + y0, xs.max() + x0, ys.max() + y0


def test_text_sits_under_the_welcome_lettering():
    diff, W, H = _ink("Username", 128)
    assert (W, H) == (2170, 725)                                   # the layout fractions are tuned for this banner
    ux0, uy0, ux1, uy1 = _bbox(diff, 0, 1100, 440, 560)            # username, under the WELCOME lettering
    assert abs(ux0 - 153) < 10 and uy0 > 455 and uy1 < 545
    mx0, my0, mx1, my1 = _bbox(diff, 0, 1100, 560, 660)            # "MEMBER #n . BLUE HEAVEN SECT"
    assert abs(mx0 - 157) < 10 and my0 > uy1 and my1 < 650


def test_template_has_no_placeholders_left():
    base = np.array(Image.open(WELCOME_TEMPLATE).convert("RGB")).astype(int)
    assert (base[:, 1700:].min(axis=2) > 235).sum() == 0           # no placeholder avatar ring in the purple panel
    assert (base[440:640, 100:900].max(axis=2) > 60).sum() == 0    # no USERNAME / MEMBER text left on the banner


def test_avatar_is_376px_with_a_ring_and_stays_inside_the_panel():
    diff, W, H = _ink("Username", 128)
    x0, y0, x1, y1 = _bbox(diff, 1700, W, 0, H)
    assert 396 <= x1 - x0 + 1 <= 408 and 396 <= y1 - y0 + 1 <= 408   # 376px avatar + 12px ring each side
    assert x1 < W - 5 and y0 > 5 and y1 < H - 5                      # nothing clipped by the banner edge


def test_long_names_shrink_and_stay_off_the_character():
    diff, W, H = _ink("xX_TheLongestDisplayName_Ever_Xx_Plus_More", 123456)
    for y0, y1 in ((440, 560), (560, 660)):
        assert _bbox(diff, 0, 1110, y0, y1)[2] < 1100                # the waving hand starts at about x=1120


def _character_region(img):
    """The hooded character (nothing the bot draws on) as an array, to prove the real banner was used."""
    return np.array(img.convert("RGB").crop((1100, 0, 1700, 725))).astype(int)


def _assert_full_banner(buf):
    out = Image.open(buf)
    assert out.size == (2170, 725)                                  # never the plain purple gradient card
    want = _character_region(Image.open(DEFAULT_WELCOME_TEMPLATE))
    assert np.abs(_character_region(out) - want).max() == 0


def test_missing_banner_in_assets_still_gives_the_banner_card(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "WELCOME_TEMPLATE", tmp_path / "gone.png")   # e.g. an empty bind-mounted assets/
    _assert_full_banner(render_welcome(_avatar(), "Username", 128, DEFAULT_LAYOUT))


def test_unreadable_banner_in_assets_still_gives_the_banner_card(tmp_path, monkeypatch):
    bad = tmp_path / "welcome_card.png"
    bad.write_bytes(b"not an image")
    monkeypatch.setattr(images, "WELCOME_TEMPLATE", bad)
    _assert_full_banner(render_welcome(_avatar(), "Username", 128, DEFAULT_LAYOUT))
