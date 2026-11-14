import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from homelab_monitor.immich_classify import build_prompt, classify_all, classify_image
from homelab_monitor.immich_embed import remember_pushed
from homelab_monitor.immich_embed_plan import EmbedItem, exiftool_args, plan_items
from homelab_monitor.immich_embed_steps import EmbedConfig, push_command, read_back
from homelab_monitor.immich_embed_verify import check_item
from homelab_monitor.immich_ollama import OllamaClient, extract_json
from homelab_monitor.immich_presets import load_presets
from homelab_monitor.immich_review_store import DecisionStore
from tests.immich_fakes import TZ, make_asset, uuid

PRESETS = [{"name": "Gym", "hint": "weights"}, {"name": "Home", "hint": "sofa"}]
ROOT = "/usr/src/app/upload"


class FakeOllama(OllamaClient):
    def __init__(self, answers):
        self.answers = answers

    def ask_json(self, prompt, image):
        if image == b"bad":
            raise ValueError("model did not return a JSON object: 'x'")
        return self.answers.pop(0)


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.tmp = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()


class ClassifyTest(TempDirTest):
    def test_prompt_lists_places_with_hints(self):
        prompt = build_prompt(PRESETS)
        self.assertIn('"Gym": weights', prompt)
        self.assertIn('"Home": sofa', prompt)

    def test_unknown_answers_become_no_place(self):
        for answer in ({"place": "none"}, {"place": "Beach"}, {}):
            self.assertIsNone(classify_image(FakeOllama([answer]), b"x", PRESETS)["place"])

    def test_resumes_skips_done_and_reports_failures_without_recording(self):
        store = DecisionStore(self.tmp / "c.json")
        store.record("a", {"place": "Home", "reason": ""})
        cards = [{"id": "a", "name": "a.jpg"}, {"id": "b", "name": "b.jpg"}, {"id": "c", "name": "c.jpg"}]
        fetch = lambda asset_id: b"bad" if asset_id == "c" else b"ok"  # noqa: E731
        self.assertEqual(classify_all(cards, fetch, FakeOllama([{"place": "Gym", "reason": "r"}]), PRESETS, store), 1)
        self.assertEqual(sorted(store.load()), ["a", "b"])

    def test_extract_json_handles_think_tags_and_prose(self):
        self.assertEqual(extract_json('<think>{x}</think> ok {"a": 1} bye'), {"a": 1})
        with self.assertRaises(ValueError):
            extract_json("no json here")


class PresetsTest(TempDirTest):
    def test_missing_file_is_no_presets_and_incomplete_preset_is_named(self):
        self.assertEqual(load_presets(self.tmp / "places.json"), [])
        path = self.tmp / "places.json"
        path.write_text(json.dumps([{"name": "Gym", "lat": 1}]))
        with self.assertRaisesRegex(ValueError, "lon"):
            load_presets(path)


class EmbedTest(TempDirTest):
    def test_plan_keeps_only_changed_fields(self):
        asset = make_asset(uuid(1), "a.jpg", "2026-07-15T18:04:37Z", (-25.4, -49.2))
        asset["originalPath"] = f"{ROOT}/library/admin/2026/07/a.jpg"
        decisions = {uuid(1): {"action": "applied", "fields": {"latitude": -25.4, "longitude": -49.2}},
                     uuid(2): {"action": "applied", "fields": {}}}
        [item] = plan_items({uuid(1): asset}, decisions, TZ, ROOT)
        self.assertEqual((item.rel_path, item.lat, item.taken), ("library/admin/2026/07/a.jpg", -25.4, None))

    def test_image_and_video_arguments(self):
        taken = datetime(2026, 7, 15, 15, 4, 37, tzinfo=TZ)
        image = exiftool_args(EmbedItem("i", "x/a.jpg", -25.4, -49.2, taken))
        self.assertEqual(image[:3], ["-overwrite_original", "-P", "-m"])
        for arg in ("-GPSLatitudeRef=S", "-GPSLongitudeRef=W", "-GPSLatitude=25.4",
                    "-DateTimeOriginal=2026:07:15 15:04:37", "-OffsetTimeOriginal=-03:00"):
            self.assertIn(arg, image)
        video = exiftool_args(EmbedItem("i", "x/a.mp4", -25.4, -49.2, taken))
        self.assertIn("-Keys:GPSCoordinates=-25.400000, -49.200000", video)
        self.assertIn("-CreateDate=2026:07:15 18:04:37", video)

    def test_verify_flags_wrong_values_and_shrunk_files(self):
        original, edited = self.tmp / "o.jpg", self.tmp / "e.jpg"
        original.write_bytes(b"x" * 100)
        edited.write_bytes(b"x" * 50)
        problems = check_item(EmbedItem("i", "library/e.jpg", -25.4, -49.2, None), {"GPSLatitude": 1.0}, original, edited)
        self.assertEqual(len(problems), 3)

    def test_push_is_a_dry_run_unless_applied_and_uses_only_verified_files(self):
        config = EmbedConfig("me@server", "/lib", ROOT, self.tmp)
        self.assertIn("--dry-run", push_command(config, apply=False))
        command = push_command(config, apply=True)
        self.assertNotIn("--dry-run", command)
        self.assertIn(f"--files-from={self.tmp / 'verified.txt'}", command)

    def test_pushed_photos_are_remembered_so_the_next_plan_skips_them(self):
        (self.tmp / "plan.json").write_text(json.dumps([
            {"asset_id": "a", "rel_path": "x/a.jpg", "lat": 1, "lon": 2, "taken": None},
            {"asset_id": "b", "rel_path": "x/b.jpg", "lat": 1, "lon": 2, "taken": None}]))
        (self.tmp / "verified.txt").write_text("x/a.jpg\n")
        self.assertEqual(remember_pushed(self.tmp, EmbedConfig("s", "/l", ROOT, self.tmp)), 1)
        self.assertEqual(json.loads((self.tmp / "embedded.json").read_text()), ["a"])

    @unittest.skipUnless(shutil.which("exiftool") and shutil.which("ffmpeg"), "needs exiftool and ffmpeg")
    def test_real_exiftool_round_trip(self):
        original, edited = self.tmp / "a.jpg", self.tmp / "b.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=16x16",
                        "-frames:v", "1", str(original)], check=True, timeout=60)
        shutil.copy(original, edited)
        item = EmbedItem("i", "library/b.jpg", -25.4, -49.2, datetime(2026, 7, 15, 15, 4, 37, tzinfo=TZ))
        subprocess.run(["exiftool", *exiftool_args(item), str(edited)], check=True, capture_output=True, timeout=60)
        self.assertEqual(check_item(item, read_back(edited), original, edited), [])


if __name__ == "__main__":
    unittest.main()
