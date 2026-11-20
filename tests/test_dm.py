import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from orion.cogs.welcome import build_dm_embeds

BANNER = "attachment://dm_banner.png"


def test_top_banner_is_a_slim_first_embed():
    head, main = build_dm_embeds("T", "hi", "foot", 1, "top", True)
    assert head.image.url == BANNER and not head.title and not head.description
    assert main.title == "T" and main.description == "hi" and main.footer.text == "foot" and not main.image.url


def test_bottom_banner_sits_in_the_main_embed():
    (main,) = build_dm_embeds("T", "hi", "", 1, "bottom", True)
    assert main.image.url == BANNER


def test_hidden_or_missing_banner_means_no_image():
    for pos, has in (("off", True), ("top", False), ("bottom", False)):
        (main,) = build_dm_embeds("T", "hi", "", 1, pos, has)
        assert not main.image.url


def test_banner_only_keeps_footer_on_the_banner():
    (head,) = build_dm_embeds("", "", "foot", 1, "top", True)
    assert head.image.url == BANNER and head.footer.text == "foot"
