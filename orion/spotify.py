"""Spotify link support for the music player.

Spotify's audio is DRM protected, so no bot can stream it. What we do instead is read the *metadata* (title, artists,
length, cover art) from Spotify's public embed page, which needs no login and no API keys, and play the matching song
from YouTube. Tracks are matched lazily: only the song that is about to play (plus the next one) is looked up.

Supported: open.spotify.com track / album / playlist links (incl. /intl-xx/ ones), spotify.link short links and
spotify:track:<id> URIs. Spotify only exposes the first 100 tracks of a playlist on the embed page.

Nothing here touches Discord, so the parsing is unit-tested without a connection (tests/test_spotify.py).
"""
import html
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp

from . import config
from .music import Track, UserError

log = logging.getLogger("orion.spotify")

KINDS = ("track", "album", "playlist")
OPEN_HOST = "open.spotify.com"
SHORT_HOSTS = {"spotify.link", "spotify.app.link"}
_ID = re.compile(r"^[A-Za-z0-9]{22}$")                     # every Spotify id is 22 base-62 characters
_OPEN_IN_TEXT = re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]{22})")
# tolerant on purpose: attribute order/extra attributes (e.g. crossorigin="anonymous") must not break the match
_NEXT_DATA = re.compile(r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S | re.I)
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"}
_TIMEOUT = aiohttp.ClientTimeout(total=15)
_MAX_BYTES = 3_000_000
PLAYLIST_VISIBLE = 100                                      # what the embed page shows of a playlist


@dataclass(frozen=True)
class SpotifyRef:
    kind: str
    id: str

    @property
    def url(self) -> str:
        return f"https://{OPEN_HOST}/{self.kind}/{self.id}"


@dataclass
class SpotifyResult:
    kind: str
    title: str
    tracks: list[Track]
    truncated: bool = False         # a playlist that shows fewer tracks than it really has


# ───────────────────────────── link handling (pure) ──────────────────────────
def _host(text: str) -> str:
    return urlparse(text).netloc.lower().removeprefix("www.")


def looks_like_spotify(text: str) -> bool:
    """True for anything that is clearly meant as a Spotify link, supported or not."""
    t = text.strip()
    if t.lower().startswith("spotify:"):
        return True
    return t.lower().startswith(("http://", "https://")) and (_host(t) == OPEN_HOST or _host(t) in SHORT_HOSTS)


def is_short_link(text: str) -> bool:
    t = text.strip()
    return t.lower().startswith(("http://", "https://")) and _host(t) in SHORT_HOSTS


def parse_ref(text: str) -> SpotifyRef | None:
    """open.spotify.com URL or spotify: URI -> SpotifyRef, or None if it isn't a track/album/playlist."""
    t = text.strip()
    if t.lower().startswith("spotify:"):
        parts = t.split(":")
        # spotify:track:<id>   (also the long form spotify:user:<name>:playlist:<id>)
        if len(parts) >= 3 and parts[-2] in KINDS and _ID.match(parts[-1]):
            return SpotifyRef(parts[-2], parts[-1])
        return None
    if not (t.lower().startswith(("http://", "https://")) and _host(t) == OPEN_HOST):
        return None
    segs = [s for s in urlparse(t).path.split("/") if s]
    if segs and segs[0].startswith("intl-"):
        segs = segs[1:]
    if segs and segs[0] == "embed":
        segs = segs[1:]
    if len(segs) >= 2 and segs[0] in KINDS and _ID.match(segs[1]):
        return SpotifyRef(segs[0], segs[1])
    return None


# ───────────────────────────── embed page parsing (pure) ─────────────────────
def _dig(obj: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _clean(s: Any) -> str:
    return " ".join(html.unescape(str(s or "")).replace("\xa0", " ").split())


def entity_from_page(page: str) -> dict:
    """Pull the track / album / playlist object out of an embed page's __NEXT_DATA__ blob."""
    m = _NEXT_DATA.search(page)
    if not m:
        # log what we actually got (a consent/bot-check page looks very different from the embed page)
        title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        scripts = [t[:160] for t in re.findall(r"<script\b[^>]*>", page, re.I)][:12]
        log.warning("spotify embed has no __NEXT_DATA__ (len=%d, title=%r)\n  script tags: %s\n  body tail: %r",
                    len(page), _clean(title.group(1)) if title else None, scripts, page[-300:])
        raise UserError("Spotify's page didn't look the way I expected, so I can't read that link right now.")
    try:
        data = json.loads(m.group(1))
    except ValueError:
        log.warning("spotify __NEXT_DATA__ is not valid JSON (len=%d)", len(m.group(1)))
        raise UserError("Spotify's page didn't look the way I expected, so I can't read that link right now.")
    entity = _dig(data, "props", "pageProps", "state", "data", "entity")
    if not isinstance(entity, dict):
        raise UserError("I couldn't read that Spotify link. It may be private or unavailable in my region.")
    return entity


def _artists(item: dict) -> str:
    arts = item.get("artists")
    if isinstance(arts, list):
        names = [_clean(a.get("name")) for a in arts if isinstance(a, dict) and a.get("name")]
        if names:
            return ", ".join(names)
    return _clean(item.get("subtitle"))


def _cover(entity: dict) -> str | None:
    for path in (("coverArt", "sources"), ("visualIdentity", "image"), ("images",)):
        sources = _dig(entity, *path)
        if isinstance(sources, list):
            with_url = [s for s in sources if isinstance(s, dict) and s.get("url")]
            if with_url:
                best = max(with_url, key=lambda s: s.get("width") or s.get("maxWidth") or 0)
                return best["url"]
    return None


def _seconds(ms: Any) -> int | None:
    return int(ms) // 1000 if isinstance(ms, (int, float)) and ms > 0 else None


def _make_track(name: str, artists: str, ms: Any, url: str, image: str | None) -> Track:
    title = f"{name} - {artists}" if artists else name
    return Track(title=title, url=url, duration=_seconds(ms), image=image,
                 search=f"{artists} - {name}" if artists else name)


def tracks_from_entity(ref: SpotifyRef, entity: dict) -> SpotifyResult:
    name = _clean(entity.get("name") or entity.get("title"))
    image = _cover(entity)
    if ref.kind == "track":
        if not name:
            raise UserError("I couldn't read that Spotify track.")
        return SpotifyResult("track", name, [_make_track(name, _artists(entity), entity.get("duration"), ref.url, image)])

    items = entity.get("trackList")
    if not isinstance(items, list):
        raise UserError("I couldn't read the tracks of that Spotify link. It may be private.")
    tracks: list[Track] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = _clean(it.get("title") or it.get("name"))
        if not title:
            continue
        uri = str(it.get("uri") or "")
        tid = uri.rsplit(":", 1)[-1]
        url = f"https://{OPEN_HOST}/track/{tid}" if uri.startswith("spotify:track:") and _ID.match(tid) else ref.url
        tracks.append(_make_track(title, _artists(it), it.get("duration"), url, image))
    tracks = tracks[:config.MUSIC_MAX_PLAYLIST]
    if not tracks:
        raise UserError("I couldn't find any tracks in that Spotify link.")
    return SpotifyResult(ref.kind, name or ref.kind, tracks,
                         truncated=ref.kind == "playlist" and len(tracks) >= PLAYLIST_VISIBLE)


# ───────────────────────────── network ───────────────────────────────────────
async def _resolve_short(text: str, session: aiohttp.ClientSession) -> SpotifyRef:
    async with session.get(text.strip(), allow_redirects=True, max_redirects=5) as r:
        ref = parse_ref(str(r.url))
        if ref:
            return ref
        body = (await r.content.read(_MAX_BYTES)).decode("utf-8", "ignore")
    m = _OPEN_IN_TEXT.search(body)
    if m:
        return SpotifyRef(m.group(1), m.group(2))
    raise UserError("That Spotify link doesn't point to a track, album or playlist.")


async def _fetch_page(ref: SpotifyRef, session: aiohttp.ClientSession) -> str:
    async with session.get(f"https://{OPEN_HOST}/embed/{ref.kind}/{ref.id}") as r:
        if r.status == 404:
            raise UserError("Spotify says that link doesn't exist, or it's private.")
        if r.status != 200:
            raise UserError("I couldn't reach Spotify just now. Try again in a minute.")
        return (await r.content.read(_MAX_BYTES)).decode("utf-8", "ignore")


async def load(text: str) -> SpotifyResult:
    """Spotify link -> its tracks (not yet matched to YouTube). Raises UserError with a friendly message."""
    ref = parse_ref(text)
    try:
        async with aiohttp.ClientSession(headers=_HEADERS, timeout=_TIMEOUT) as session:
            if ref is None and is_short_link(text):
                ref = await _resolve_short(text, session)
            if ref is None:
                raise UserError("I can play Spotify **tracks, albums and playlists**. "
                                "That link is something else (an artist or podcast, say).")
            page = await _fetch_page(ref, session)
    except (aiohttp.ClientError, TimeoutError) as e:
        log.warning("spotify fetch failed: %r", e)
        raise UserError("I couldn't reach Spotify just now. Try again in a minute.") from e
    result = tracks_from_entity(ref, entity_from_page(page))
    log.info("Spotify %s %s: %d track(s)", ref.kind, ref.id, len(result.tracks))
    return result
