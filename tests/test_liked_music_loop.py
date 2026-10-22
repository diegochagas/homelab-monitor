"""Tests for the liked-music loop's decision inbox and heartbeat snapshot. ssh is a fake."""

import subprocess
import tempfile
import unittest

from homelab_monitor.liked_music import LikedMusicSettings
from homelab_monitor.liked_music_loop import LikedMusicLoop
from homelab_monitor.liked_music_servers import UnlikeSettings
from homelab_monitor.liked_music_state import INDEX_NAME, REMOVAL_PENDING_NAME, pending_removals, save_state


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


if __name__ == "__main__":
    unittest.main()
