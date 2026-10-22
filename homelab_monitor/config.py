"""Configuration loaded from environment variables (see monitor.env.example)."""

import os
from dataclasses import dataclass, field


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _int(name, default):
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _list(name, default=""):
    raw = _env(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _int_list(name):
    result = []
    for item in _list(name):
        try:
            result.append(int(item))
        except ValueError:
            pass
    return result


@dataclass
class Config:
    # Identity
    node_name: str = field(default_factory=lambda: _env("NODE_NAME", "central"))

    # Telegram
    bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    allowed_user_ids: list = field(default_factory=lambda: _int_list("TELEGRAM_ALLOWED_USER_IDS"))
    allowed_chat_ids: list = field(default_factory=lambda: _int_list("TELEGRAM_ALLOWED_CHAT_IDS"))

    # Paths
    host_root: str = field(default_factory=lambda: _env("HOST_ROOT", "").rstrip("/"))
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "./data"))

    # Agent HTTP endpoint (central)
    listen_host: str = field(default_factory=lambda: _env("LISTEN_HOST", "0.0.0.0"))
    listen_port: int = field(default_factory=lambda: _int("LISTEN_PORT", 8787))
    registration_key: str = field(default_factory=lambda: _env("REGISTRATION_KEY"))

    # Agent (remote node)
    central_url: str = field(default_factory=lambda: _env("CENTRAL_URL").rstrip("/"))
    agent_id: str = field(default_factory=lambda: _env("AGENT_ID"))
    agent_token: str = field(default_factory=lambda: _env("AGENT_TOKEN"))
    heartbeat_interval: int = field(default_factory=lambda: _int("HEARTBEAT_INTERVAL", 60))

    # Monitoring
    check_interval: int = field(default_factory=lambda: _int("CHECK_INTERVAL", 60))
    alert_repeat_seconds: int = field(default_factory=lambda: _int("ALERT_REPEAT_SECONDS", 3600))
    alert_confirmations: int = field(default_factory=lambda: _int("ALERT_CONFIRMATIONS", 2))
    offline_after_missed: int = field(default_factory=lambda: _int("OFFLINE_AFTER_MISSED", 3))

    # Thresholds
    cpu_percent: float = field(default_factory=lambda: _float("ALERT_CPU_PERCENT", 90))
    memory_percent: float = field(default_factory=lambda: _float("ALERT_MEMORY_PERCENT", 90))
    disk_percent: float = field(default_factory=lambda: _float("ALERT_DISK_PERCENT", 90))
    temp_c: float = field(default_factory=lambda: _float("ALERT_TEMP_C", 80))
    disk_ignore: list = field(default_factory=lambda: _list("DISK_IGNORE"))
    backup_only_nodes: list = field(default_factory=lambda: [n.lower() for n in _list("BACKUP_ONLY_NODES")])

    # Backups
    server_backup_log: str = field(default_factory=lambda: _env("SERVER_BACKUP_LOG"))
    backup_log_dir: str = field(default_factory=lambda: _env("BACKUP_LOG_DIR"))
    backup_max_age_hours: int = field(default_factory=lambda: _int("BACKUP_MAX_AGE_HOURS", 48))

    # Liked music sync (agent): download new YouTube Music likes into a folder.
    # Off unless LIKED_MUSIC_DIR is set. LM is YouTube's id for every account's
    # own Liked Music playlist, not a personal value.
    liked_music_dir: str = field(default_factory=lambda: _env("LIKED_MUSIC_DIR"))
    liked_music_playlist: str = field(
        default_factory=lambda: _env("LIKED_MUSIC_PLAYLIST", "https://music.youtube.com/playlist?list=LM")
    )
    liked_music_browser: str = field(default_factory=lambda: _env("LIKED_MUSIC_BROWSER", "firefox"))
    liked_music_ytdlp: str = field(default_factory=lambda: _env("LIKED_MUSIC_YTDLP", "yt-dlp"))
    liked_music_interval_hours: float = field(default_factory=lambda: _float("LIKED_MUSIC_INTERVAL_HOURS", 6))
    liked_music_timeout_minutes: int = field(default_factory=lambda: _int("LIKED_MUSIC_TIMEOUT_MINUTES", 120))
    liked_music_extra_path: str = field(default_factory=lambda: _env("LIKED_MUSIC_EXTRA_PATH"))
    # Optional upload of the downloads to the music server (rsync over SSH,
    # e.g. user@server:/path/Music/). Local copies are removed only after
    # rsync confirms each transfer; LIKED_MUSIC_DIR keeps the archive.
    liked_music_push_target: str = field(default_factory=lambda: _env("LIKED_MUSIC_PUSH_TARGET"))
    liked_music_push_rsync_path: str = field(default_factory=lambda: _env("LIKED_MUSIC_PUSH_RSYNC_PATH"))
    liked_music_push_chmod: str = field(default_factory=lambda: _env("LIKED_MUSIC_PUSH_CHMOD"))
    # Unlike sync: songs that leave Liked Music are removed from the server
    # playlists and their files deleted. Off unless LIKED_MUSIC_REMOVE_UNLIKED=true;
    # more than LIKED_MUSIC_MAX_REMOVALS vanishing at once deletes nothing.
    liked_music_remove_unliked: bool = field(
        default_factory=lambda: _env("LIKED_MUSIC_REMOVE_UNLIKED", "false").lower() in ("1", "true", "yes")
    )
    liked_music_max_removals: int = field(default_factory=lambda: _int("LIKED_MUSIC_MAX_REMOVALS", 25))
    # Optional playlist servers (each off unless its URL is set). *_DIR is the
    # music folder as that server sees it (containers mount it elsewhere).
    liked_music_jellyfin_url: str = field(default_factory=lambda: _env("LIKED_MUSIC_JELLYFIN_URL").rstrip("/"))
    liked_music_jellyfin_api_key: str = field(default_factory=lambda: _env("LIKED_MUSIC_JELLYFIN_API_KEY"))
    liked_music_jellyfin_user: str = field(default_factory=lambda: _env("LIKED_MUSIC_JELLYFIN_USER"))
    liked_music_jellyfin_dir: str = field(default_factory=lambda: _env("LIKED_MUSIC_JELLYFIN_DIR"))
    liked_music_jellyfin_playlist: str = field(default_factory=lambda: _env("LIKED_MUSIC_JELLYFIN_PLAYLIST"))
    liked_music_swing_url: str = field(default_factory=lambda: _env("LIKED_MUSIC_SWING_URL").rstrip("/"))
    liked_music_swing_user: str = field(default_factory=lambda: _env("LIKED_MUSIC_SWING_USER"))
    liked_music_swing_password: str = field(default_factory=lambda: _env("LIKED_MUSIC_SWING_PASSWORD"))
    liked_music_swing_dir: str = field(default_factory=lambda: _env("LIKED_MUSIC_SWING_DIR"))
    liked_music_swing_playlist: str = field(default_factory=lambda: _env("LIKED_MUSIC_SWING_PLAYLIST"))

    def host_path(self, path):
        """Map a host filesystem path to where it is visible from here."""
        if not self.host_root:
            return path
        return self.host_root + path

    def validate_central(self):
        problems = []
        if not self.bot_token:
            problems.append("TELEGRAM_BOT_TOKEN is not set")
        if not self.chat_id:
            problems.append("TELEGRAM_CHAT_ID is not set")
        if not self.allowed_user_ids and not self.allowed_chat_ids:
            problems.append("TELEGRAM_ALLOWED_USER_IDS is not set — the bot would answer nobody")
        if not self.registration_key:
            problems.append("REGISTRATION_KEY is not set — agents could not register")
        return problems

    def validate_agent(self):
        problems = []
        if not self.central_url:
            problems.append("CENTRAL_URL is not set")
        if not self.agent_id:
            problems.append("AGENT_ID is not set")
        if not self.agent_token:
            problems.append("AGENT_TOKEN is not set")
        if not self.registration_key:
            problems.append("REGISTRATION_KEY is not set")
        return problems


def load_env_file(path):
    """Load KEY=VALUE lines into os.environ (without overriding existing vars)."""
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)
