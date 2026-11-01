"""Mirror other YouTube Music playlists into the library (agent side).

LIKED_MUSIC_PLAYLISTS names playlists besides Liked Music whose songs belong in
the library too, e.g. "Christians=https://music.youtube.com/playlist?list=PL...".
Every run lists each playlist (video ids, in the playlist's order), saves the
lists in .playlist-ids.json and downloads the songs the library doesn't have.
The lists feed the .m3u8 playlist files (liked_music_m3u) and protect their
songs from the unlike removal: a song that leaves Liked Music but is still in
a mirrored playlist stays.

Songs YouTube no longer serves are tried again on every run (one request
each). Trouble here is reported but never counts as a failed sync.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List

from .liked_music_remote import LikedListingError
from .liked_music_state import INDEX_NAME, PLAYLIST_IDS_NAME, load_state, record_downloads, save_state

ListIds = Callable[[str], List[str]]  # playlist url -> video ids in playlist order
DownloadIds = Callable[[List[str]], Dict[str, str]]  # video ids -> {id: filename} of those that got through


@dataclass
class PlaylistSource:
    name: str  # playlist file name: "Christians" -> Christians.m3u8
    url: str


@dataclass
class MirrorOutcome:
    downloaded: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)


def parse_playlist_sources(spec: str) -> List[PlaylistSource]:
    """"Name=url;Other=url" -> sources; raises ValueError naming the bad entry.

    Example: parse_playlist_sources("Christians=https://music.youtube.com/playlist?list=PLx")[0].name
             -> "Christians"
    """
    sources = []
    for entry in (part.strip() for part in spec.split(";") if part.strip()):
        name, _, url = entry.partition("=")
        name, url = name.strip(), url.strip()
        if not is_plain_name(name) or not url.startswith("http"):
            raise ValueError(f"LIKED_MUSIC_PLAYLISTS entry {entry!r}: expected Name=https://...")
        sources.append(PlaylistSource(name, url))
    return sources


def is_plain_name(name: str) -> bool:
    """A playlist name usable as a file name next to the songs."""
    return bool(name) and "/" not in name and not name.startswith(".")


def mirror_playlist_sources(folder: str, sources: List[PlaylistSource], list_ids: ListIds,
                            download_ids: DownloadIds) -> MirrorOutcome:
    """List every source playlist, save the lists, download the songs still missing.

    A playlist whose listing fails keeps its list from the last run.
    Example: mirror_playlist_sources(d, [src], lister, downloader).downloaded -> {"Christians": 2}
    """
    outcome = MirrorOutcome()
    saved: Dict[str, List[str]] = load_state(folder, PLAYLIST_IDS_NAME, {})
    for source in sources:
        try:
            ids = list(dict.fromkeys(list_ids(source.url)))
        except LikedListingError as error:
            outcome.errors.append(f"{source.name}: {error}")
            continue
        saved[source.name] = ids
        _download_missing(folder, source.name, ids, download_ids, outcome)
    wanted = {source.name for source in sources}
    save_state(folder, PLAYLIST_IDS_NAME, {name: ids for name, ids in saved.items() if name in wanted})
    return outcome


def _download_missing(folder: str, name: str, ids: List[str], download_ids: DownloadIds,
                      outcome: MirrorOutcome) -> None:
    index: Dict[str, str] = load_state(folder, INDEX_NAME, {})
    missing = [vid for vid in ids if vid not in index]
    outcome.downloaded[name] = 0
    if not missing:
        return
    try:
        downloads = download_ids(missing)
    except RuntimeError as error:
        outcome.errors.append(f"{name}: {error}")
        return
    record_downloads(folder, downloads, liked=False)
    outcome.downloaded[name] = len(downloads)
    if len(downloads) < len(missing):
        outcome.errors.append(f"{name}: {len(missing) - len(downloads)} of {len(missing)} missing song(s) "
                              "could not be downloaded")
