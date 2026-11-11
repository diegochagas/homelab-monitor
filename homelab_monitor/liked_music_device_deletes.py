"""A song deleted on one device is removed everywhere and disliked on YouTube Music.

The library folder is shared with phones and laptops by Syncthing. The music
server shares it Send Only, so a delete made on a device that shares it
Send & Receive is only *reported* to the server (syncthing_folder). This step
reads that report and, for every reported file that is a song the sync put in
the library:

1. removes it from the Jellyfin playlists and deletes the file (and its .lrc)
   on the music server, from where Syncthing deletes it on every other device;
2. forgets it (index, queues) and remembers the id in .device-deleted.json so
   a mirrored playlist that still lists the song doesn't download it again;
3. dislikes it on YouTube Music (ytmusic_rating), which also takes it out of
   Liked Music. A dislike that fails waits in .dislike-pending.json and is
   retried on every run; the song's download-archive line stays until then.

No Telegram question here: deleting the file on a device is the owner's
decision already. Safety: only files the sync indexed count (anything else a
device deleted or changed is ignored), and more than LIKED_MUSIC_MAX_REMOVALS
songs at once (a wiped or half-mounted phone folder) removes nothing.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .liked_music_reconcile import ReconcileDeps, ReconcileOutcome, remove_everywhere
from .liked_music_state import (DEVICE_DELETED_NAME, DISLIKE_PENDING_NAME, INDEX_NAME, REMOVAL_PENDING_NAME,
                                drop_archive_lines, forget_songs, load_state, save_state)

DislikeSong = Callable[[str], None]


@dataclass
class DeviceDeleteDeps:
    library: ReconcileDeps  # folder, max_removals, Jellyfin client, remote delete
    list_deleted: Callable[[], List[str]]  # filenames other devices deleted
    open_rating: Optional[Callable[[], DislikeSong]] = None  # None: remove the files, dislike nothing


@dataclass
class DeviceDeleteOutcome:
    deleted: List[str] = field(default_factory=list)  # filenames removed everywhere this run
    disliked: int = 0
    errors: List[str] = field(default_factory=list)


def apply_device_deletions(deps: DeviceDeleteDeps) -> DeviceDeleteOutcome:
    """Remove the songs other devices deleted, then dislike the ones still waiting for it.

    Example: apply_device_deletions(deps).deleted -> ["Artist - Song.opus"]
    """
    outcome = DeviceDeleteOutcome()
    _remove_deleted_songs(deps, outcome)
    _dislike_waiting(deps, outcome)
    return outcome


def songs_among(deleted_names: List[str], index: Dict[str, str]) -> Dict[str, List[str]]:
    """{filename: [video ids]} for the deleted names that are library songs (two ids can share a file).

    Example: songs_among(["A.opus", "notes.txt"], {"a": "A.opus"}) -> {"A.opus": ["a"]}
    """
    wanted = set(deleted_names)
    songs: Dict[str, List[str]] = {}
    for video_id, name in index.items():
        if name in wanted:
            songs.setdefault(name, []).append(video_id)
    return songs


def _remove_deleted_songs(deps: DeviceDeleteDeps, outcome: DeviceDeleteOutcome) -> None:
    folder, limit = deps.library.folder, deps.library.max_removals
    try:
        reported = deps.list_deleted()
    except Exception as error:  # noqa: BLE001 - Syncthing being down must not stop the rest of the sync
        outcome.errors.append(f"device deletes not checked: {error}")
        return
    songs = songs_among(reported, load_state(folder, INDEX_NAME, {}))
    if len(songs) > limit:
        outcome.errors.append(f"{len(songs)} songs were deleted on a device at once (limit LIKED_MUSIC_MAX_REMOVALS="
                              f"{limit}); nothing removed, check that device's folder")
        return
    removal = ReconcileOutcome()
    if not songs or not remove_everywhere(deps.library, list(songs), removal):
        outcome.errors.extend(removal.errors)
        return
    _forget_deleted(folder, [vid for ids in songs.values() for vid in ids], rate=deps.open_rating is not None)
    outcome.deleted = list(songs)


def _forget_deleted(folder: str, video_ids: List[str], rate: bool) -> None:
    """Index and queues forget the songs; the archive lines stay (the songs are still liked)."""
    forget_songs(folder, video_ids, keep_archive=True)
    waiting: Dict[str, str] = load_state(folder, REMOVAL_PENDING_NAME, {})
    save_state(folder, REMOVAL_PENDING_NAME, {vid: name for vid, name in waiting.items() if vid not in video_ids})
    _append_ids(folder, DEVICE_DELETED_NAME, video_ids)
    if rate:
        _append_ids(folder, DISLIKE_PENDING_NAME, video_ids)


def _append_ids(folder: str, state_name: str, video_ids: List[str]) -> None:
    known: List[str] = load_state(folder, state_name, [])
    save_state(folder, state_name, list(dict.fromkeys(known + video_ids)))


def _dislike_waiting(deps: DeviceDeleteDeps, outcome: DeviceDeleteOutcome) -> None:
    folder = deps.library.folder
    waiting: List[str] = load_state(folder, DISLIKE_PENDING_NAME, [])
    if not waiting or deps.open_rating is None:
        return
    try:
        dislike = deps.open_rating()
        for video_id in list(waiting):
            dislike(video_id)
            waiting.remove(video_id)
            save_state(folder, DISLIKE_PENDING_NAME, waiting)
            drop_archive_lines(folder, {video_id})  # disliked: a later re-like downloads it again
            outcome.disliked += 1
    except Exception as error:  # noqa: BLE001 - signed out or offline: the ids stay queued for the next run
        outcome.errors.append(f"{len(waiting)} deleted song(s) not disliked yet (retried next run): {error}")
