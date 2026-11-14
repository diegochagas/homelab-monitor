"""Checks that an edited copy holds the intended metadata and is still the same media."""

from pathlib import Path

from .immich_embed_plan import EmbedItem
from .immich_embed_steps import run

TOLERANCE = 1e-5


def check_item(item: EmbedItem, found: dict, original: Path, edited: Path) -> list[str]:
    """Problems with one edited file (empty list = good)."""
    problems = []
    if item.lat is not None and not _close(found.get("GPSLatitude"), item.lat):
        problems.append(f"latitude {found.get('GPSLatitude')!r}, expected {item.lat}")
    if item.lon is not None and not _close(found.get("GPSLongitude"), item.lon):
        problems.append(f"longitude {found.get('GPSLongitude')!r}, expected {item.lon}")
    if item.taken is not None and not _date_ok(found, item):
        problems.append(f"date {found.get('DateTimeOriginal')!r}, expected {item.taken}")
    problems += _video_problems(item, original, edited)
    if edited.stat().st_size < original.stat().st_size - allowed_shrink(item):
        problems.append(f"file shrank: {original.stat().st_size} -> {edited.stat().st_size} bytes")
    return problems


def _close(found: object, wanted: float) -> bool:
    return isinstance(found, int | float) and abs(found - wanted) < TOLERANCE


def _date_ok(found: dict, item: EmbedItem) -> bool:
    if item.rel_path.lower().endswith((".mp4", ".mov")):
        return True  # QuickTime dates are UTC; the video is checked by duration instead
    return found.get("DateTimeOriginal") == item.taken.strftime("%Y:%m:%d %H:%M:%S")


def _video_problems(item: EmbedItem, original: Path, edited: Path) -> list[str]:
    if not item.rel_path.lower().endswith((".mp4", ".mov")):
        return []
    before, after = duration(original), duration(edited)
    return [] if abs(before - after) < 0.05 else [f"duration {before}s -> {after}s"]


def duration(path: Path) -> float:
    """Length in seconds that ffprobe reports; raises if the file no longer parses."""
    shown = run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    )
    return float(shown.stdout.strip())


def allowed_shrink(item: EmbedItem) -> int:
    """Bytes a rewrite may drop: exiftool discards a truncated trailing MP4 atom (~150 B)."""
    return 512 if item.rel_path.lower().endswith((".mp4", ".mov")) else 0
