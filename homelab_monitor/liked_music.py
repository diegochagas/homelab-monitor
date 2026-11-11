"""Download songs newly liked on YouTube Music into a local folder (agent side).

The agent runs this every LIKED_MUSIC_INTERVAL_HOURS in a background thread so a
long download never delays a heartbeat. yt-dlp does the work: it reads the
signed-in browser's cookies (the Liked Music playlist is private), walks the
playlist newest first and stops at the first song already in the folder's
download archive (--break-on-existing), so a run with nothing new takes seconds.

One run, in order:
1. download new likes;
2. mirror the other playlists in LIKED_MUSIC_PLAYLISTS (liked_music_sources);
3. upload the staged files to the push target, if configured;
4. with LIKED_MUSIC_SYNCTHING_URL: songs deleted on a device that shares the
   library are removed everywhere and disliked (liked_music_device_deletes);
   check_device_deletes repeats this step alone between runs;
5. liked_music_reconcile: Jellyfin playlist adds and, with
   LIKED_MUSIC_REMOVE_UNLIKED=true, the unlike queue;
6. rewrite the .m3u8 playlist files that changed (liked_music_m3u);
7. lyrics lookups (liked_music_lyrics).

One-off run (prints what it downloaded):
    python3 -m homelab_monitor.liked_music monitor.env --once
"""

import fcntl
import functools
import logging
import os
import subprocess
import sys
import time
from typing import Callable, Dict, List

from .config import Config, load_env_file
from .liked_music_device_deletes import apply_device_deletions
from .liked_music_m3u import write_playlists
from .liked_music_push import PushSettings, push_staged_songs
from .liked_music_reconcile import ReconcileDeps, apply_removal_decisions, reconcile_library
from .liked_music_servers import (build_device_delete_deps, build_reconcile_deps, lyrics_step, playlist_lister,
                                  playlist_writer)
from .liked_music_settings import (  # noqa: F401 - re-exported for the loop, the agent and the tests
    MAX_REPORTED_TITLES, LikedMusicSettings, LikedSyncResult, settings_from_config)
from .liked_music_sources import mirror_playlist_sources
from .liked_music_state import ARCHIVE_NAME, record_downloads  # noqa: F401 - ARCHIVE_NAME re-exported
from .liked_music_ytdlp_command import (  # noqa: F401 - re-exported for the tests and older callers
    OUTPUT_TEMPLATE, VIDEO_URL, build_ytdlp_command, build_ytdlp_video_command, ytdlp_environment, ytdlp_options)
from .liked_music_ytdlp_output import printed_downloads, read_archive_ids, ytdlp_failure_reason

log = logging.getLogger("liked_music")

LOCK_NAME = ".liked-music.lock"

Runner = Callable[..., subprocess.CompletedProcess]


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
        _mirror_sources(settings, runner, result)
        _push_if_configured(settings, push_runner, result)
        _reconcile(settings, runner, push_runner, result)
        return result


def _remove_device_deletes(settings: LikedMusicSettings, runner: Runner, ssh_runner: Runner,
                           result: LikedSyncResult) -> None:
    """Songs deleted on another device leave the library and get disliked; off without Syncthing settings."""
    if settings.unlike_error or settings.unlike.syncthing is None:
        return
    deps = build_device_delete_deps(_reconcile_deps(settings, runner, ssh_runner), settings.unlike,
                                    [settings.ytdlp_bin, settings.browser], ytdlp_environment(settings.extra_path), runner)
    outcome = apply_device_deletions(deps)
    result.device_deleted, result.disliked = outcome.deleted, outcome.disliked
    _add_errors(result, outcome.errors)


def check_device_deletes(settings: LikedMusicSettings, runner: Runner = subprocess.run,
                         ssh_runner: Runner = subprocess.run) -> LikedSyncResult:
    """The device-delete step alone, under the sync lock (the loop runs it between syncs).

    Example: check_device_deletes(settings).device_deleted -> ["A - B.opus"]
    """
    result = LikedSyncResult(checked_at=time.time())
    lock = try_sync_lock(settings.folder)
    if lock is None:
        return result  # a sync is running and does this step itself
    with lock:
        _remove_device_deletes(settings, runner, ssh_runner, result)
        if result.device_deleted:
            _write_playlist_files(settings, ssh_runner, result)  # deleted songs leave the playlist files too
    return result


def _mirror_sources(settings: LikedMusicSettings, runner: Runner, result: LikedSyncResult) -> None:
    """Download what the mirrored playlists are missing; trouble is reported, never a failed sync."""
    if settings.sources_error:
        _add_playlist_error(result, f"playlist mirror skipped: {settings.sources_error}")
        return
    if not settings.sources:
        return
    lister = playlist_lister(settings.ytdlp_bin, settings.browser, ytdlp_environment(settings.extra_path), runner)
    outcome = mirror_playlist_sources(settings.folder, settings.sources, lister,
                                      functools.partial(_download_videos, settings, runner))
    result.mirrored = outcome.downloaded
    for error in outcome.errors:
        _add_playlist_error(result, error)


def _download_videos(settings: LikedMusicSettings, runner: Runner, video_ids: List[str]) -> Dict[str, str]:
    """{id: filename} of the videos that got through; raises RuntimeError when yt-dlp can't run."""
    before = read_archive_ids(settings.folder)
    try:
        completed = runner(
            build_ytdlp_video_command(settings, video_ids), capture_output=True, text=True,
            timeout=settings.timeout_minutes * 60, env=ytdlp_environment(settings.extra_path),
        )
    except FileNotFoundError as error:
        raise RuntimeError(f"yt-dlp not found at {settings.ytdlp_bin!r}: set LIKED_MUSIC_YTDLP") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"yt-dlp did not finish within {settings.timeout_minutes} minutes") from error
    return printed_downloads(completed.stdout, read_archive_ids(settings.folder) - before)


def _reconcile(settings: LikedMusicSettings, runner: Runner, ssh_runner: Runner, result: LikedSyncResult) -> None:
    """Device deletes, playlist adds and unlike removals; their errors join the run's error.

    `runner` runs yt-dlp (the Liked Music listing), `ssh_runner` the ssh steps
    (deletes, playlist files, lyrics) on the push target.
    """
    if settings.unlike_error:
        _add_errors(result, [f"playlist/unlike sync skipped: {settings.unlike_error}"])
        return
    _remove_device_deletes(settings, runner, ssh_runner, result)  # first: its songs must not be queued as unliked
    outcome = reconcile_library(_reconcile_deps(settings, runner, ssh_runner))
    result.queued = outcome.queued
    result.playlist_added = outcome.playlist_added
    result.playlist_waiting = outcome.playlist_waiting
    _add_errors(result, outcome.errors)
    _write_playlist_files(settings, ssh_runner, result)
    _add_lyrics(settings, ssh_runner, result)


def _write_playlist_files(settings: LikedMusicSettings, runner: Runner, result: LikedSyncResult) -> None:
    """Rewrite the .m3u8 files whose content changed (Liked Music if named, plus every mirrored playlist)."""
    if settings.sources_error or not (settings.m3u_name or settings.sources):
        return
    writer = playlist_writer(settings.folder, settings.push_target, runner)
    try:
        result.playlists_written = write_playlists(settings.folder, settings.m3u_name, writer)
    except (OSError, RuntimeError, ValueError) as error:
        _add_playlist_error(result, f"playlist files: {error}")


def _add_lyrics(settings: LikedMusicSettings, runner: Runner, result: LikedSyncResult) -> None:
    unlike = settings.unlike
    if not (unlike.lyrics and unlike.jellyfin and settings.push_target):
        return
    outcome = lyrics_step(settings.folder, unlike.jellyfin, settings.push_target, unlike.lyrics_per_run, runner)
    result.lyrics_added = len(outcome.written)
    result.lyrics_error = "; ".join(outcome.errors) or None


def _reconcile_deps(settings: LikedMusicSettings, runner: Runner, ssh_runner: Runner) -> ReconcileDeps:
    ytdlp = [settings.ytdlp_bin, settings.browser, settings.playlist_url]
    return build_reconcile_deps(settings.folder, ytdlp, settings.push_target, settings.unlike,
                                ytdlp_environment(settings.extra_path), runner,
                                keep_listing=bool(settings.m3u_name), ssh_runner=ssh_runner)


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
        outcome = apply_removal_decisions(_reconcile_deps(settings, runner, runner), delete_ids, keep_ids)
        result.unliked = outcome.unliked
        _add_errors(result, outcome.errors)
        if outcome.unliked:
            _write_playlist_files(settings, runner, result)  # deleted songs leave the playlist files too
    return result


def _add_errors(result: LikedSyncResult, errors: List[str]) -> None:
    for error in errors:
        result.error = f"{result.error}; {error}" if result.error else error


def _add_playlist_error(result: LikedSyncResult, error: str) -> None:
    result.playlist_error = f"{result.playlist_error}; {error}" if result.playlist_error else error


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
    if result.playlist_error:
        print(f"playlists: {result.playlist_error}")
    if result.error:
        sys.exit(result.error)


if __name__ == "__main__":
    main()
