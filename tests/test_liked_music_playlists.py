"""End-to-end sync with mirrored playlists and playlist files; yt-dlp is a fake answering by command."""

import os
import subprocess
import tempfile
import unittest
from typing import Dict, List
from unittest import mock

from homelab_monitor.config import Config
from homelab_monitor.liked_music import LikedMusicSettings, run_liked_sync, settings_from_config
from homelab_monitor.liked_music_sources import PlaylistSource
from homelab_monitor.liked_music_state import ARCHIVE_NAME, PLAYLIST_IDS_NAME, load_state

LIKED_URL = "https://music.youtube.com/playlist?list=LM"
CHRISTIANS_URL = "https://music.youtube.com/playlist?list=PLx"


class FakeYtDlpByCommand:
    """Answers the three yt-dlp calls of a run: liked download, flat listings, video downloads."""

    def __init__(self, folder: str, liked_songs: Dict[str, str], listings: Dict[str, List[str]],
                 videos: Dict[str, str], failing_listings=()):
        self.folder = folder
        self.liked_songs = liked_songs
        self.listings = listings
        self.videos = videos
        self.failing_listings = set(failing_listings)
        self.commands: List[List[str]] = []

    def __call__(self, command, **kwargs) -> subprocess.CompletedProcess:
        self.commands.append(command)
        if "--flat-playlist" in command:
            if command[-1] in self.failing_listings:
                return subprocess.CompletedProcess(command, 1, "", "ERROR: cookies expired")
            return subprocess.CompletedProcess(command, 0, "".join(f"{v}\n" for v in self.listings[command[-1]]), "")
        if "--no-playlist" in command:
            wanted = [url.rsplit("=", 1)[1] for url in command if url.startswith("https://music.youtube.com/watch")]
            return self._download({vid: self.videos[vid] for vid in wanted if vid in self.videos}, 1 if len(wanted) > 0 else 0)
        return self._download(self.liked_songs, 101)

    def _download(self, songs: Dict[str, str], returncode: int) -> subprocess.CompletedProcess:
        with open(os.path.join(self.folder, ARCHIVE_NAME), "a", encoding="utf-8") as archive:
            for video_id in songs:
                archive.write(f"youtube {video_id}\n")
        stdout = "".join(f"{vid}\t{self.folder}/{name}\n" for vid, name in songs.items())
        return subprocess.CompletedProcess([], returncode, stdout, "")


def make_settings(folder: str) -> LikedMusicSettings:
    return LikedMusicSettings(folder=folder, playlist_url=LIKED_URL, browser="firefox", ytdlp_bin="yt-dlp",
                              interval_hours=6, timeout_minutes=5, extra_path="", m3u_name="YouTube Liked",
                              sources=[PlaylistSource("Christians", CHRISTIANS_URL)])


def read(folder: str, name: str) -> str:
    with open(os.path.join(folder, name), encoding="utf-8") as handle:
        return handle.read()


class PlaylistSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_mirrors_the_playlist_and_writes_both_files(self) -> None:
        fake = FakeYtDlpByCommand(self.folder, {"newA": "A.opus"},
                                  {LIKED_URL: ["newA"], CHRISTIANS_URL: ["xian", "newA"]}, {"xian": "C.m4a"})
        result = run_liked_sync(make_settings(self.folder), fake)
        self.assertIsNone(result.error)
        self.assertIsNone(result.playlist_error)
        self.assertEqual((result.new_titles, result.mirrored), (["A"], {"Christians": 1}))
        self.assertEqual(result.playlists_written, ["YouTube Liked.m3u8", "Christians.m3u8"])
        self.assertEqual(read(self.folder, "YouTube Liked.m3u8"), "#EXTM3U\nA.opus\n")
        self.assertEqual(read(self.folder, "Christians.m3u8"), "#EXTM3U\nC.m4a\nA.opus\n")
        self.assertEqual(load_state(self.folder, PLAYLIST_IDS_NAME, {}), {"Christians": ["xian", "newA"]})

    def test_mirror_trouble_never_fails_the_sync(self) -> None:
        fake = FakeYtDlpByCommand(self.folder, {}, {LIKED_URL: []}, {}, failing_listings=[CHRISTIANS_URL])
        result = run_liked_sync(make_settings(self.folder), fake)
        self.assertIn("Christians: listing Liked Music failed", result.playlist_error)
        self.assertIn("listing Liked Music failed", result.error)  # the empty liked listing, a separate problem
        self.assertFalse(os.path.exists(os.path.join(self.folder, "Christians.m3u8")))

    def test_bad_config_skips_the_mirror_and_the_files(self) -> None:
        settings = make_settings(self.folder)
        settings.sources_error = "LIKED_MUSIC_PLAYLISTS entry 'Christians': expected Name=https://..."
        fake = FakeYtDlpByCommand(self.folder, {"newA": "A.opus"}, {LIKED_URL: ["newA"]}, {})
        result = run_liked_sync(settings, fake)
        self.assertIsNone(result.error)
        self.assertIn("playlist mirror skipped: LIKED_MUSIC_PLAYLISTS entry", result.playlist_error)
        self.assertEqual(result.playlists_written, [])
        self.assertEqual([c for c in fake.commands if "--no-playlist" in c], [])

    def test_without_playlist_settings_nothing_is_listed_or_written(self) -> None:
        settings = make_settings(self.folder)
        settings.m3u_name, settings.sources = "", []
        fake = FakeYtDlpByCommand(self.folder, {"newA": "A.opus"}, {}, {})
        result = run_liked_sync(settings, fake)
        self.assertIsNone(result.error)
        self.assertEqual(len(fake.commands), 1)
        self.assertFalse(os.path.exists(os.path.join(self.folder, "YouTube Liked.m3u8")))


class PlaylistConfigTest(unittest.TestCase):
    def test_settings_read_the_playlist_keys(self) -> None:
        env = {"LIKED_MUSIC_DIR": "/m", "LIKED_MUSIC_M3U_NAME": "YouTube Liked",
               "LIKED_MUSIC_PLAYLISTS": "Christians=" + CHRISTIANS_URL}
        with mock.patch.dict("os.environ", env, clear=True):
            settings = settings_from_config(Config())
        self.assertEqual((settings.m3u_name, settings.sources_error), ("YouTube Liked", ""))
        self.assertEqual(settings.sources, [PlaylistSource("Christians", CHRISTIANS_URL)])

    def test_bad_m3u_name_is_reported_not_fatal(self) -> None:
        with mock.patch.dict("os.environ", {"LIKED_MUSIC_DIR": "/m", "LIKED_MUSIC_M3U_NAME": "../x"}, clear=True):
            settings = settings_from_config(Config())
        self.assertIn("LIKED_MUSIC_M3U_NAME '../x'", settings.sources_error)


if __name__ == "__main__":
    unittest.main()
