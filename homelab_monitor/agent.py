"""Remote node agent: collects metrics and posts heartbeats to the central bot.

Run with:  python3 -m homelab_monitor.agent [path/to/monitor.env]
"""

import json
import logging
import sys
import time
import urllib.error
import urllib.request

from . import VERSION, metrics
from .config import Config, load_env_file

log = logging.getLogger("agent")


def heartbeat(cfg):
    payload = {
        "id": cfg.agent_id,
        "name": cfg.node_name,
        "token": cfg.agent_token,
        "registration_key": cfg.registration_key,
        "version": VERSION,
        "snapshot": metrics.snapshot(cfg),
    }
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        cfg.central_url + "/api/v1/heartbeat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode())


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if len(sys.argv) > 1:
        load_env_file(sys.argv[1])
    cfg = Config()
    problems = cfg.validate_agent()
    if problems:
        for problem in problems:
            log.error(problem)
        sys.exit(1)

    once = "--once" in sys.argv
    log.info("agent %s (%s) -> %s", cfg.node_name, cfg.agent_id, cfg.central_url)
    while True:
        started = time.time()
        try:
            result = heartbeat(cfg)
            log.info("heartbeat ok (registered as %s)", result.get("name"))
            if once:
                return
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            log.error("central rejected heartbeat: HTTP %s %s", error.code, detail)
            if once:
                sys.exit(1)
        except (urllib.error.URLError, OSError) as error:
            log.warning("central unreachable: %s", error)
            if once:
                sys.exit(1)
        except Exception:  # noqa: BLE001
            log.exception("heartbeat failed")
            if once:
                sys.exit(1)
        time.sleep(max(5, cfg.heartbeat_interval - (time.time() - started)))


if __name__ == "__main__":
    main()
