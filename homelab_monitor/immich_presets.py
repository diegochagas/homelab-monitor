"""Favourite places (home, gym...) from the gitignored places.json."""

import json
from pathlib import Path


def load_presets(path: Path) -> list[dict]:
    """Read [{name, lat, lon}] from `path`; no file means no presets.

    Example: [{"name": "Gym", "lat": 51.5, "lon": -0.12}]
    """
    if not path.is_file():
        return []
    presets = json.loads(path.read_text())
    for preset in presets:
        _check(preset, path)
    return presets


def _check(preset: dict, path: Path) -> None:
    missing = [key for key in ("name", "lat", "lon") if key not in preset]
    if missing:
        raise ValueError(f"{path}: preset {preset!r} lacks {missing}, expected name, lat, lon")
