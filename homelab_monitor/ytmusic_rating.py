"""Dislike songs on YouTube Music with the signed-in browser's cookies (thin wrapper).

yt-dlp can't rate songs, so this talks to the same endpoint the YouTube Music
web player uses (POST /youtubei/v1/like/dislike). The request is signed like
the player signs it: an "SAPISIDHASH" header built from the SAPISID cookie.

The cookies come from yt-dlp (--cookies-from-browser, the browser the sync
already reads). They are written to a private temporary folder that is removed
before open_rating_session returns; only the YouTube ones are kept, in memory,
for the calls of one run. A dislike also takes the song out of Liked Music.
"""

import hashlib
import os
import subprocess
import tempfile
import time
import urllib.request
from typing import Callable, Dict

from .http_json import Opener, request_json

Runner = Callable[..., subprocess.CompletedProcess]
DislikeSong = Callable[[str], None]

ORIGIN = "https://music.youtube.com"
DISLIKE_URL = ORIGIN + "/youtubei/v1/like/dislike?alt=json"
COOKIE_DOMAIN = ".youtube.com"
SAPISID_COOKIES = ("SAPISID", "__Secure-3PAPISID")
COOKIE_EXPORT_TIMEOUT_SECONDS = 120
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"


class RatingError(RuntimeError):
    """A song could not be rated (no session, or YouTube Music refused)."""


def parse_youtube_cookies(netscape_text: str) -> Dict[str, str]:
    """{name: value} of the youtube.com cookies in a Netscape cookie file.

    Example: parse_youtube_cookies(".youtube.com\\tTRUE\\t/\\tTRUE\\t0\\tSAPISID\\tabc") -> {"SAPISID": "abc"}
    """
    cookies = {}
    for line in netscape_text.splitlines():
        fields = line.replace("#HttpOnly_", "", 1).split("\t")
        if len(fields) == 7 and fields[0] == COOKIE_DOMAIN:
            cookies[fields[5]] = fields[6]
    return cookies


def export_browser_cookies(ytdlp_bin: str, browser: str, env: dict, runner: Runner = subprocess.run) -> Dict[str, str]:
    """The browser's youtube.com cookies, read through yt-dlp; the cookie file never outlives the call.

    yt-dlp is run without a URL: it loads the browser's cookies, saves them to
    the --cookies file and exits with "You must provide at least one URL",
    so its exit code is ignored and the file is what counts.
    """
    with tempfile.TemporaryDirectory(prefix="ytmusic-cookies-") as private:  # mode 0700
        path = os.path.join(private, "cookies.txt")
        try:
            runner([ytdlp_bin, "--cookies-from-browser", browser, "--cookies", path], capture_output=True,
                   text=True, timeout=COOKIE_EXPORT_TIMEOUT_SECONDS, env=env)
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            raise RatingError(f"reading {browser} cookies with {ytdlp_bin!r} failed: {error}") from error
        if not os.path.isfile(path):
            raise RatingError(f"{ytdlp_bin!r} wrote no cookie file for browser {browser!r}")
        with open(path, encoding="utf-8") as handle:
            return parse_youtube_cookies(handle.read())


def sapisid_authorization(sapisid: str, now: float) -> str:
    """The Authorization header the web player sends.

    Example: sapisid_authorization("abc", 1700000000) -> "SAPISIDHASH 1700000000_<sha1 hex>"
    """
    stamp = str(int(now))
    digest = hashlib.sha1(f"{stamp} {sapisid} {ORIGIN}".encode()).hexdigest()
    return f"SAPISIDHASH {stamp}_{digest}"


def rating_headers(cookies: Dict[str, str], now: float) -> Dict[str, str]:
    """Request headers for a signed-in call; raises RatingError when the browser isn't signed in."""
    sapisid = next((cookies[name] for name in SAPISID_COOKIES if cookies.get(name)), "")
    if not sapisid:
        raise RatingError(f"no YouTube sign-in cookie ({' / '.join(SAPISID_COOKIES)}): "
                          "sign in to YouTube Music in the browser the sync reads")
    return {
        "Authorization": sapisid_authorization(sapisid, now),
        "Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
        "Origin": ORIGIN, "X-Origin": ORIGIN, "X-Goog-AuthUser": "0", "User-Agent": USER_AGENT,
    }


def dislike_request_body(video_id: str, now: float) -> dict:
    """Body of the dislike call; the client version is dated like the web player's."""
    version = time.strftime("1.%Y%m%d.01.00", time.gmtime(now))
    return {"context": {"client": {"clientName": "WEB_REMIX", "clientVersion": version, "hl": "en"}},
            "target": {"videoId": video_id}}


def open_rating_session(ytdlp_bin: str, browser: str, env: dict, runner: Runner = subprocess.run,
                        opener: Opener = urllib.request.urlopen,
                        clock: Callable[[], float] = time.time) -> DislikeSong:
    """Read the cookies once and return dislike(video_id) for this run.

    Example: open_rating_session("yt-dlp", "firefox", env)("dQw4w9WgXcQ")
    """
    cookies = export_browser_cookies(ytdlp_bin, browser, env, runner)
    rating_headers(cookies, clock())  # fail now, before any song is touched, when signed out

    def dislike(video_id: str) -> None:
        now = clock()
        try:
            request_json("POST", DISLIKE_URL, rating_headers(cookies, now),
                         dislike_request_body(video_id, now), opener=opener)
        except RuntimeError as error:  # HttpJsonError: carries the status, never the headers
            raise RatingError(f"disliking {video_id!r} on YouTube Music failed: {error}") from error
    return dislike

