"""Look up song lyrics on lrclib.net (free, open database, no API key).

Three tries per song, stopping at the first hit:
1. /api/get with title, artist, album and duration (exact match; only for
   songs that have an album tag, i.e. real YouTube Music tracks);
2. /api/search with title and artist;
3. /api/search with a cleaned-up title as free text (video noise such as
   "(Official Video)" removed), for songs saved from plain videos.

A search hit only counts when its duration is within DURATION_TOLERANCE_SECONDS
of the file's, so a cover or live version doesn't get the original's (wrongly
timed) lyrics. Synced lyrics win over plain ones; instrumentals count as none.
"""

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from .http_json import HttpJsonError, build_url, request_json

LRCLIB_URL = "https://lrclib.net"
USER_AGENT = "homelab-monitor liked-music lyrics (https://github.com/diegochagas/homelab-monitor)"
DURATION_TOLERANCE_SECONDS = 3
PAUSE_SECONDS = 1.0  # between requests: one at a time, gently, on a free service
BUSY_WAITS_SECONDS = (5, 15, 30)  # lrclib answers 503/429 "server is busy" under load; wait, then retry
NOISE = re.compile(r"[(\[【][^)\]】]*(official|video|lyric|audio|\bmv\b|m/v|\bhd\b|4k|visualizer|legendad|"
                   r"tradu[cç][aã]o|clipe|full|live|ao vivo)[^)\]】]*[)\]】]", re.IGNORECASE)
TRAILING_NOISE = re.compile(r"\s+[-|｜]\s*(legendado|tradu[cç][aã]o|lyrics?|letra)\s*$", re.IGNORECASE)

Send = Callable[..., Any]


@dataclass
class SongMeta:
    title: str
    artist: str
    album: str
    seconds: int


@dataclass
class Lyrics:
    text: str
    synced: bool


def clean_title(title: str) -> str:
    """Title without video noise and fancy quotes.

    Example: clean_title('Kiss The Girl (Official Video)') -> 'Kiss The Girl'
    """
    plain = NOISE.sub("", title.replace("＂", '"'))
    plain = TRAILING_NOISE.sub("", plain)
    return re.sub(r"\s+", " ", plain).strip(' "-|｜')


def best_match(results: Any, seconds: int) -> Optional[Lyrics]:
    """Closest-length result with lyrics, synced first; None when nothing is close enough.

    Example: best_match([{"duration": 200, "syncedLyrics": "[00:01.00] Hi"}], 201).synced -> True
    """
    usable = [r for r in results or [] if isinstance(r, dict) and not r.get("instrumental")
              and abs((r.get("duration") or 0) - seconds) <= DURATION_TOLERANCE_SECONDS
              and (r.get("syncedLyrics") or r.get("plainLyrics"))]
    if not usable:
        return None
    usable.sort(key=lambda r: (not r.get("syncedLyrics"), abs(r["duration"] - seconds)))
    return _as_lyrics(usable[0])


def _as_lyrics(result: dict) -> Optional[Lyrics]:
    if result.get("instrumental"):
        return None
    if result.get("syncedLyrics"):
        return Lyrics(result["syncedLyrics"], True)
    if result.get("plainLyrics"):
        return Lyrics(result["plainLyrics"], False)
    return None


def _busy(error: HttpJsonError) -> bool:
    return "HTTP 503" in str(error) or "HTTP 429" in str(error)


class LrclibClient:
    """lrclib.net lookups. `send` is request_json (or a fake); `pause` sleeps between requests."""

    def __init__(self, send: Send = request_json, pause: Callable[[float], None] = time.sleep):
        self.send = send
        self.pause = pause

    def _get(self, path: str, query: dict) -> Any:
        url = build_url(LRCLIB_URL, path, query)
        for wait in (PAUSE_SECONDS, *BUSY_WAITS_SECONDS):
            self.pause(wait)
            try:
                return self.send("GET", url, {"User-Agent": USER_AGENT})
            except HttpJsonError as error:
                if "HTTP 404" in str(error):  # lrclib's "no such track"
                    return None
                if not _busy(error) or wait == BUSY_WAITS_SECONDS[-1]:
                    raise
        return None

    def find(self, song: SongMeta) -> Optional[Lyrics]:
        """Lyrics for a song, or None; raises HttpJsonError when lrclib is unreachable."""
        if song.album:
            exact = self._get("/api/get", {"track_name": song.title, "artist_name": song.artist,
                                           "album_name": song.album, "duration": song.seconds})
            if isinstance(exact, dict) and _as_lyrics(exact):
                return _as_lyrics(exact)
        for query in self._search_queries(song):
            found = best_match(self._get("/api/search", query), song.seconds)
            if found:
                return found
        return None

    @staticmethod
    def _search_queries(song: SongMeta) -> List[dict]:
        # The free-text query also catches titles whose artist tag is the uploader
        # (e.g. "Backstreet Boys - Larger Than Life (Official Video)").
        return [{"track_name": song.title, "artist_name": song.artist}, {"q": clean_title(song.title) or song.title}]
