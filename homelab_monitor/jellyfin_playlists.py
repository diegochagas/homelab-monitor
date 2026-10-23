"""Jellyfin playlist edits for the liked-music sync (REST API with an API key).

Songs are matched by their path as Jellyfin sees it: LIKED_MUSIC_JELLYFIN_DIR
plus the library filename (Jellyfin usually runs in a container, so its path
differs from the server's).
"""

from dataclasses import dataclass
from typing import Callable, Dict, List

from .http_json import build_url, request_json

Send = Callable[..., object]


@dataclass
class JellyfinSettings:
    url: str
    api_key: str
    username: str
    music_dir: str
    liked_playlist: str


class JellyfinPlaylists:
    """Edits the user's playlists. `send` is request_json, or a fake in tests."""

    def __init__(self, settings: JellyfinSettings, send: Send = request_json):
        self.settings = settings
        self.send = send
        self._user_id = ""

    def _call(self, method: str, path: str, query: Dict = None) -> object:
        headers = {"Authorization": f'MediaBrowser Token="{self.settings.api_key}"'}
        return self.send(method, build_url(self.settings.url, path, query), headers)

    def path_for(self, filename: str) -> str:
        return self.settings.music_dir.rstrip("/") + "/" + filename

    def user_id(self) -> str:
        if not self._user_id:
            users = self._call("GET", "/Users")
            match = [u["Id"] for u in users if u.get("Name") == self.settings.username]
            if not match:
                raise LookupError(f"Jellyfin user {self.settings.username!r} not found (LIKED_MUSIC_JELLYFIN_USER)")
            self._user_id = match[0]
        return self._user_id

    def playlists(self) -> Dict[str, str]:
        """{playlist name: id} for the user."""
        found = self._call("GET", f"/Users/{self.user_id()}/Items",
                           {"IncludeItemTypes": "Playlist", "Recursive": "true"})
        return {item["Name"]: item["Id"] for item in found.get("Items", [])}

    def remove_files(self, filenames: List[str]) -> int:
        """Remove these songs from every playlist; returns entries removed."""
        paths = {self.path_for(name) for name in filenames}
        removed = 0
        for playlist_id in self.playlists().values():
            entries = self._call("GET", f"/Playlists/{playlist_id}/Items",
                                 {"userId": self.user_id(), "Fields": "Path"}).get("Items", [])
            entry_ids = [e["PlaylistItemId"] for e in entries if e.get("Path") in paths]
            if entry_ids:
                self._call("DELETE", f"/Playlists/{playlist_id}/Items", {"entryIds": ",".join(entry_ids)})
                removed += len(entry_ids)
        return removed

    def add(self, filenames: List[str]) -> List[str]:
        """Append songs Jellyfin has indexed to the liked playlist; returns those now in it.

        Songs already in the playlist are not added again, so a retried add is safe.
        New likes go to the end: Jellyfin's Move endpoint takes the user from the
        login session and refuses API-key requests (HTTP 400), so no reordering.
        """
        playlist_id = self.playlists().get(self.settings.liked_playlist)
        if playlist_id is None:
            raise LookupError(f"Jellyfin playlist {self.settings.liked_playlist!r} not found")
        item_ids = self._item_ids_by_path([self.path_for(name) for name in filenames])
        ready = [name for name in filenames if self.path_for(name) in item_ids]
        present = {entry["Id"] for entry in self._entries(playlist_id)}
        missing = [item_ids[self.path_for(name)] for name in ready if item_ids[self.path_for(name)] not in present]
        if missing:
            self._call("POST", f"/Playlists/{playlist_id}/Items", {"ids": ",".join(missing), "userId": self.user_id()})
        return ready

    def _entries(self, playlist_id: str) -> List[dict]:
        return self._call("GET", f"/Playlists/{playlist_id}/Items", {"userId": self.user_id()}).get("Items", [])

    def _item_ids_by_path(self, paths: List[str]) -> Dict[str, str]:
        wanted = set(paths)
        found = self._call("GET", f"/Users/{self.user_id()}/Items",
                           {"IncludeItemTypes": "Audio", "Recursive": "true", "Fields": "Path"})
        return {i["Path"]: i["Id"] for i in found.get("Items", []) if i.get("Path") in wanted}
