"""Tests for the .m3u8 playlist files. The writer is a fake recording (filename, content)."""

import os
import tempfile
import unittest
from typing import List, Tuple

from homelab_monitor.liked_music_m3u import (EXTRA_SUFFIX, local_writer, playlist_members, protected_ids,
                                             render_m3u, write_playlists)
from homelab_monitor.liked_music_state import INDEX_NAME, LIKED_SNAPSHOT_NAME, PLAYLIST_IDS_NAME, save_state


class FakeWriter:
    def __init__(self):
        self.written: List[Tuple[str, str]] = []

    def __call__(self, filename: str, content: str) -> None:
        self.written.append((filename, content))


class PlaylistFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name
        save_state(self.folder, INDEX_NAME, {"new": "New.opus", "old": "Old.opus", "xian": "Christian.m4a"})
        save_state(self.folder, LIKED_SNAPSHOT_NAME, ["new", "unavailable", "old"])
        save_state(self.folder, PLAYLIST_IDS_NAME, {"Christians": ["xian", "old", "xian"]})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_render_is_one_plain_filename_per_line(self) -> None:
        self.assertEqual(render_m3u(["A - B.opus", "C.m4a"]), "#EXTM3U\nA - B.opus\nC.m4a\n")

    def test_liked_file_follows_the_listing_newest_first(self) -> None:
        members = playlist_members(self.folder, "YouTube Liked")
        self.assertEqual(members["YouTube Liked"], ["New.opus", "Old.opus"])

    def test_mirrored_playlist_keeps_its_order_and_adds_the_extra_list(self) -> None:
        with open(os.path.join(self.folder, "Christians" + EXTRA_SUFFIX), "w", encoding="utf-8") as extra:
            extra.write("# videos YouTube dropped\nDeleted Video.opus\nOld.opus\n\n")
        members = playlist_members(self.folder, "")
        self.assertEqual(list(members), ["Christians"])
        self.assertEqual(members["Christians"], ["Christian.m4a", "Old.opus", "Deleted Video.opus"])

    def test_protected_ids_cover_mirrored_and_extra_songs(self) -> None:
        with open(os.path.join(self.folder, "Christians" + EXTRA_SUFFIX), "w", encoding="utf-8") as extra:
            extra.write("New.opus\nNot In Library.opus\n")
        self.assertEqual(protected_ids(self.folder), {"xian", "old", "new"})

    def test_files_are_written_once_until_their_content_changes(self) -> None:
        writer = FakeWriter()
        self.assertEqual(write_playlists(self.folder, "YouTube Liked", writer), ["YouTube Liked.m3u8", "Christians.m3u8"])
        self.assertEqual(writer.written[0], ("YouTube Liked.m3u8", "#EXTM3U\nNew.opus\nOld.opus\n"))
        self.assertEqual(write_playlists(self.folder, "YouTube Liked", writer), [])
        save_state(self.folder, LIKED_SNAPSHOT_NAME, ["old"])
        self.assertEqual(write_playlists(self.folder, "YouTube Liked", writer), ["YouTube Liked.m3u8"])
        self.assertEqual(len(writer.written), 3)

    def test_a_failed_write_is_retried_next_time(self) -> None:
        def refuse(filename: str, content: str) -> None:
            raise RuntimeError("writing failed (ssh exit 255)")
        with self.assertRaises(RuntimeError):
            write_playlists(self.folder, "YouTube Liked", refuse)
        self.assertEqual(write_playlists(self.folder, "YouTube Liked", FakeWriter()),
                         ["YouTube Liked.m3u8", "Christians.m3u8"])

    def test_local_writer_puts_the_file_in_the_folder(self) -> None:
        local_writer(self.folder)("Christians.m3u8", "#EXTM3U\nChristian.m4a\n")
        with open(os.path.join(self.folder, "Christians.m3u8"), encoding="utf-8") as written:
            self.assertEqual(written.read(), "#EXTM3U\nChristian.m4a\n")
        self.assertFalse(os.path.exists(os.path.join(self.folder, "Christians.m3u8.tmp")))


if __name__ == "__main__":
    unittest.main()
