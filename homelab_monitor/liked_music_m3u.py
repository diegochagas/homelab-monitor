"""Playlist files (.m3u8) next to the songs, one per playlist, kept current after each sync.

With LIKED_MUSIC_M3U_NAME set, "<name>.m3u8" lists the library's Liked Music
songs newest first, and every LIKED_MUSIC_PLAYLISTS entry gets "<Name>.m3u8"
in the playlist's own order. Entries are plain filenames, so any player that
reads M3U (Poweramp, Strawberry, Navidrome, a Syncthing copy on a phone)
resolves them next to the file. A file is rewritten only when its content
changed, so a run with nothing new doesn't make every device re-sync it.

Hand additions: "<Name>.extra.m3u8" kept in LIKED_MUSIC_DIR (one library
filename per line, # comments allowed) is appended to that playlist, and its
songs are protected from the unlike removal too. Use it for songs YouTube
Music can't hold any more (videos deleted from YouTube, still in the library).
"""

import hashlib
import os
from typing import Callable, Dict, List, Set

from .liked_music_state import (INDEX_NAME, LIKED_SNAPSHOT_NAME, M3U_HASHES_NAME, PLAYLIST_IDS_NAME,
                                load_state, save_state)

M3U_EXTENSION = ".m3u8"
EXTRA_SUFFIX = ".extra.m3u8"
Writer = Callable[[str, str], None]  # (filename, content) into the library folder


def render_m3u(filenames: List[str]) -> str:
    """M3U text with one plain filename per line.

    Example: render_m3u(["A - B.opus"]) -> "#EXTM3U\\nA - B.opus\\n"
    """
    return "#EXTM3U\n" + "".join(name + "\n" for name in filenames)


def playlist_members(folder: str, liked_name: str) -> Dict[str, List[str]]:
    """{playlist name: library filenames in order} for Liked Music (if named) and every mirrored playlist.

    Example: playlist_members("/staging", "YouTube Liked")["YouTube Liked"][0] -> "Newest - Like.opus"
    """
    index: Dict[str, str] = load_state(folder, INDEX_NAME, {})
    members: Dict[str, List[str]] = {}
    if liked_name:
        members[liked_name] = _names(load_state(folder, LIKED_SNAPSHOT_NAME, []), index)
    mirrored: Dict[str, List[str]] = load_state(folder, PLAYLIST_IDS_NAME, {})
    for name, ids in mirrored.items():
        members[name] = list(dict.fromkeys(_names(ids, index) + extra_members(folder, name)))
    return members


def _names(video_ids: List[str], index: Dict[str, str]) -> List[str]:
    return [index[vid] for vid in dict.fromkeys(video_ids) if vid in index]


def extra_members(folder: str, name: str) -> List[str]:
    """Filenames listed by hand in "<name>.extra.m3u8", or [] without the file."""
    path = os.path.join(folder, name + EXTRA_SUFFIX)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as extra:
        return [line.strip() for line in extra if line.strip() and not line.startswith("#")]


def protected_ids(folder: str) -> Set[str]:
    """Ids of songs in a mirrored playlist or its .extra list: never queued as unliked.

    Example: protected_ids("/staging") -> {"dQw4w9WgXcQ"}
    """
    mirrored: Dict[str, List[str]] = load_state(folder, PLAYLIST_IDS_NAME, {})
    ids = {vid for playlist in mirrored.values() for vid in playlist}
    index: Dict[str, str] = load_state(folder, INDEX_NAME, {})
    by_name = {filename: vid for vid, filename in index.items()}
    for name in mirrored:
        ids.update(by_name[f] for f in extra_members(folder, name) if f in by_name)
    return ids


def write_playlists(folder: str, liked_name: str, writer: Writer) -> List[str]:
    """Write every playlist file whose content changed; returns the files written.

    Example: write_playlists("/staging", "YouTube Liked", local_writer("/staging")) -> ["YouTube Liked.m3u8"]
    """
    written_hashes: Dict[str, str] = load_state(folder, M3U_HASHES_NAME, {})
    written = []
    for name, filenames in playlist_members(folder, liked_name).items():
        content = render_m3u(filenames)
        digest = hashlib.sha1(content.encode("utf-8")).hexdigest()
        filename = name + M3U_EXTENSION
        if written_hashes.get(filename) == digest:
            continue
        writer(filename, content)
        written_hashes[filename] = digest
        save_state(folder, M3U_HASHES_NAME, written_hashes)
        written.append(filename)
    return written


def local_writer(folder: str) -> Writer:
    """Writer for a library that lives in LIKED_MUSIC_DIR itself (no push target)."""
    def write(filename: str, content: str) -> None:
        path = os.path.join(folder, filename)
        with open(path + ".tmp", "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(path + ".tmp", path)
    return write
