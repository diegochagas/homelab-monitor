"""What the liked-music sync remembers between runs, kept in LIKED_MUSIC_DIR.

- .library-index.json   {video_id: filename} for every song the sync put in
                        the library, so an unliked video maps back to its file.
- .liked-ids.json       the Liked Music list (video ids, newest first) seen on
                        the last complete listing; unlikes are ids that drop out.
- .playlist-pending.json  {"jellyfin": [ids]} downloads not yet added to the
                        server's liked playlist (it may not have indexed the
                        file yet), retried on every run.
- .playlist-ids.json    {playlist name: [video ids]} of every mirrored playlist
                        (LIKED_MUSIC_PLAYLISTS) as last listed, in its order.
- .m3u-written.json     {playlist file: sha1 of its content} so an unchanged
                        .m3u8 isn't rewritten (and re-synced) after every run.
- .removal-pending.json  {video_id: filename} songs that left Liked Music and
                        wait for the owner's confirmation (Telegram) before anything
                        is deleted; a re-like drops them from this queue.
- .lyrics-checked.json  {video_id: {"result": "synced"|"plain"|"none"|"deferred"|"present",
                        "at": epoch}} lyrics lookups done, so misses are only
                        retried after a while.
- .device-deleted.json  [video ids] of songs deleted on a device that shares the
                        library (liked_music_device_deletes): a mirrored
                        playlist that still lists one doesn't download it again.
- .dislike-pending.json  [video ids] of those songs not yet disliked on YouTube
                        Music (retried every run); their archive lines stay
                        until then, so a still-liked song isn't downloaded again.

Writes go to a temporary file first and are renamed into place, so a crash
never leaves half a JSON file behind.
"""

import json
import os
from typing import Any, Dict, Iterable, List

INDEX_NAME = ".library-index.json"
LIKED_SNAPSHOT_NAME = ".liked-ids.json"
PENDING_NAME = ".playlist-pending.json"
ARCHIVE_NAME = ".archive.txt"
REMOVAL_PENDING_NAME = ".removal-pending.json"
LYRICS_STATE_NAME = ".lyrics-checked.json"
PLAYLIST_IDS_NAME = ".playlist-ids.json"
M3U_HASHES_NAME = ".m3u-written.json"
DEVICE_DELETED_NAME = ".device-deleted.json"
DISLIKE_PENDING_NAME = ".dislike-pending.json"
PLAYLIST_SERVERS = ("jellyfin",)


def load_state(folder: str, name: str, default: Any) -> Any:
    """Read one state file, or return default when it doesn't exist yet.

    Example: load_state("/staging", INDEX_NAME, {}) -> {"dQw4w9WgXcQ": "A - B.opus"}
    """
    path = os.path.join(folder, name)
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save_state(folder: str, name: str, value: Any) -> None:
    """Atomically replace one state file with value (JSON)."""
    path = os.path.join(folder, name)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=1)
    os.replace(temporary, path)


def record_downloads(folder: str, downloads: Dict[str, str], liked: bool = True) -> None:
    """Add new {video_id: filename} pairs to the index and, for likes, the playlist queue.

    Songs mirrored from another playlist (liked=False) are indexed but never
    queued for the server's liked playlist.
    """
    if not downloads:
        return
    index: Dict[str, str] = load_state(folder, INDEX_NAME, {})
    index.update(downloads)
    save_state(folder, INDEX_NAME, index)
    if not liked:
        return
    pending: Dict[str, List[str]] = load_state(folder, PENDING_NAME, {})
    for server in PLAYLIST_SERVERS:
        queued = pending.setdefault(server, [])
        queued[:0] = [vid for vid in downloads if vid not in queued]
    save_state(folder, PENDING_NAME, pending)


def forget_songs(folder: str, video_ids: Iterable[str], keep_archive: bool = False) -> None:
    """Drop removed songs from the index, the playlist queues and the archive.

    Removing the archive line matters: liking the song again later must
    download it again instead of being skipped as "already downloaded".
    keep_archive leaves the line for a song that is still liked (deleted on a
    device, not disliked yet), which the next run would otherwise download again.
    """
    gone = set(video_ids)
    index: Dict[str, str] = load_state(folder, INDEX_NAME, {})
    save_state(folder, INDEX_NAME, {vid: name for vid, name in index.items() if vid not in gone})
    pending: Dict[str, List[str]] = load_state(folder, PENDING_NAME, {})
    save_state(folder, PENDING_NAME, {server: [v for v in ids if v not in gone] for server, ids in pending.items()})
    checked: Dict[str, dict] = load_state(folder, LYRICS_STATE_NAME, {})
    if gone & checked.keys():  # a re-like downloads the song again, so look its lyrics up again too
        save_state(folder, LYRICS_STATE_NAME, {vid: entry for vid, entry in checked.items() if vid not in gone})
    if not keep_archive:
        drop_archive_lines(folder, gone)


def drop_archive_lines(folder: str, gone: set) -> None:
    """Remove these video ids from yt-dlp's download archive, so they can be downloaded again."""
    path = os.path.join(folder, ARCHIVE_NAME)
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as archive:
        kept = [line for line in archive if line.strip() and line.split()[-1] not in gone]
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as archive:
        archive.writelines(kept)
    os.replace(temporary, path)


def pending_removals(folder: str) -> Dict[str, str]:
    """{video_id: filename} of unliked songs waiting for confirmation.

    Example: pending_removals("/staging") -> {"dQw4w9WgXcQ": "A - B.opus"}
    """
    return load_state(folder, REMOVAL_PENDING_NAME, {})
