"""Tests for the music helpers (orion/music.py) and the queue logic of the player.

Ported from the Sarada bot's test_helpers.py. No network and no Discord connection.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from unittest.mock import MagicMock

import discord
import pytest

from orion.cogs.music import NO_MENTIONS, GuildPlayer
from orion.music import Track, classify, fmt_duration, list_id

PLAYLIST = "PLrAXtmRdnEQy6nuLMfO6uJhFhrqcTZJg"


def track(url: str) -> Track:
    return Track(title="test", url=url)


# ────────────────────────────────── classify ────────────────────────────────
@pytest.mark.parametrize("text", [
    "never gonna give you up",
    "Rick Astley - Never Gonna Give You Up",
    "",
    "   ",
    "youtube.com/watch?v=abc123",        # pasted without a scheme
    "https:/typo.com",                   # one slash
    "ftp://youtube.com/watch?v=abc123",
])
def test_plain_text_is_a_search(text):
    assert classify(text) == "search"


@pytest.mark.parametrize("text", [
    "https://youtu.be/dQw4w9WgXcQ",
    "http://youtu.be/dQw4w9WgXcQ",       # plain http counts too
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/watch?v=dQw4w9WgXcQ",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube.com/live/dQw4w9WgXcQ",
    "https://m.youtube.com/shorts/abc123",
    # a watch link that also carries a list queues the video, not the playlist
    f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={PLAYLIST}",
    f"https://youtu.be/dQw4w9WgXcQ?list={PLAYLIST}",
])
def test_video_links(text):
    assert classify(text) == "video"


@pytest.mark.parametrize("text", [
    f"https://www.youtube.com/playlist?list={PLAYLIST}",
    f"https://youtube.com/playlist?list={PLAYLIST}",
    f"https://m.youtube.com/playlist?list={PLAYLIST}",
    "https://music.youtube.com/playlist?list=OLAK5uy_kSomething",
    f"https://www.youtube.com/watch?list={PLAYLIST}",      # no v=, so it's a playlist
    f"https://www.youtube.com/watch?index=3&list={PLAYLIST}",
])
def test_playlist_links(text):
    assert classify(text) == "playlist"


@pytest.mark.parametrize("text", [
    "https://vimeo.com/123456789",
    "https://soundcloud.com/artist/song",
    "https://example.com/watch?v=dQw4w9WgXcQ",   # a v= param isn't enough
    "https://notyoutube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/",
    "https://www.youtube.com/@somechannel",
    "https://www.youtube.com/c/somechannel",
])
def test_non_youtube_and_bare_paths_are_unsupported(text):
    assert classify(text) == "unsupported"


def test_scheme_check_is_case_sensitive():
    # urlparse copes with a capital scheme but classify's startswith does not,
    # so an upper-case link silently becomes a search. Recorded because it reads
    # as an oversight rather than a decision.
    assert classify("HTTPS://WWW.YOUTUBE.COM/watch?v=dQw4w9WgXcQ") == "search"


def test_bare_youtu_be_host_is_still_a_video():
    # youtu.be is accepted on the host alone, with no id behind it to play.
    assert classify("https://youtu.be/") == "video"


# ─────────────────────────────────── list_id ─────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    (f"https://www.youtube.com/playlist?list={PLAYLIST}", PLAYLIST),
    (f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={PLAYLIST}", PLAYLIST),
    (f"https://www.youtube.com/watch?list={PLAYLIST}&v=dQw4w9WgXcQ", PLAYLIST),
    ("https://music.youtube.com/playlist?list=OLAK5uy_kSomething", "OLAK5uy_kSomething"),
    (f"https://www.youtube.com/playlist?list={PLAYLIST}&list=PLsecond", PLAYLIST),
    ("https://youtu.be/dQw4w9WgXcQ", None),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", None),
    ("https://www.youtube.com/shorts/abc123", None),
    ("https://www.youtube.com/playlist?list=", None),     # blank is dropped
    ("", None),
    ("never gonna give you up", None),
])
def test_list_id(text, expected):
    assert list_id(text) == expected


def test_every_playlist_carries_the_id_it_was_queued_from():
    url = f"https://www.youtube.com/playlist?list={PLAYLIST}"
    assert classify(url) == "playlist"
    assert list_id(url) == PLAYLIST


# ──────────────────────────────── fmt_duration ───────────────────────────────
@pytest.mark.parametrize("seconds,expected", [
    (None, "?:??"),
    (0, "?:??"),
    (5, "0:05"),
    (59, "0:59"),
    (60, "1:00"),
    (61, "1:01"),
    (599, "9:59"),
    (600, "10:00"),
    (3599, "59:59"),
    (3600, "1:00:00"),
    (3661, "1:01:01"),
    (86399, "23:59:59"),
    (90061, "25:01:01"),       # hours keep counting, they don't wrap
])
def test_fmt_duration(seconds, expected):
    assert fmt_duration(seconds) == expected


# ─────────────────────────────── Track.thumb ─────────────────────────────────
@pytest.mark.parametrize("url,video_id", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtube.com/watch?v=abc123", "abc123"),
    ("https://m.youtube.com/watch?v=abc123", "abc123"),
    ("https://music.youtube.com/watch?v=abc123", "abc123"),
    # the v= param is read first, so it wins over anything in the path
    (f"https://www.youtube.com/watch?v=fromquery&list={PLAYLIST}", "fromquery"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/abc123?si=tracking_id", "abc123"),
    ("https://www.youtube.com/shorts/abc123", "abc123"),
    ("https://www.youtube.com/live/xyz789", "xyz789"),
])
def test_thumb_uses_the_video_id(url, video_id):
    assert track(url).thumb == f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/playlist?list={PLAYLIST}",    # a playlist has no picture
    "https://www.youtube.com/watch",                      # v= present but empty
    "https://www.youtube.com/",
    "",
])
def test_thumb_is_none_without_a_video_id(url):
    assert track(url).thumb is None


# ───────────────────────────── player queue logic ────────────────────────────
def player(playing=True):
    p = GuildPlayer(MagicMock())
    p.vc = MagicMock()
    p.vc.is_playing.return_value = playing
    p.vc.is_paused.return_value = False
    return p


def t(name):
    return Track(title=name, url=f"https://youtu.be/{name}")


def test_previous_needs_a_voice_client():
    p = GuildPlayer(MagicMock())
    p.current = t("a")
    assert p.previous() is False


def test_previous_with_nothing_at_all():
    assert player().previous() is False


def test_previous_goes_back_one_and_requeues_the_current():
    p = player()
    p.history.append(t("one"))
    p.current = t("two")
    p.queue.extend([t("three")])
    assert p.previous() is True
    assert [x.title for x in p.queue] == ["one", "two", "three"]
    assert p._suppress_history is True
    p.vc.stop.assert_called_once()           # stopping is what triggers `after` -> play_next


def test_previous_with_empty_history_restarts_the_current_track():
    p = player()
    p.current = t("only")
    assert p.previous() is True
    assert [x.title for x in p.queue] == ["only"]


def test_skip_only_stops_when_something_is_active():
    p = player(playing=True)
    p.skip()
    p.vc.stop.assert_called_once()
    idle = player(playing=False)
    idle.skip()
    idle.vc.stop.assert_not_called()


def test_clear_and_shuffle_keep_the_same_tracks():
    p = player()
    p.queue.extend(t(str(i)) for i in range(20))
    p.shuffle()
    assert sorted(x.title for x in p.queue) == sorted(str(i) for i in range(20))
    p.clear()
    assert not p.queue


def test_history_is_capped_at_50():
    p = player()
    for i in range(80):
        p.history.append(t(str(i)))
    assert len(p.history) == 50
    assert p.history[-1].title == "79"


def test_embed_shows_up_next_and_requester():
    p = player()
    p.current = Track("Song", "https://youtu.be/abc123", 185, "Artist", "Sam")
    p.queue.extend([t("next")])
    e = p.build_embed()
    assert e.title == "Song" and e.description == "Artist · 3:05"
    assert e.author.name == "Now playing" and p.build_embed(paused=True).author.name == "Paused"
    assert [f.value for f in e.fields] == ["next", "1"]
    assert e.footer.text == "Requested by Sam"


def test_embed_with_nothing_playing():
    e = player().build_embed()
    assert e.title == "Nothing is playing"
    assert [f.value for f in e.fields] == ["nothing", "0"]


def test_titles_cannot_ping_anyone():
    # track titles are untrusted text: every message that echoes one sends with these mentions
    assert NO_MENTIONS.everyone is False and NO_MENTIONS.users is False and NO_MENTIONS.roles is False
