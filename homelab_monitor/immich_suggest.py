"""Pure suggestion logic: date from the file name, place from the nearest geotagged photo."""

import bisect
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo

# 20260715_150437 / 20260715-150437: date and time (phone camera names)
FULL_STAMP = re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})(?!\d)")
# 20251225, 2012-07-21, 1987_03_25 (WhatsApp and hand-named scans)
DATE_ONLY = re.compile(r"(?<!\d)((?:19|20)\d{2})([-_.]?)(\d{2})\2(\d{2})(?!\d)")
WHATSAPP = re.compile(r"-WA\d+", re.IGNORECASE)
MAX_GAP = timedelta(hours=6)
# A one-day difference is usually a time zone at midnight, not a wrong date.
DAY_TOLERANCE = 1


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
    "1987-03-25 Casamento (1).jpg" -> (1987-03-25 00:00, False).
    """
    full = FULL_STAMP.search(name)
    if full:
        return _build(full.groups(), tz), True
    day = DATE_ONLY.search(name)
    if not day:
        return None, False
    year, _, month, mday = day.groups()
    return _build((year, month, mday, "0", "0", "0"), tz), False


def _build(parts: tuple[str, ...], tz: tzinfo) -> datetime | None:
    try:
        year, month, day, hour, minute, second = (int(p) for p in parts)
        return datetime(year, month, day, hour, minute, second, tzinfo=tz)
    except ValueError:
        return None


def date_conflicts(current: datetime, name: str) -> bool:
    """True when the file name's date says the capture date is wrong.

    A WhatsApp name holds the day the photo was *received*, so an older capture
    date is fine; only a capture date after it is a conflict.
    """
    stamp, _ = date_from_filename(name, current.tzinfo)
    if stamp is None:
        return False
    days = (current.date() - stamp.date()).days
    if abs(days) <= DAY_TOLERANCE:
        return False
    return not (WHATSAPP.search(name) and days < 0)


def suggest_date(current: datetime, name: str) -> tuple[datetime, str]:
    """Suggested capture time (same zone as `current`) and where it came from."""
    stamp, has_time = date_from_filename(name, current.tzinfo)
    if stamp is None:
        return current, "no date in file name: kept current"
    if not date_conflicts(current, name):
        return current, "current date agrees with the file name"
    if has_time:
        return stamp, "date and time from file name"
    noon = datetime.combine(stamp.date(), time(12, 0), tzinfo=current.tzinfo)
    return noon, "date from file name, time unknown (noon)"


class GeoIndex:
    """Geotagged photos sorted by capture time, for fast nearest-in-time lookups."""

    def __init__(self, pool: list[Geo]) -> None:
        self.pool = sorted(pool, key=lambda geo: geo.taken_at)
        self.times = [geo.taken_at for geo in self.pool]

    def nearest(self, target: datetime, skip_id: str = "") -> tuple[Geo, timedelta] | None:
        """Closest photo within MAX_GAP of `target` (not `skip_id`), with the gap.

        Example: GeoIndex(pool).nearest(when) -> (Geo(...), timedelta(minutes=20)) or None
        """
        index = bisect.bisect_left(self.times, target)
        window = self.pool[max(index - 2, 0):index + 2]
        candidates = [geo for geo in window if geo.asset_id != skip_id]
        best = min(candidates, key=lambda geo: abs(geo.taken_at - target), default=None)
        if best is None or abs(best.taken_at - target) > MAX_GAP:
            return None
        return best, abs(best.taken_at - target)


def nearest_geotagged(target: datetime, pool: list[Geo]) -> tuple[Geo, timedelta] | None:
    """Closest-in-time geotagged photo within MAX_GAP of `target` (one-off lookup)."""
    return GeoIndex(pool).nearest(target)
