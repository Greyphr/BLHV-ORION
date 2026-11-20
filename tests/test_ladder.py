import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from orion import ui


def test_ladder_without_banner_is_one_embed(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "LEVEL_BANNER", tmp_path / "missing.png")
    kw = ui.ladder_message()
    assert len(kw["embeds"]) == 1 and "file" not in kw


def test_ladder_with_banner_puts_it_first(tmp_path, monkeypatch):
    f = tmp_path / "b.png"
    f.write_bytes(b"x")
    monkeypatch.setattr(ui, "LEVEL_BANNER", f)
    kw = ui.ladder_message()
    assert kw["embeds"][0].image.url == "attachment://level_roles.png" and kw["file"].filename == "level_roles.png"
