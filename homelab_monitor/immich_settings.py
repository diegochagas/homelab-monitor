"""Immich settings from monitor.env, shared by the daily check and the review tools.

Required for anything Immich: IMMICH_URL, IMMICH_API_KEY, IMMICH_TIMEZONE.
The review page also needs IMMICH_REVIEW_DIR; classify the IMMICH_OLLAMA_* keys;
embed the IMMICH_EMBED_* keys. Each tool checks only what it uses.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE_KEYS = ("IMMICH_URL", "IMMICH_API_KEY", "IMMICH_TIMEZONE")
REVIEW_KEYS = ("IMMICH_REVIEW_DIR",)
OLLAMA_KEYS = ("IMMICH_OLLAMA_URL", "IMMICH_OLLAMA_MODEL")
EMBED_KEYS = ("IMMICH_EMBED_SSH", "IMMICH_LIBRARY_ROOT", "IMMICH_CONTAINER_ROOT",
              "IMMICH_EMBED_WORK_DIR")


@dataclass(frozen=True)
class ImmichSettings:
    url: str
    api_key: str
    tz: ZoneInfo
    web_url: str  # link shown in Telegram (Immich's own pages); "" = IMMICH_URL
    review_days: int
    review_port: int
    check_hour: int  # local hour of the daily check on the central server
    raw: Mapping[str, str]

    @property
    def review_dir(self) -> Path:
        return Path(require(self.raw, REVIEW_KEYS)["IMMICH_REVIEW_DIR"])


def require(env: Mapping[str, str], keys: tuple) -> dict:
    """The values of `keys`; ValueError naming every missing one.

    Example: require(os.environ, ("IMMICH_URL",)) -> {"IMMICH_URL": "http://..."}
    """
    missing = [key for key in keys if not env.get(key, "").strip()]
    if missing:
        raise ValueError(f"monitor.env: missing or empty {missing}, expected all of {list(keys)}")
    return {key: env[key].strip() for key in keys}


def immich_settings(env: Mapping[str, str] = os.environ) -> ImmichSettings:
    """Settings for any Immich feature; ValueError when the base keys are missing or wrong."""
    base = require(env, BASE_KEYS)
    try:
        tz = ZoneInfo(base["IMMICH_TIMEZONE"])
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"IMMICH_TIMEZONE {base['IMMICH_TIMEZONE']!r} is not an IANA zone like Area/City") from error
    return ImmichSettings(
        url=base["IMMICH_URL"].rstrip("/"), api_key=base["IMMICH_API_KEY"], tz=tz,
        web_url=(env.get("IMMICH_WEB_URL", "").strip() or base["IMMICH_URL"]).rstrip("/"),
        review_days=_number(env, "IMMICH_REVIEW_DAYS", 365),
        review_port=_number(env, "IMMICH_REVIEW_PORT", 3070),
        check_hour=_number(env, "IMMICH_CHECK_HOUR", 9), raw=dict(env),
    )


def immich_enabled(env: Mapping[str, str] = os.environ) -> bool:
    """The daily Immich check runs only when IMMICH_URL is set."""
    return bool(env.get("IMMICH_URL", "").strip())


def _number(env: Mapping[str, str], key: str, default: int) -> int:
    text = env.get(key, "").strip()
    if not text:
        return default
    if not text.isdigit():
        raise ValueError(f"{key} {text!r} is not a whole number")
    return int(text)
