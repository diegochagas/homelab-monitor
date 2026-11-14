"""HTTP routes: static page, JSON API, and an image proxy that keeps the API key server-side."""

import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .immich_review_desk import ReviewDesk

STATIC = Path(__file__).resolve().parent / "immich_review_static"
IMAGE_ROUTE = re.compile(r"^/img/([0-9a-f-]{36})/(thumbnail|preview)$")


class ReviewRequests(BaseHTTPRequestHandler):
    """Routes for one review session; `desk` is bound by `bind_requests`."""

    desk: ReviewDesk

    def do_GET(self) -> None:
        route = urlparse(self.path)
        image = IMAGE_ROUTE.match(route.path)
        if image:
            self._image(*image.groups())
        elif route.path == "/api/queue":
            self._json(self.desk.queue())
        elif route.path == "/api/presets":
            self._json(self.desk.presets)
        elif route.path == "/api/places":
            self._json(self._places(parse_qs(route.query)))
        else:
            self._static(route.path)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        try:
            self._json(decide(self.desk, body))
        except ValueError as error:
            self._json({"error": str(error)}, status=400)
        except OSError as error:  # Immich unreachable or refused the write
            self._json({"error": f"Immich did not accept the change: {error!r}"}, status=502)

    def _places(self, query: dict[str, list[str]]) -> list[dict]:
        name = query.get("q", [""])[0]
        return self.desk.client.search_places(name) if name else []

    def _image(self, asset_id: str, size: str) -> None:
        data, content_type = self.desk.client.fetch_image(asset_id, size)
        self._send(200, data, content_type, cache=True)

    def _static(self, path: str) -> None:
        target = STATIC / ("index.html" if path == "/" else path.lstrip("/"))
        if not target.resolve().is_relative_to(STATIC) or not target.is_file():
            self._send(404, b"not found", "text/plain")
            return
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), kind)

    def _json(self, payload: object, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _send(self, status: int, data: bytes, kind: str, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


def bind_requests(desk: ReviewDesk) -> type[ReviewRequests]:
    """Build a request handler bound to `desk`."""
    return type("BoundReviewRequests", (ReviewRequests,), {"desk": desk})


def decide(desk: ReviewDesk, body: dict) -> dict:
    """Route a POSTed decision: {id, action: apply|skip, takenAt?, location?}."""
    asset_id = str(body.get("id", ""))
    if not IMAGE_ROUTE.match(f"/img/{asset_id}/preview"):
        raise ValueError(f"bad asset id {asset_id!r}, expected a UUID")
    if body.get("action") == "skip":
        return desk.skip(asset_id)
    if body.get("action") == "apply":
        return desk.apply(asset_id, body.get("takenAt"), body.get("location"))
    raise ValueError(f"bad action {body.get('action')!r}, expected 'apply' or 'skip'")


def serve(desk: ReviewDesk, port: int) -> None:
    """Serve on localhost only: the page can write to the photo library."""
    server = ThreadingHTTPServer(("127.0.0.1", port), bind_requests(desk))
    print(f"Immich review on http://127.0.0.1:{port}")
    server.serve_forever()
