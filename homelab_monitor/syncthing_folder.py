"""Ask a Syncthing instance which files another device deleted (thin REST wrapper).

The music server shares the library folder as Send Only, so it never applies
what another device changed: a song deleted on a phone that shares the folder
as Send & Receive stays on the server and shows up in the folder's "need"
list (GET /rest/db/need) marked deleted. That list is the only thing read
here; nothing is ever written to Syncthing.

A phone that shares the folder as Receive Only never announces its deletes,
so nothing is listed for it.
"""

import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List

from .http_json import Opener, build_url, request_json

NEED_PAGE_SIZE = 10000  # one page: the caller refuses long lists anyway (LIKED_MUSIC_MAX_REMOVALS)
NEED_SECTIONS = ("progress", "queued", "rest")


@dataclass
class SyncthingFolderSettings:
    url: str  # http://server:8384
    api_key: str
    folder_id: str  # Syncthing's folder ID, not its label


def remotely_deleted_files(settings: SyncthingFolderSettings,
                           opener: Opener = urllib.request.urlopen) -> List[str]:
    """Names (relative to the folder) of files another device deleted and this one still has.

    Example: remotely_deleted_files(settings) -> ["Artist - Song.opus"]
    """
    url = build_url(settings.url, "/rest/db/need", {"folder": settings.folder_id, "perpage": NEED_PAGE_SIZE})
    reply = request_json("GET", url, {"X-API-Key": settings.api_key}, opener=opener)
    if not isinstance(reply, dict):
        raise ValueError(f"Syncthing need list for folder {settings.folder_id!r}: expected an object, got {reply!r:.80}")
    return list(dict.fromkeys(name for name in _deleted_names(reply)))


def _deleted_names(reply: Dict[str, Any]) -> List[str]:
    entries = [entry for section in NEED_SECTIONS for entry in reply.get(section) or []]
    return [str(entry["name"]) for entry in entries
            if isinstance(entry, dict) and entry.get("deleted") and entry.get("name")]
