"""Tests for the Syncthing and YouTube Music wrappers. HTTP and yt-dlp are fakes; no network, no browser."""

import hashlib
import json
import os
import subprocess
import unittest
from typing import List

from homelab_monitor.syncthing_folder import SyncthingFolderSettings, remotely_deleted_files
from homelab_monitor.ytmusic_rating import (DISLIKE_URL, RatingError, export_browser_cookies, open_rating_session,
                                            parse_youtube_cookies, sapisid_authorization)

COOKIE_FILE = ("# Netscape HTTP Cookie File\n"
               ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tfake-sapisid\n"
               "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfake-sid\n"
               ".example.org\tTRUE\t/\tTRUE\t0\tSAPISID\tsomeone-else\n")


class FakeHttpReply:
    def __init__(self, body: str):
        self.body = body

    def __enter__(self) -> "FakeHttpReply":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return self.body.encode()


class FakeHttpServer:
    """urlopen stand-in: answers every request with `reply` and records the requests."""

    def __init__(self, reply):
        self.reply = reply
        self.requests: List = []

    def __call__(self, request, timeout=None) -> FakeHttpReply:
        self.requests.append(request)
        return FakeHttpReply(json.dumps(self.reply))


class FakeCookieYtDlp:
    """yt-dlp stand-in: writes `cookie_text` to the --cookies path and exits 2 (no URL given)."""

    def __init__(self, cookie_text: str = COOKIE_FILE):
        self.cookie_text = cookie_text
        self.cookie_paths: List[str] = []

    def __call__(self, command, **kwargs) -> subprocess.CompletedProcess:
        path = command[command.index("--cookies") + 1]
        self.cookie_paths.append(path)
        if self.cookie_text:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.cookie_text)
        return subprocess.CompletedProcess(command, 2, "", "You must provide at least one URL")


class SyncthingNeedListTest(unittest.TestCase):
    SETTINGS = SyncthingFolderSettings("http://server:8384", "fake-key", "music")

    def test_only_files_other_devices_deleted_are_returned(self) -> None:
        server = FakeHttpServer({"progress": [], "queued": [{"name": "Gone.opus", "deleted": True}],
                                 "rest": [{"name": "Edited.opus", "deleted": False},
                                          {"name": "Also gone.m4a", "deleted": True}]})
        self.assertEqual(remotely_deleted_files(self.SETTINGS, server), ["Gone.opus", "Also gone.m4a"])
        request = server.requests[0]
        self.assertEqual(request.full_url, "http://server:8384/rest/db/need?folder=music&perpage=10000")
        self.assertEqual(request.get_header("X-api-key"), "fake-key")
        self.assertEqual(request.get_method(), "GET")

    def test_in_sync_folder_reports_nothing(self) -> None:
        self.assertEqual(remotely_deleted_files(self.SETTINGS, FakeHttpServer({"rest": None})), [])

    def test_unexpected_reply_names_the_folder(self) -> None:
        with self.assertRaisesRegex(ValueError, "folder 'music': expected an object"):
            remotely_deleted_files(self.SETTINGS, FakeHttpServer(["nope"]))


class YouTubeMusicRatingTest(unittest.TestCase):
    def test_only_youtube_cookies_are_read_including_http_only_ones(self) -> None:
        self.assertEqual(parse_youtube_cookies(COOKIE_FILE), {"SAPISID": "fake-sapisid", "SID": "fake-sid"})

    def test_cookie_file_is_gone_after_the_export(self) -> None:
        ytdlp = FakeCookieYtDlp()
        cookies = export_browser_cookies("yt-dlp", "firefox", {}, ytdlp)
        self.assertEqual(cookies["SID"], "fake-sid")
        self.assertFalse(os.path.exists(os.path.dirname(ytdlp.cookie_paths[0])))

    def test_missing_cookie_file_is_an_error_naming_the_browser(self) -> None:
        with self.assertRaisesRegex(RatingError, "no cookie file for browser 'firefox'"):
            export_browser_cookies("yt-dlp", "firefox", {}, FakeCookieYtDlp(cookie_text=""))

    def test_authorization_is_the_web_players_sapisid_hash(self) -> None:
        digest = hashlib.sha1(b"1700000000 fake-sapisid https://music.youtube.com").hexdigest()
        self.assertEqual(sapisid_authorization("fake-sapisid", 1700000000.9), f"SAPISIDHASH 1700000000_{digest}")

    def test_dislike_posts_the_video_id_signed_with_the_cookies(self) -> None:
        server = FakeHttpServer({})
        dislike = open_rating_session("yt-dlp", "firefox", {}, FakeCookieYtDlp(), server, clock=lambda: 1700000000.0)
        dislike("dQw4w9WgXcQ")
        request = server.requests[0]
        self.assertEqual((request.get_method(), request.full_url), ("POST", DISLIKE_URL))
        self.assertEqual(json.loads(request.data)["target"], {"videoId": "dQw4w9WgXcQ"})
        self.assertEqual(json.loads(request.data)["context"]["client"]["clientVersion"], "1.20231114.01.00")
        self.assertTrue(request.get_header("Authorization").startswith("SAPISIDHASH 1700000000_"))
        self.assertEqual(request.get_header("Cookie"), "SAPISID=fake-sapisid; SID=fake-sid")

    def test_signed_out_browser_fails_before_any_request(self) -> None:
        server = FakeHttpServer({})
        signed_out = FakeCookieYtDlp(".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tf1=1\n")
        with self.assertRaisesRegex(RatingError, "sign in to YouTube Music"):
            open_rating_session("yt-dlp", "firefox", {}, signed_out, server)
        self.assertEqual(server.requests, [])


if __name__ == "__main__":
    unittest.main()
