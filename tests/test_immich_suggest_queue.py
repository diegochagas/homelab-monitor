import unittest
from datetime import datetime, timedelta

from homelab_monitor.immich_queue import build_queue
from homelab_monitor.immich_suggest import Geo, GeoIndex, date_conflicts, date_from_filename, nearest_geotagged, suggest_date
from tests.immich_fakes import TZ, make_asset, uuid


class FileNameDateTest(unittest.TestCase):
    def test_full_stamp_gives_date_and_time(self):
        stamp, has_time = date_from_filename("20260715_150437.jpg", TZ)
        self.assertEqual(stamp, datetime(2026, 7, 15, 15, 4, 37, tzinfo=TZ))
        self.assertTrue(has_time)

    def test_whatsapp_name_gives_date_only(self):
        stamp, has_time = date_from_filename("IMG-20251225-WA0002.jpg", TZ)
        self.assertEqual(stamp.date().isoformat(), "2025-12-25")
        self.assertFalse(has_time)

    def test_no_date_or_impossible_date(self):
        self.assertEqual(date_from_filename("motion_photo_2522284434845014720.jpg", TZ), (None, False))
        self.assertEqual(date_from_filename("IMG-20261399-WA0001.jpg", TZ), (None, False))

    def test_suggest_keeps_current_time_when_day_matches(self):
        current = datetime(2025, 12, 25, 9, 23, 44, tzinfo=TZ)
        self.assertEqual(suggest_date(current, "IMG-20251225-WA0002.jpg")[0], current)

    def test_suggest_noon_when_day_differs(self):
        current = datetime(2026, 11, 13, 9, 0, tzinfo=TZ)
        suggested, source = suggest_date(current, "IMG-20260612-WA0005.jpg")
        self.assertEqual(suggested, datetime(2026, 6, 12, 12, 0, tzinfo=TZ))
        self.assertIn("noon", source)

    def test_suggest_file_name_time(self):
        current = datetime(2026, 11, 13, 9, 0, tzinfo=TZ)
        self.assertEqual(suggest_date(current, "20260715_150437.jpg")[0].hour, 15)


class LibraryWideDateRulesTest(unittest.TestCase):
    """Rules that keep the whole-library review to real problems (2026-11-14 survey: 22 conflicts of 7457)."""

    def test_hand_named_scans_and_old_years_are_read(self):
        for name in ("1987-03-25 Casamento (1).jpg", "1987_03_25 x.jpg", "19870325.jpg"):
            self.assertEqual(date_from_filename(name, TZ)[0].date().isoformat(), "1987-03-25")

    def test_digit_runs_inside_numbers_are_not_dates(self):
        self.assertEqual(date_from_filename("1538308078_HIGH.mp4", TZ), (None, False))
        self.assertEqual(date_from_filename("2012-0721.jpg", TZ), (None, False))

    def test_one_day_off_is_a_time_zone_not_a_conflict(self):
        self.assertFalse(date_conflicts(datetime(1965, 9, 3, 1, tzinfo=TZ), "1965-09-02 Luciene.jpg"))
        self.assertTrue(date_conflicts(datetime(2012, 1, 7, tzinfo=TZ), "2011-01-07 Aniversario.JPG"))

    def test_whatsapp_date_is_an_upper_bound(self):
        older = datetime(2005, 11, 10, tzinfo=TZ)
        self.assertFalse(date_conflicts(older, "IMG-20180515-WA0034.jpg"))
        self.assertEqual(suggest_date(older, "IMG-20180515-WA0034.jpg")[0], older)
        self.assertTrue(date_conflicts(datetime(2019, 1, 1, tzinfo=TZ), "IMG-20180515-WA0034.jpg"))


class NeighbourTest(unittest.TestCase):
    def test_index_skips_the_photo_itself(self):
        base = datetime(2026, 7, 15, 12, 0, tzinfo=TZ)
        me, other = Geo("me", base, 1, 1, ""), Geo("other", base + timedelta(hours=1), 2, 2, "")
        self.assertEqual(GeoIndex([other, me]).nearest(base, skip_id="me")[0], other)

    def test_closest_within_six_hours(self):
        base = datetime(2026, 7, 15, 12, 0, tzinfo=TZ)
        near = Geo("near", base + timedelta(minutes=20), 1.0, 2.0, "A")
        far = Geo("far", base + timedelta(hours=5), 3.0, 4.0, "B")
        gone = Geo("gone", base + timedelta(hours=7), 5.0, 6.0, "C")
        self.assertEqual(nearest_geotagged(base, [far, gone, near]), (near, timedelta(minutes=20)))

    def test_none_when_too_far(self):
        base = datetime(2026, 7, 15, 12, 0, tzinfo=TZ)
        self.assertIsNone(nearest_geotagged(base, [Geo("x", base + timedelta(days=2), 0, 0, "")]))


class QueueTest(unittest.TestCase):
    def test_complete_photo_is_not_queued(self):
        asset = make_asset(uuid(1), "20260715_150437.jpg", "2026-07-15T18:04:37Z", (-25.4, -49.2))
        self.assertEqual(build_queue([asset], TZ), [])

    def test_missing_location_gets_neighbour_suggestion(self):
        located = make_asset(uuid(1), "20260715_150437.jpg", "2026-07-15T18:04:37Z", (-25.4, -49.2), "Curitiba")
        bare = make_asset(uuid(2), "IMG-20260715-WA0001.jpg", "2026-07-15T18:30:00Z")
        cards = build_queue([located, bare], TZ)
        self.assertEqual([c["id"] for c in cards], [uuid(2)])
        suggestion = cards[0]["suggestedLocation"]
        self.assertEqual((suggestion["label"], suggestion["neighborId"], suggestion["gapMinutes"]),
                         ("Curitiba", uuid(1), 25))

    def test_date_differing_from_name_is_flagged(self):
        asset = make_asset(uuid(3), "IMG-20260612-WA0005.jpg", "2026-11-13T12:00:00Z", (1.0, 2.0))
        card = build_queue([asset], TZ)[0]
        self.assertEqual(card["reasons"], ["date differs from file name"])
        self.assertEqual(card["suggestedTakenAt"], "2026-06-12T12:00:00")

    def test_times_in_review_zone(self):
        asset = make_asset(uuid(4), "x.jpg", "2026-07-15T18:04:37Z")
        self.assertEqual(build_queue([asset], TZ)[0]["takenAt"], "2026-07-15T15:04:37")

    def test_file_time_without_camera_or_name_date_is_no_capture_date(self):
        asset = make_asset(uuid(6), "IMG_2661.HEIC", "2024-03-01T10:00:00Z", (1.0, 2.0))
        asset["fileModifiedAt"] = "2024-03-01T10:00:00.000Z"
        self.assertEqual(build_queue([asset], TZ)[0]["reasons"], ["no capture date (Immich used the file's time)"])
        asset["exifInfo"]["make"] = "Apple"
        self.assertEqual(build_queue([asset], TZ), [])

    def test_photo_is_never_its_own_neighbour(self):
        only = make_asset(uuid(5), "x.jpg", "2026-07-15T18:04:37Z", (1.0, 2.0))
        only["exifInfo"]["latitude"] = None
        self.assertIsNone(build_queue([only], TZ)[0]["suggestedLocation"])


if __name__ == "__main__":
    unittest.main()
