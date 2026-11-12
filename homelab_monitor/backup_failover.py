"""Server backup paused by a homelab-backup failover.

While homelab-backup's watchdog has the apps running from the backup drive it
writes `failover.active` in its state folder and `zimaos/backup.sh` refuses to
run. The backup log the monitor reads is then the backup drive's copy, taken
in the middle of the last nightly run, so it ends with "Backup started" and no
"Backup finished": on 2026-11-12 that became a "started ... and never finished
/ check the scheduler" alert every hour, while the real reason was the failover.
"""
import os
from typing import Optional


def read_failover_since(flag_path: str) -> Optional[str]:
    """First line of the failover flag, or None when no failover is active.

    >>> read_failover_since("/nonexistent/failover.active") is None
    True
    """
    if not flag_path or not os.path.isfile(flag_path):
        return None
    try:
        with open(flag_path, encoding="utf-8", errors="replace") as handle:
            return handle.readline().strip() or "an unknown time"
    except OSError:
        return "an unknown time"


def mark_paused_by_failover(stage: dict, flag_path: str) -> dict:
    """`stage` (from server_backup_status) with status "paused" while the flag exists.

    >>> mark_paused_by_failover({"available": True, "status": "ok"}, "")["status"]
    'ok'
    """
    since = read_failover_since(flag_path)
    if since is None:
        return stage
    paused = dict(stage)
    paused.update(available=True, status="paused", detail=f"failover active since {since}")
    paused.setdefault("mtime", 0)
    return paused
