"""HTTP endpoint where remote agents post heartbeats (stdlib http.server)."""

import hmac
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("server")

NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,31}$")
ID_RE = re.compile(r"^[a-f0-9-]{8,64}$")
MAX_BODY = 512 * 1024


def make_handler(cfg, state):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HomelabMonitor"

        def log_message(self, fmt, *args):  # quiet
            log.debug(fmt, *args)

        def _reply(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/api/v1/ping":
                self._reply(200, {"ok": True})
            else:
                self._reply(404, {"ok": False, "error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/api/v1/heartbeat":
                self._reply(404, {"ok": False, "error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._reply(413, {"ok": False, "error": "bad length"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode())
            except (ValueError, UnicodeDecodeError):
                self._reply(400, {"ok": False, "error": "invalid json"})
                return

            node_id = str(payload.get("id", ""))
            name = str(payload.get("name", ""))
            token = str(payload.get("token", ""))
            registration_key = str(payload.get("registration_key", ""))
            snapshot = payload.get("snapshot")

            if not ID_RE.match(node_id) or not NAME_RE.match(name) or not token or not isinstance(snapshot, dict):
                self._reply(400, {"ok": False, "error": "invalid payload"})
                return

            node = state.node_by_id(node_id)
            if node is None:
                if not cfg.registration_key or not hmac.compare_digest(registration_key, cfg.registration_key):
                    log.warning("rejected registration for %s from %s", name, self.client_address[0])
                    self._reply(403, {"ok": False, "error": "registration key rejected"})
                    return
                state.register_node(node_id, name, token, snapshot.get("hostname"))
                node = state.node_by_id(node_id)
                log.info("registered node %s (%s) from %s", node["name"], node_id, self.client_address[0])
                self.server.on_registered(node)
            elif not hmac.compare_digest(token, node["token"]):
                log.warning("bad token for node %s from %s", name, self.client_address[0])
                self._reply(403, {"ok": False, "error": "token rejected"})
                return

            state.store_snapshot(node_id, snapshot)
            self._reply(200, {"ok": True, "name": node["name"], **self.server.after_snapshot(node, snapshot)})

    return Handler


class AgentServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cfg, state, on_registered, unlikes=None):
        super().__init__((cfg.listen_host, cfg.listen_port), make_handler(cfg, state))
        self.on_registered = on_registered
        self.unlikes = unlikes  # UnlikeConfirmations, or None

    def after_snapshot(self, node, snapshot):
        """Extra reply fields for the agent (unlike decisions); never fails the heartbeat."""
        if self.unlikes is None:
            return {}
        try:
            self.unlikes.on_snapshot(node, snapshot)
            return self.unlikes.reply_for(node["id"])
        except Exception:  # noqa: BLE001 - a bad snapshot must not reject the heartbeat
            log.exception("unlike confirmation failed for %s", node["name"])
            return {}

    def start(self):
        thread = threading.Thread(target=self.serve_forever, name="agent-server", daemon=True)
        thread.start()
        return thread
