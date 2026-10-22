"""Tests for the liked-music sync. yt-dlp is replaced by FakeYtDlp; no network."""

import fcntl
import os
import subprocess
import tempfile
import unittest

from homelab_monitor import liked_music
from homelab_monitor.liked_music_loop import LikedMusicLoop
from homelab_monitor.liked_music_state import INDEX_NAME, PENDING_NAME, load_state
from homelab_monitor.liked_music_ytdlp_output import printed_downloads
from homelab_monitor.liked_music import (
    ARCHIVE_NAME,
    LOCK_NAME,
    LikedMusicSettings,
    build_ytdlp_command,
    run_liked_sync,
)


class FakeYtDlp:
    """Stands in for subprocess.run: appends new ids to the archive like yt-dlp."""

    def __init__(self, folder: str, new_songs=(), returncode: int = 101, stderr: str = "", raises=None):
        self.folder = folder
        self.new_songs = list(new_songs)
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls = []

    def __call__(self, command, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append((command, kwargs))
        if self.raises is not None:
            raise self.raises
        with open(os.path.join(self.folder, ARCHIVE_NAME), "a", encoding="utf-8") as archive:
            for video_id, _title in self.new_songs:
                archive.write(f"youtube {video_id}\n")
        stdout = "".join(f"{vid}\t{self.folder}/{title}.opus\n" for vid, title in self.new_songs)
        return subprocess.CompletedProcess(command, self.returncode, stdout, self.stderr)


def make_settings(folder: str) -> LikedMusicSettings:
    return LikedMusicSettings(
        folder=folder, playlist_url="https://music.youtube.com/playlist?list=LM", browser="firefox",
        ytdlp_bin="yt-dlp", interval_hours=6, timeout_minutes=5, extra_path="/opt/deno/bin",
    )


class LikedMusicSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name
        with open(os.path.join(self.folder, ARCHIVE_NAME), "w", encoding="utf-8") as archive:
            archive.write("youtube oldSong0001\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reports_new_titles(self) -> None:
        fake = FakeYtDlp(self.folder, [("newSong0001", "Song A"), ("newSong0002", "Song B")])
        result = run_liked_sync(make_settings(self.folder), fake)
        self.assertIsNone(result.error)
        self.assertEqual(result.new_titles, ["Song A", "Song B"])

    def test_downloads_are_indexed_and_queued_for_playlists(self) -> None:
        fake = FakeYtDlp(self.folder, [("newSong0001", "Song A")])
        run_liked_sync(make_settings(self.folder), fake)
        self.assertEqual(load_state(self.folder, INDEX_NAME, {}), {"newSong0001": "Song A.opus"})
        self.assertEqual(load_state(self.folder, PENDING_NAME, {})["jellyfin"], ["newSong0001"])

    def test_printed_lines_of_skipped_videos_are_ignored(self) -> None:
        stdout = "abc\t/m/A - B.opus\nzzz\t/m/Other.opus\nnoise\n"
        self.assertEqual(printed_downloads(stdout, {"abc"}), {"abc": "A - B.opus"})

    def test_half_configured_server_keeps_downloading_and_reports(self) -> None:
        settings = make_settings(self.folder)
        settings.unlike_error = "LIKED_MUSIC_SWING_URL is set but LIKED_MUSIC_SWING_PASSWORD is empty"
        result = run_liked_sync(settings, FakeYtDlp(self.folder, [("newSong0001", "Song A")]))
        self.assertEqual(result.new_titles, ["Song A"])
        self.assertIn("playlist/unlike sync skipped: LIKED_MUSIC_SWING_URL", result.error)

    def test_nothing_new_is_not_an_error(self) -> None:
        result = run_liked_sync(make_settings(self.folder), FakeYtDlp(self.folder))
        self.assertIsNone(result.error)
        self.assertEqual(result.new_titles, [])

    def test_unavailable_videos_with_downloads_still_succeed(self) -> None:
        fake = FakeYtDlp(self.folder, [("newSong0001", "Song A")], returncode=1, stderr="ERROR: Video unavailable")
        result = run_liked_sync(make_settings(self.folder), fake)
        self.assertIsNone(result.error)
        self.assertEqual(result.new_titles, ["Song A"])

    def test_error_without_downloads_is_reported(self) -> None:
        fake = FakeYtDlp(self.folder, returncode=1, stderr="ERROR: Sign in to confirm you're not a bot")
        result = run_liked_sync(make_settings(self.folder), fake)
        self.assertIn("Sign in", result.error)

    def test_timeout_is_reported_with_the_limit(self) -> None:
        fake = FakeYtDlp(self.folder, raises=subprocess.TimeoutExpired("yt-dlp", 300))
        result = run_liked_sync(make_settings(self.folder), fake)
        self.assertIn("5 minutes", result.error)

    def test_missing_ytdlp_names_the_setting(self) -> None:
        result = run_liked_sync(make_settings(self.folder), FakeYtDlp(self.folder, raises=FileNotFoundError()))
        self.assertIn("LIKED_MUSIC_YTDLP", result.error)

    def test_missing_folder_never_runs_ytdlp(self) -> None:
        fake = FakeYtDlp(self.folder)
        result = run_liked_sync(make_settings(os.path.join(self.folder, "absent")), fake)
        self.assertIn("not a directory", result.error)
        self.assertEqual(fake.calls, [])

    def test_second_sync_is_refused_while_one_runs(self) -> None:
        with open(os.path.join(self.folder, LOCK_NAME), "w", encoding="utf-8") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fake = FakeYtDlp(self.folder)
            result = run_liked_sync(make_settings(self.folder), fake)
        self.assertIn("already running", result.error)
        self.assertEqual(fake.calls, [])

    def test_extra_path_and_timeout_reach_ytdlp(self) -> None:
        fake = FakeYtDlp(self.folder)
        run_liked_sync(make_settings(self.folder), fake)
        _command, kwargs = fake.calls[0]
        self.assertTrue(kwargs["env"]["PATH"].startswith("/opt/deno/bin" + os.pathsep))
        self.assertEqual(kwargs["timeout"], 300)


class BuildCommandTest(unittest.TestCase):
    def test_stops_at_first_archived_song_and_never_overwrites(self) -> None:
        command = build_ytdlp_command(make_settings("/music"))
        self.assertIn("--break-on-existing", command)
        self.assertIn("/music/" + ARCHIVE_NAME, command)
        self.assertNotIn("--force-overwrites", command)
        self.assertEqual(command[-1], "https://music.youtube.com/playlist?list=LM")


class LoopAndSnapshotTest(unittest.TestCase):
    def test_run_once_keeps_result_for_the_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            fake = FakeYtDlp(folder, [("newSong0001", "Song A")])
            loop = LikedMusicLoop(make_settings(folder), fake)
            loop.run_once()
        snapshot = loop.last_result.to_snapshot()
        self.assertEqual(snapshot["new_count"], 1)
        self.assertEqual(snapshot["new_titles"], ["Song A"])

    def test_snapshot_caps_reported_titles(self) -> None:
        result = liked_music.LikedSyncResult(checked_at=0, new_titles=[f"t{i}" for i in range(25)])
        snapshot = result.to_snapshot()
        self.assertEqual(snapshot["new_count"], 25)
        self.assertEqual(len(snapshot["new_titles"]), liked_music.MAX_REPORTED_TITLES)


if __name__ == "__main__":
    unittest.main()
