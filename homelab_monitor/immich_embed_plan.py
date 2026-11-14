"""Plan and build exiftool arguments to write reviewed date/GPS into the library files."""

from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import PurePosixPath

VIDEO_SUFFIXES = {".mp4", ".mov"}
QUICKTIME_DATES = (
    "CreateDate",
    "ModifyDate",
    "TrackCreateDate",
    "TrackModifyDate",
    "MediaCreateDate",
    "MediaModifyDate",
)


@dataclass(frozen=True)
class EmbedItem:
    asset_id: str
    rel_path: str  # path inside the library root, e.g. library/admin/2026/11/x.jpg
    lat: float | None
    lon: float | None
    taken: datetime | None  # aware, in the review zone


def plan_items(
    assets: dict[str, dict], decisions: dict[str, dict], tz: tzinfo, container_root: str
) -> list[EmbedItem]:
    """One item per reviewed photo whose date and/or location was changed."""
    items = []
    for asset_id, decision in decisions.items():
        fields = decision.get("fields") or {}
        if asset_id in assets and fields:
            items.append(_item(assets[asset_id], fields, tz, container_root))
    return items


def _item(asset: dict, fields: dict, tz: tzinfo, container_root: str) -> EmbedItem:
    exif = asset["exifInfo"]
    rel = str(PurePosixPath(asset["originalPath"]).relative_to(container_root))
    has_gps = "latitude" in fields
    taken = datetime.fromisoformat(exif["dateTimeOriginal"]).astimezone(tz)
    return EmbedItem(
        asset_id=asset["id"],
        rel_path=rel,
        lat=exif["latitude"] if has_gps else None,
        lon=exif["longitude"] if has_gps else None,
        taken=taken if "dateTimeOriginal" in fields else None,
    )


def exiftool_args(item: EmbedItem) -> list[str]:
    """Arguments (without the file name) that write this item's metadata."""
    is_video = PurePosixPath(item.rel_path).suffix.lower() in VIDEO_SUFFIXES
    args = ["-overwrite_original", "-P", "-m"]
    if item.lat is not None and item.lon is not None:
        args += _video_gps(item) if is_video else _image_gps(item)
    if item.taken is not None:
        args += _video_dates(item.taken) if is_video else _image_dates(item.taken)
    return args


def _image_gps(item: EmbedItem) -> list[str]:
    return [
        f"-GPSLatitude={abs(item.lat)}",
        f"-GPSLatitudeRef={'N' if item.lat >= 0 else 'S'}",
        f"-GPSLongitude={abs(item.lon)}",
        f"-GPSLongitudeRef={'E' if item.lon >= 0 else 'W'}",
    ]


def _video_gps(item: EmbedItem) -> list[str]:
    return [f"-Keys:GPSCoordinates={item.lat:+.6f}, {item.lon:+.6f}"]


def _image_dates(taken: datetime) -> list[str]:
    stamp = taken.strftime("%Y:%m:%d %H:%M:%S")
    offset = taken.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}"
    return [f"-DateTimeOriginal={stamp}", f"-CreateDate={stamp}", f"-OffsetTimeOriginal={offset}"]


def _video_dates(taken: datetime) -> list[str]:
    stamp = taken.astimezone(UTC).strftime("%Y:%m:%d %H:%M:%S")
    return ["-api", "QuickTimeUTC=1", *(f"-{name}={stamp}" for name in QUICKTIME_DATES)]
