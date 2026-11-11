"""Keep the music server in step with YouTube Music after each download run.

1. New likes: songs downloaded earlier are appended to the "liked" playlist on
   Jellyfin (it can't be reordered with the agent's credentials: Jellyfin's
   move endpoint ignores API keys). A song Jellyfin hasn't indexed yet stays
   queued and the loop retries soon.
2. Unlikes: the full Liked Music list is compared with the one seen last time.
   Songs that dropped out and that the sync put in the library are QUEUED in
   .removal-pending.json; nothing is deleted. The heartbeat reports the queue,
   the central bot asks on Telegram, and the owner's answer comes back in a
   heartbeat reply. YouTube can also drop a removed or private video from the
   list, which looks exactly like an unlike, so a person decides. Songs still
   in a mirrored playlist (liked_music_m3u.protected_ids) are never queued.
3. apply_removal_decisions: "delete" removes the song from every Jellyfin
   playlist, deletes the file, then forgets it (index, queues, download
   archive) so a re-like downloads it again; "keep" only leaves the queue.

Safety: nothing is queued when the listing fails, on the first listing (no
earlier list to compare) or when more than LIKED_MUSIC_MAX_REMOVALS songs
vanish at once (a truncated listing). A failed playlist removal deletes
nothing and leaves the song queued.
"""

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from .liked_music_lyrics import lrc_name
from .liked_music_m3u import protected_ids
from .liked_music_remote import LikedListingError
from .liked_music_state import (INDEX_NAME, LIKED_SNAPSHOT_NAME, PENDING_NAME, PLAYLIST_SERVERS,
                                REMOVAL_PENDING_NAME, forget_songs, load_state, pending_removals, save_state)


@dataclass
class ReconcileDeps:
    folder: str
    max_removals: int
    list_ids: Callable[[], List[str]]
    delete_files: Callable[[List[str]], None]
    remove_unliked: bool = True
    keep_listing: bool = False  # read and save the Liked Music list even without unlike removal
    jellyfin: Optional[object] = None  # JellyfinPlaylists


@dataclass
class ReconcileOutcome:
    unliked: List[str] = field(default_factory=list)  # deleted after confirmation
    queued: List[str] = field(default_factory=list)  # waiting for confirmation
    kept: List[str] = field(default_factory=list)
    playlist_added: Dict[str, int] = field(default_factory=dict)
    playlist_waiting: int = 0  # songs a server hasn't indexed yet (retried soon)
    errors: List[str] = field(default_factory=list)


def reconcile_library(deps: ReconcileDeps) -> ReconcileOutcome:
    """Add queued songs to playlists, then remove songs that were unliked.

    Example: reconcile_library(deps).unliked -> ["Artist - Song.opus"]
    """
    outcome = ReconcileOutcome()
    _add_pending(deps, outcome)
    if not (deps.remove_unliked or deps.keep_listing):
        return outcome
    try:
        current = deps.list_ids()
    except LikedListingError as error:
        outcome.errors.append(str(error))
        return outcome
    if deps.remove_unliked:
        _queue_unliked(deps, current, outcome)
    else:
        save_state(deps.folder, LIKED_SNAPSHOT_NAME, current)
    return outcome


def _add_pending(deps: ReconcileDeps, outcome: ReconcileOutcome) -> None:
    pending: Dict[str, List[str]] = load_state(deps.folder, PENDING_NAME, {})
    index: Dict[str, str] = load_state(deps.folder, INDEX_NAME, {})
    for server, client in zip(PLAYLIST_SERVERS, (deps.jellyfin,)):
        queued = [vid for vid in pending.get(server, []) if vid in index]
        if client is None or not queued:
            continue
        added = _add_to_server(server, client, [index[vid] for vid in queued], outcome)
        pending[server] = [vid for vid in queued if index[vid] not in added]
        outcome.playlist_added[server] = len(added)
        outcome.playlist_waiting += len(pending[server])
    save_state(deps.folder, PENDING_NAME, pending)


def _add_to_server(server: str, client: object, names: List[str], outcome: ReconcileOutcome) -> set:
    """Add what the server has indexed; the rest stays queued for the loop's short retry."""
    try:
        return set(client.add(names))
    except Exception as error:  # noqa: BLE001 - a server down must not stop the unlike check
        outcome.errors.append(f"{server} playlist add: {error}")
        return set()


def unliked_ids(previous: List[str], current: List[str], index: Dict[str, str],
                protected: Set[str] = frozenset()) -> List[str]:
    """Ids in the old list, gone from the new one, that have a library file and aren't protected.

    Example: unliked_ids(["a", "b"], ["b"], {"a": "A.opus"}) -> ["a"]
    """
    still_liked = set(current)
    return [vid for vid in previous if vid not in still_liked and vid in index and vid not in protected]


def _queue_unliked(deps: ReconcileDeps, current: List[str], outcome: ReconcileOutcome) -> None:
    previous: List[str] = load_state(deps.folder, LIKED_SNAPSHOT_NAME, [])
    index: Dict[str, str] = load_state(deps.folder, INDEX_NAME, {})
    pending = pending_removals(deps.folder)
    gone = unliked_ids(previous, current, index, protected_ids(deps.folder)) if previous else []
    gone = [vid for vid in gone if vid not in pending]
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
    if names and not remove_everywhere(deps, names, outcome):
        return outcome
    forget_songs(deps.folder, to_delete)
    to_keep = [vid for vid in dict.fromkeys(keep_ids) if vid in pending and vid not in to_delete]
    left = {vid: name for vid, name in pending.items() if vid not in to_delete and vid not in to_keep}
    save_state(deps.folder, REMOVAL_PENDING_NAME, left)
    outcome.unliked, outcome.kept = names, [pending[vid] for vid in to_keep]
    return outcome


def remove_everywhere(deps: ReconcileDeps, names: List[str], outcome: ReconcileOutcome) -> bool:
    """Playlists first, then files; False (and nothing deleted) if a step fails.

    Example: remove_everywhere(deps, ["A - B.opus"], outcome) -> True
    """
    try:
        if deps.jellyfin is not None:
            deps.jellyfin.remove_files(names)
        deps.delete_files(names + [lrc_name(name) for name in names])  # rm -f: a missing .lrc is fine
    except Exception as error:  # noqa: BLE001 - the song stays queued, so a repeated decision retries
        outcome.errors.append(f"removing {len(names)} song(s) failed: {error}")
        return False
    for name in names:
        staged = os.path.join(deps.folder, name)
        if os.path.isfile(staged):  # downloaded but never uploaded
            os.remove(staged)
    return True
