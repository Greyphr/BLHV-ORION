"""Generate Blue Heaven's placeholder banners (deep-blue celestial starfield, 2170x725).

Run:  python scripts/make_default_art.py
Writes assets/welcome_card.png, assets/levelup_card.png and orion/defaults/welcome_card.png.
Replace those files with real artwork any time; the layout is read as fractions of the image size.
"""
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "assets" / "fonts" / "welcome_bold.ttf"
W, H = 2170, 725


def starfield(seed: int) -> Image.Image:
    rnd = random.Random(seed)
    img = Image.new("RGB", (W, H))
    px = img.load()
    top, bot = (5, 10, 32), (10, 18, 52)
    for y in range(H):
        t = y / (H - 1)
        c = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        for x in range(W):
            px[x, y] = c
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse((W * 0.70, -H * 0.2, W * 1.15, H * 1.3), fill=(40, 90, 200))
    img = Image.blend(img, Image.composite(glow, img, glow.convert("L").filter(ImageFilter.GaussianBlur(160))), 0.55)
    d = ImageDraw.Draw(img)
    for _ in range(420):
        x, y, r = rnd.randrange(W), rnd.randrange(H), rnd.choice((1, 1, 1, 2, 2, 3))
        a = rnd.randint(110, 225)
        if 100 <= x <= 900 and 440 <= y <= 640:      # keep the username / member-number area clean
            continue
        d.ellipse((x - r, y - r, x + r, y + r), fill=(a, a, 255))
    return img


def banner(path: Path, title: str, subtitle: str, seed: int):
    img = starfield(seed).convert("RGBA")
    d = ImageDraw.Draw(img)
    f1, f2 = ImageFont.truetype(str(FONT), 190), ImageFont.truetype(str(FONT), 46)
    d.text((150, 30), title, font=f1, fill=(255, 255, 255))
    d.text((158, 245), subtitle, font=f2, fill=(125, 211, 252))
    d.rectangle((158, 318, 640, 324), fill=(56, 189, 248))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


if __name__ == "__main__":
    banner(ROOT / "assets" / "welcome_card.png", "WELCOME", "BLUE HEAVEN SECT", 1)
    banner(ROOT / "orion" / "defaults" / "welcome_card.png", "WELCOME", "BLUE HEAVEN SECT", 1)
    banner(ROOT / "assets" / "levelup_card.png", "ASCENSION", "BLUE HEAVEN SECT", 2)
    print("done")
