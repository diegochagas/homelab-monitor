import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from homelab_monitor import immich_loop
from homelab_monitor.commands import Commands
from homelab_monitor.immich_check import ImmichWatch
from homelab_monitor.immich_client import ImmichClient
from homelab_monitor.immich_settings import immich_enabled, immich_settings
from tests.immich_fakes import TZ, FakeImmich, make_asset, settings_env, uuid

MORNING = datetime(2026, 11, 14, 10, 0, tzinfo=TZ)
DUPLICATE = {"duplicateId": "d1", "assets": [{"originalFileName": "a.jpg"}, {"originalFileName": "b.jpg"}]}


def bare(n, name="IMG-20261113-WA0001.jpg"):
    return make_asset(uuid(n), name, "2026-11-13T12:00:00Z")


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.sent = []
        self.fake = FakeImmich([bare(1)], [DUPLICATE])
        self.watch = ImmichWatch(self.fake, immich_settings(settings_env()), self.folder.name, self.sent.append)

    def tearDown(self):
        self.folder.cleanup()

    def test_first_check_announces_photos_and_duplicates_with_links(self):
        text = self.watch.run_if_due(MORNING)
        self.assertEqual(self.sent, [text])
        self.assertIn("IMG-20261113-WA0001.jpg — no location", text)
        self.assertIn("a.jpg, b.jpg", text)
        self.assertIn("http://immich:2283/utilities/duplicates", text)
        self.assertIn("scripts/immich-review", text)

    def test_runs_once_a_day_and_not_before_the_hour(self):
        self.assertIsNone(self.watch.run_if_due(MORNING.replace(hour=8)))
        self.watch.run_if_due(MORNING)
        self.assertIsNone(self.watch.run_if_due(MORNING.replace(hour=20)))
        self.assertEqual(len(self.sent), 1)

    def test_next_day_only_new_findings_are_announced(self):
        self.watch.run_if_due(MORNING)
        self.fake.assets.append(bare(2, "IMG-20261114-WA0007.jpg"))
        text = self.watch.run_if_due(MORNING.replace(day=15))
        self.assertIn("<b>1</b> new photo(s)", text)
        self.assertIn("(2 in total)", text)
        self.assertNotIn("duplicate", text)

    def test_nothing_new_sends_nothing_but_marks_the_day(self):
        self.watch.run_if_due(MORNING)
        self.assertIsNone(self.watch.run_if_due(MORNING.replace(day=15)))
        seen = json.loads((Path(self.folder.name) / "immich-check.json").read_text())
        self.assertEqual(seen["day"], "2026-11-15")
        self.assertEqual(len(self.sent), 1)

    def test_status_shows_totals_without_marking_anything_seen(self):
        self.assertIn("<b>1</b> photo(s)", self.watch.status(MORNING))
        self.assertFalse((Path(self.folder.name) / "immich-check.json").exists())
        self.assertIsNotNone(self.watch.run_if_due(MORNING))

    def test_immich_command(self):
        commands = Commands(mock.Mock(), mock.Mock(), lambda: {})
        self.assertIn("Immich check is off", commands.handle("immich", []))
        commands.immich = self.watch
        self.assertIn("duplicate group(s)", commands.handle("immich", []))


class LoopAndSettingsTest(unittest.TestCase):
    def test_unreachable_immich_is_logged_not_raised(self):
        watch = mock.Mock()
        watch.settings.tz = TZ
        watch.run_if_due.side_effect = OSError("connection refused")
        with self.assertLogs("immich", level="WARNING"):
            immich_loop.watch_forever(watch, ticks=1)

    def test_check_is_off_without_url_and_bad_values_are_named(self):
        self.assertFalse(immich_enabled({}))
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(immich_loop.start_immich_watch("/tmp", print))
        with self.assertRaisesRegex(ValueError, "IMMICH_API_KEY"):
            immich_settings({"IMMICH_URL": "http://x"})
        with self.assertRaisesRegex(ValueError, "Area/City"):
            immich_settings(settings_env(IMMICH_TIMEZONE="Nowhere/Land"))
        with self.assertRaisesRegex(ValueError, "IMMICH_CHECK_HOUR"):
            immich_settings(settings_env(IMMICH_CHECK_HOUR="nine"))

    def test_settings_defaults(self):
        settings = immich_settings(settings_env())
        self.assertEqual((settings.url, settings.web_url, settings.check_hour, settings.review_days),
                         ("http://immich:2283", "http://immich:2283", 9, 365))
        with self.assertRaisesRegex(ValueError, "IMMICH_REVIEW_DIR"):
            settings.review_dir


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.payload


class ClientTest(unittest.TestCase):
    def test_search_follows_pages_with_a_timeout_and_reads_duplicates(self):
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url.rsplit("/api", 1)[1], timeout))
            if request.full_url.endswith("/duplicates"):
                return FakeResponse([DUPLICATE])
            page = json.loads(request.data)["page"]
            items = [make_asset(f"p{page}", "x.jpg", "2026-01-01T00:00:00Z")]
            return FakeResponse({"assets": {"items": items, "nextPage": "2" if page == 1 else None}})

        client = ImmichClient("http://immich", "key", opener)
        self.assertEqual([a["id"] for a in client.search_assets("2026-01-01")], ["p1", "p2"])
        self.assertEqual(client.duplicate_groups(), [DUPLICATE])
        self.assertEqual(calls, [("/search/metadata", 60)] * 2 + [("/duplicates", 60)])


if __name__ == "__main__":
    unittest.main()
