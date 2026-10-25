"""Build the playlist clients and reconcile dependencies from the config.

Each server is optional: Jellyfin needs LIKED_MUSIC_JELLYFIN_URL, Swing needs
LIKED_MUSIC_SWING_URL. A server with a URL but a missing field is reported
(with the field's name) instead of half-working.
"""

import functools
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional

from .config import Config
from .jellyfin_playlists import JellyfinPlaylists, JellyfinSettings
from .liked_music_reconcile import ReconcileDeps
from .liked_music_lyrics import LyricsOutcome, add_missing_lyrics
from .liked_music_remote import delete_remote_files, list_liked_ids, write_remote_file
from .lyrics_lrclib import LrclibClient
from .swing_playlists import SwingPlaylists, SwingSettings

Runner = Callable[..., subprocess.CompletedProcess]
LISTING_TIMEOUT_SECONDS = 900


@dataclass
class UnlikeSettings:
    remove_unliked: bool = False
    max_removals: int = 25
    jellyfin: Optional[JellyfinSettings] = None
    swing: Optional[SwingSettings] = None
    lyrics: bool = False
    lyrics_per_run: int = 150


def unlike_settings_from_config(cfg: Config) -> UnlikeSettings:
    """Unlike-sync and playlist settings; raises ValueError naming a missing field.

    Example: unlike_settings_from_config(Config()).jellyfin -> None (no URL set)
    """
    return UnlikeSettings(
        remove_unliked=cfg.liked_music_remove_unliked,
        max_removals=cfg.liked_music_max_removals,
        jellyfin=_jellyfin_settings(cfg),
        swing=_swing_settings(cfg),
        lyrics=cfg.liked_music_lyrics,
        lyrics_per_run=cfg.liked_music_lyrics_per_run,
    )


def _jellyfin_settings(cfg: Config) -> Optional[JellyfinSettings]:
    if not cfg.liked_music_jellyfin_url:
        return None
    settings = JellyfinSettings(cfg.liked_music_jellyfin_url, cfg.liked_music_jellyfin_api_key,
                                cfg.liked_music_jellyfin_user, cfg.liked_music_jellyfin_dir,
                                cfg.liked_music_jellyfin_playlist)
    _require("LIKED_MUSIC_JELLYFIN", api_key=settings.api_key, user=settings.username,
             dir=settings.music_dir, playlist=settings.liked_playlist)
    return settings


def _swing_settings(cfg: Config) -> Optional[SwingSettings]:
    if not cfg.liked_music_swing_url:
        return None
    settings = SwingSettings(cfg.liked_music_swing_url, cfg.liked_music_swing_user,
                             cfg.liked_music_swing_password, cfg.liked_music_swing_dir,
                             cfg.liked_music_swing_playlist)
    _require("LIKED_MUSIC_SWING", user=settings.username, password=settings.password,
             dir=settings.music_dir, playlist=settings.liked_playlist)
    return settings


def _require(prefix: str, **values: str) -> None:
    missing = [f"{prefix}_{name.upper()}" for name, value in values.items() if not value]
    if missing:
        raise ValueError(f"{prefix}_URL is set but {', '.join(missing)} is empty")


def build_reconcile_deps(folder: str, ytdlp: List[str], push_target: str, unlike: UnlikeSettings,
                         env: dict, runner: Runner = subprocess.run) -> ReconcileDeps:
    """Wire the real listing, deletion and playlist clients.

    ytdlp is [binary, browser, playlist_url]. Without a push target the songs
    only live in `folder`, so there is nothing remote to delete.
    """
    binary, browser, playlist_url = ytdlp
    list_ids = functools.partial(list_liked_ids, binary, browser, playlist_url, env,
                                 LISTING_TIMEOUT_SECONDS, runner)
    if push_target:
        delete_files = functools.partial(delete_remote_files, push_target, runner=runner)
    else:
        delete_files = _no_remote_files
    return ReconcileDeps(
        folder=folder, max_removals=unlike.max_removals, list_ids=list_ids, delete_files=delete_files,
        remove_unliked=unlike.remove_unliked,
        jellyfin=JellyfinPlaylists(unlike.jellyfin) if unlike.jellyfin else None,
        swing=SwingPlaylists(unlike.swing) if unlike.swing else None,
    )


def lyrics_step(folder: str, jellyfin: JellyfinSettings, push_target: str, limit: int,
                runner: Runner = subprocess.run) -> LyricsOutcome:
    """Wire lrclib, Jellyfin and the ssh writer into add_missing_lyrics."""
    writer = functools.partial(write_remote_file, push_target, runner=runner)
    return add_missing_lyrics(folder, JellyfinPlaylists(jellyfin), LrclibClient().find, writer, limit)


def _no_remote_files(filenames: List[str]) -> None:
    """Local-only library: reconcile deletes the staged copies itself."""
