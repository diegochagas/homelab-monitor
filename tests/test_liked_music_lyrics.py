"""Tests for lyrics lookup (lrclib) and the lyrics step. HTTP, Jellyfin and ssh are fakes."""

import subprocess
import tempfile
import unittest
from typing import Dict, List

from homelab_monitor.http_json import HttpJsonError
from homelab_monitor.liked_music_lyrics import (BUSY_COOLDOWN_SECONDS, MAX_FAILED_BATCHES, RETRY_MISSES_DAYS,
                                                LyricsOutcome, add_missing_lyrics, backfill, due_songs, lrc_name)
from homelab_monitor.liked_music_remote import write_remote_file
from homelab_monitor.liked_music_state import INDEX_NAME, LYRICS_STATE_NAME, forget_songs, load_state, save_state
from homelab_monitor.lyrics_lrclib import Lyrics, LrclibClient, SongMeta, best_match, clean_title

SYNCED = "[00:13.13] Yeah\n[00:27.16] I've been tryna call"


class FakeLrclib:
    """Answers lrclib URLs from a table of {path: result}; 'missing' means HTTP 404."""

    def __init__(self, answers: Dict[str, object]):
        self.answers = answers
        self.urls: List[str] = []

    def __call__(self, method, url, headers, body=None):
        self.urls.append(url)
        path = url.split("lrclib.net", 1)[1].split("?", 1)[0]
        answer = self.answers.get(path, "missing")
        if answer == "missing":
            raise HttpJsonError(f"GET {url} -> HTTP 404: not found")
        return answer


class LrclibClientTest(unittest.TestCase):
    def client(self, answers) -> LrclibClient:
        self.fake = FakeLrclib(answers)
        return LrclibClient(send=self.fake, pause=lambda seconds: None)

    def test_exact_match_for_tracks_with_an_album(self) -> None:
        found = self.client({"/api/get": {"duration": 200, "syncedLyrics": SYNCED}}).find(
            SongMeta("Blinding Lights", "The Weeknd", "After Hours", 200))
        self.assertEqual(found, Lyrics(SYNCED, True))
        self.assertEqual(len(self.fake.urls), 1)

    def test_search_rejects_a_different_length_version(self) -> None:
        answers = {"/api/search": [{"duration": 260, "syncedLyrics": SYNCED}]}
        self.assertIsNone(self.client(answers).find(SongMeta("Everlong (cover)", "Caio Bap", "", 238)))

    def test_video_title_is_cleaned_for_the_free_text_search(self) -> None:
        self.client({"/api/search": []}).find(SongMeta("Kiss The Girl (Official Video)", "Ashley Tisdale", "", 180))
        self.assertIn("q=Kiss+The+Girl", self.fake.urls[-1])

    def test_busy_service_is_waited_out(self) -> None:
        answers = iter([HttpJsonError("GET x -> HTTP 503: busy"), {"duration": 100, "syncedLyrics": SYNCED}])
        def busy_once(*args, **kwargs):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer
        waits = []
        found = LrclibClient(send=busy_once, pause=waits.append).find(SongMeta("A", "B", "Album", 100))
        self.assertTrue(found.synced)
        self.assertEqual(waits, [1.0, 5])

    def test_service_busy_for_too_long_raises(self) -> None:
        def always_busy(*args, **kwargs):
            raise HttpJsonError("GET x -> HTTP 503: busy")
        with self.assertRaisesRegex(HttpJsonError, "503"):
            LrclibClient(send=always_busy, pause=lambda s: None).find(SongMeta("A", "B", "", 100))

    def test_unreachable_service_raises(self) -> None:
        def offline(*args, **kwargs):
            raise HttpJsonError("GET https://lrclib.net/api/search -> unreachable: timed out")
        with self.assertRaises(HttpJsonError):
            LrclibClient(send=offline, pause=lambda s: None).find(SongMeta("A", "B", "", 100))

    def test_best_match_prefers_synced_and_skips_instrumentals(self) -> None:
        results = [{"duration": 100, "plainLyrics": "words"}, {"duration": 101, "syncedLyrics": SYNCED},
                   {"duration": 100, "instrumental": True}]
        self.assertTrue(best_match(results, 100).synced)

    def test_clean_title_drops_video_noise(self) -> None:
        self.assertEqual(clean_title("Casting Crowns - Stained Glass Masquerade - Legendado"),
                         "Casting Crowns - Stained Glass Masquerade")


class FakeJellyfin:
    def __init__(self, songs: Dict[str, dict]):
        self.songs = songs
        self.refreshed: List[str] = []

    def audio_metadata(self) -> Dict[str, dict]:
        return self.songs

    def refresh_item(self, item_id: str) -> None:
        self.refreshed.append(item_id)


def song(item_id: str, title: str, has_lyrics: bool = False) -> dict:
    return {"id": item_id, "title": title, "artist": "Artist", "album": "", "seconds": 200, "has_lyrics": has_lyrics}


class LyricsStepTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name
        save_state(self.folder, INDEX_NAME, {"hit": "A - Hit.opus", "miss": "B - Miss.opus",
                                             "has": "C - Has.m4a", "unscanned": "D - New.opus"})
        self.jellyfin = FakeJellyfin({"A - Hit.opus": song("1", "Hit"), "B - Miss.opus": song("2", "Miss"),
                                      "C - Has.m4a": song("3", "Has", has_lyrics=True)})
        self.written: Dict[str, str] = {}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def lookup(self, meta: SongMeta):
        return Lyrics(SYNCED, True) if meta.title == "Hit" else None

    def run_step(self, limit: int = 10, now: float = 1000.0):
        return add_missing_lyrics(self.folder, self.jellyfin, self.lookup, self.written.__setitem__, limit,
                                  clock=lambda: now)

    def test_writes_sidecar_and_refreshes_only_hits(self) -> None:
        outcome = self.run_step()
        self.assertEqual((outcome.written, outcome.misses), (["A - Hit.opus"], 1))
        self.assertEqual(self.written, {"A - Hit.lrc": SYNCED})
        self.assertEqual(self.jellyfin.refreshed, ["1"])
        checked = load_state(self.folder, LYRICS_STATE_NAME, {})
        self.assertEqual({vid: entry["result"] for vid, entry in checked.items()},
                         {"hit": "synced", "miss": "none", "has": "present"})

    def test_songs_jellyfin_has_not_scanned_wait(self) -> None:
        self.run_step()
        self.assertNotIn("unscanned", load_state(self.folder, LYRICS_STATE_NAME, {}))

    def test_misses_are_retried_only_after_the_retry_window(self) -> None:
        self.run_step(now=1000.0)
        checked = load_state(self.folder, LYRICS_STATE_NAME, {})
        index = load_state(self.folder, INDEX_NAME, {})
        self.assertEqual(due_songs(index, checked, 2000.0), ["unscanned"])
        later = 1000.0 + RETRY_MISSES_DAYS * 86400 + 1
        self.assertEqual(due_songs(index, checked, later), ["miss", "unscanned"])

    def test_limit_caps_lookups_per_run(self) -> None:
        self.run_step(limit=1)
        self.assertEqual(len(load_state(self.folder, LYRICS_STATE_NAME, {})), 1)

    def test_service_failure_stops_without_recording(self) -> None:
        def offline(meta):
            raise HttpJsonError("GET https://lrclib.net/api/get -> unreachable")
        outcome = add_missing_lyrics(self.folder, self.jellyfin, offline, self.written.__setitem__, 10)
        self.assertIn("unreachable", outcome.errors[0])
        self.assertEqual(load_state(self.folder, LYRICS_STATE_NAME, {}), {})

    def test_forgotten_song_is_looked_up_again_after_a_relike(self) -> None:
        self.run_step()
        forget_songs(self.folder, ["hit"])
        self.assertNotIn("hit", load_state(self.folder, LYRICS_STATE_NAME, {}))

    def test_lrc_name_keeps_the_song_name(self) -> None:
        self.assertEqual(lrc_name("ReoNa - ANIMA.m4a"), "ReoNa - ANIMA.lrc")


class FakeLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class BackfillTest(unittest.TestCase):
    def test_cools_down_after_a_busy_batch_and_stops_when_nothing_is_due(self) -> None:
        batches = iter([LyricsOutcome(["a"], 1, []), LyricsOutcome([], 0, ["HTTP 503"]), LyricsOutcome(["b"], 0, []),
                        LyricsOutcome([], 0, [])])
        sleeps = []
        total = backfill("/f", lambda n: next(batches), FakeLock, 100, sleep=sleeps.append)
        self.assertEqual((total.written, total.misses, sleeps), (["a", "b"], 1, [BUSY_COOLDOWN_SECONDS]))

    def test_gives_up_after_too_many_failing_batches(self) -> None:
        sleeps = []
        backfill("/f", lambda n: LyricsOutcome([], 0, ["HTTP 503"]), FakeLock, 100, sleep=sleeps.append)
        self.assertEqual(len(sleeps), MAX_FAILED_BATCHES)

    def test_waits_while_a_sync_holds_the_lock(self) -> None:
        locks = iter([None, FakeLock()])
        sleeps = []
        backfill("/f", lambda n: LyricsOutcome([], 0, []), lambda: next(locks), 100, sleep=sleeps.append)
        self.assertEqual(sleeps, [60])


class FakeSshWriter:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.calls = []

    def __call__(self, command, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, self.returncode, "", "Permission denied")


class WriteRemoteFileTest(unittest.TestCase):
    def test_streams_content_into_the_quoted_target(self) -> None:
        fake = FakeSshWriter()
        write_remote_file("u@h:/m/Liked/", "It's - Me.lrc", SYNCED, fake)
        command, kwargs = fake.calls[0]
        self.assertEqual(command[-1], "umask 002 && cat > '/m/Liked/It'\"'\"'s - Me.lrc'")
        self.assertEqual(kwargs["input"], SYNCED)

    def test_refuses_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "plain library filename"):
            write_remote_file("u@h:/m/", "../x.lrc", "", FakeSshWriter())

    def test_ssh_failure_raises_with_the_name(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "A.lrc.*ssh exit 1"):
            write_remote_file("u@h:/m/", "A.lrc", "x", FakeSshWriter(returncode=1))


if __name__ == "__main__":
    unittest.main()
