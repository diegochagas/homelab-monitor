"""Background thread for the liked-music sync, plus the Telegram decision inbox.

The thread runs run_liked_sync every LIKED_MUSIC_INTERVAL_HOURS. The heartbeat
thread hands over the owner's answers about queued unlikes (they arrive in the
central bot's heartbeat reply) with submit_decisions, which wakes the thread so
a confirmed delete happens within seconds instead of at the next sync.

The central bot repeats a decision in every reply until the agent's snapshot no
longer lists those songs, so a decision lost to a restart or a busy lock is
simply applied on a later heartbeat.
"""

import logging
import os
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional

from .liked_music import LikedMusicSettings, LikedSyncResult, apply_decisions, run_liked_sync
from .liked_music_state import pending_removals

log = logging.getLogger("liked_music")

Runner = Callable[..., subprocess.CompletedProcess]
MAX_PENDING_REPORTED = 40  # keeps the heartbeat and the Telegram message small
# A run that failed (the network is often not up yet right after boot) or left
# songs a server hasn't indexed is retried after RETRY_MINUTES instead of the
# full interval, at most MAX_QUICK_RETRIES times in a row.
RETRY_MINUTES = 15
MAX_QUICK_RETRIES = 4


class LikedMusicLoop:
    """Runs the sync on a timer and applies submitted delete/keep decisions."""

    def __init__(self, settings: LikedMusicSettings, runner: Runner = subprocess.run,
                 push_runner: Runner = subprocess.run):
        self.settings = settings
        self.runner = runner
        self.push_runner = push_runner
        self.last_result: Optional[LikedSyncResult] = None
        self._wake = threading.Event()
        self._inbox_lock = threading.Lock()
        self._inbox: Dict[str, List[str]] = {"delete": [], "keep": []}
        self._quick_retries = 0

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._loop, name="liked-music", daemon=True)
        thread.start()
        return thread

    def _loop(self) -> None:
        next_sync = 0.0
        while True:
            if time.time() >= next_sync:
                next_sync = time.time() + self.next_delay(self.run_once())
            self._wake.wait(timeout=max(1.0, next_sync - time.time()))
            self._wake.clear()
            self.apply_submitted()

    def run_once(self) -> LikedSyncResult:
        result = run_liked_sync(self.settings, self.runner, self.push_runner)
        self.last_result = result
        if result.error:
            log.warning("liked music sync failed: %s", result.error)
        else:
            log.info("liked music sync: %d new, %d uploaded, %d unliked waiting for confirmation",
                     len(result.new_titles), result.pushed, len(result.queued))
        self.apply_submitted()
        return result

    def next_delay(self, result: LikedSyncResult) -> float:
        """Seconds until the next run: RETRY_MINUTES after a failed or unfinished run, else the interval.

        Example: loop.next_delay(LikedSyncResult(0, error="offline")) -> 900.0
        """
        unfinished = bool(result.error) or result.playlist_waiting > 0
        if unfinished and self._quick_retries < MAX_QUICK_RETRIES:
            self._quick_retries += 1
            return RETRY_MINUTES * 60.0
        self._quick_retries = 0
        return max(60.0, self.settings.interval_hours * 3600)

    def submit_decisions(self, decisions: Optional[dict]) -> None:
        """Queue {"delete": [ids], "keep": [ids]} from the heartbeat reply; ids not waiting are dropped.

        Example: loop.submit_decisions({"delete": ["dQw4w9WgXcQ"], "keep": []})
        """
        if not isinstance(decisions, dict):
            return
        waiting = pending_removals(self.settings.folder)
        picked = {kind: [str(v) for v in decisions.get(kind) or [] if str(v) in waiting] for kind in ("delete", "keep")}
        if not picked["delete"] and not picked["keep"]:
            return
        with self._inbox_lock:
            for kind, ids in picked.items():
                self._inbox[kind] = list(dict.fromkeys(self._inbox[kind] + ids))
        self._wake.set()

    def apply_submitted(self) -> Optional[LikedSyncResult]:
        with self._inbox_lock:
            delete, keep = self._inbox["delete"], self._inbox["keep"]
            self._inbox = {"delete": [], "keep": []}
        if not delete and not keep:
            return None
        result = apply_decisions(self.settings, delete, keep, self.push_runner)
        if result.error:
            log.warning("unlike decision not applied yet: %s", result.error)
        else:
            log.info("unlike decision: deleted %d song(s), kept %d", len(result.unliked), len(keep))
        return result

    def snapshot(self) -> dict:
        """Heartbeat summary: last sync result plus the songs waiting for confirmation."""
        summary = self.last_result.to_snapshot() if self.last_result else {}
        waiting = pending_removals(self.settings.folder)
        summary["pending_count"] = len(waiting)
        summary["pending_removals"] = [{"id": vid, "title": os.path.splitext(name)[0]}
                                       for vid, name in list(waiting.items())[:MAX_PENDING_REPORTED]]
        return summary
