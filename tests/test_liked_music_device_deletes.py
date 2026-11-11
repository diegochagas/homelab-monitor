"""Tests for songs deleted on another device. Syncthing's report, YouTube Music, Jellyfin and ssh are fakes."""

import os
import tempfile
import unittest
from typing import Callable, List, Optional

from homelab_monitor.liked_music_device_deletes import DeviceDeleteDeps, apply_device_deletions, songs_among
from homelab_monitor.liked_music_reconcile import ReconcileDeps
from homelab_monitor.liked_music_sources import PlaylistSource, mirror_playlist_sources
from homelab_monitor.liked_music_state import (ARCHIVE_NAME, DEVICE_DELETED_NAME, DISLIKE_PENDING_NAME, INDEX_NAME,
                                               PENDING_NAME, REMOVAL_PENDING_NAME, load_state, save_state)


class FakeDeviceReport:
    """Stands in for Syncthing's list of files other devices deleted."""

    def __init__(self, names=(), fail: Optional[Exception] = None):
        self.names = list(names)
        self.fail = fail

    def __call__(self) -> List[str]:
        if self.fail:
            raise self.fail
        return self.names


class FakeYouTubeMusic:
    """Records dislikes; `signed_out` fails opening the session, `refuse` fails on that id."""

    def __init__(self, signed_out: bool = False, refuse: str = ""):
        self.signed_out = signed_out
        self.refuse = refuse
        self.disliked: List[str] = []
        self.sessions = 0

    def open_rating(self) -> Callable[[str], None]:
        self.sessions += 1
        if self.signed_out:
            raise RuntimeError("no YouTube sign-in cookie")
        return self._dislike

    def _dislike(self, video_id: str) -> None:
        if video_id == self.refuse:
            raise RuntimeError(f"disliking {video_id!r} failed: HTTP 500")
        self.disliked.append(video_id)


class FakeMusicServer:
    """Jellyfin playlist removal and the remote `rm` in one: records both, in order."""

    def __init__(self, fail: Optional[Exception] = None):
        self.fail = fail
        self.steps: List[tuple] = []

    def remove_files(self, filenames: List[str]) -> int:
        self.steps.append(("playlists", list(filenames)))
        return len(filenames)

    def delete_files(self, filenames: List[str]) -> None:
        if self.fail:
            raise self.fail
        self.steps.append(("files", list(filenames)))


class DeviceDeletesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name
        save_state(self.folder, INDEX_NAME, {"keep": "Keep.opus", "gone": "Gone.opus", "twin": "Gone.opus",
                                             "other": "Other.m4a"})
        save_state(self.folder, PENDING_NAME, {"jellyfin": ["gone", "keep"]})
        with open(os.path.join(self.folder, ARCHIVE_NAME), "w", encoding="utf-8") as archive:
            archive.write("youtube keep\nyoutube gone\nyoutube twin\nyoutube other\n")
        self.server, self.music = FakeMusicServer(), FakeYouTubeMusic()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def deps(self, report: FakeDeviceReport, max_removals: int = 25, rate: bool = True) -> DeviceDeleteDeps:
        library = ReconcileDeps(folder=self.folder, max_removals=max_removals, list_ids=list,
                                delete_files=self.server.delete_files, jellyfin=self.server)
        return DeviceDeleteDeps(library, report, self.music.open_rating if rate else None)

    def archive_ids(self) -> List[str]:
        with open(os.path.join(self.folder, ARCHIVE_NAME), encoding="utf-8") as archive:
            return [line.split()[-1] for line in archive]

    def test_deleted_song_is_removed_everywhere_then_disliked(self) -> None:
        outcome = apply_device_deletions(self.deps(FakeDeviceReport(["Gone.opus"])))
        self.assertEqual(outcome.deleted, ["Gone.opus"])
        self.assertEqual(self.server.steps, [("playlists", ["Gone.opus"]), ("files", ["Gone.opus", "Gone.lrc"])])
        self.assertEqual(self.music.disliked, ["gone", "twin"])  # both ids that share the file
        self.assertEqual((outcome.disliked, outcome.errors), (2, []))

    def test_deleted_song_is_forgotten_so_a_later_like_downloads_it_again(self) -> None:
        apply_device_deletions(self.deps(FakeDeviceReport(["Gone.opus"])))
        self.assertEqual(load_state(self.folder, INDEX_NAME, {}), {"keep": "Keep.opus", "other": "Other.m4a"})
        self.assertEqual(load_state(self.folder, PENDING_NAME, {}), {"jellyfin": ["keep"]})
        self.assertEqual(self.archive_ids(), ["keep", "other"])
        self.assertEqual(load_state(self.folder, DISLIKE_PENDING_NAME, None), [])

    def test_files_the_sync_did_not_index_are_ignored(self) -> None:
        report = FakeDeviceReport(["Keep.lrc", "YouTube Liked.m3u8", "holiday.jpg"])
        outcome = apply_device_deletions(self.deps(report))
        self.assertEqual((outcome.deleted, self.server.steps, self.music.sessions), ([], [], 0))

    def test_too_many_deletes_at_once_remove_nothing(self) -> None:
        outcome = apply_device_deletions(self.deps(FakeDeviceReport(["Gone.opus", "Keep.opus"]), max_removals=1))
        self.assertIn("2 songs were deleted on a device at once (limit LIKED_MUSIC_MAX_REMOVALS=1)", outcome.errors[0])
        self.assertEqual((self.server.steps, self.music.disliked), ([], []))
        self.assertEqual(len(load_state(self.folder, INDEX_NAME, {})), 4)

    def test_unreachable_syncthing_is_reported_and_removes_nothing(self) -> None:
        report = FakeDeviceReport(fail=RuntimeError("GET http://server/rest/db/need -> unreachable"))
        outcome = apply_device_deletions(self.deps(report))
        self.assertIn("device deletes not checked", outcome.errors[0])
        self.assertEqual(self.server.steps, [])

    def test_failed_server_delete_keeps_the_song_for_the_next_run(self) -> None:
        self.server.fail = RuntimeError("ssh exit 255")
        outcome = apply_device_deletions(self.deps(FakeDeviceReport(["Gone.opus"])))
        self.assertIn("removing 1 song(s) failed: ssh exit 255", outcome.errors[0])
        self.assertEqual(outcome.deleted, [])
        self.assertIn("gone", load_state(self.folder, INDEX_NAME, {}))
        self.assertEqual(self.music.disliked, [])

    def test_failed_dislike_waits_and_keeps_the_archive_line(self) -> None:
        self.music.signed_out = True
        outcome = apply_device_deletions(self.deps(FakeDeviceReport(["Gone.opus"])))
        self.assertEqual(outcome.deleted, ["Gone.opus"])  # the files are gone either way
        self.assertIn("2 deleted song(s) not disliked yet", outcome.errors[0])
        self.assertEqual(load_state(self.folder, DISLIKE_PENDING_NAME, []), ["gone", "twin"])
        self.assertIn("gone", self.archive_ids())  # still liked: must not be downloaded again

    def test_waiting_dislike_is_retried_on_the_next_run(self) -> None:
        self.music.refuse = "twin"
        apply_device_deletions(self.deps(FakeDeviceReport(["Gone.opus"])))
        self.assertEqual(load_state(self.folder, DISLIKE_PENDING_NAME, []), ["twin"])
        self.music.refuse = ""
        outcome = apply_device_deletions(self.deps(FakeDeviceReport()))
        self.assertEqual((outcome.disliked, self.music.disliked), (1, ["gone", "twin"]))
        self.assertEqual(self.archive_ids(), ["keep", "other"])

    def test_without_rating_the_song_is_removed_but_never_disliked(self) -> None:
        outcome = apply_device_deletions(self.deps(FakeDeviceReport(["Gone.opus"]), rate=False))
        self.assertEqual((outcome.deleted, self.music.sessions), (["Gone.opus"], 0))
        self.assertEqual(load_state(self.folder, DISLIKE_PENDING_NAME, []), [])
        self.assertIn("gone", self.archive_ids())  # it stays liked, so it must stay "already downloaded"

    def test_song_waiting_as_unliked_leaves_that_queue(self) -> None:
        save_state(self.folder, REMOVAL_PENDING_NAME, {"gone": "Gone.opus", "other": "Other.m4a"})
        apply_device_deletions(self.deps(FakeDeviceReport(["Gone.opus"])))
        self.assertEqual(load_state(self.folder, REMOVAL_PENDING_NAME, {}), {"other": "Other.m4a"})

    def test_mirrored_playlist_does_not_download_a_deleted_song_again(self) -> None:
        apply_device_deletions(self.deps(FakeDeviceReport(["Gone.opus"])))
        self.assertEqual(load_state(self.folder, DEVICE_DELETED_NAME, []), ["gone", "twin"])
        asked: List[List[str]] = []
        mirror_playlist_sources(self.folder, [PlaylistSource("Christians", "https://music.youtube.com/playlist?list=PLx")],
                                lambda url: ["gone", "keep", "fresh"], lambda ids: asked.append(ids) or {})
        self.assertEqual(asked, [["fresh"]])


class SongsAmongTest(unittest.TestCase):
    def test_maps_deleted_names_to_every_id_of_the_file(self) -> None:
        index = {"a": "A.opus", "b": "A.opus", "c": "C.opus"}
        self.assertEqual(songs_among(["A.opus", "notes.txt"], index), {"A.opus": ["a", "b"]})


if __name__ == "__main__":
    unittest.main()
