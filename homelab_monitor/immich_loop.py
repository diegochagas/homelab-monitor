"""Background thread on the central server that runs the daily Immich check."""

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from .immich_check import ImmichWatch
from .immich_client import ImmichClient
from .immich_settings import immich_enabled, immich_settings

log = logging.getLogger("immich")
TICK_SECONDS = 900  # how often the thread looks at the clock; the check itself runs once a day


def start_immich_watch(data_dir: str, notify: Callable[[str], None]) -> Optional[ImmichWatch]:
    """Start the daily check when IMMICH_URL is set; None (and a log line) otherwise."""
    if not immich_enabled():
        log.info("immich check off: IMMICH_URL is not set")
        return None
    try:
        settings = immich_settings()
    except ValueError as error:
        log.error("immich check off: %s", error)
        return None
    watch = ImmichWatch(ImmichClient(settings.url, settings.api_key), settings, data_dir, notify)
    threading.Thread(target=watch_forever, args=(watch,), name="immich", daemon=True).start()
    return watch


def watch_forever(watch: ImmichWatch, ticks: Optional[int] = None) -> None:
    """Every TICK_SECONDS: run the check if due. An unreachable Immich is logged and tried next tick."""
    count = 0
    while ticks is None or count < ticks:
        try:
            watch.run_if_due(datetime.now(watch.settings.tz))
        except (OSError, ValueError, KeyError) as error:
            log.warning("immich check failed, trying again in %ss: %s", TICK_SECONDS, error)
        count += 1
        if ticks is None or count < ticks:
            time.sleep(TICK_SECONDS)
