"""Minimal Docker Engine API client over the Unix socket (stdlib only).

Docker is optional: every function degrades to "unavailable" instead of raising.
"""

import http.client
import json
import os
import socket

DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=5):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def available():
    return os.path.exists(DOCKER_SOCKET)


def _get(path):
    conn = _UnixHTTPConnection(DOCKER_SOCKET)
    try:
        conn.request("GET", path, headers={"Host": "docker"})
        response = conn.getresponse()
        body = response.read()
        if response.status >= 400:
            raise RuntimeError(f"Docker API {path} -> HTTP {response.status}")
        return json.loads(body)
    finally:
        conn.close()


def _ports(entries):
    """Render the PublishedPort/PrivatePort list compactly, like `docker ps`."""
    published = set()
    internal_only = set()
    for entry in entries or []:
        private = entry.get("PrivatePort")
        public = entry.get("PublicPort")
        if public:
            published.add(f"{public}" if public == private else f"{public}→{private}")
        elif private:
            internal_only.add(str(private))
    if published:
        return sorted(published, key=lambda p: int(p.split("→")[0]))
    if internal_only:
        return []  # exposed but not published — treat as none published
    return []


def list_containers():
    """Return (ok, containers|error). Each container:
    name, image, state (running/exited/...), status ('Up 2 weeks (healthy)'),
    health (healthy/unhealthy/starting/none), ports [..]."""
    if not available():
        return False, "Docker socket not found"
    try:
        raw = _get("/containers/json?all=1")
    except Exception as error:  # noqa: BLE001 - any failure means unavailable
        return False, str(error)

    containers = []
    for item in raw:
        names = item.get("Names") or ["?"]
        name = names[0].lstrip("/")
        status = item.get("Status", "")
        health = "none"
        lowered = status.lower()
        if "(healthy)" in lowered:
            health = "healthy"
        elif "(unhealthy)" in lowered:
            health = "unhealthy"
        elif "(health: starting)" in lowered:
            health = "starting"
        containers.append({
            "name": name,
            "image": item.get("Image", ""),
            "state": item.get("State", ""),
            "status": status,
            "health": health,
            "ports": _ports(item.get("Ports")),
        })
    containers.sort(key=lambda c: c["name"])
    return True, containers


def version():
    if not available():
        return None
    try:
        info = _get("/version")
        return info.get("Version")
    except Exception:  # noqa: BLE001
        return None


def container_is_ok(container):
    return container["state"] == "running" and container["health"] != "unhealthy"


def summarize(containers):
    total = len(containers)
    running = sum(1 for c in containers if c["state"] == "running")
    healthy = sum(1 for c in containers if container_is_ok(c))
    problems = [c["name"] for c in containers if not container_is_ok(c)]
    return {"total": total, "running": running, "healthy": healthy, "problems": problems}
