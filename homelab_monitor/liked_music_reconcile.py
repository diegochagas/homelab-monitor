"""Keep the music server in step with YouTube Music after each download run.

1. New likes: songs downloaded earlier are added to the "liked" playlists on
   Jellyfin (top, newest first) and Swing (end). A song the server hasn't
   indexed yet stays queued and is retried next run.
2. Unlikes: the full Liked Music list is compared with the one seen last time.
   Songs that dropped out and that the sync put in the library are removed
   from every playlist on both servers, then their files are deleted, then
   they are forgotten (index, queues, download archive) so a re-like
   downloads them again. Finamp drops the phone copy on its next sync.

Safety: nothing is deleted when the listing fails, when it is the first
listing (no earlier list to compare), when more than LIKED_MUSIC_MAX_REMOVALS
songs vanish at once (a truncated listing, not real unlikes), or when any
playlist removal fails (retried next run with the old list kept).
"""

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .liked_music_remote import LikedListingError
from .liked_music_state import (INDEX_NAME, LIKED_SNAPSHOT_NAME, PENDING_NAME, forget_songs,
                                load_state, save_state)


@dataclass
class ReconcileDeps:
    folder: str
    max_removals: int
    list_ids: Callable[[], List[str]]
    delete_files: Callable[[List[str]], None]
    remove_unliked: bool = True
    jellyfin: Optional[object] = None  # JellyfinPlaylists
    swing: Optional[object] = None  # SwingPlaylists


@dataclass
class ReconcileOutcome:
    unliked: List[str] = field(default_factory=list)
    playlist_added: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)


def reconcile_library(deps: ReconcileDeps) -> ReconcileOutcome:
    """Add queued songs to playlists, then remove songs that were unliked.

    Example: reconcile_library(deps).unliked -> ["Artist - Song.opus"]
    """
    outcome = ReconcileOutcome()
    _add_pending(deps, outcome)
    if not deps.remove_unliked:
        return outcome
    try:
        current = deps.list_ids()
    except LikedListingError as error:
        outcome.errors.append(str(error))
        return outcome
    _remove_unliked(deps, current, outcome)
    return outcome


def _add_pending(deps: ReconcileDeps, outcome: ReconcileOutcome) -> None:
    pending: Dict[str, List[str]] = load_state(deps.folder, PENDING_NAME, {"jellyfin": [], "swing": []})
    index: Dict[str, str] = load_state(deps.folder, INDEX_NAME, {})
    for server, client, add in (("jellyfin", deps.jellyfin, "add_to_top"), ("swing", deps.swing, "add")):
        queued = [vid for vid in pending.get(server, []) if vid in index]
        if client is None or not queued:
            continue
        try:
            added_names = set(getattr(client, add)([index[vid] for vid in queued]))
        except Exception as error:  # noqa: BLE001 - one server down must not stop the other
            outcome.errors.append(f"{server} playlist add: {error}")
            continue
        pending[server] = [vid for vid in queued if index[vid] not in added_names]
        outcome.playlist_added[server] = len(added_names)
    save_state(deps.folder, PENDING_NAME, pending)


def unliked_ids(previous: List[str], current: List[str], index: Dict[str, str]) -> List[str]:
    """Ids in the old list, gone from the new one, that have a library file.

    Example: unliked_ids(["a", "b"], ["b"], {"a": "A.opus"}) -> ["a"]
    """
    still_liked = set(current)
    return [vid for vid in previous if vid not in still_liked and vid in index]


def _remove_unliked(deps: ReconcileDeps, current: List[str], outcome: ReconcileOutcome) -> None:
    previous: List[str] = load_state(deps.folder, LIKED_SNAPSHOT_NAME, [])
    index: Dict[str, str] = load_state(deps.folder, INDEX_NAME, {})
    gone = unliked_ids(previous, current, index) if previous else []
    if len(gone) > deps.max_removals:
        outcome.errors.append(f"{len(gone)} songs left Liked Music at once (limit LIKED_MUSIC_MAX_REMOVALS="
                              f"{deps.max_removals}); nothing deleted, check the listing")
        return
    names = [index[vid] for vid in gone]
    if names and not _remove_everywhere(deps, names, outcome):
        return
    forget_songs(deps.folder, gone)
    save_state(deps.folder, LIKED_SNAPSHOT_NAME, current)
    outcome.unliked = names


def _remove_everywhere(deps: ReconcileDeps, names: List[str], outcome: ReconcileOutcome) -> bool:
    """Playlists first, then files; False (and nothing deleted) if a step fails."""
    try:
        for client in (deps.jellyfin, deps.swing):
            if client is not None:
                client.remove_files(names)
        deps.delete_files(names)
    except Exception as error:  # noqa: BLE001 - keep the old list so the next run retries
        outcome.errors.append(f"removing {len(names)} unliked song(s) failed: {error}")
        return False
    for name in names:
        staged = os.path.join(deps.folder, name)
        if os.path.isfile(staged):  # downloaded but never uploaded
            os.remove(staged)
    return True
