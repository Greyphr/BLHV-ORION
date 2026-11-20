"""Music helpers: YouTube link handling, the Track type and the (blocking) yt-dlp wrappers.

Ported from the standalone Sarada bot. Nothing in here touches Discord, so the string
handling can be unit-tested without a connection (see tests/test_music.py).

yt-dlp is imported lazily, inside the functions that need it, so that update_ytdlp() can
upgrade it at startup *before* the first import picks up the old version.
"""
import asyncio
import importlib.metadata
import logging
import subprocess
import sys
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import config

log = logging.getLogger("orion.music")

YOUTUBE_HOSTS = {"youtube.com", "youtu.be"}
BAD_TITLES = {"[Private video]", "[Deleted video]"}
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"


class UserError(Exception):
    """An error whose message is safe to show to the user."""


# ───────────────────────────── track + link handling ─────────────────────────
@dataclass
class Track:
    title: str
    url: str                      # watch URL; the stream URL is resolved at play time
    duration: int | None = None
    uploader: str | None = None
    requester: str | None = None
    # Spotify tracks: Spotify only supplies the metadata, the audio comes from YouTube. `url` stays the
    # Spotify link (shown on the panel), `search` is what we look up on YouTube, and `yt_url` caches the result.
    image: str | None = None
    search: str | None = None
    yt_url: str | None = None

    @property
    def thumb(self) -> str | None:
        if self.image:
            return self.image
        u = urlparse(self.url)
        vid = parse_qs(u.query).get("v", [None])[0]
        if not vid and (u.netloc.endswith("youtu.be") or u.path.startswith(("/shorts/", "/live/"))):
            segments = [s for s in u.path.split("/") if s]
            vid = segments[-1] if segments else None
        return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else None


def fmt_duration(sec: int | None) -> str:
    if not sec:
        return "?:??"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def classify(text: str) -> str:
    """Returns 'search', 'video', 'playlist' or 'unsupported'."""
    if not text.startswith(("http://", "https://")):
        return "search"
    u = urlparse(text)
    host = u.netloc.lower()
    for prefix in ("www.", "m.", "music."):
        host = host.removeprefix(prefix)
    if host not in YOUTUBE_HOSTS:
        return "unsupported"
    q = parse_qs(u.query)
    if host == "youtu.be" or "v" in q or u.path.startswith(("/shorts/", "/live/")):
        return "video"          # includes watch?v=X&list=Y (we queue just the video)
    if "list" in q:
        return "playlist"
    return "unsupported"


def list_id(text: str) -> str | None:
    return parse_qs(urlparse(text).query).get("list", [None])[0]


# ───────────────────────────── yt-dlp (blocking helpers) ─────────────────────
def _ytdlp_version() -> str | None:
    try:
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return None


def update_ytdlp() -> None:
    """Best-effort `pip install -U yt-dlp[default]`. Never fatal: on any failure the bot just
    carries on with whatever version is already installed. Blocking: call it from a thread.

    YouTube changes often and an outdated yt-dlp is the most common cause of failed loads.
    Set AUTO_UPDATE_YTDLP=0 to turn this off."""
    if not config.AUTO_UPDATE_YTDLP:
        log.info("yt-dlp auto-update is disabled (AUTO_UPDATE_YTDLP=0)")
        return
    before = _ytdlp_version()
    try:
        # sys.executable keeps the install inside the interpreter this bot runs in.
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]",
             "--disable-pip-version-check", "-q"],
            capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        log.warning("yt-dlp update timed out after 120s; continuing with the installed version")
        return
    except Exception as e:
        log.warning("yt-dlp update failed: %r; continuing with the installed version", e)
        return
    if proc.returncode != 0:
        log.warning("yt-dlp update failed (pip exit %d): %s", proc.returncode, (proc.stderr or "").strip()[-500:])
        return
    after = _ytdlp_version()
    if after is None:
        log.warning("yt-dlp version is still unreadable after the update attempt")
    elif before is not None and before != after:
        log.info("yt-dlp updated: %s -> %s", before, after)
    else:
        log.info("yt-dlp is up to date (%s)", after)


class YDLLogger:
    """Feeds yt-dlp's own messages into our log, in place of its console."""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.error("yt-dlp: %s", msg)


def _ydl(**extra: Any):
    import yt_dlp      # lazy on purpose, see the module docstring
    # socket_timeout/retries keep a stalled request from pinning the worker thread that
    # asyncio.to_thread is blocked on, forever.
    opts: dict[str, Any] = {
        "quiet": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 3,
        "logger": YDLLogger(),
    }
    if config.COOKIES_FILE:
        opts["cookiefile"] = config.COOKIES_FILE
    opts.update(extra)
    return yt_dlp.YoutubeDL(opts)


def _watch_url(entry: dict) -> str:
    return entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}"


def _track_from_entry(entry: dict) -> Track:
    """Build a Track from a flat (extract_flat) entry, as playlists and search give."""
    return Track(
        entry.get("title") or "Unknown",
        _watch_url(entry),
        entry.get("duration"),
        entry.get("channel") or entry.get("uploader"),
    )


def fetch_video(url: str) -> Track:
    with _ydl(noplaylist=True, format="bestaudio/best") as ydl:
        info = ydl.extract_info(url, download=False)
    title = info.get("title")
    if title is None:
        raise KeyError("title")
    return Track(
        title,
        info.get("webpage_url") or url,
        info.get("duration"),
        info.get("channel") or info.get("uploader"),
    )


def fetch_playlist(url: str) -> tuple[str, list[Track]]:
    with _ydl(extract_flat="in_playlist", playlistend=config.MUSIC_MAX_PLAYLIST) as ydl:
        info = ydl.extract_info(url, download=False)
    tracks = [
        _track_from_entry(e)
        for e in (info.get("entries") or [])
        if e and e.get("title") not in BAD_TITLES
    ]
    return info.get("title") or "playlist", tracks


def search_first(query: str) -> Track | None:
    with _ydl(extract_flat=True) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        return None
    return _track_from_entry(entries[0])


def resolve_stream(watch_url: str) -> str:
    with _ydl(format="bestaudio/best", noplaylist=True) as ydl:
        info = ydl.extract_info(watch_url, download=False)
    url = info.get("url")
    if url is None:
        raise KeyError("url")
    return url


def find_on_youtube(track: Track) -> str:
    """Spotify tracks only: look the song up on YouTube once and remember the watch URL. Blocking."""
    if not track.yt_url:
        found = search_first(track.search or track.title)
        if not found:
            raise LookupError(f"no YouTube match for {track.title!r}")
        track.yt_url = found.url
        track.duration = track.duration or found.duration
    return track.yt_url


def resolve_track(track: Track) -> str:
    """The playable stream URL for any track, YouTube or Spotify. Blocking."""
    if track.search:
        return resolve_stream(find_on_youtube(track))
    return resolve_stream(track.url)


# asyncio keeps only a weak reference to a running task, so a fire-and-forget create_task can
# be garbage-collected before it ever runs. Parking them here until they finish keeps them alive.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Start a coroutine in the background and hold a reference until it's done."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task
