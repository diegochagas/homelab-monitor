"""Reading what a yt-dlp download run left behind: archive ids, printed files, exit status."""

import os
import subprocess
from typing import Dict, Optional

from .liked_music_state import ARCHIVE_NAME


def read_archive_ids(folder: str) -> set:
    """Video ids already downloaded ("youtube <id>" lines of the archive)."""
    path = os.path.join(folder, ARCHIVE_NAME)
    if not os.path.isfile(path):
        return set()
    with open(path, encoding="utf-8") as archive:
        return {line.split()[-1] for line in archive if line.strip()}


def printed_downloads(stdout: Optional[str], new_ids: set) -> Dict[str, str]:
    """{video_id: filename} from yt-dlp's "<id>\t<filepath>" lines, archived ids only.

    Example: printed_downloads("abc\t/m/A - B.opus\n", {"abc"}) -> {"abc": "A - B.opus"}
    """
    downloads: Dict[str, str] = {}
    for line in (stdout or "").splitlines():
        video_id, _, path = line.strip().partition("\t")
        if video_id in new_ids and path:
            downloads[video_id] = os.path.basename(path)
    return downloads


def ytdlp_failure_reason(completed: subprocess.CompletedProcess, new_ids: set) -> Optional[str]:
    """yt-dlp exits 101 when --break-on-existing stops it (the normal end of a
    run) and 1 when single videos fail (unavailable, private). Exit 1 is only a
    failure if nothing got through and stderr shows an error (e.g. expired cookies)."""
    if completed.returncode in (0, 101) or new_ids:
        return None
    errors = [line for line in (completed.stderr or "").splitlines() if line.startswith("ERROR")]
    if not errors:
        return None
    return f"yt-dlp exited {completed.returncode}: {errors[-1][:200]}"
