"""The yt-dlp command lines of the liked-music sync, and the environment they run in."""

import os
from typing import List

from .liked_music_settings import LikedMusicSettings
from .liked_music_state import ARCHIVE_NAME

# Same layout as the manual download script: byte-limited names because long
# Japanese titles otherwise exceed the 255-byte filename limit.
OUTPUT_TEMPLATE = "%(artist,uploader).60B - %(title).150B.%(ext)s"
VIDEO_URL = "https://music.youtube.com/watch?v={}"



def ytdlp_options(settings: LikedMusicSettings) -> List[str]:
    """yt-dlp arguments shared by every download: audio only, tags, archive, output names."""
    folder = settings.folder
    return [
        settings.ytdlp_bin,
        "--cookies-from-browser", settings.browser, "--ignore-errors",
        "-x", "--embed-thumbnail", "--embed-metadata",
        "--sleep-requests", "0.3", "--sleep-interval", "1", "--max-sleep-interval", "3",
        "--download-archive", os.path.join(folder, ARCHIVE_NAME),
        "-o", os.path.join(folder, OUTPUT_TEMPLATE),
        "--print", "after_move:%(id)s\t%(filepath)s",
    ]


def build_ytdlp_command(settings: LikedMusicSettings) -> List[str]:
    """yt-dlp arguments that fetch only the songs missing from the archive.

    Example: build_ytdlp_command(s)[0] == s.ytdlp_bin
    """
    return ytdlp_options(settings) + ["--yes-playlist", "--lazy-playlist", "--break-on-existing",
                                      settings.playlist_url]


def build_ytdlp_video_command(settings: LikedMusicSettings, video_ids: List[str]) -> List[str]:
    """yt-dlp arguments that fetch these videos (songs of a mirrored playlist).

    Example: build_ytdlp_video_command(s, ["dQw4w9WgXcQ"])[-1] -> ".../watch?v=dQw4w9WgXcQ"
    """
    return ytdlp_options(settings) + ["--no-playlist"] + [VIDEO_URL.format(vid) for vid in video_ids]


def ytdlp_environment(extra_path: str) -> dict:
    """Process environment with extra_path prepended to PATH.

    yt-dlp needs a JavaScript runtime (deno) for YouTube's challenges, and a
    systemd user service doesn't see the PATH additions of a login shell.
    """
    env = dict(os.environ)
    if extra_path:
        env["PATH"] = extra_path + os.pathsep + env.get("PATH", "")
    return env
