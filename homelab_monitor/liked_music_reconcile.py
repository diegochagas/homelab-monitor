"""Keep the music server in step with YouTube Music after each download run.

1. New likes: songs downloaded earlier are added to the "liked" playlists on
   Jellyfin (top, newest first) and Swing (unordered). A song the server
   hasn't indexed yet stays queued and is retried next run.
2. Unlikes: the full Liked Music list is compared with the one seen last time.
   Songs that dropped out and that the sync put in the library are QUEUED in
   .removal-pending.json; nothing is deleted. The heartbeat reports the queue,
   the central bot asks on Telegram, and the owner's answer comes back in a
   heartbeat reply. YouTube can also drop a removed or private video from the
   list, which looks exactly like an unlike, so a person decides.
3. apply_removal_decisions: "delete" removes the song from every playlist on
   both servers, deletes the file, then forgets it (index, queues, download
   archive) so a re-like downloads it again; "keep" only leaves the queue.

Safety: nothing is queued when the listing fails, on the first listing (no
earlier list to compare) or when more than LIKED_MUSIC_MAX_REMOVALS songs
vanish at once (a truncated listing). A failed playlist removal deletes
nothing and leaves the song queued.
"""

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .liked_music_remote import LikedListingError
from .liked_music_state import (INDEX_NAME, LIKED_SNAPSHOT_NAME, PENDING_NAME, REMOVAL_PENDING_NAME,
                                forget_songs, load_state, pending_removals, save_state)


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
    unliked: List[str] = field(default_factory=list)  # deleted after confirmation
    queued: List[str] = field(default_factory=list)  # waiting for confirmation
    kept: List[str] = field(default_factory=list)
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
    _queue_unliked(deps, current, outcome)
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


def _queue_unliked(deps: ReconcileDeps, current: List[str], outcome: ReconcileOutcome) -> None:
    previous: List[str] = load_state(deps.folder, LIKED_SNAPSHOT_NAME, [])
    index: Dict[str, str] = load_state(deps.folder, INDEX_NAME, {})
    pending = pending_removals(deps.folder)
    gone = [vid for vid in (unliked_ids(previous, current, index) if previous else []) if vid not in pending]
    if len(gone) > deps.max_removals:
        outcome.errors.append(f"{len(gone)} songs left Liked Music at once (limit LIKED_MUSIC_MAX_REMOVALS="
                              f"{deps.max_removals}); nothing queued, check the listing")
        return
    still_liked = set(current)
    pending = {vid: name for vid, name in pending.items() if vid not in still_liked}  # re-liked
    pending.update({vid: index[vid] for vid in gone})
    save_state(deps.folder, REMOVAL_PENDING_NAME, pending)
    save_state(deps.folder, LIKED_SNAPSHOT_NAME, current)
    outcome.queued = [index[vid] for vid in gone]


def apply_removal_decisions(deps: ReconcileDeps, delete_ids: List[str], keep_ids: List[str]) -> ReconcileOutcome:
    """Delete or keep queued songs; ids not in the queue are ignored (repeats are safe).

    Example: apply_removal_decisions(deps, ["abc"], []).unliked -> ["A - B.opus"]
    """
    outcome = ReconcileOutcome()
    pending = pending_removals(deps.folder)
    to_delete = [vid for vid in dict.fromkeys(delete_ids) if vid in pending]
    names = [pending[vid] for vid in to_delete]
    if names and not _remove_everywhere(deps, names, outcome):
        return outcome
    forget_songs(deps.folder, to_delete)
    to_keep = [vid for vid in dict.fromkeys(keep_ids) if vid in pending and vid not in to_delete]
    left = {vid: name for vid, name in pending.items() if vid not in to_delete and vid not in to_keep}
    save_state(deps.folder, REMOVAL_PENDING_NAME, left)
    outcome.unliked, outcome.kept = names, [pending[vid] for vid in to_keep]
    return outcome


def _remove_everywhere(deps: ReconcileDeps, names: List[str], outcome: ReconcileOutcome) -> bool:
    """Playlists first, then files; False (and nothing deleted) if a step fails."""
    try:
        for client in (deps.jellyfin, deps.swing):
            if client is not None:
                client.remove_files(names)
        deps.delete_files(names)
    except Exception as error:  # noqa: BLE001 - the song stays queued, so a repeated decision retries
        outcome.errors.append(f"removing {len(names)} unliked song(s) failed: {error}")
        return False
    for name in names:
        staged = os.path.join(deps.folder, name)
        if os.path.isfile(staged):  # downloaded but never uploaded
            os.remove(staged)
    return True
