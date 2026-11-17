"""A failing container alerts once, then repeats at most once a day."""

import unittest
from unittest import mock

from homelab_monitor.alerts import AlertEngine
from homelab_monitor.config import Config

DAY_SECONDS = 86400


class FakeAlertState:
    """In-memory stand-in for the sqlite state: one row per alert key."""

    def __init__(self):
        self.rows = {}

    def alert(self, key):
        return self.rows.get(key)

    def save_alert(self, key, node_id, failures, active, first_seen, last_notified, detail):
        self.rows[key] = {"failures": failures, "active": int(active), "first_seen": first_seen,
                          "last_notified": last_notified, "detail": detail}


class AlertRepeatTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.engine = AlertEngine(Config(), FakeAlertState(), self.sent.append)

    def feed_failure_at(self, clock_seconds):
        with mock.patch("homelab_monitor.alerts.time.time", return_value=clock_seconds):
            self.engine.evaluate("n:container:bitcoin", 1, True, "d", "problem", "recovered")

    def test_default_repeat_is_once_a_day(self):
        self.assertEqual(self.engine.cfg.alert_repeat_seconds, DAY_SECONDS)

    def test_down_container_alerts_once_per_day(self):
        start = 1_000_000
        for hour in range(0, 23):  # hourly checks for 23 h after the first alert
            self.feed_failure_at(start + hour * 3600)
        self.assertEqual(self.sent, ["problem"])  # confirmation takes 2 reads, so 1 alert
        self.feed_failure_at(start + 25 * 3600)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("still ongoing", self.sent[1])


if __name__ == "__main__":
    unittest.main()
