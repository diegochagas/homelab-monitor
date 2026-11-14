"""Pure suggestion logic: date from the file name, place from the nearest geotagged photo."""

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo

FULL_STAMP = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})(?!\d)")
DATE_ONLY = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
MAX_GAP = timedelta(hours=6)


@dataclass(frozen=True)
class Geo:
    asset_id: str
    taken_at: datetime
    lat: float
    lon: float
    label: str


def date_from_filename(name: str, tz: tzinfo) -> tuple[datetime | None, bool]:
    """Return (datetime, has_time) read from a file name.

    Example: "20260715_150437.jpg" -> (2026-07-15 15:04:37, True);
    "IMG-20251225-WA0002.jpg" -> (2025-12-25 00:00, False).
    """
    full = FULL_STAMP.search(name)
    if full:
        return _build(full.groups(), tz), True
    day = DATE_ONLY.search(name)
    return (_build((*day.groups(), "0", "0", "0"), tz), False) if day else (None, False)


def _build(parts: tuple[str, ...], tz: tzinfo) -> datetime | None:
    try:
        year, month, day, hour, minute, second = (int(p) for p in parts)
        return datetime(year, month, day, hour, minute, second, tzinfo=tz)
    except ValueError:
        return None


def suggest_date(current: datetime, name: str) -> tuple[datetime, str]:
    """Suggested capture time (same zone as `current`) and where it came from."""
    stamp, has_time = date_from_filename(name, current.tzinfo)
    if stamp is None:
        return current, "no date in file name: kept current"
    if has_time:
        return stamp, "date and time from file name"
    if stamp.date() == current.date():
        return current, "date from file name matches"
    noon = datetime.combine(stamp.date(), time(12, 0), tzinfo=current.tzinfo)
    return noon, "date from file name, time unknown (noon)"


def nearest_geotagged(target: datetime, pool: list[Geo]) -> tuple[Geo, timedelta] | None:
    """Closest-in-time geotagged photo within MAX_GAP of `target`, with the gap."""
    best: tuple[Geo, timedelta] | None = None
    for geo in pool:
        gap = abs(geo.taken_at - target)
        if gap <= MAX_GAP and (best is None or gap < best[1]):
            best = (geo, gap)
    return best
