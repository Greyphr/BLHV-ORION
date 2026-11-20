"""Tests for Spotify link support (orion/spotify.py) and the lazy YouTube matching. No network, no Discord."""
import asyncio
import json
import os

os.environ.setdefault("DISCORD_TOKEN", "x"); os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://x")

from unittest.mock import MagicMock

import pytest

from orion import music, spotify
from orion.cogs.music import GuildPlayer
from orion.music import Track, UserError
from orion.spotify import SpotifyRef

TID = "4cOdK2wGLETKBW3PvgPWqT"
AID = "1DFixLWuPkv3KT3TnV35m3"
PID = "37i9dQZF1DXcBWIGoYBM5M"


# ───────────────────────────────── link parsing ──────────────────────────────
@pytest.mark.parametrize("text,expected", [
    (f"https://open.spotify.com/track/{TID}", ("track", TID)),
    (f"https://open.spotify.com/track/{TID}?si=abcdef123", ("track", TID)),
    (f"https://open.spotify.com/intl-de/track/{TID}?si=x", ("track", TID)),
    (f"https://open.spotify.com/embed/track/{TID}", ("track", TID)),
    (f"https://open.spotify.com/album/{AID}", ("album", AID)),
    (f"https://open.spotify.com/playlist/{PID}?si=x&pt=y", ("playlist", PID)),
    (f"  https://open.spotify.com/track/{TID}  ", ("track", TID)),
    (f"HTTPS://OPEN.SPOTIFY.COM/track/{TID}", ("track", TID)),
    (f"spotify:track:{TID}", ("track", TID)),
    (f"spotify:user:someone:playlist:{PID}", ("playlist", PID)),
])
def test_supported_links_parse(text, expected):
    ref = spotify.parse_ref(text)
    assert ref is not None and (ref.kind, ref.id) == expected


@pytest.mark.parametrize("text", [
    "https://open.spotify.com/artist/" + AID,            # artists aren't playable
    "https://open.spotify.com/show/" + AID,
    "https://open.spotify.com/track/tooshort",           # not a 22-char id
    "https://open.spotify.com/track/" + TID + "x",
    "https://open.spotify.com/track/../../etc/passwd",
    "https://evil.com/track/" + TID,
    "https://open.spotify.com.evil.com/track/" + TID,
    f"spotify:artist:{AID}",
    "spotify:track:short",
    "never gonna give you up",
    "",
])
def test_unsupported_links_do_not_parse(text):
    assert spotify.parse_ref(text) is None


def test_looks_like_spotify():
    assert spotify.looks_like_spotify(f"https://open.spotify.com/artist/{AID}")       # recognised, then rejected politely
    assert spotify.looks_like_spotify("https://spotify.link/AbCdEf")
    assert spotify.looks_like_spotify(f"spotify:track:{TID}")
    assert not spotify.looks_like_spotify("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert not spotify.looks_like_spotify("spotify song by drake")                    # plain text stays a search
    assert not spotify.looks_like_spotify("https://open.spotify.com.evil.com/track/" + TID)


def test_short_links_only_for_spotify_hosts():
    assert spotify.is_short_link("https://spotify.link/AbCdEf")
    assert not spotify.is_short_link("https://example.com/AbCdEf")      # we never follow arbitrary links


# ──────────────────────────────── embed parsing ──────────────────────────────
def page(entity) -> str:
    blob = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(blob)}</script></html>'


def test_single_track():
    entity = {"name": "Blinding Lights", "artists": [{"name": "The Weeknd"}], "duration": 200040,
              "coverArt": {"sources": [{"url": "https://i.scdn.co/small", "width": 64},
                                       {"url": "https://i.scdn.co/big", "width": 640}]}}
    res = spotify.tracks_from_entity(SpotifyRef("track", TID), spotify.entity_from_page(page(entity)))
    assert res.kind == "track" and len(res.tracks) == 1
    t = res.tracks[0]
    assert t.title == "Blinding Lights - The Weeknd" and t.search == "The Weeknd - Blinding Lights"
    assert t.duration == 200 and t.image == "https://i.scdn.co/big"
    assert t.url == f"https://open.spotify.com/track/{TID}" and t.yt_url is None
    assert t.thumb == "https://i.scdn.co/big"


def test_playlist_tracks_use_subtitle_for_artists_and_clean_it():
    entity = {"name": "Today's Top Hits", "trackList": [
        {"uri": f"spotify:track:{TID}", "title": "Song A", "subtitle": "Artist 1,\xa0Artist 2", "duration": 185000},
        {"uri": "spotify:track:" + "b" * 22, "title": "Song B &amp; Co", "subtitle": "Artist 3", "duration": 0},
        {"title": ""},                                          # junk entries are skipped
        "not a dict",
    ]}
    res = spotify.tracks_from_entity(SpotifyRef("playlist", PID), entity)
    assert res.title == "Today's Top Hits" and [t.title for t in res.tracks] == [
        "Song A - Artist 1, Artist 2", "Song B & Co - Artist 3"]
    assert res.tracks[0].url == f"https://open.spotify.com/track/{TID}"
    assert res.tracks[1].duration is None
    assert not res.truncated


def test_big_playlist_is_flagged_as_truncated():
    entity = {"name": "Big", "trackList": [{"uri": f"spotify:track:{TID}", "title": f"s{i}", "subtitle": "a"}
                                           for i in range(100)]}
    assert spotify.tracks_from_entity(SpotifyRef("playlist", PID), entity).truncated
    assert not spotify.tracks_from_entity(SpotifyRef("album", AID), entity).truncated      # albums show everything


@pytest.mark.parametrize("bad_page", [
    "<html>no data here</html>",
    '<script id="__NEXT_DATA__" type="application/json">{not json</script>',
    page(None),
    page("a string"),
])
def test_unreadable_pages_give_a_friendly_error(bad_page):
    with pytest.raises(UserError):
        spotify.entity_from_page(bad_page)


def test_empty_results_give_a_friendly_error():
    with pytest.raises(UserError):
        spotify.tracks_from_entity(SpotifyRef("playlist", PID), {"name": "x", "trackList": []})
    with pytest.raises(UserError):
        spotify.tracks_from_entity(SpotifyRef("playlist", PID), {"name": "x"})
    with pytest.raises(UserError):
        spotify.tracks_from_entity(SpotifyRef("track", TID), {})


def test_artist_link_is_rejected_politely_without_any_network():
    with pytest.raises(UserError, match="tracks, albums and playlists"):
        asyncio.run(spotify.load(f"https://open.spotify.com/artist/{AID}"))


# ─────────────────────────── lazy YouTube matching ───────────────────────────
def spotify_track():
    return Track("Song - Artist", f"https://open.spotify.com/track/{TID}", 200, search="Artist - Song")


def test_resolving_a_spotify_track_searches_youtube_once(monkeypatch):
    calls = []
    monkeypatch.setattr(music, "search_first", lambda q: calls.append(q) or Track("x", "https://youtu.be/abc", 201))
    monkeypatch.setattr(music, "resolve_stream", lambda url: f"stream:{url}")
    t = spotify_track()
    assert music.resolve_track(t) == "stream:https://youtu.be/abc"
    assert music.resolve_track(t) == "stream:https://youtu.be/abc"      # second time (e.g. /previous) reuses the match
    assert calls == ["Artist - Song"] and t.yt_url == "https://youtu.be/abc"
    assert t.duration == 200                                            # Spotify's length is kept


def test_no_youtube_match_raises(monkeypatch):
    monkeypatch.setattr(music, "search_first", lambda q: None)
    with pytest.raises(LookupError):
        music.resolve_track(spotify_track())


def test_youtube_tracks_still_resolve_directly(monkeypatch):
    monkeypatch.setattr(music, "search_first", lambda q: pytest.fail("must not search"))
    monkeypatch.setattr(music, "resolve_stream", lambda url: f"stream:{url}")
    assert music.resolve_track(Track("v", "https://youtu.be/abc")) == "stream:https://youtu.be/abc"


def test_prefetch_matches_only_the_next_spotify_track(monkeypatch):
    seen = []
    monkeypatch.setattr("orion.cogs.music.find_on_youtube", lambda t: seen.append(t.title))
    p = GuildPlayer(MagicMock())
    asyncio.run(p._prefetch())                                           # empty queue: nothing to do
    p.queue.extend([Track("yt", "https://youtu.be/a"), spotify_track()])
    asyncio.run(p._prefetch())                                           # next is a YouTube track: nothing to do
    p.queue.appendleft(spotify_track())
    asyncio.run(p._prefetch())
    assert seen == ["Song - Artist"]


def test_prefetch_swallows_errors(monkeypatch):
    def boom(t):
        raise RuntimeError("nope")
    monkeypatch.setattr("orion.cogs.music.find_on_youtube", boom)
    p = GuildPlayer(MagicMock())
    p.queue.append(spotify_track())
    asyncio.run(p._prefetch())                                           # must not raise


@pytest.mark.parametrize("tag", [
    '<script id="__NEXT_DATA__" type="application/json">',
    '<script id="__NEXT_DATA__" type="application/json" crossorigin="anonymous">',
    "<script type='application/json' id='__NEXT_DATA__' nonce=\"abc\">",
])
def test_next_data_survives_extra_script_attributes(tag):
    blob = {"props": {"pageProps": {"state": {"data": {"entity": {"name": "Song"}}}}}}
    page = f"<html><head>{tag}{json.dumps(blob)}</script></head></html>"
    assert spotify.entity_from_page(page) == {"name": "Song"}
