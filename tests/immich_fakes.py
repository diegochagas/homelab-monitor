"""Named fakes and builders shared by the Immich tests (never call the real Immich or Ollama)."""

from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")


class FakeImmich:
    """Records writes; serves canned assets, images, places and duplicate groups."""

    def __init__(self, assets=None, duplicates=None):
        self.assets = assets or []
        self.duplicates = duplicates or []
        self.updates = []

    def search_assets(self, since):
        return self.assets

    def duplicate_groups(self):
        return self.duplicates

    def fetch_image(self, asset_id, size):
        return b"JPEGBYTES", "image/jpeg"

    def update_asset(self, asset_id, fields):
        self.updates.append((asset_id, fields))

    def search_places(self, name):
        return [{"name": name, "latitude": -25.4, "longitude": -49.2}]


def make_asset(asset_id, name, taken_utc, gps=None, city=""):
    exif = {"dateTimeOriginal": taken_utc, "city": city, "state": None, "country": None}
    if gps:
        exif["latitude"], exif["longitude"] = gps
    return {"id": asset_id, "originalFileName": name, "type": "IMAGE", "exifInfo": exif}


def uuid(n):
    return f"00000000-0000-0000-0000-{n:012d}"


def settings_env(**extra):
    """A monitor.env as a dict with the base Immich keys filled in."""
    env = {"IMMICH_URL": "http://immich:2283/", "IMMICH_API_KEY": "key",
           "IMMICH_TIMEZONE": "America/Sao_Paulo"}
    env.update(extra)
    return env
