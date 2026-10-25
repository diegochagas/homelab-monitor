"""Tests for the Jellyfin/Swing playlist clients against in-memory fake APIs."""

import unittest
import urllib.parse
from typing import Dict, List

from homelab_monitor.jellyfin_playlists import JellyfinPlaylists, JellyfinSettings
from homelab_monitor.swing_playlists import SwingPlaylists, SwingSettings

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


class FakeSwingApi:
    """Swing 2.1 behaviour that matters: remove-tracks handles items one by one
    and skips an item whose hash is no longer at the given index."""

    def __init__(self, files: Dict[str, str], playlists: Dict[int, List[str]]):
        self.files = files  # trackhash -> filepath
        self.playlists = playlists
        self.logins = 0
        self.scans = 0

    def __call__(self, method, url, headers, body=None, opener=None):
        path = urllib.parse.urlparse(url).path.strip("/").split("/")
        if path == ["auth", "login"]:
            self.logins += 1
            return {"msg": "Logged in"}
        if path == ["notsettings", "trigger-scan"]:
            self.scans += 1
            return {"msg": "Scan triggered!"}
        if path == ["folder"]:
            return {"tracks": [{"trackhash": h, "filepath": p} for h, p in self.files.items()]}
        if path == ["playlists"]:
            return {"data": [{"name": f"P{pid}", "id": pid} for pid in self.playlists]}
        return self._playlist(int(path[1]), path[2:], body)

    def _playlist(self, pid: int, action: List[str], body):
        hashes = self.playlists[pid]
        if not action:
            return {"info": {}, "tracks": [{"trackhash": h, "filepath": self.files[h]} for h in hashes]}
        if action == ["remove-tracks"]:
            for item in body["tracks"]:
                if item["index"] < len(hashes) and hashes[item["index"]] == item["trackhash"]:
                    del hashes[item["index"]]
        else:
            hashes.extend(body["itemhash"].split(","))
        return {"msg": "Done"}


def swing(api: FakeSwingApi) -> SwingPlaylists:
    return SwingPlaylists(SwingSettings("http://sw", "admin", "pw", "/music/Liked", "P1"), send=api, opener=object())


class SwingPlaylistsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeSwingApi({"ha": "/music/Liked/A.opus", "hb": "/music/Liked/B.opus", "hc": "/music/Liked/C.m4a"},
                                {1: ["ha", "hb", "hc"], 2: ["hb"]})

    def test_removes_adjacent_songs_without_index_drift(self) -> None:
        self.assertEqual(swing(self.api).remove_files(["A.opus", "B.opus"]), 3)
        self.assertEqual(self.api.playlists, {1: ["hc"], 2: []})

    def test_adds_only_indexed_songs_and_logs_in_once(self) -> None:
        self.api.playlists[1] = []
        client = swing(self.api)
        self.assertEqual(client.add(["C.m4a", "New.opus"]), ["C.m4a"])
        client.playlists()
        self.assertEqual((self.api.playlists[1], self.api.logins), (["hc"], 1))

    def test_request_scan_uses_the_get_endpoint(self) -> None:
        swing(self.api).request_scan()
        self.assertEqual(self.api.scans, 1)

    def test_missing_playlist_names_it(self) -> None:
        client = SwingPlaylists(SwingSettings("http://sw", "a", "p", "/music/Liked", "Nope"), send=self.api, opener=object())
        with self.assertRaisesRegex(LookupError, "'Nope'"):
            client.add(["A.opus"])


if __name__ == "__main__":
    unittest.main()
