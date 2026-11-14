"""Build the review queue: photos with no location, a date that disagrees with the file name, or no capture date."""

from datetime import UTC, datetime, timedelta, tzinfo

from .immich_suggest import Geo, GeoIndex, date_conflicts, date_from_filename, suggest_date

LOCAL_FORMAT = "%Y-%m-%dT%H:%M:%S"


def parse_taken(asset: dict, tz: tzinfo) -> datetime:
    """The asset's capture time as an aware datetime in `tz`."""
    raw = asset["exifInfo"]["dateTimeOriginal"]
    return datetime.fromisoformat(raw).astimezone(tz)


def place_label(exif: dict) -> str:
    """ "City, State, Country" from Immich's reverse geocoding, skipping empty parts."""
    parts = [exif.get(key) for key in ("city", "state", "country")]
    return ", ".join(part for part in parts if part)


def has_gps(asset: dict) -> bool:
    exif = asset.get("exifInfo") or {}
    return exif.get("latitude") is not None and exif.get("longitude") is not None


def geotagged_pool(assets: list[dict]) -> list[Geo]:
    """Every asset that has a GPS position, as a Geo for neighbour lookup."""
    pool = []
    for asset in assets:
        if has_gps(asset) and asset["exifInfo"].get("dateTimeOriginal"):
            exif = asset["exifInfo"]
            taken = parse_taken(asset, UTC)
            lat, lon = exif["latitude"], exif["longitude"]
            pool.append(Geo(asset["id"], taken, lat, lon, place_label(exif)))
    return pool


def reasons_for(asset: dict, taken: datetime, tz: tzinfo) -> list[str]:
    """Why this photo needs a look; empty list means it can be left alone."""
    reasons = []
    if not has_gps(asset):
        reasons.append("no location")
    if date_conflicts(taken, asset["originalFileName"]):
        reasons.append("date differs from file name")
    elif date_unknown(asset, tz):
        reasons.append("no capture date (Immich used the file's time)")
    return reasons


def date_unknown(asset: dict, tz: tzinfo) -> bool:
    """No camera data, no date in the name, and Immich's date is just the file's own time."""
    exif = asset["exifInfo"]
    if exif.get("make") or date_from_filename(asset["originalFileName"], tz)[0] is not None:
        return False
    file_times = {(asset.get(key) or "")[:19] for key in ("fileModifiedAt", "fileCreatedAt")}
    return (exif.get("dateTimeOriginal") or "")[:19] in file_times


def build_item(asset: dict, pool: GeoIndex, tz: tzinfo) -> dict | None:
    """One review card, or None when the photo already has date and location."""
    taken = parse_taken(asset, tz)
    reasons = reasons_for(asset, taken, tz)
    if not reasons:
        return None
    new_time, source = suggest_date(taken, asset["originalFileName"])
    return {
        "id": asset["id"],
        "name": asset["originalFileName"],
        "type": asset["type"],
        "reasons": reasons,
        "takenAt": taken.strftime(LOCAL_FORMAT),
        "suggestedTakenAt": new_time.strftime(LOCAL_FORMAT),
        "dateSource": source,
        "location": _current_location(asset),
        "suggestedLocation": _suggest_location(asset, new_time, pool),
    }


def _current_location(asset: dict) -> dict | None:
    if not has_gps(asset):
        return None
    exif = asset["exifInfo"]
    return {"lat": exif["latitude"], "lon": exif["longitude"], "label": place_label(exif)}


def _suggest_location(asset: dict, when: datetime, pool: GeoIndex) -> dict | None:
    found = pool.nearest(when, skip_id=asset["id"])
    if found is None:
        return None
    geo, gap = found
    return {
        "lat": geo.lat,
        "lon": geo.lon,
        "label": geo.label,
        "neighborId": geo.asset_id,
        "gapMinutes": round(gap / timedelta(minutes=1)),
    }


def build_queue(assets: list[dict], tz: tzinfo) -> list[dict]:
    """Review cards for every photo that needs a look, in capture order."""
    pool = GeoIndex(geotagged_pool(assets))
    cards = (build_item(asset, pool, tz) for asset in assets if _dated(asset))
    return [card for card in cards if card is not None]


def _dated(asset: dict) -> bool:
    return bool((asset.get("exifInfo") or {}).get("dateTimeOriginal"))
