"""The only module that talks to the Immich HTTP API."""

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

Opener = Callable[..., Any]


class ImmichClient:
    """Minimal Immich API wrapper; `opener` is injectable for tests."""

    def __init__(
        self, base_url: str, api_key: str, opener: Opener = urllib.request.urlopen
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.opener = opener

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[bytes, str]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"x-api-key": self.api_key, "Content-Type": "application/json"}
        req = urllib.request.Request(self.base_url + path, data, headers, method=method)
        with self.opener(req, timeout=60) as resp:
            return resp.read(), resp.headers.get("Content-Type", "")

    def search_assets(self, taken_after: str) -> list[dict]:
        """All assets (with EXIF) taken after an ISO timestamp, oldest first.

        By capture date on purpose: the library was bulk-uploaded, so "uploaded in the last
        year" would mean every old scan. WhatsApp strips EXIF, so a forwarded old photo gets
        its received date and still falls in the window.
        """
        items: list[dict] = []
        page = 1
        while page:
            body = {"takenAfter": taken_after, "withExif": True, "size": 1000, "page": page}
            raw, _ = self._request("POST", "/api/search/metadata", {**body, "order": "asc"})
            found = json.loads(raw)["assets"]
            items += found["items"]
            page = int(found["nextPage"]) if found.get("nextPage") else 0
        return items

    def duplicate_groups(self) -> list[dict]:
        """Immich's duplicate groups: [{duplicateId, assets: [...]}] (needs its duplicate detection job)."""
        raw, _ = self._request("GET", "/api/duplicates")
        return json.loads(raw)

    def fetch_image(self, asset_id: str, size: str) -> tuple[bytes, str]:
        """Thumbnail bytes (`size` = thumbnail|preview) and their content type."""
        query = urllib.parse.urlencode({"size": size})
        return self._request("GET", f"/api/assets/{asset_id}/thumbnail?{query}")

    def update_asset(self, asset_id: str, fields: dict) -> None:
        """Set dateTimeOriginal / latitude / longitude on one asset."""
        self._request("PUT", "/api/assets", {"ids": [asset_id], **fields})

    def search_places(self, name: str) -> list[dict]:
        """Place name search: [{name, latitude, longitude, admin1name, admin2name}]."""
        query = urllib.parse.urlencode({"name": name})
        raw, _ = self._request("GET", f"/api/search/places?{query}")
        return json.loads(raw)
