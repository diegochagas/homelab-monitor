"""Tests for mirroring other YouTube Music playlists. Listing and downloads are fakes."""

import tempfile
import unittest
from typing import Dict, List

from homelab_monitor.liked_music_remote import LikedListingError
from homelab_monitor.liked_music_sources import (PlaylistSource, mirror_playlist_sources,
                                                 parse_playlist_sources)
from homelab_monitor.liked_music_state import INDEX_NAME, PENDING_NAME, PLAYLIST_IDS_NAME, load_state, save_state

CHRISTIANS = PlaylistSource("Christians", "https://music.youtube.com/playlist?list=PLx")


class FakeLister:
    """url -> ids; a url in `failing` raises like an expired-cookie listing."""

    def __init__(self, lists: Dict[str, List[str]], failing=()):
        self.lists = lists
        self.failing = set(failing)

    def __call__(self, url: str) -> List[str]:
        if url in self.failing:
            raise LikedListingError("listing failed (yt-dlp exit 1): cookies expired")
        return self.lists[url]


class FakeDownloader:
    """ids -> {id: filename} for the ids in `available`; records what was asked."""

    def __init__(self, available: Dict[str, str], raises=None):
        self.available = available
        self.raises = raises
        self.asked: List[List[str]] = []

    def __call__(self, video_ids: List[str]) -> Dict[str, str]:
        self.asked.append(list(video_ids))
        if self.raises:
            raise self.raises
        return {vid: self.available[vid] for vid in video_ids if vid in self.available}


class ParseSourcesTest(unittest.TestCase):
    def test_parses_name_url_pairs(self) -> None:
        sources = parse_playlist_sources(" Christians=https://m/pl?list=A ; Worship=https://m/pl?list=B ")
        self.assertEqual([(s.name, s.url) for s in sources],
                         [("Christians", "https://m/pl?list=A"), ("Worship", "https://m/pl?list=B")])

    def test_empty_spec_means_no_sources(self) -> None:
        self.assertEqual(parse_playlist_sources(""), [])

    def test_bad_entries_are_named(self) -> None:
        for spec in ("Christians", "../x=https://m", ".hidden=https://m", "Name=ftp://m"):
            with self.assertRaisesRegex(ValueError, "LIKED_MUSIC_PLAYLISTS entry"):
                parse_playlist_sources(spec)


class MirrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name
        save_state(self.folder, INDEX_NAME, {"liked": "Liked.opus"})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_downloads_only_the_missing_songs_without_queueing_them(self) -> None:
        downloader = FakeDownloader({"xian": "Christian.m4a"})
        lister = FakeLister({CHRISTIANS.url: ["liked", "xian", "xian"]})
        outcome = mirror_playlist_sources(self.folder, [CHRISTIANS], lister, downloader)
        self.assertEqual(downloader.asked, [["xian"]])
        self.assertEqual(outcome.downloaded, {"Christians": 1})
        self.assertEqual(outcome.errors, [])
        self.assertEqual(load_state(self.folder, INDEX_NAME, {})["xian"], "Christian.m4a")
        self.assertEqual(load_state(self.folder, PENDING_NAME, {}), {})
        self.assertEqual(load_state(self.folder, PLAYLIST_IDS_NAME, {}), {"Christians": ["liked", "xian"]})

    def test_failed_listing_keeps_the_last_list(self) -> None:
        save_state(self.folder, PLAYLIST_IDS_NAME, {"Christians": ["liked"]})
        outcome = mirror_playlist_sources(self.folder, [CHRISTIANS], FakeLister({}, failing=[CHRISTIANS.url]),
                                          FakeDownloader({}))
        self.assertIn("Christians: listing failed", outcome.errors[0])
        self.assertEqual(load_state(self.folder, PLAYLIST_IDS_NAME, {}), {"Christians": ["liked"]})

    def test_unavailable_songs_are_counted_in_the_error(self) -> None:
        lister = FakeLister({CHRISTIANS.url: ["xian", "deleted"]})
        outcome = mirror_playlist_sources(self.folder, [CHRISTIANS], lister, FakeDownloader({"xian": "C.m4a"}))
        self.assertEqual(outcome.downloaded, {"Christians": 1})
        self.assertIn("Christians: 1 of 2 missing song(s) could not be downloaded", outcome.errors)

    def test_downloader_failure_is_reported_and_the_list_still_saved(self) -> None:
        lister = FakeLister({CHRISTIANS.url: ["xian"]})
        downloader = FakeDownloader({}, raises=RuntimeError("yt-dlp did not finish within 5 minutes"))
        outcome = mirror_playlist_sources(self.folder, [CHRISTIANS], lister, downloader)
        self.assertEqual(outcome.errors, ["Christians: yt-dlp did not finish within 5 minutes"])
        self.assertEqual(load_state(self.folder, PLAYLIST_IDS_NAME, {}), {"Christians": ["xian"]})

    def test_playlists_dropped_from_the_config_leave_the_state(self) -> None:
        save_state(self.folder, PLAYLIST_IDS_NAME, {"Old": ["liked"], "Christians": ["liked"]})
        mirror_playlist_sources(self.folder, [CHRISTIANS], FakeLister({CHRISTIANS.url: ["liked"]}), FakeDownloader({}))
        self.assertEqual(load_state(self.folder, PLAYLIST_IDS_NAME, {}), {"Christians": ["liked"]})


if __name__ == "__main__":
    unittest.main()
