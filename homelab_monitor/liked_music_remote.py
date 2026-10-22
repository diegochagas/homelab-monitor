"""Read the whole Liked Music list, and delete unliked songs on the music server.

Listing uses yt-dlp's flat playlist mode (ids only, no downloads); it must
finish with exit 0 or the caller treats the list as unknown and deletes nothing.

Deleting runs `rm -f --` over SSH (BatchMode, so it never prompts) on files
inside the push target folder only. Filenames come from the sync's own index
and must be plain names: anything with a "/" or starting with "." is refused.
"""

import shlex
import subprocess
from typing import Callable, List, Tuple

from .liked_music_push import PUSH_TIMEOUT_SECONDS

Runner = Callable[..., subprocess.CompletedProcess]
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]


class LikedListingError(RuntimeError):
    """The Liked Music list could not be read completely."""


def list_liked_ids(ytdlp_bin: str, browser: str, playlist_url: str, env: dict,
                   timeout_seconds: int, runner: Runner = subprocess.run) -> List[str]:
    """Every video id in the playlist, newest first.

    Example: list_liked_ids("yt-dlp", "firefox", url, env, 600) -> ["dQw4w9WgXcQ", ...]
    """
    command = [ytdlp_bin, "--cookies-from-browser", browser, "--flat-playlist",
               "--print", "%(id)s", playlist_url]
    try:
        completed = runner(command, capture_output=True, text=True, timeout=timeout_seconds, env=env)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise LikedListingError(f"listing Liked Music failed: {error}") from error
    ids = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or not ids:
        detail = (completed.stderr or "").strip().splitlines()[-1:] or ["no output"]
        raise LikedListingError(f"listing Liked Music failed (yt-dlp exit {completed.returncode}): {detail[0][:200]}")
    return ids


def split_target(push_target: str) -> Tuple[str, str]:
    """("user@host", "/folder/") from an rsync target "user@host:/folder/"."""
    host, _, folder = push_target.partition(":")
    if not host or not folder.startswith("/"):
        raise ValueError(f"LIKED_MUSIC_PUSH_TARGET {push_target!r}: expected user@host:/absolute/folder/")
    return host, folder.rstrip("/") + "/"


def delete_remote_files(push_target: str, filenames: List[str], runner: Runner = subprocess.run) -> None:
    """Delete these files from the push target folder; raises if ssh fails."""
    for name in filenames:
        if not name or "/" in name or name.startswith("."):
            raise ValueError(f"refusing to delete {name!r}: expected a plain library filename")
    if not filenames:
        return
    host, folder = split_target(push_target)
    remote = "rm -f -- " + " ".join(shlex.quote(folder + name) for name in filenames)
    completed = runner(["ssh", *SSH_OPTIONS, host, remote], capture_output=True, text=True,
                       timeout=PUSH_TIMEOUT_SECONDS)
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[:200]
        raise RuntimeError(f"deleting {len(filenames)} file(s) on {host} failed (ssh exit {completed.returncode}): {detail}")
