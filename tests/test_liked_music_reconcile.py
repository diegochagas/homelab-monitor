"""Tests for the unlike sync and playlist queue. Servers, listing and ssh are fakes."""

import os
import tempfile
import unittest
from typing import List, Optional

from homelab_monitor.liked_music_reconcile import (ReconcileDeps, apply_removal_decisions, reconcile_library,
                                                   unliked_ids)
from homelab_monitor.liked_music_remote import LikedListingError
from homelab_monitor.liked_music_state import (ARCHIVE_NAME, INDEX_NAME, LIKED_SNAPSHOT_NAME, PENDING_NAME,
                                               REMOVAL_PENDING_NAME, load_state, pending_removals,
                                               record_downloads, save_state)


class FakePlaylistServer:
    """Jellyfin/Swing stand-in: records removals; only `indexed` names can be added."""

    def __init__(self, indexed=(), fail: Optional[Exception] = None):
        self.indexed = set(indexed)
        self.fail = fail
        self.removed: List[str] = []
        self.added: List[str] = []
        self.scans = 0

    def remove_files(self, filenames: List[str]) -> int:
        if self.fail:
            raise self.fail
        self.removed.extend(filenames)
        return len(filenames)

    def add_to_top(self, filenames: List[str]) -> List[str]:
        if self.fail:
            raise self.fail
        ready = [name for name in filenames if name in self.indexed]
        self.added.extend(ready)
        return ready

    add = add_to_top

    def request_scan(self) -> None:
        self.scans += 1


class FakeListing:
    def __init__(self, ids=(), fail: bool = False):
        self.ids = list(ids)
        self.fail = fail
        self.calls = 0

    def __call__(self) -> List[str]:
        self.calls += 1
        if self.fail:
            raise LikedListingError("listing Liked Music failed (yt-dlp exit 1): cookies expired")
        return self.ids


class FakeRemoteDelete:
    def __init__(self):
        self.deleted: List[str] = []

    def __call__(self, filenames: List[str]) -> None:
        self.deleted.extend(filenames)


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name
        save_state(self.folder, INDEX_NAME, {"keep": "Keep.opus", "gone": "Gone.opus", "xian": "Christian.m4a"})
        with open(os.path.join(self.folder, ARCHIVE_NAME), "w", encoding="utf-8") as archive:
            archive.write("youtube keep\nyoutube gone\nyoutube xian\n")
        self.jellyfin, self.swing, self.delete = FakePlaylistServer(), FakePlaylistServer(), FakeRemoteDelete()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def deps(self, listing: FakeListing, max_removals: int = 25, **overrides) -> ReconcileDeps:
        fields = dict(folder=self.folder, max_removals=max_removals, list_ids=listing,
                      delete_files=self.delete, jellyfin=self.jellyfin, swing=self.swing)
        fields.update(overrides)
        return ReconcileDeps(**fields)

    def test_first_listing_only_saves_the_list(self) -> None:
        outcome = reconcile_library(self.deps(FakeListing(["keep"])))
        self.assertEqual((outcome.unliked, self.delete.deleted), ([], []))
        self.assertEqual(load_state(self.folder, LIKED_SNAPSHOT_NAME, None), ["keep"])

    def test_unliked_song_is_queued_and_nothing_deleted(self) -> None:
        save_state(self.folder, LIKED_SNAPSHOT_NAME, ["keep", "gone"])
        outcome = reconcile_library(self.deps(FakeListing(["new", "keep"])))
        self.assertEqual((outcome.queued, outcome.unliked), (["Gone.opus"], []))
        self.assertEqual((self.jellyfin.removed, self.swing.removed, self.delete.deleted), ([], [], []))
        self.assertEqual(pending_removals(self.folder), {"gone": "Gone.opus"})
        self.assertIn("gone", load_state(self.folder, INDEX_NAME, {}))
        self.assertEqual(load_state(self.folder, LIKED_SNAPSHOT_NAME, None), ["new", "keep"])

    def test_confirmed_delete_leaves_playlists_then_disk_then_archive(self) -> None:
        save_state(self.folder, LIKED_SNAPSHOT_NAME, ["keep", "gone"])
        reconcile_library(self.deps(FakeListing(["keep"])))
        outcome = apply_removal_decisions(self.deps(FakeListing()), ["gone"], [])
        self.assertEqual(outcome.unliked, ["Gone.opus"])
        self.assertEqual((self.jellyfin.removed, self.swing.removed, self.delete.deleted), (["Gone.opus"],) * 3)
        self.assertNotIn("gone", load_state(self.folder, INDEX_NAME, {}))
        with open(os.path.join(self.folder, ARCHIVE_NAME), encoding="utf-8") as archive:
            self.assertEqual(archive.read(), "youtube keep\nyoutube xian\n")
        self.assertEqual(pending_removals(self.folder), {})

    def test_keep_decision_leaves_the_file_alone(self) -> None:
        save_state(self.folder, REMOVAL_PENDING_NAME, {"gone": "Gone.opus"})
        outcome = apply_removal_decisions(self.deps(FakeListing()), [], ["gone"])
        self.assertEqual((outcome.kept, self.delete.deleted), (["Gone.opus"], []))
        self.assertEqual(pending_removals(self.folder), {})
        self.assertIn("gone", load_state(self.folder, INDEX_NAME, {}))

    def test_repeated_or_unknown_decisions_do_nothing(self) -> None:
        save_state(self.folder, REMOVAL_PENDING_NAME, {"gone": "Gone.opus"})
        apply_removal_decisions(self.deps(FakeListing()), ["gone", "gone"], [])
        apply_removal_decisions(self.deps(FakeListing()), ["gone", "keep"], [])
        self.assertEqual(self.delete.deleted, ["Gone.opus"])

    def test_relike_drops_the_song_from_the_queue(self) -> None:
        save_state(self.folder, LIKED_SNAPSHOT_NAME, ["keep"])
        save_state(self.folder, REMOVAL_PENDING_NAME, {"gone": "Gone.opus"})
        reconcile_library(self.deps(FakeListing(["gone", "keep"])))
        self.assertEqual(pending_removals(self.folder), {})

    def test_songs_never_seen_in_liked_music_are_safe(self) -> None:
        save_state(self.folder, LIKED_SNAPSHOT_NAME, ["keep"])
        reconcile_library(self.deps(FakeListing(["keep"])))
        self.assertIn("xian", load_state(self.folder, INDEX_NAME, {}))
        self.assertEqual(self.delete.deleted, [])

    def test_mass_disappearance_queues_nothing(self) -> None:
        save_state(self.folder, LIKED_SNAPSHOT_NAME, ["keep", "gone"])
        outcome = reconcile_library(self.deps(FakeListing(["other"]), max_removals=1))
        self.assertIn("LIKED_MUSIC_MAX_REMOVALS=1", outcome.errors[0])
        self.assertEqual((self.delete.deleted, self.jellyfin.removed), ([], []))
        self.assertEqual(load_state(self.folder, LIKED_SNAPSHOT_NAME, None), ["keep", "gone"])
        self.assertEqual(pending_removals(self.folder), {})

    def test_failed_listing_deletes_nothing(self) -> None:
        save_state(self.folder, LIKED_SNAPSHOT_NAME, ["keep", "gone"])
        outcome = reconcile_library(self.deps(FakeListing(fail=True)))
        self.assertIn("cookies expired", outcome.errors[0])
        self.assertEqual(self.delete.deleted, [])

    def test_playlist_failure_keeps_file_and_song_queued(self) -> None:
        save_state(self.folder, REMOVAL_PENDING_NAME, {"gone": "Gone.opus"})
        self.swing.fail = RuntimeError("POST /auth/login -> HTTP 401")
        outcome = apply_removal_decisions(self.deps(FakeListing()), ["gone"], [])
        self.assertIn("HTTP 401", outcome.errors[0])
        self.assertEqual(self.delete.deleted, [])
        self.assertIn("gone", load_state(self.folder, INDEX_NAME, {}))
        self.assertEqual(pending_removals(self.folder), {"gone": "Gone.opus"})

    def test_staged_copy_of_a_deleted_song_is_removed(self) -> None:
        save_state(self.folder, REMOVAL_PENDING_NAME, {"gone": "Gone.opus"})
        open(os.path.join(self.folder, "Gone.opus"), "wb").close()
        apply_removal_decisions(self.deps(FakeListing()), ["gone"], [])
        self.assertFalse(os.path.exists(os.path.join(self.folder, "Gone.opus")))

    def test_switch_off_never_lists(self) -> None:
        listing = FakeListing(["keep"])
        reconcile_library(self.deps(listing, remove_unliked=False))
        self.assertEqual(listing.calls, 0)

    def test_unliked_ids_keeps_list_order(self) -> None:
        self.assertEqual(unliked_ids(["a", "b", "c"], ["b"], {"a": "A", "c": "C"}), ["a", "c"])


class PendingPlaylistAddTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name
        record_downloads(self.folder, {"old": "Old.opus"})
        record_downloads(self.folder, {"new": "New.opus"})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_adds(self, jellyfin: FakePlaylistServer, swing: FakePlaylistServer):
        deps = ReconcileDeps(folder=self.folder, max_removals=25, list_ids=FakeListing(),
                             delete_files=FakeRemoteDelete(), remove_unliked=False, jellyfin=jellyfin, swing=swing)
        return reconcile_library(deps)

    def test_newest_download_is_queued_first(self) -> None:
        self.assertEqual(load_state(self.folder, PENDING_NAME, {})["jellyfin"], ["new", "old"])

    def test_songs_not_indexed_yet_stay_queued(self) -> None:
        outcome = self.run_adds(FakePlaylistServer({"New.opus", "Old.opus"}), FakePlaylistServer({"Old.opus"}))
        self.assertEqual(outcome.playlist_added, {"jellyfin": 2, "swing": 1})
        self.assertEqual(load_state(self.folder, PENDING_NAME, {}), {"jellyfin": [], "swing": ["new"]})
        self.assertEqual(outcome.playlist_waiting, 1)

    def test_server_missing_a_song_is_asked_to_rescan(self) -> None:
        jellyfin, swing = FakePlaylistServer({"New.opus", "Old.opus"}), FakePlaylistServer({"Old.opus"})
        self.run_adds(jellyfin, swing)
        self.assertEqual((jellyfin.scans, swing.scans), (0, 1))

    def test_one_server_down_does_not_block_the_other(self) -> None:
        down = FakePlaylistServer(fail=RuntimeError("GET /Users -> unreachable"))
        outcome = self.run_adds(down, FakePlaylistServer({"New.opus", "Old.opus"}))
        self.assertIn("jellyfin playlist add: GET /Users -> unreachable", outcome.errors)
        self.assertEqual(load_state(self.folder, PENDING_NAME, {}), {"jellyfin": ["new", "old"], "swing": []})


if __name__ == "__main__":
    unittest.main()
