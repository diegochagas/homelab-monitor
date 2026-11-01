"""Tests for the Jellyfin playlist client against an in-memory fake API."""

import unittest
import urllib.parse
from typing import Dict, List

from homelab_monitor.jellyfin_playlists import JellyfinPlaylists, JellyfinSettings

JELLYFIN_DIR = "/Media/Music/Liked"


class FakeJellyfinApi:
    """Answers the handful of Jellyfin endpoints the client uses. Playlists hold
    [item_id, ...]; entry ids are "e<item>" like Jellyfin's PlaylistItemId."""

    def __init__(self, songs: Dict[str, str], playlists: Dict[str, List[str]]):
        self.songs = songs  # item id -> filename
        self.playlists = playlists  # name -> [item ids]
        self.headers: List[dict] = []
        self.moves = 0

    def __call__(self, method, url, headers, body=None):
        self.headers.append(headers)
        parsed = urllib.parse.urlparse(url)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        parts = parsed.path.strip("/").split("/")
        if parts == ["Users"]:
            return [{"Name": "diego", "Id": "u1"}]
        if parts[-1] == "Items" and parts[0] == "Users":
            return self._library(query["IncludeItemTypes"])
        return self._playlist(method, parts, query)

    def _library(self, kind: str) -> dict:
        if kind == "Playlist":
            return {"Items": [{"Name": name, "Id": name} for name in self.playlists]}
        return {"Items": [{"Id": i, "Path": f"{JELLYFIN_DIR}/{f}"} for i, f in self.songs.items()]}

    def _playlist(self, method: str, parts: List[str], query: dict):
        items = self.playlists[parts[1]]
        if method == "GET":
            return {"Items": [{"Id": i, "PlaylistItemId": "e" + i, "Path": f"{JELLYFIN_DIR}/{self.songs[i]}"}
                              for i in items]}
        if method == "DELETE":
            drop = set(query["entryIds"].split(","))
            items[:] = [i for i in items if "e" + i not in drop]
        elif parts[-2] == "Move":  # real Jellyfin answers API-key moves with HTTP 400
            self.moves += 1
        else:
            items.extend(query["ids"].split(","))
        return None


def jellyfin(api: FakeJellyfinApi) -> JellyfinPlaylists:
    return JellyfinPlaylists(JellyfinSettings("http://jf", "secret-key", "diego", JELLYFIN_DIR, "Liked"), send=api)


class JellyfinPlaylistsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeJellyfinApi({"1": "A.opus", "2": "B.opus", "3": "C.m4a"},
                                   {"Liked": ["1", "2"], "Christians": ["2"]})

    def test_removes_the_song_from_every_playlist(self) -> None:
        self.assertEqual(jellyfin(self.api).remove_files(["B.opus"]), 2)
        self.assertEqual(self.api.playlists, {"Liked": ["1"], "Christians": []})

    def test_new_likes_are_appended_once(self) -> None:
        self.api.playlists["Liked"] = ["1", "2"]
        ready = jellyfin(self.api).add(["C.m4a", "B.opus", "Unindexed.opus"])
        self.assertEqual(ready, ["C.m4a", "B.opus"])
        self.assertEqual(self.api.playlists["Liked"], ["1", "2", "3"])
        self.assertEqual(self.api.moves, 0)

    def test_api_key_travels_in_the_header_only(self) -> None:
        jellyfin(self.api).playlists()
        self.assertEqual(self.api.headers[0], {"Authorization": 'MediaBrowser Token="secret-key"'})

    def test_audio_metadata_maps_library_files(self) -> None:
        songs = jellyfin(self.api).audio_metadata()
        self.assertEqual(sorted(songs), ["A.opus", "B.opus", "C.m4a"])
        self.assertEqual(songs["A.opus"]["id"], "1")

    def test_unknown_user_names_the_setting(self) -> None:
        client = JellyfinPlaylists(JellyfinSettings("http://jf", "k", "nobody", JELLYFIN_DIR, "Liked"), send=self.api)
        with self.assertRaisesRegex(LookupError, "LIKED_MUSIC_JELLYFIN_USER"):
            client.playlists()


if __name__ == "__main__":
    unittest.main()
