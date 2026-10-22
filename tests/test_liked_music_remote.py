"""Tests for listing, remote deletion, server settings and the JSON helper. No network."""

import io
import subprocess
import unittest
import urllib.error
from unittest import mock

from homelab_monitor.config import Config
from homelab_monitor.http_json import HttpJsonError, build_url, request_json
from homelab_monitor.liked_music_remote import LikedListingError, delete_remote_files, list_liked_ids
from homelab_monitor.liked_music_servers import unlike_settings_from_config


class FakeProcess:
    """subprocess.run stand-in returning a fixed result and recording the command."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.result = (returncode, stdout, stderr)
        self.commands = []

    def __call__(self, command, **kwargs) -> subprocess.CompletedProcess:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, *self.result)


class ListLikedIdsTest(unittest.TestCase):
    def list_with(self, fake: FakeProcess):
        return list_liked_ids("yt-dlp", "firefox", "https://music.youtube.com/playlist?list=LM", {}, 60, fake)

    def test_returns_ids_newest_first(self) -> None:
        self.assertEqual(self.list_with(FakeProcess(stdout="b\na\n")), ["b", "a"])

    def test_partial_listing_is_an_error(self) -> None:
        with self.assertRaisesRegex(LikedListingError, r"exit 1\): ERROR: HTTP 429"):
            self.list_with(FakeProcess(1, "b\n", "WARNING x\nERROR: HTTP 429"))

    def test_empty_listing_is_an_error(self) -> None:
        with self.assertRaises(LikedListingError):
            self.list_with(FakeProcess(0, ""))


class DeleteRemoteFilesTest(unittest.TestCase):
    def test_quotes_names_inside_the_target_folder(self) -> None:
        fake = FakeProcess()
        delete_remote_files("u@h:/m/Liked", ["It's - Me.opus"], fake)
        self.assertEqual(fake.commands[0][-2:], ["u@h", "rm -f -- '/m/Liked/It'\"'\"'s - Me.opus'"])

    def test_refuses_paths_and_dotfiles(self) -> None:
        for name in ("../x.opus", ".archive.txt", ""):
            with self.assertRaisesRegex(ValueError, "plain library filename"):
                delete_remote_files("u@h:/m/", [name], FakeProcess())

    def test_ssh_failure_raises(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ssh exit 255"):
            delete_remote_files("u@h:/m/", ["a.opus"], FakeProcess(255, stderr="Connection refused"))


class UnlikeSettingsTest(unittest.TestCase):
    def test_servers_are_off_without_url(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            settings = unlike_settings_from_config(Config())
        self.assertEqual((settings.jellyfin, settings.swing, settings.remove_unliked), (None, None, False))

    def test_half_configured_server_names_missing_fields(self) -> None:
        env = {"LIKED_MUSIC_SWING_URL": "http://sw", "LIKED_MUSIC_SWING_USER": "admin"}
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaisesRegex(ValueError, "LIKED_MUSIC_SWING_PASSWORD, LIKED_MUSIC_SWING_DIR"):
                unlike_settings_from_config(Config())


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class RequestJsonTest(unittest.TestCase):
    def test_build_url_encodes_query(self) -> None:
        self.assertEqual(build_url("http://h/", "/Items", {"ids": "a,b"}), "http://h/Items?ids=a%2Cb")

    def test_empty_reply_is_none(self) -> None:
        self.assertIsNone(request_json("DELETE", "http://h/x", {}, opener=lambda r, timeout: FakeResponse(b"")))

    def test_http_error_keeps_headers_out_of_the_message(self) -> None:
        def refuse(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "no", {}, io.BytesIO(b"bad token"))
        with self.assertRaises(HttpJsonError) as caught:
            request_json("GET", "http://h/Users", {"Authorization": "secret"}, opener=refuse)
        self.assertIn("HTTP 401: bad token", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
