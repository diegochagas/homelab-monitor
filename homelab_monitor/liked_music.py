"""Download songs newly liked on YouTube Music into a local folder (agent side).

The agent runs this every LIKED_MUSIC_INTERVAL_HOURS in a background thread so a
long download never delays a heartbeat. yt-dlp does the work: it reads the
signed-in browser's cookies (the Liked Music playlist is private), walks the
playlist newest first and stops at the first song already in the folder's
download archive (--break-on-existing), so a run with nothing new takes seconds.

After the download (and upload, if configured) liked_music_reconcile adds the
new songs to the server playlists and, with LIKED_MUSIC_REMOVE_UNLIKED=true,
removes songs that were unliked.

One-off run (prints what it downloaded):
    python3 -m homelab_monitor.liked_music monitor.env --once
"""

import fcntl
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .config import Config, load_env_file
from .liked_music_push import PushSettings, push_staged_songs
from .liked_music_reconcile import ReconcileDeps, apply_removal_decisions, reconcile_library
from .liked_music_servers import UnlikeSettings, build_reconcile_deps, lyrics_step, unlike_settings_from_config
from .liked_music_state import ARCHIVE_NAME, record_downloads
from .liked_music_ytdlp_output import printed_downloads, read_archive_ids, ytdlp_failure_reason

log = logging.getLogger("liked_music")

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
    unlike: UnlikeSettings = field(default_factory=UnlikeSettings)
    unlike_error: str = ""  # half-configured server: downloads go on, reconcile is skipped


@dataclass
class LikedSyncResult:
    checked_at: float
    new_titles: List[str] = field(default_factory=list)
    pushed: int = 0
    unliked: List[str] = field(default_factory=list)
    queued: List[str] = field(default_factory=list)
    lyrics_added: int = 0
    playlist_waiting: int = 0
    playlist_added: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None

    def to_snapshot(self) -> dict:
        """Summary for the heartbeat snapshot, e.g. {"new_count": 2, ...}."""
        summary = asdict(self)
        summary["new_count"] = len(self.new_titles)
        summary["new_titles"] = self.new_titles[:MAX_REPORTED_TITLES]
        summary["unliked"] = self.unliked[:MAX_REPORTED_TITLES]
        summary["queued"] = self.queued[:MAX_REPORTED_TITLES]
        return summary


def settings_from_config(cfg: Config) -> Optional[LikedMusicSettings]:
    """Settings, or None when LIKED_MUSIC_DIR is unset (feature off).

    Example: settings_from_config(Config()) -> LikedMusicSettings(folder=...)
    """
    if not cfg.liked_music_dir:
        return None
    unlike, unlike_error = _unlike_or_error(cfg)
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
        unlike=unlike, unlike_error=unlike_error,
    )


def _unlike_or_error(cfg: Config) -> Tuple[UnlikeSettings, str]:
    try:
        return unlike_settings_from_config(cfg), ""
    except ValueError as error:
        return UnlikeSettings(), str(error)


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
        "--print", "after_move:%(id)s\t%(filepath)s",
        settings.playlist_url,
    ]


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
    lock = try_sync_lock(settings.folder)
    if lock is None:
        result.error = f"another liked-music sync is already running in {settings.folder!r}"
        return result
    with lock:
        _download_new(settings, runner, result)
        _push_if_configured(settings, push_runner, result)
        _reconcile(settings, push_runner, result)
        return result


def _reconcile(settings: LikedMusicSettings, runner: Runner, result: LikedSyncResult) -> None:
    """Playlist adds and unlike removals; their errors join the run's error."""
    if settings.unlike_error:
        _add_errors(result, [f"playlist/unlike sync skipped: {settings.unlike_error}"])
        return
    outcome = reconcile_library(_reconcile_deps(settings, runner))
    result.queued = outcome.queued
    result.playlist_added = outcome.playlist_added
    result.playlist_waiting = outcome.playlist_waiting
    _add_errors(result, outcome.errors)
    _add_lyrics(settings, runner, result)


def _add_lyrics(settings: LikedMusicSettings, runner: Runner, result: LikedSyncResult) -> None:
    unlike = settings.unlike
    if not (unlike.lyrics and unlike.jellyfin and settings.push_target):
        return
    outcome = lyrics_step(settings.folder, unlike.jellyfin, settings.push_target, unlike.lyrics_per_run, runner)
    result.lyrics_added = len(outcome.written)
    _add_errors(result, outcome.errors)


def _reconcile_deps(settings: LikedMusicSettings, runner: Runner) -> ReconcileDeps:
    ytdlp = [settings.ytdlp_bin, settings.browser, settings.playlist_url]
    return build_reconcile_deps(settings.folder, ytdlp, settings.push_target, settings.unlike,
                                ytdlp_environment(settings.extra_path), runner)


def apply_decisions(settings: LikedMusicSettings, delete_ids: List[str], keep_ids: List[str],
                    runner: Runner = subprocess.run) -> LikedSyncResult:
    """Carry out the owner's Telegram answer for queued unlikes, under the sync lock.

    Example: apply_decisions(settings, ["abc"], []).unliked -> ["A - B.opus"]
    """
    result = LikedSyncResult(checked_at=time.time())
    if settings.unlike_error or not settings.unlike.remove_unliked:
        result.error = settings.unlike_error or "LIKED_MUSIC_REMOVE_UNLIKED is off; decision ignored"
        return result
    lock = try_sync_lock(settings.folder)
    if lock is None:
        result.error = "liked-music sync running; decision retried after it"
        return result
    with lock:
        outcome = apply_removal_decisions(_reconcile_deps(settings, runner), delete_ids, keep_ids)
    result.unliked = outcome.unliked
    _add_errors(result, outcome.errors)
    return result


def _add_errors(result: LikedSyncResult, errors: List[str]) -> None:
    for error in errors:
        result.error = f"{result.error}; {error}" if result.error else error


def _push_if_configured(settings: LikedMusicSettings, push_runner: Runner, result: LikedSyncResult) -> None:
    """Upload staged songs; also runs when nothing is new, to retry a failed upload."""
    if not settings.push_target:
        return
    push = PushSettings(settings.folder, settings.push_target, settings.push_rsync_path, settings.push_chmod)
    outcome = push_staged_songs(push, push_runner)
    result.pushed = outcome.pushed
    if outcome.error:
        _add_errors(result, [outcome.error])


def try_sync_lock(folder: str):
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
        downloads = printed_downloads(completed.stdout, new_ids)
        record_downloads(settings.folder, downloads)
        result.new_titles = [os.path.splitext(name)[0] for name in downloads.values()] or sorted(new_ids)
        result.error = ytdlp_failure_reason(completed, new_ids)
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        load_env_file(sys.argv[1])
    settings = settings_from_config(Config())
    if settings is None:
        sys.exit("LIKED_MUSIC_DIR is not set: nothing to sync")
    from .liked_music_loop import LikedMusicLoop  # the loop module imports this one

    result = LikedMusicLoop(settings).run_once()
    for title in result.new_titles:
        print(f"downloaded: {title}")
    if result.error:
        sys.exit(result.error)


if __name__ == "__main__":
    main()
