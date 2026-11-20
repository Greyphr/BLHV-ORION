"""Pillow renderers: welcome card (your banner) and rank card (generated)."""
import logging
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .levels import LEVEL_ROLES, rank_name

log = logging.getLogger("orion.images")

ASSETS = Path(__file__).resolve().parent.parent / "assets"
WELCOME_TEMPLATE = ASSETS / "welcome_card.png"
# The welcome banner also ships inside the package. docker-compose bind-mounts ./assets over the image's assets, so a
# missing/unreadable assets/welcome_card.png must never turn the card into a plain one: this copy is used instead.
DEFAULT_WELCOME_TEMPLATE = Path(__file__).resolve().parent / "defaults" / "welcome_card.png"
LEVEL_BANNER = ASSETS / "level_roles_banner.png"   # header on the rank ladder (just drop the file in assets/)
LEVELUP_TEMPLATE = ASSETS / "levelup_card.png"   # "LEVEL UP" banner (placeholders already removed)
MILESTONE_DIR = ASSETS / "milestones"       # one banner per rank: harmless.png, nuisance.png ... orion.png
DM_BANNER = ASSETS / "dm_banner.png"        # optional banner on the welcome DM (set via /welcome dm-banner)

VIOLET = (56, 189, 248)
VIOLET_LIGHT = (186, 230, 253)
SILVER = (203, 203, 220)
WHITE = (255, 255, 255)

# Positions are FRACTIONS of the image (0-1) so any banner size works.
# Tune them with /welcome layout and check with /welcome preview.
# Tuned for the 2170x725 "WELCOME" banner (assets/welcome_card.png has no USERNAME / MEMBER / avatar placeholders, so the
# bot's text and avatar sit on a clean background). Extra text options:
#   family: see FONT_FILES, "upper" = ALL CAPS,
#   "spacing" = letter-spacing as a fraction of the font size, "ring_width" = avatar ring thickness,
#   "ring_color" = avatar ring colour, "glow" = soft halo around the ring (radius as a fraction of the width).
DEFAULT_LAYOUT = {
    # The banner's purple panel is empty, so the avatar gets its own white ring + soft glow, centred where the
    # banner's old placeholder ring was. AVATAR_SIZE/RING are set below.
    "avatar":   {"x": 0.90140, "y": 0.49862, "size": 0.17300, "ring": True, "ring_width": 0.00553,
                 "ring_color": "#FFFFFF", "glow": 0.0065},
    "username": {"x": 0.06930, "y": 0.69800, "size": 0.03290, "max_width": 0.43, "color": "#FFFFFF",
                 "align": "left", "family": "card_title", "upper": True},
    "count":    {"x": 0.07270, "y": 0.81720, "size": 0.01430, "max_width": 0.43, "color": "#9FC5EA",
                 "format": "MEMBER #{count} \u00b7 BLUE HEAVEN SECT", "align": "left", "family": "card_sub",
                 "spacing": 0.240},
}


# Level-up banner (2170x725). Fractions of the image, measured from the banner's own slots:
# the avatar sits inside its baked-in ring, the username inside the slanted bar, and
# "LV <level> — <rank>" on the line underneath (level in white, rank in purple).
LEVELUP_LAYOUT = {
    "avatar":   {"x": 0.88974, "y": 0.54414, "size": 0.18785},
    "username": {"x": 0.10130, "y": 0.56630, "size": 0.0345, "max_width": 0.305, "color": "#FFFFFF"},
    "level":    {"x": 0.06500, "y": 0.71130, "size": 0.0290, "max_width": 0.355,
                 "color": "#FFFFFF", "rank_color": "#38BDF8"},
}

# Milestone banners (2170x725, one per rank, "LVL <n> — <RANK>" is already painted on them). Only the empty avatar ring
# and the username are added. The ring is 290px across with an 11px edge; the avatar fills the 268px dark circle inside.
# The username sits centred in the empty purple panel under the ring.
MILESTONE_LAYOUT = {
    "avatar":   {"x": 0.90990, "y": 0.49862, "size": 0.12350},
    "username": {"x": 0.90990, "y": 0.79000, "size": 0.03300, "max_width": 0.20, "color": "#FFFFFF"},
}
MILESTONE_LEVELS = {lv: name.lower() for lv, name in LEVEL_ROLES}


def milestone_template(level: int) -> Path | None:
    """The special banner for this exact level (1, 5, 15, 30, 50, 75, 100), or None for any other level / missing file."""
    name = MILESTONE_LEVELS.get(level)
    p = MILESTONE_DIR / f"{name}.png" if name else None
    return p if p and p.exists() else None


# Named font families -> file in assets/fonts (anything missing falls back to the normal bold font).
#   "welcome"    = Poppins Bold Italic (level-up / milestone cards)
#   "card_title" = Inter ExtraBold Italic (welcome card username), "card_sub" = Inter Bold Italic (member line)
FONT_FILES = {"welcome": "welcome_bold.ttf", "card_title": "Inter-ExtraBoldItalic.otf",
              "card_sub": "Inter-BoldItalic.otf"}


def _font(size: int, bold: bool = True, family: str | None = None) -> ImageFont.FreeTypeFont:
    if family in FONT_FILES:           # a named family; falls back to the normal font if the file is missing
        p = ASSETS / "fonts" / FONT_FILES[family]
        if p.exists():
            return ImageFont.truetype(str(p), size)
    names = ["bold.ttf", "DejaVuSans-Bold.ttf"] if bold else ["regular.ttf", "DejaVuSans.ttf"]
    for d in (ASSETS / "fonts", Path("/usr/share/fonts/truetype/dejavu")):
        for n in names:
            p = d / n
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


def _text_width(text: str, f: ImageFont.FreeTypeFont, spacing: float = 0) -> float:
    """Width of `text`; `spacing` is extra letter-spacing in pixels between characters."""
    if not spacing:
        return f.getlength(text)
    return sum(f.getlength(c) for c in text) + spacing * max(len(text) - 1, 0)


def _fit(text: str, size: int, max_w: int, bold: bool = True, family: str | None = None, spacing_em: float = 0):
    """Shrink the font until the text fits. Returns (font, letter-spacing in px)."""
    f = _font(size, bold, family)
    while _text_width(text, f, spacing_em * size) > max_w and size > 12:
        size -= 2
        f = _font(size, bold, family)
    return f, spacing_em * size


def _draw_text(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, f, fill, align: str, spacing: float = 0):
    """Same anchoring as before ('left' = x is the left edge, otherwise the centre; y is the vertical middle),
    with optional letter-spacing."""
    if not spacing:
        draw.text((x, y), text, font=f, fill=fill, anchor="lm" if align == "left" else "mm")
        return
    pen = x if align == "left" else x - _text_width(text, f, spacing) / 2
    for ch in text:
        draw.text((pen, y), ch, font=f, fill=fill, anchor="lm")
        pen += f.getlength(ch) + spacing


def _hex(c: str):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _gradient(w: int, h: int, top, bottom) -> Image.Image:
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(h - 1, 1)
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return img


def _circle(img: Image.Image, d: int) -> Image.Image:
    img = img.convert("RGBA").resize((d, d), Image.LANCZOS)
    mask = Image.new("L", (d * 4, d * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, d * 4 - 1, d * 4 - 1), fill=255)
    img.putalpha(mask.resize((d, d), Image.LANCZOS))
    return img


def _to_png(img: Image.Image) -> BytesIO:
    buf = BytesIO()
    img.convert("RGBA").save(buf, "PNG")
    buf.seek(0)
    return buf


# --------------------------------------------------------------- welcome card
def _welcome_base() -> Image.Image:
    """The welcome banner: the one in assets/ (also what /welcome banner uploads), else the copy built into the bot."""
    if WELCOME_TEMPLATE.exists():
        try:
            return Image.open(WELCOME_TEMPLATE).convert("RGBA")
        except Exception:
            log.warning("assets/welcome_card.png is unreadable, using the built-in banner", exc_info=True)
    return Image.open(DEFAULT_WELCOME_TEMPLATE).convert("RGBA")


def render_welcome(avatar_bytes: bytes, username: str, count: int, layout: dict) -> BytesIO:
    base = _welcome_base()
    W, H = base.size
    draw = ImageDraw.Draw(base)

    a = layout["avatar"]
    d = int(a["size"] * W)
    cx, cy = int(a["x"] * W), int(a["y"] * H)
    if a.get("ring", True):
        r = max(2, round(a["ring_width"] * W)) if a.get("ring_width") else max(4, d // 28)
        if a.get("glow"):                                              # soft lilac halo behind the ring
            blur = max(2, round(a["glow"] * W))
            pad = blur * 4
            gm = Image.new("L", (d + 2 * r + 2 * pad,) * 2, 0)
            ImageDraw.Draw(gm).ellipse((pad, pad, pad + d + 2 * r, pad + d + 2 * r), fill=255)
            gm = gm.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * 0.30))
            halo = Image.new("RGBA", gm.size, (186, 167, 255, 0))
            halo.putalpha(gm)
            base.alpha_composite(halo, (max(cx - gm.width // 2, 0), max(cy - gm.height // 2, 0)))
        ring = _circle(Image.new("RGB", (4, 4), _hex(a["ring_color"]) if a.get("ring_color") else VIOLET), d + 2 * r)
        base.paste(ring, (cx - d // 2 - r, cy - d // 2 - r), ring)     # anti-aliased, like the avatar itself
    av = _circle(Image.open(BytesIO(avatar_bytes)), d)
    base.paste(av, (cx - d // 2, cy - d // 2), av)

    u = layout["username"]
    name = username.upper() if u.get("upper") else username
    f, sp = _fit(name, int(u["size"] * W), int(u.get("max_width", 0.8) * W), family=u.get("family"),
                 spacing_em=u.get("spacing", 0))
    _draw_text(draw, int(u["x"] * W), int(u["y"] * H), name, f, _hex(u.get("color", "#FFFFFF")),
               u.get("align", "mid"), sp)

    c = layout["count"]
    text = c.get("format", "#{count}").replace("{count}", f"{count:,}")
    if c.get("upper"):
        text = text.upper()
    f, sp = _fit(text, int(c["size"] * W), int(c.get("max_width", 0.6) * W), family=c.get("family"),
                 spacing_em=c.get("spacing", 0))
    _draw_text(draw, int(c["x"] * W), int(c["y"] * H), text, f, _hex(c.get("color", "#C4B5FD")),
               c.get("align", "mid"), sp)
    return _to_png(base)


# ----------------------------------------------------------------- rank card
def render_rank(avatar_bytes: bytes, name: str, level: int, title: str, pos,
                into: int, need: int, total_xp: int) -> BytesIO:
    W, H = 934, 282
    img = _gradient(W, H, (10, 6, 20), (38, 16, 70)).convert("RGBA")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, W - 9, H - 9), radius=26, outline=VIOLET, width=3)

    ad = 190
    ax, ay = 48, (H - ad) // 2
    d.ellipse((ax - 5, ay - 5, ax + ad + 5, ay + ad + 5), fill=VIOLET)
    av = _circle(Image.open(BytesIO(avatar_bytes)), ad)
    img.paste(av, (ax, ay), av)

    left, right = 285, W - 48
    d.text((left, 92), name, font=_fit(name, 42, 420)[0], fill=WHITE, anchor="ls")
    d.text((left, 132), title.upper(), font=_font(26), fill=VIOLET_LIGHT, anchor="ls")
    d.text((right, 92), rank_name(level).upper(), font=_font(42), fill=VIOLET_LIGHT, anchor="rs")
    d.text((right, 132), f"SERVER RANK #{pos}" if pos else "UNRANKED",
           font=_font(22), fill=SILVER, anchor="rs")

    bx0, bx1, by0, by1 = left, right, 170, 204
    d.rounded_rectangle((bx0, by0, bx1, by1), radius=17, fill=(24, 14, 44), outline=(70, 40, 120))
    frac = 1.0 if need == 0 else max(0.0, min(1.0, into / need))
    if frac > 0:
        fx = bx0 + max(34, int((bx1 - bx0) * frac))
        d.rounded_rectangle((bx0, by0, fx, by1), radius=17, fill=VIOLET)
    label = "MAX LEVEL" if need == 0 else f"{into:,} / {need:,} XP"
    d.text((bx1, 238), label, font=_font(24), fill=WHITE, anchor="rs")
    d.text((bx0, 238), f"TOTAL {total_xp:,} XP", font=_font(20, False), fill=SILVER, anchor="ls")
    return _to_png(img)


# ------------------------------------------------------------ level-up banner
def render_levelup(avatar_bytes: bytes, username: str, level: int, rank: str, layout: dict | None = None) -> BytesIO:
    """The 'LEVEL UP' banner: avatar in the ring, username in the bar, 'LV <n> — <rank>' underneath."""
    ms = milestone_template(level) if layout is None else None
    if ms:
        return _render_milestone(avatar_bytes, username, ms)
    L = layout or LEVELUP_LAYOUT
    if LEVELUP_TEMPLATE.exists():
        base = Image.open(LEVELUP_TEMPLATE).convert("RGBA")
    else:  # fallback so the bot still works if the asset is missing
        base = _gradient(2172, 724, (8, 4, 16), (46, 16, 101)).convert("RGBA")
        ImageDraw.Draw(base).text((60, 80), "LEVEL UP", font=_font(120), fill=WHITE, anchor="lt")
    W, H = base.size
    draw = ImageDraw.Draw(base)

    a = L["avatar"]
    d = int(a["size"] * W)
    cx, cy = int(a["x"] * W), int(a["y"] * H)
    av = _circle(Image.open(BytesIO(avatar_bytes)), d)
    base.paste(av, (cx - d // 2, cy - d // 2), av)

    u = L["username"]
    name = username.upper()
    f, sp = _fit(name, int(u["size"] * W), int(u["max_width"] * W), family="welcome")
    _draw_text(draw, int(u["x"] * W), int(u["y"] * H), name, f, _hex(u["color"]), "left", sp)

    # "LV 17 — " in white, then the rank in purple; the whole line shrinks together to fit.
    lv = L["level"]
    head, tail = "ASCENDED TO ", rank_name(level).upper()
    size, max_w = int(lv["size"] * W), int(lv["max_width"] * W)
    f = _font(size, family="welcome")
    while f.getlength(head + tail) > max_w and size > 12:
        size -= 2
        f = _font(size, family="welcome")
    x, y = int(lv["x"] * W), int(lv["y"] * H)
    draw.text((x, y), head, font=f, fill=_hex(lv["color"]), anchor="lm")
    draw.text((x + f.getlength(head), y), tail, font=f, fill=_hex(lv["rank_color"]), anchor="lm")
    return _to_png(base)


def _render_milestone(avatar_bytes: bytes, username: str, template: Path, layout: dict | None = None) -> BytesIO:
    """Milestone banner: avatar into the ring, username under it. Level and rank text are already on the artwork."""
    L = layout or MILESTONE_LAYOUT
    base = Image.open(template).convert("RGBA")
    W, H = base.size
    draw = ImageDraw.Draw(base)

    a = L["avatar"]
    d = int(a["size"] * W)
    cx, cy = int(a["x"] * W), int(a["y"] * H)
    av = _circle(Image.open(BytesIO(avatar_bytes)), d)
    base.paste(av, (cx - d // 2, cy - d // 2), av)

    u = L["username"]
    name = username.upper()
    f, sp = _fit(name, int(u["size"] * W), int(u["max_width"] * W), family="welcome")
    _draw_text(draw, int(u["x"] * W), int(u["y"] * H), name, f, _hex(u["color"]), "mid", sp)
    return _to_png(base)
