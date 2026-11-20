"""Render sample cards WITHOUT Discord, to tune the look:
    docker compose run --rm bot python -m scripts.preview_cards
Writes out/welcome_preview.png, out/rank_preview.png and out/levelup_preview.png
(inside the container: /app/out; run locally to get them on your disk)."""
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from orion.images import DEFAULT_LAYOUT, render_levelup, render_rank, render_welcome


def fake_avatar() -> bytes:
    im = Image.new("RGB", (256, 256), (90, 60, 160))
    ImageDraw.Draw(im).ellipse((64, 40, 192, 168), fill=(220, 220, 240))
    b = BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


if __name__ == "__main__":
    out = Path("out")
    out.mkdir(exist_ok=True)
    av = fake_avatar()
    (out / "welcome_preview.png").write_bytes(render_welcome(av, "SampleUser", 1234, DEFAULT_LAYOUT).read())
    (out / "rank_preview.png").write_bytes(render_rank(av, "SampleUser", 12, "Planetary", 4, 1234, 5678, 4500).read())
    (out / "levelup_preview.png").write_bytes(render_levelup(av, "SampleUser", 9, "Planetary").read())
    for lv in (0, 9, 18, 45, 72, 89):
        (out / f"levelup_rank_{lv}.png").write_bytes(render_levelup(av, "SampleUser", lv, "").read())
    print("wrote", out.resolve())
