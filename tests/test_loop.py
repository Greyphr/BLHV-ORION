"""Loop modes and the voice-problem check. No Discord connection, no ffmpeg."""
import asyncio
import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from orion.cogs import music as cog
from orion.cogs.music import GuildPlayer, PlayerView
from orion.music import Track


def t(name):
    return Track(title=name, url=f"https://youtu.be/{name}")


@pytest.fixture
def player(monkeypatch):
    monkeypatch.setattr(cog, "resolve_track", lambda track: "stream")
    monkeypatch.setattr(cog.discord, "FFmpegPCMAudio", MagicMock())
    monkeypatch.setattr(cog.discord, "PCMVolumeTransformer", MagicMock())
    monkeypatch.setattr(cog, "set_presence", AsyncMock())
    p = GuildPlayer(MagicMock())
    p.vc = MagicMock()
    p.vc.is_playing.return_value = False
    p.vc.is_paused.return_value = False
    p.vc.is_connected.return_value = True
    p._post_panel = AsyncMock(return_value=True)
    p._end_panel = AsyncMock()
    p._prefetch = AsyncMock()
    p._say = AsyncMock()
    return p


def advance(p, played_for=60.0):
    """A track just ended (after `played_for` seconds): run the next play_next."""
    p._started = time.monotonic() - played_for
    asyncio.run(p.play_next())


def names(p):
    return [x.title for x in p.queue]


def test_loop_off_just_moves_on(player):
    player.queue.extend([t("a"), t("b")])
    advance(player); assert player.current.title == "a"
    advance(player); assert player.current.title == "b"
    advance(player); assert player.current is None


def test_loop_track_repeats_the_song_and_keeps_history_clean(player):
    player.loop_mode = "track"
    player.queue.extend([t("a"), t("b")])
    advance(player)
    for _ in range(3):
        advance(player)
        assert player.current.title == "a"
    assert names(player) == ["b"] and not player.history


def test_skip_leaves_a_song_loop(player):
    player.loop_mode = "track"
    player.queue.extend([t("a"), t("b")])
    advance(player)
    player.vc.is_playing.return_value = True
    player.skip()
    player.vc.is_playing.return_value = False
    advance(player)
    assert player.current.title == "b"


def test_loop_queue_recycles_finished_songs(player):
    player.loop_mode = "queue"
    player.queue.extend([t("a"), t("b")])
    seen = []
    for _ in range(5):
        advance(player); seen.append(player.current.title)
    assert seen == ["a", "b", "a", "b", "a"]


def test_loop_queue_with_a_single_song_repeats_it(player):
    player.loop_mode = "queue"
    player.queue.append(t("a"))
    for _ in range(3):
        advance(player)
        assert player.current.title == "a"


def test_stop_never_repeats_whatever_the_loop_mode(player):
    for mode in ("track", "queue"):
        player.loop_mode = mode
        player.current = None
        player.queue.clear()
        player.queue.extend([t("a"), t("b")])
        advance(player)
        player.vc.is_playing.return_value = True
        player.stop_playback()
        player.vc.is_playing.return_value = False
        advance(player)
        assert player.current is None and not player.queue


def test_leaving_voice_does_not_leave_a_stale_song_queued(player):
    player.loop_mode = "queue"
    player.queue.extend([t("a")])
    advance(player)
    player.clear()
    player.vc.is_connected.return_value = False          # disconnect, then the player's `after` fires
    advance(player)
    assert not player.queue


def test_a_track_that_dies_instantly_is_not_looped(player):
    player.loop_mode = "track"
    player.queue.extend([t("a")])
    advance(player)
    advance(player, played_for=0.5)                      # ffmpeg failed right away
    assert player.current is None and not player.queue
    player._say.assert_awaited()


def test_previous_does_not_double_queue_under_a_loop(player):
    player.loop_mode = "queue"
    player.queue.extend([t("a"), t("b")])
    advance(player); advance(player)                     # now playing b, history [a]
    player.vc.is_playing.return_value = True             # b is playing, so previous() stops it
    assert player.previous() is True
    player.vc.is_playing.return_value = False            # ...and the player's `after` hook then fires
    advance(player)
    assert player.current.title == "a"
    assert names(player) == ["b", "a"]                   # a was already recycled to the back; nothing is duplicated


def test_loop_cycles_and_shows_in_the_embed(player):
    assert [player.cycle_loop() for _ in range(4)] == ["track", "queue", "off", "track"]
    player.current = t("a")
    assert [f.name for f in player.build_embed().fields][-1] == "Loop"
    player.loop_mode = "off"
    assert "Loop" not in [f.name for f in player.build_embed().fields]


def test_view_has_a_loop_button_that_reflects_the_mode(player):
    labels = lambda v: [c.label for c in v.children]
    assert "Loop: off" in labels(PlayerView(player))
    player.loop_mode = "track"
    v = PlayerView(player)
    assert "Loop: song" in labels(v) and len(v.children) <= 5


def test_voice_problems_flags_missing_pieces(monkeypatch):
    monkeypatch.setattr(cog.shutil, "which", lambda name: None)
    monkeypatch.setattr(cog.importlib.util, "find_spec", lambda name: None)
    problems = cog.voice_problems()
    assert any("ffmpeg" in p for p in problems) and any("davey" in p for p in problems)
