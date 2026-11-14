"""Read Immich's XMP sidecars: which ones changed, and the date/GPS each one holds.

Immich writes `<photo>.<ext>.xmp` next to the original whenever a date or location
is edited (in its web UI or through scripts/immich-review), but never touches the
photo itself. immich_embed_auto copies those values into the originals.
"""

import xml.etree.ElementTree as ElementTree
from datetime import UTC, datetime, tzinfo
from pathlib import PurePosixPath

from .immich_embed_plan import EmbedItem

EXIF_NS = "{http://ns.adobe.com/exif/1.0/}"
LISTING_TIME = "%Y/%m/%d %H:%M:%S"


def parse_listing(text: str) -> dict[str, datetime]:
    """`rsync --list-only -r` output (run with TZ=UTC) -> {relative path: UTC mtime} for files.

    Example line: "-rw-r--r--   440 2026/11/14 17:49:54 admin/1991/06/x (1).jpg.xmp"
    """
    files = {}
    for line in text.splitlines():
        parts = line.split(maxsplit=4)
        if len(parts) == 5 and parts[0].startswith("-"):
            stamp = datetime.strptime(f"{parts[2]} {parts[3]}", LISTING_TIME)
            files[parts[4]] = stamp.replace(tzinfo=UTC)
    return files


def changed_sidecars(
    files: dict[str, datetime], baseline: datetime, done: dict[str, str]
) -> list[str]:
    """Sidecars written after the baseline whose current mtime was not embedded yet."""
    return sorted(
        rel
        for rel, mtime in files.items()
        if rel.lower().endswith(".xmp") and mtime > baseline and done.get(rel) != mtime.isoformat()
    )


def media_for(sidecar: str, files: dict[str, datetime]) -> str | None:
    """The photo a sidecar belongs to: `x.jpg.xmp` -> `x.jpg` (or the older `x.xmp` -> `x.*`)."""
    full = sidecar[: -len(".xmp")]
    if full in files:
        return full
    stem = PurePosixPath(full)
    matches = [rel for rel in files if PurePosixPath(rel).with_suffix("") == stem and rel != sidecar]
    return matches[0] if len(matches) == 1 else None


def item_from_sidecar(xml_text: str, rel_path: str, tz: tzinfo) -> EmbedItem | None:
    """Date and GPS from one sidecar as an EmbedItem; None when it holds neither.

    Example: <exif:GPSLatitude>24,3.2035061S</exif:GPSLatitude> -> lat -24.05339...
    """
    root = ElementTree.fromstring(xml_text)
    taken = _parse_date(_xmp_value(root, "DateTimeOriginal"), tz)
    lat = _parse_gps(_xmp_value(root, "GPSLatitude"))
    lon = _parse_gps(_xmp_value(root, "GPSLongitude"))
    if lat is None or lon is None:
        lat = lon = None
    if taken is None and lat is None:
        return None
    return EmbedItem(asset_id="", rel_path=rel_path, lat=lat, lon=lon, taken=taken)


def _xmp_value(root: ElementTree.Element, name: str) -> str | None:
    """An exif: value written either as an element or as an rdf:Description attribute."""
    for element in root.iter():
        if element.tag == EXIF_NS + name and element.text:
            return element.text.strip()
        if EXIF_NS + name in element.attrib:
            return element.attrib[EXIF_NS + name].strip()
    return None


def _parse_date(value: str | None, tz: tzinfo) -> datetime | None:
    """ISO ("1991-06-02T16:28:37.000-03:00") or EXIF ("1991:06:02 16:28:37") -> aware, in tz."""
    if not value:
        return None
    if value[4:5] == ":":
        value = value[:10].replace(":", "-") + "T" + value[11:]
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _parse_gps(value: str | None) -> float | None:
    """XMP GPS "DDD,MM.mmmR" or "DDD,MM,SSR" (R = N/S/E/W) -> signed decimal degrees."""
    if not value or value[-1].upper() not in "NSEW":
        return None
    parts = [float(part) for part in value[:-1].split(",")]
    degrees = sum(part / 60**index for index, part in enumerate(parts))
    return -degrees if value[-1].upper() in "SW" else degrees
