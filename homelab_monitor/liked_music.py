"""Download songs newly liked on YouTube Music into a local folder (agent side).

The agent runs this every LIKED_MUSIC_INTERVAL_HOURS in a background thread so a
long download never delays a heartbeat. yt-dlp does the work: it reads the
signed-in browser's cookies (the Liked Music playlist is private), walks the
playlist newest first and stops at the first song already in the folder's
download archive (--break-on-existing), so a run with nothing new takes seconds.

One-off run (prints what it downloaded):
    python3 -m homelab_monitor.liked_music monitor.env --once
"""

import fcntl
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional

from .config import Config, load_env_file
from .liked_music_push import PushSettings, push_staged_songs

log = logging.getLogger("liked_music")

ARCHIVE_NAME = ".archive.txt"
LOCK_NAME = ".liked-music.lock"
MAX_REPORTED_TITLES = 10
# Same layout as the manual download script: byte-limited names because long
# Japanese titles otherwise exceed the 255-byte filename limit.
OUTPUT_TEMPLATE = "%(artist,uploader).60B - %(title).150B.%(ext)s"

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class LikedMusicSettings:
    folder: str
    playlist_url: str
    browser: str
    ytdlp_bin: str
    interval_hours: float
    timeout_minutes: int
    extra_path: str
    push_target: str = ""
    push_rsync_path: str = ""
    push_chmod: str = ""


@dataclass
class LikedSyncResult:
    checked_at: float
    new_titles: List[str] = field(default_factory=list)
    pushed: int = 0
    error: Optional[str] = None

    def to_snapshot(self) -> dict:
        """Summary for the heartbeat snapshot, e.g. {"new_count": 2, ...}."""
        summary = asdict(self)
        summary["new_count"] = len(self.new_titles)
        summary["new_titles"] = self.new_titles[:MAX_REPORTED_TITLES]
        return summary


def settings_from_config(cfg: Config) -> Optional[LikedMusicSettings]:
    """Build settings, or None when LIKED_MUSIC_DIR is unset (feature off).

    Example: settings_from_config(Config()) -> LikedMusicSettings(folder=...)
    """
    if not cfg.liked_music_dir:
        return None
    return LikedMusicSettings(
        folder=cfg.liked_music_dir,
        playlist_url=cfg.liked_music_playlist,
        browser=cfg.liked_music_browser,
        ytdlp_bin=cfg.liked_music_ytdlp,
        interval_hours=cfg.liked_music_interval_hours,
        timeout_minutes=cfg.liked_music_timeout_minutes,
        extra_path=cfg.liked_music_extra_path,
        push_target=cfg.liked_music_push_target,
        push_rsync_path=cfg.liked_music_push_rsync_path,
        push_chmod=cfg.liked_music_push_chmod,
    )


def build_ytdlp_command(settings: LikedMusicSettings) -> List[str]:
    """yt-dlp arguments that fetch only the songs missing from the archive.

    Example: build_ytdlp_command(s)[0] == s.ytdlp_bin
    """
    folder = settings.folder
    return [
        settings.ytdlp_bin,
        "--cookies-from-browser", settings.browser,
        "--yes-playlist", "--lazy-playlist", "--break-on-existing", "--ignore-errors",
        "-x", "--embed-thumbnail", "--embed-metadata",
        "--sleep-requests", "0.3", "--sleep-interval", "1", "--max-sleep-interval", "3",
        "--download-archive", os.path.join(folder, ARCHIVE_NAME),
        "-o", os.path.join(folder, OUTPUT_TEMPLATE),
        "--print", "after_move:%(title)s",
        settings.playlist_url,
    ]


def read_archive_ids(folder: str) -> set:
    """Video ids already downloaded ("youtube <id>" lines of the archive)."""
    path = os.path.join(folder, ARCHIVE_NAME)
    if not os.path.isfile(path):
        return set()
    with open(path, encoding="utf-8") as archive:
        return {line.split()[-1] for line in archive if line.strip()}


def ytdlp_environment(extra_path: str) -> dict:
    """Process environment with extra_path prepended to PATH.

    yt-dlp needs a JavaScript runtime (deno) for YouTube's challenges, and a
    systemd user service doesn't see the PATH additions of a login shell.
    """
    env = dict(os.environ)
    if extra_path:
        env["PATH"] = extra_path + os.pathsep + env.get("PATH", "")
    return env


def run_liked_sync(
    settings: LikedMusicSettings, runner: Runner = subprocess.run, push_runner: Runner = subprocess.run
) -> LikedSyncResult:
    """Download newly liked songs once, upload them if a target is set, report titles.

    Example: run_liked_sync(settings).new_titles -> ["Song A", "Song B"]
    """
    result = LikedSyncResult(checked_at=time.time())
    if not os.path.isdir(settings.folder):
        result.error = f"LIKED_MUSIC_DIR {settings.folder!r} is not a directory"
        return result
    lock = _try_lock(settings.folder)
    if lock is None:
        result.error = f"another liked-music sync is already running in {settings.folder!r}"
        return result
    with lock:
        _download_new(settings, runner, result)
        _push_if_configured(settings, push_runner, result)
        return result


def _push_if_configured(settings: LikedMusicSettings, push_runner: Runner, result: LikedSyncResult) -> None:
    """Upload staged songs; also runs when nothing is new, to retry a failed upload."""
    if not settings.push_target:
        return
    push = PushSettings(settings.folder, settings.push_target, settings.push_rsync_path, settings.push_chmod)
    outcome = push_staged_songs(push, push_runner)
    result.pushed = outcome.pushed
    if outcome.error:
        result.error = f"{result.error}; {outcome.error}" if result.error else outcome.error


def _try_lock(folder: str):
    """Exclusive non-blocking lock file, or None if another sync holds it."""
    handle = open(os.path.join(folder, LOCK_NAME), "w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _download_new(settings: LikedMusicSettings, runner: Runner, result: LikedSyncResult) -> LikedSyncResult:
    before = read_archive_ids(settings.folder)
    try:
        completed = runner(
            build_ytdlp_command(settings), capture_output=True, text=True,
            timeout=settings.timeout_minutes * 60, env=ytdlp_environment(settings.extra_path),
        )
    except FileNotFoundError:
        result.error = f"yt-dlp not found at {settings.ytdlp_bin!r}: set LIKED_MUSIC_YTDLP"
        return result
    except subprocess.TimeoutExpired:
        result.error = f"yt-dlp did not finish within {settings.timeout_minutes} minutes"
    new_ids = read_archive_ids(settings.folder) - before
    if result.error is None:
        result.new_titles = _printed_titles(completed.stdout)[: len(new_ids)] or sorted(new_ids)
        result.error = _failure_reason(completed, new_ids)
    return result


def _printed_titles(stdout: Optional[str]) -> List[str]:
    return [line.strip() for line in (stdout or "").splitlines() if line.strip()]


def _failure_reason(completed: subprocess.CompletedProcess, new_ids: set) -> Optional[str]:
    """yt-dlp exits 101 when --break-on-existing stops it (the normal end of a
    run) and 1 when single videos fail (unavailable, private). Exit 1 is only a
    failure if nothing got through and stderr shows an error (e.g. expired cookies)."""
    if completed.returncode in (0, 101) or new_ids:
        return None
    errors = [line for line in (completed.stderr or "").splitlines() if line.startswith("ERROR")]
    if not errors:
        return None
    return f"yt-dlp exited {completed.returncode}: {errors[-1][:200]}"


class LikedMusicLoop:
    """Background thread that runs run_liked_sync every interval_hours."""

    def __init__(self, settings: LikedMusicSettings, runner: Runner = subprocess.run,
                 push_runner: Runner = subprocess.run):
        self.settings = settings
        self.runner = runner
        self.push_runner = push_runner
        self.last_result: Optional[LikedSyncResult] = None

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._loop, name="liked-music", daemon=True)
        thread.start()
        return thread

    def _loop(self) -> None:
        while True:
            self.run_once()
            time.sleep(max(60.0, self.settings.interval_hours * 3600))

    def run_once(self) -> LikedSyncResult:
        result = run_liked_sync(self.settings, self.runner, self.push_runner)
        self.last_result = result
        if result.error:
            log.warning("liked music sync failed: %s", result.error)
        else:
            log.info("liked music sync: %d new song(s), %d uploaded", len(result.new_titles), result.pushed)
        return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        load_env_file(sys.argv[1])
    settings = settings_from_config(Config())
    if settings is None:
        sys.exit("LIKED_MUSIC_DIR is not set: nothing to sync")
    result = LikedMusicLoop(settings).run_once()
    for title in result.new_titles:
        print(f"downloaded: {title}")
    if result.error:
        sys.exit(result.error)


if __name__ == "__main__":
    main()
