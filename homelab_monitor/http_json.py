"""Tiny JSON-over-HTTP helper (stdlib urllib) shared by the playlist clients.

Every request has a timeout. Errors name the method, the URL (never the
headers, which carry API keys) and the server's status and reply.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

REQUEST_TIMEOUT_SECONDS = 30

Opener = Callable[..., Any]


class HttpJsonError(RuntimeError):
    """A request failed: carries the method, URL and status for the log."""


def build_url(base_url: str, path: str, query: Optional[Dict[str, Any]] = None) -> str:
    """Join base, path and query.

    Example: build_url("http://h:8097", "/Users", {"a": 1}) -> "http://h:8097/Users?a=1"
    """
    url = base_url.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


def request_json(method: str, url: str, headers: Dict[str, str], body: Any = None,
                 opener: Opener = urllib.request.urlopen) -> Any:
    """Send one request and decode the JSON reply (None for an empty reply).

    Example: request_json("GET", "http://h/Users", {"Authorization": "..."}) -> [...]
    """
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read().decode() or "null"
    except urllib.error.HTTPError as error:
        reply = error.read().decode(errors="replace")[:200]
        raise HttpJsonError(f"{method} {url} -> HTTP {error.code}: {reply}") from error
    except (urllib.error.URLError, OSError) as error:
        raise HttpJsonError(f"{method} {url} -> unreachable: {error}") from error
    return json.loads(raw)
