import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from homelab_monitor.immich_embed_auto import (
    SyncReport,
    SyncState,
    describe_sync,
    load_state,
    prune_runs,
    record,
    save_state,
)
from homelab_monitor.immich_embed_plan import EmbedItem
from homelab_monitor.immich_sidecars import changed_sidecars, item_from_sidecar, media_for, parse_listing
from tests.immich_fakes import TZ

# Shape of a sidecar Immich 2.7 wrote after a date + location edit (from the real library).
IMMICH_SIDECAR = """<?xpacket begin='' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
 <rdf:Description rdf:about='' xmlns:exif='http://ns.adobe.com/exif/1.0/'>
  <exif:DateTimeOriginal>1991-06-02T16:28:37.000-03:00</exif:DateTimeOriginal>
  <exif:GPSLatitude>24,3.2035061S</exif:GPSLatitude>
  <exif:GPSLongitude>46,31.98441637W</exif:GPSLongitude>
 </rdf:Description></rdf:RDF></x:xmpmeta>"""

TAGS_ONLY = """<x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
 <rdf:Description rdf:about='' xmlns:digiKam='http://www.digikam.org/ns/1.0/'>
  <digiKam:TagsList><rdf:Seq><rdf:li>Google Photos</rdf:li></rdf:Seq></digiKam:TagsList>
 </rdf:Description></rdf:RDF></x:xmpmeta>"""

ATTRIBUTE_FORM = """<x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
 <rdf:Description rdf:about='' xmlns:exif='http://ns.adobe.com/exif/1.0/'
  exif:DateTimeOriginal='2020:01:05 10:00:00'/></rdf:RDF></x:xmpmeta>"""

LISTING = """drwxr-xr-x          4,096 2026/11/14 17:00:00 library/admin
-rw-r--r--        197,995 2025/03/01 10:00:00 library/admin/1991/06/1991-06-02 (1).jpg
-rw-r--r--            913 2026/11/14 17:50:25 library/admin/1991/06/1991-06-02 (1).jpg.xmp
-rw-------        100,000 2025/03/01 10:00:00 library/admin/2020/a.MP4
-rw-r--r--            440 2026/06/26 08:00:00 library/admin/2020/a.MP4.xmp
"""


class SidecarTest(unittest.TestCase):
    def test_listing_keeps_files_with_spaces_and_utc_times(self):
        files = parse_listing(LISTING)
        self.assertNotIn("library/admin", files)
        self.assertEqual(
            files["library/admin/1991/06/1991-06-02 (1).jpg.xmp"], datetime(2026, 11, 14, 17, 50, 25, tzinfo=UTC)
        )

    def test_changed_sidecars_skips_old_and_already_done(self):
        files = parse_listing(LISTING)
        baseline = datetime(2026, 11, 13, 22, 10, tzinfo=UTC)
        new = "library/admin/1991/06/1991-06-02 (1).jpg.xmp"
        self.assertEqual(changed_sidecars(files, baseline, {}), [new])
        self.assertEqual(changed_sidecars(files, baseline, {new: files[new].isoformat()}), [])

    def test_changed_sidecars_come_back_when_edited_again(self):
        files = parse_listing(LISTING)
        new = "library/admin/1991/06/1991-06-02 (1).jpg.xmp"
        done = {new: "2026-11-14T10:00:00+00:00"}
        self.assertEqual(changed_sidecars(files, datetime(2026, 1, 1, tzinfo=UTC), done)[0], new)

    def test_media_for_full_and_legacy_names(self):
        files = parse_listing(LISTING) | {"library/b.jpg": datetime.now(UTC), "library/b.xmp": datetime.now(UTC)}
        self.assertEqual(media_for("library/admin/2020/a.MP4.xmp", files), "library/admin/2020/a.MP4")
        self.assertEqual(media_for("library/b.xmp", files), "library/b.jpg")
        self.assertIsNone(media_for("library/gone.jpg.xmp", files))

    def test_immich_sidecar_gives_date_and_signed_gps(self):
        item = item_from_sidecar(IMMICH_SIDECAR, "library/x.jpg", TZ)
        self.assertEqual(item.taken, datetime(1991, 6, 2, 16, 28, 37, tzinfo=TZ))
        self.assertAlmostEqual(item.lat, -24.053391768, places=6)
        self.assertAlmostEqual(item.lon, -46.533073606, places=6)

    def test_tags_only_sidecar_has_nothing_to_write(self):
        self.assertIsNone(item_from_sidecar(TAGS_ONLY, "library/x.jpg", TZ))

    def test_attribute_form_and_exif_date_format(self):
        item = item_from_sidecar(ATTRIBUTE_FORM, "library/x.jpg", TZ)
        self.assertEqual(item.taken, datetime(2020, 1, 5, 10, 0, tzinfo=TZ))
        self.assertIsNone(item.lat)


class SyncBookkeepingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_first_run_starts_from_now(self):
        now = datetime(2026, 11, 15, 12, 0, tzinfo=TZ)
        state = load_state(self.tmp, now)
        self.assertEqual(state.baseline, now.astimezone(UTC))
        self.assertTrue((self.tmp / "embedded-sidecars.json").is_file())

    def test_state_round_trip(self):
        state = SyncState(datetime(2026, 11, 13, 22, 10, tzinfo=UTC), {"a.jpg.xmp": "t"})
        save_state(self.tmp, state)
        self.assertEqual(load_state(self.tmp, datetime.now(TZ)), state)

    def test_record_keeps_failed_open_and_marks_the_rest(self):
        files = {"a.jpg.xmp": datetime(2026, 11, 14, tzinfo=UTC), "b.jpg.xmp": datetime(2026, 11, 14, tzinfo=UTC),
                 "c.jpg.xmp": datetime(2026, 11, 14, tzinfo=UTC)}
        planned = {name: EmbedItem("", name[:-4], 1.0, 2.0, None) for name in ("a.jpg.xmp", "b.jpg.xmp")}
        state = SyncState(datetime(2026, 1, 1, tzinfo=UTC))
        report = record(state, sorted(files), files, planned, pushed={"a.jpg"})
        self.assertEqual(report.pushed, ["a.jpg"])
        self.assertEqual(report.failed, ["b.jpg"])
        self.assertEqual(sorted(state.done), ["a.jpg.xmp", "c.jpg.xmp"])

    def test_prune_removes_only_old_run_folders(self):
        for name in ("2026-10-01_1200", "2026-11-14_1200", "notes"):
            (self.tmp / name).mkdir()
        removed = prune_runs(self.tmp, datetime(2026, 11, 15, 12, 0, tzinfo=TZ))
        self.assertEqual([p.name for p in removed], ["2026-10-01_1200"])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["2026-11-14_1200", "notes"])

    def test_summary_escapes_names_and_lists_failures(self):
        text = describe_sync(SyncReport(pushed=["library/a<b>.jpg"], failed=["library/c.jpg"]))
        self.assertIn("1 photos updated", text)
        self.assertIn("a&lt;b&gt;.jpg", text)
        self.assertIn("retried tomorrow", text)
        json.dumps(text)


if __name__ == "__main__":
    unittest.main()
