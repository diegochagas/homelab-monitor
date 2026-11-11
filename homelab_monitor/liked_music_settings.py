"""Settings and result of the liked-music sync, built from the agent's config."""

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from .config import Config
from .liked_music_servers import UnlikeSettings, unlike_settings_from_config
from .liked_music_sources import PlaylistSource, is_plain_name, parse_playlist_sources

MAX_REPORTED_TITLES = 10


@dataclass
class LikedMusicSettings:
    folder: str
    playlist_url: str
    browser: str
    ytdlp_bin: str
    interval_hours: float
    timeout_minutes: int
    extra_path: str
    push_target: str = ""
    push_rsync_path: str = ""
    push_chmod: str = ""
    unlike: UnlikeSettings = field(default_factory=UnlikeSettings)
    unlike_error: str = ""  # half-configured server: downloads go on, reconcile is skipped
    m3u_name: str = ""  # name of the Liked Music playlist file; "" = none
    sources: List[PlaylistSource] = field(default_factory=list)  # other playlists mirrored into the library
    sources_error: str = ""  # bad LIKED_MUSIC_PLAYLISTS / M3U name: downloads go on, mirror and files are skipped


@dataclass
class LikedSyncResult:
    checked_at: float
    new_titles: List[str] = field(default_factory=list)
    pushed: int = 0
    unliked: List[str] = field(default_factory=list)
    queued: List[str] = field(default_factory=list)
    device_deleted: List[str] = field(default_factory=list)  # deleted on a device, now removed everywhere
    disliked: int = 0  # of those, disliked on YouTube Music this run
    lyrics_added: int = 0
    lyrics_error: Optional[str] = None  # reported, but not a failed sync: no quick retry for a busy lrclib
    playlist_waiting: int = 0
    playlist_added: Dict[str, int] = field(default_factory=dict)
    mirrored: Dict[str, int] = field(default_factory=dict)  # songs downloaded per mirrored playlist
    playlists_written: List[str] = field(default_factory=list)  # .m3u8 files rewritten this run
    playlist_error: Optional[str] = None  # mirror or playlist-file trouble: reported, not a failed sync
    error: Optional[str] = None

    def to_snapshot(self) -> dict:
        """Summary for the heartbeat snapshot, e.g. {"new_count": 2, ...}."""
        summary = asdict(self)
        summary["new_count"] = len(self.new_titles)
        summary["new_titles"] = self.new_titles[:MAX_REPORTED_TITLES]
        summary["unliked"] = self.unliked[:MAX_REPORTED_TITLES]
        summary["queued"] = self.queued[:MAX_REPORTED_TITLES]
        summary["device_deleted"] = self.device_deleted[:MAX_REPORTED_TITLES]
        return summary


def settings_from_config(cfg: Config) -> Optional[LikedMusicSettings]:
    """Settings, or None when LIKED_MUSIC_DIR is unset (feature off).

    Example: settings_from_config(Config()) -> LikedMusicSettings(folder=...)
    """
    if not cfg.liked_music_dir:
        return None
    unlike, unlike_error = _unlike_or_error(cfg)
    sources, sources_error = _sources_or_error(cfg)
    return LikedMusicSettings(
        folder=cfg.liked_music_dir,
        playlist_url=cfg.liked_music_playlist,
        browser=cfg.liked_music_browser,
        ytdlp_bin=cfg.liked_music_ytdlp,
        interval_hours=cfg.liked_music_interval_hours,
        timeout_minutes=cfg.liked_music_timeout_minutes,
        extra_path=cfg.liked_music_extra_path,
        push_target=cfg.liked_music_push_target,
        push_rsync_path=cfg.liked_music_push_rsync_path,
        push_chmod=cfg.liked_music_push_chmod,
        unlike=unlike, unlike_error=unlike_error,
        m3u_name=cfg.liked_music_m3u_name, sources=sources, sources_error=sources_error,
    )


def _unlike_or_error(cfg: Config) -> Tuple[UnlikeSettings, str]:
    try:
        return unlike_settings_from_config(cfg), ""
    except ValueError as error:
        return UnlikeSettings(), str(error)


def _sources_or_error(cfg: Config) -> Tuple[List[PlaylistSource], str]:
    if cfg.liked_music_m3u_name and not is_plain_name(cfg.liked_music_m3u_name):
        return [], f"LIKED_MUSIC_M3U_NAME {cfg.liked_music_m3u_name!r}: expected a plain file name"
    try:
        return parse_playlist_sources(cfg.liked_music_playlists), ""
    except ValueError as error:
        return [], str(error)
