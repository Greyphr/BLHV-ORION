import io
import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

import pytest
from PIL import Image

from orion.cogs.announce import Draft, build_embed, build_embeds, detect_image


def _img(fmt):
    b = io.BytesIO(); Image.new("RGB", (4, 4)).save(b, fmt); return b.getvalue()


def test_detect_image():
    assert detect_image(_img("PNG")) == "png"
    assert detect_image(_img("JPEG")) == "jpg"
    with pytest.raises(ValueError):
        detect_image(b"not an image")
    with pytest.raises(ValueError):
        detect_image(_img("BMP"))


def test_draft_roundtrip_ignores_unknown_keys():
    d = Draft(title="Hi", fields=[{"name": "a", "value": "b", "inline": True}])
    d2 = Draft.from_dict({**d.to_dict(), "junk": 1})
    assert d2 == d


def test_embed_building():
    d = Draft(title="T", description="D", fields=[{"name": "n", "value": "v", "inline": True}])
    e = build_embed(d, "attachment://thumbnail.png", "attachment://banner.png")
    assert e.title == "T" and e.image.url == "attachment://banner.png" and e.timestamp is not None
    big = Draft(description="x" * 4000, footer="f" * 2000,
                fields=[{"name": "n", "value": "v" * 1000, "inline": False}] * 3)
    assert len(build_embed(big)) > 6000


def test_top_banner_embeds():
    d = Draft(title="T", description="D")
    # no top banner: unchanged single embed
    assert len(build_embeds(d, None, "attachment://banner.png")) == 1
    # with top banner: slim image-only embed first, normal embed (with bottom banner) second
    head, main = build_embeds(d, None, "attachment://banner.png", "attachment://top_banner.png")
    assert head.image.url == "attachment://top_banner.png" and not head.title and not head.description
    assert main.title == "T" and main.image.url == "attachment://banner.png"
    assert head.color == main.color
    # only a top banner: a single embed, footer kept
    only = build_embeds(Draft(), None, None, "attachment://top_banner.png")
    assert len(only) == 1 and only[0].image.url == "attachment://top_banner.png"
    assert only[0].footer.text == Draft().footer


def test_old_drafts_load_without_top_banner():
    old = {"title": "x", "description": "", "banner_id": 5}
    assert Draft.from_dict(old).top_banner_id is None
    assert Draft.from_dict({**Draft(top_banner_id=7).to_dict()}).top_banner_id == 7
