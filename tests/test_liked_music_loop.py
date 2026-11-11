"""Tests for the liked-music loop's decision inbox and heartbeat snapshot. ssh is a fake."""

import subprocess
import tempfile
import unittest
from unittest import mock

from homelab_monitor import liked_music_servers
from homelab_monitor.liked_music import LikedMusicSettings, LikedSyncResult
from homelab_monitor.liked_music_loop import MAX_QUICK_RETRIES, RETRY_MINUTES, LikedMusicLoop
from homelab_monitor.liked_music_servers import UnlikeSettings
from homelab_monitor.liked_music_state import (INDEX_NAME, REMOVAL_PENDING_NAME, load_state, pending_removals,
                                               save_state)
from homelab_monitor.syncthing_folder import SyncthingFolderSettings


class FakeSsh:
    """subprocess.run stand-in for the remote `rm`; records each command."""

    def __init__(self):
        self.commands = []

    def __call__(self, command, **kwargs) -> subprocess.CompletedProcess:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")


def make_loop(folder: str, remove_unliked: bool = True) -> LikedMusicLoop:
    settings = LikedMusicSettings(folder, "https://music.youtube.com/playlist?list=LM", "firefox", "yt-dlp",
                                  6, 5, "", push_target="u@h:/music/",
                                  unlike=UnlikeSettings(remove_unliked=remove_unliked))
    return LikedMusicLoop(settings, push_runner=FakeSsh())


class DecisionInboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name
        save_state(self.folder, INDEX_NAME, {"gone": "A - Gone.opus", "kept": "B - Kept.opus"})
        save_state(self.folder, REMOVAL_PENDING_NAME, {"gone": "A - Gone.opus", "kept": "B - Kept.opus"})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_confirmed_delete_runs_ssh_and_keep_does_not(self) -> None:
        loop = make_loop(self.folder)
        loop.submit_decisions({"delete": ["gone", "never-queued"], "keep": ["kept"]})
        result = loop.apply_submitted()
        self.assertEqual(result.unliked, ["A - Gone.opus"])
        self.assertEqual(len(loop.push_runner.commands), 1)
        self.assertIn("/music/A - Gone.opus", loop.push_runner.commands[0][-1])
        self.assertEqual(pending_removals(self.folder), {})

    def test_decision_for_songs_not_waiting_is_ignored(self) -> None:
        loop = make_loop(self.folder)
        loop.submit_decisions({"delete": ["never-queued"]})
        self.assertIsNone(loop.apply_submitted())

    def test_decision_is_refused_while_the_switch_is_off(self) -> None:
        loop = make_loop(self.folder, remove_unliked=False)
        loop.submit_decisions({"delete": ["gone"]})
        self.assertIn("LIKED_MUSIC_REMOVE_UNLIKED", loop.apply_submitted().error)
        self.assertEqual(loop.push_runner.commands, [])

    def test_snapshot_lists_waiting_songs_by_title(self) -> None:
        snapshot = make_loop(self.folder).snapshot()
        self.assertEqual(snapshot["pending_count"], 2)
        self.assertEqual(snapshot["pending_removals"][0], {"id": "gone", "title": "A - Gone"})


class RetryDelayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = make_loop("/unused")

    def test_failed_run_is_retried_soon(self) -> None:
        self.assertEqual(self.loop.next_delay(LikedSyncResult(0, error="Network is unreachable")), RETRY_MINUTES * 60)

    def test_song_waiting_for_a_server_scan_is_retried_soon(self) -> None:
        self.assertEqual(self.loop.next_delay(LikedSyncResult(0, playlist_waiting=1)), RETRY_MINUTES * 60)

    def test_quick_retries_stop_after_the_limit(self) -> None:
        delays = [self.loop.next_delay(LikedSyncResult(0, error="cookies expired")) for _ in range(MAX_QUICK_RETRIES + 1)]
        self.assertEqual(delays[-1], 6 * 3600)

    def test_busy_lyrics_service_does_not_trigger_quick_retries(self) -> None:
        self.assertEqual(self.loop.next_delay(LikedSyncResult(0, lyrics_error="HTTP 503: busy")), 6 * 3600)

    def test_clean_run_waits_the_full_interval(self) -> None:
        self.loop.next_delay(LikedSyncResult(0, error="offline"))
        self.assertEqual(self.loop.next_delay(LikedSyncResult(0)), 6 * 3600)


class FakeSyncthingReport:
    """Stands in for Syncthing's need list: these files were deleted on another device."""

    def __init__(self, names):
        self.names = names

    def __call__(self, settings) -> list:
        return list(self.names)


class DeviceCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name
        save_state(self.folder, INDEX_NAME, {"gone": "A - Gone.opus", "kept": "B - Kept.opus"})
        self.report = mock.patch.object(liked_music_servers, "remotely_deleted_files",
                                        FakeSyncthingReport(["A - Gone.opus"]))
        self.report.start()

    def tearDown(self) -> None:
        self.report.stop()
        self.tmp.cleanup()

    def loop_with_syncthing(self) -> LikedMusicLoop:
        loop = make_loop(self.folder)
        loop.settings.unlike = UnlikeSettings(syncthing=SyncthingFolderSettings("http://server:8384", "fake-key", "music"),
                                              syncthing_check_minutes=10, dislike_deleted=False)
        return loop

    def test_check_between_syncs_deletes_the_song_on_the_server(self) -> None:
        loop = self.loop_with_syncthing()
        result = loop.check_devices()
        self.assertEqual(result.device_deleted, ["A - Gone.opus"])
        self.assertIn("/music/A - Gone.opus", loop.push_runner.commands[0][-1])
        self.assertEqual(load_state(self.folder, INDEX_NAME, {}), {"kept": "B - Kept.opus"})

    def test_checks_run_every_few_minutes_only_with_syncthing_configured(self) -> None:
        self.assertEqual(self.loop_with_syncthing().device_check_seconds(), 600)
        self.assertEqual(make_loop(self.folder).device_check_seconds(), float("inf"))


if __name__ == "__main__":
    unittest.main()
