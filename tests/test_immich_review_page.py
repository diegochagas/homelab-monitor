import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from homelab_monitor.immich_review_desk import ReviewDesk, ReviewSettings
from homelab_monitor.immich_review_server import bind_requests
from homelab_monitor.immich_review_store import DecisionStore
from tests.immich_fakes import TZ, FakeImmich, make_asset, uuid

GYM = {"name": "Gym", "lat": 1.5, "lon": 2.5}


class ReviewPageTest(unittest.TestCase):
    """The review page's HTTP API against a FakeImmich (nothing real is written)."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.fake = FakeImmich([make_asset(uuid(1), "IMG-20260715-WA0001.jpg", "2026-07-15T18:30:00Z")])
        self.store = DecisionStore(root / "decisions.json")
        guesses = DecisionStore(root / "classifications.json")
        guesses.record(uuid(1), {"place": "Gym", "reason": "weights"})
        desk = ReviewDesk(self.fake, self.store, guesses, ReviewSettings(TZ, 365, [GYM]))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), bind_requests(desk))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.folder.cleanup()

    def post(self, body):
        request = Request(self.url + "/api/decision", json.dumps(body).encode(), method="POST")
        with urlopen(request) as response:
            return json.load(response)

    def get_json(self, path):
        with urlopen(self.url + path) as response:
            return json.load(response)

    def assert_rejected(self, body, code=400):
        with self.assertRaises(HTTPError) as caught:
            self.post(body)
        self.assertEqual(caught.exception.code, code)
        return caught.exception.read().decode()

    def test_apply_writes_offset_date_and_location(self):
        self.post({"id": uuid(1), "action": "apply", "takenAt": "2026-07-15T15:30:00",
                   "location": {"lat": -25.4, "lon": -49.2}})
        self.assertEqual(self.fake.updates, [(uuid(1), {"dateTimeOriginal": "2026-07-15T15:30:00.000-03:00",
                                                        "latitude": -25.4, "longitude": -49.2})])
        self.assertEqual(self.store.load()[uuid(1)]["action"], "applied")

    def test_date_without_seconds_is_accepted(self):
        self.post({"id": uuid(1), "action": "apply", "takenAt": "2025-12-22T12:00"})
        self.assertEqual(self.fake.updates[0][1]["dateTimeOriginal"], "2025-12-22T12:00:00.000-03:00")

    def test_confirming_without_changes_writes_nothing(self):
        self.post({"id": uuid(1), "action": "apply"})
        self.assertEqual(self.fake.updates, [])
        self.assertEqual(self.store.load()[uuid(1)]["fields"], {})

    def test_skip_is_remembered_and_queue_carries_decision_and_ai_guess(self):
        self.post({"id": uuid(1), "action": "skip"})
        card = self.get_json("/api/queue")[0]
        self.assertEqual(card["decision"], {"action": "skipped"})
        self.assertEqual(card["aiPlace"], {**GYM, "reason": "weights"})

    def test_bad_coordinates_ids_dates_and_actions_are_rejected(self):
        for location in ({"lat": 95, "lon": 0}, {"lat": "x", "lon": 0}, {"lat": 1}):
            self.assert_rejected({"id": uuid(1), "action": "apply", "location": location})
        self.assert_rejected({"id": "../etc", "action": "skip"})
        self.assert_rejected({"id": uuid(1), "action": "apply", "takenAt": "yesterday"})
        self.assert_rejected({"id": uuid(1), "action": "delete"})
        self.assertEqual(self.fake.updates, [])

    def test_immich_failure_is_a_502_with_the_reason(self):
        def refuse(asset_id, fields):
            raise OSError("HTTP 400 from Immich")

        self.fake.update_asset = refuse
        body = self.assert_rejected({"id": uuid(1), "action": "apply", "location": {"lat": 1, "lon": 2}}, 502)
        self.assertIn("HTTP 400 from Immich", body)

    def test_images_places_presets_and_page_are_served(self):
        with urlopen(f"{self.url}/img/{uuid(1)}/preview") as response:
            self.assertEqual(response.read(), b"JPEGBYTES")
        self.assertEqual(self.get_json("/api/places?q=Lapa")[0]["name"], "Lapa")
        self.assertEqual(self.get_json("/api/presets"), [GYM])
        with urlopen(self.url + "/") as response:
            self.assertIn(b"Immich Review", response.read())

    def test_static_path_traversal_is_blocked(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.url + "/..%2Fimmich_settings.py")
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
