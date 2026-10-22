"""Upload downloaded songs from the agent's staging folder to the music server.

The agent downloads where the browser cookies are (the workstation), but the
library lives on the server. After each sync, rsync moves the audio files to
LIKED_MUSIC_PUSH_TARGET over SSH. --remove-source-files deletes a local file
only after rsync has written and verified it on the server, so a failed or
interrupted upload leaves the file in place and the next run retries it. The
download archive (.archive.txt) never leaves the staging folder, so songs that
were uploaded are still known and never downloaded twice.
"""

import os
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional

AUDIO_EXTENSIONS = (".opus", ".m4a", ".mp3", ".ogg", ".flac", ".webm")
PUSH_TIMEOUT_SECONDS = 3600

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class PushSettings:
    folder: str
    target: str
    rsync_path: str
    chmod: str


@dataclass
class PushOutcome:
    pushed: int = 0
    error: Optional[str] = None


def staged_audio_files(folder: str) -> List[str]:
    """Audio files waiting in the staging folder (names only, sorted)."""
    return sorted(name for name in os.listdir(folder) if name.lower().endswith(AUDIO_EXTENSIONS))


def build_rsync_command(settings: PushSettings, names: List[str]) -> List[str]:
    """rsync arguments that move the given files to the target.

    Example: build_rsync_command(s, ["a.opus"])[-1] == s.target
    """
    command = ["rsync", "--archive", "--remove-source-files", "--mkpath",
               "-e", "ssh -o BatchMode=yes -o ConnectTimeout=15"]
    if settings.rsync_path:
        command += ["--rsync-path", settings.rsync_path]
    if settings.chmod:
        command += ["--chmod", settings.chmod]
    command += [os.path.join(settings.folder, name) for name in names]
    return command + [settings.target]


def push_staged_songs(settings: PushSettings, runner: Runner = subprocess.run) -> PushOutcome:
    """Move every staged song to the server; report how many left the folder.

    Example: push_staged_songs(settings).pushed -> 3
    """
    names = staged_audio_files(settings.folder)
    if not names:
        return PushOutcome()
    try:
        completed = runner(build_rsync_command(settings, names), capture_output=True,
                           text=True, timeout=PUSH_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        completed = None
    left = set(staged_audio_files(settings.folder))
    outcome = PushOutcome(pushed=sum(1 for name in names if name not in left))
    outcome.error = _push_failure(completed, settings.target, len(left))
    return outcome


def _push_failure(completed: Optional[subprocess.CompletedProcess], target: str, left: int) -> Optional[str]:
    if completed is None:
        return f"upload to {target!r} did not finish within {PUSH_TIMEOUT_SECONDS // 60} minutes; {left} file(s) kept"
    if completed.returncode == 0:
        return None
    detail = (completed.stderr or "").strip().splitlines()[-1:] or ["no error output"]
    return f"upload to {target!r} failed (rsync exit {completed.returncode}): {detail[0][:200]}; {left} file(s) kept"
