"""Swing Music playlist edits for the liked-music sync (REST API, cookie login).

Swing has no API keys: the agent logs in with LIKED_MUSIC_SWING_USER /
LIKED_MUSIC_SWING_PASSWORD and keeps the session cookie for the run. Songs are
matched by their path inside Swing's container (LIKED_MUSIC_SWING_DIR plus the
library filename).
"""

import http.cookiejar
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .http_json import build_url, request_json

Send = Callable[..., object]
ALL_TRACKS = 100000  # Swing's limit=-1 drops the last track (len - 1), so ask for "plenty"


@dataclass
class SwingSettings:
    url: str
    username: str
    password: str
    music_dir: str
    liked_playlist: str


def cookie_opener() -> Callable[..., object]:
    """urlopen-compatible callable that keeps Swing's session cookie."""
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())).open


class SwingPlaylists:
    """Edits the user's Swing playlists. `send` is request_json, or a fake in tests."""

    def __init__(self, settings: SwingSettings, send: Send = request_json, opener: Optional[Callable] = None):
        self.settings = settings
        self.send = send
        self.opener = opener or cookie_opener()
        self._logged_in = False

    def _call(self, method: str, path: str, body: object = None, query: Optional[Dict] = None) -> object:
        if not self._logged_in:
            self._logged_in = True
            self._call("POST", "/auth/login", {"username": self.settings.username, "password": self.settings.password})
        return self.send(method, build_url(self.settings.url, path, query), {}, body, opener=self.opener)

    def path_for(self, filename: str) -> str:
        return self.settings.music_dir.rstrip("/") + "/" + filename

    def playlists(self) -> Dict[str, int]:
        """{playlist name: id}."""
        return {p["name"]: p["id"] for p in self._call("GET", "/playlists", query={"no_images": "true"})["data"]}

    def _tracks(self, playlist_id: int) -> List[dict]:
        return self._call("GET", f"/playlists/{playlist_id}", query={"limit": ALL_TRACKS})["tracks"]

    def remove_files(self, filenames: List[str]) -> int:
        """Remove these songs from every playlist; returns entries removed.

        Items go bottom-up because Swing removes them one by one and only when
        the hash is still at the given index (a stale index removes nothing).
        """
        paths = {self.path_for(name) for name in filenames}
        removed = 0
        for playlist_id in self.playlists().values():
            tracks = self._tracks(playlist_id)
            items = [{"trackhash": t["trackhash"], "index": i} for i, t in enumerate(tracks) if t.get("filepath") in paths]
            if items:
                self._call("POST", f"/playlists/{playlist_id}/remove-tracks", {"tracks": items[::-1]})
                left = {t["trackhash"] for t in self._tracks(playlist_id)}
                removed += sum(1 for item in items if item["trackhash"] not in left)
        return removed

    def add(self, filenames: List[str]) -> List[str]:
        """Append songs Swing has indexed to the liked playlist; returns those added.

        Swing has no reorder endpoint, so new likes go at the end of its playlist.
        """
        playlist_id = self.playlists().get(self.settings.liked_playlist)
        if playlist_id is None:
            raise LookupError(f"Swing playlist {self.settings.liked_playlist!r} not found")
        hashes = self._hashes_by_path()
        ready = [name for name in filenames if self.path_for(name) in hashes]
        if ready:
            itemhash = ",".join(hashes[self.path_for(name)] for name in ready)
            self._call("POST", f"/playlists/{playlist_id}/add", {"itemtype": "tracks", "itemhash": itemhash, "sortoptions": {}})
        return ready

    def _hashes_by_path(self) -> Dict[str, str]:
        folder = self._call("POST", "/folder", {"folder": self.settings.music_dir, "tracks_only": True,
                                                "start": 0, "limit": ALL_TRACKS})
        return {t["filepath"]: t["trackhash"] for t in folder.get("tracks", [])}
