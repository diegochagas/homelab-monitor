"""Tests for uploading staged songs. rsync is replaced by FakeRsync; no network."""

import os
import subprocess
import tempfile
import unittest

from homelab_monitor.liked_music import ARCHIVE_NAME, LikedMusicSettings, run_liked_sync
from homelab_monitor.liked_music_push import PushSettings, build_rsync_command, push_staged_songs


class FakeRsync:
    """Stands in for subprocess.run: removes the sources it 'transferred', like
    --remove-source-files, or fails after transferring only some of them."""

    def __init__(self, transfer_limit=None, returncode: int = 0, stderr: str = "", raises=None):
        self.transfer_limit = transfer_limit
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls = []

    def __call__(self, command, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(command)
        if self.raises is not None:
            raise self.raises
        sources = [arg for arg in command[1:-1] if os.path.isfile(arg)]
        for source in sources[: self.transfer_limit]:
            os.remove(source)
        return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)


def stage(folder: str, *names: str) -> None:
    for name in names:
        with open(os.path.join(folder, name), "w", encoding="utf-8") as song:
            song.write("audio")


class PushStagedSongsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name
        stage(self.folder, ARCHIVE_NAME)
        self.settings = PushSettings(self.folder, "user@server:/music/", "", "D775,F664")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_moves_songs_and_keeps_the_archive(self) -> None:
        stage(self.folder, "A - One.opus", "B - Two.m4a")
        outcome = push_staged_songs(self.settings, FakeRsync())
        self.assertEqual((outcome.pushed, outcome.error), (2, None))
        self.assertEqual(os.listdir(self.folder), [ARCHIVE_NAME])

    def test_nothing_staged_never_runs_rsync(self) -> None:
        fake = FakeRsync()
        outcome = push_staged_songs(self.settings, fake)
        self.assertEqual(outcome.pushed, 0)
        self.assertEqual(fake.calls, [])

    def test_failed_upload_keeps_untransferred_songs(self) -> None:
        stage(self.folder, "A - One.opus", "B - Two.opus")
        fake = FakeRsync(transfer_limit=1, returncode=23, stderr="rsync: connection unexpectedly closed")
        outcome = push_staged_songs(self.settings, fake)
        self.assertEqual(outcome.pushed, 1)
        self.assertIn("1 file(s) kept", outcome.error)
        self.assertTrue(os.path.exists(os.path.join(self.folder, "B - Two.opus")))

    def test_timeout_keeps_songs_and_says_so(self) -> None:
        stage(self.folder, "A - One.opus")
        outcome = push_staged_songs(self.settings, FakeRsync(raises=subprocess.TimeoutExpired("rsync", 1)))
        self.assertEqual(outcome.pushed, 0)
        self.assertIn("did not finish", outcome.error)

    def test_command_only_deletes_after_transfer_and_never_prompts(self) -> None:
        command = build_rsync_command(PushSettings("/staging", "u@h:/m/", "sudo /usr/bin/rsync", ""), ["a.opus"])
        self.assertIn("--remove-source-files", command)
        self.assertNotIn("--delete", command)
        self.assertIn("ssh -o BatchMode=yes -o ConnectTimeout=15", command)
        self.assertEqual(command[-2:], ["/staging/a.opus", "u@h:/m/"])
        self.assertEqual(command[command.index("--rsync-path") + 1], "sudo /usr/bin/rsync")


class FakeYtDlpOneSong:
    def __call__(self, command, **kwargs) -> subprocess.CompletedProcess:
        folder = os.path.dirname(command[command.index("--download-archive") + 1])
        stage(folder, "New - Song.opus")
        with open(os.path.join(folder, ARCHIVE_NAME), "a", encoding="utf-8") as archive:
            archive.write("youtube newSong0001\n")
        return subprocess.CompletedProcess(command, 101, "New Song\n", "")


class SyncThenPushTest(unittest.TestCase):
    def test_sync_uploads_the_new_song(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = LikedMusicSettings(folder, "https://music.youtube.com/playlist?list=LM", "firefox",
                                          "yt-dlp", 6, 5, "", push_target="u@h:/music/")
            result = run_liked_sync(settings, FakeYtDlpOneSong(), FakeRsync())
            self.assertEqual((result.new_titles, result.pushed, result.error), (["New Song"], 1, None))
            self.assertEqual(sorted(os.listdir(folder)), sorted([ARCHIVE_NAME, ".liked-music.lock"]))

    def test_no_target_keeps_songs_local(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = LikedMusicSettings(folder, "https://music.youtube.com/playlist?list=LM", "firefox",
                                          "yt-dlp", 6, 5, "")
            fake_rsync = FakeRsync()
            result = run_liked_sync(settings, FakeYtDlpOneSong(), fake_rsync)
            self.assertEqual(result.pushed, 0)
            self.assertEqual(fake_rsync.calls, [])
            self.assertIn("New - Song.opus", os.listdir(folder))


if __name__ == "__main__":
    unittest.main()
