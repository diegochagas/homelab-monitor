"""Add lyrics files next to the liked songs on the music server.

For every song in the library index that Jellyfin has indexed and that has no
lyrics yet, look the lyrics up on lrclib.net and write "<song name>.lrc" next to
the audio file on the push target. Jellyfin (and Finamp through it) and Swing
Music both read such a sidecar: synced LRC lines scroll with the song, plain
lines show as text. Jellyfin's metadata (title, artist, album, duration) drives
the lookup, so a song is only tried once Jellyfin has scanned it.

Each run looks up at most LIKED_MUSIC_LYRICS_PER_RUN songs; misses are retried
after RETRY_MISSES_DAYS. One-off backfill of the whole library:
    python3 -m homelab_monitor.liked_music_lyrics monitor.env --limit 5000
"""

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .http_json import HttpJsonError
from .liked_music_state import INDEX_NAME, LYRICS_STATE_NAME, load_state, save_state
from .lyrics_lrclib import LookupIncomplete, LrclibClient, Lyrics, SongMeta

RETRY_MISSES_DAYS = 30
DEFERRED_RETRY_HOURS = 12  # songs whose outside lookup was busy; later, not 30 days
SAVE_EVERY = 25  # lookups between state saves, so an interrupted backfill resumes
BACKFILL_BATCH = 50
BUSY_COOLDOWN_SECONDS = 900  # after a batch failed on a busy lrclib (its busy spells last hours)
MAX_FAILED_BATCHES = 96  # about a day of failing batches in a row, then give up

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class LyricsOutcome:
    written: List[str] = field(default_factory=list)
    misses: int = 0
    errors: List[str] = field(default_factory=list)
    deferred: int = 0  # lrclib's outside lookup was busy; retried after DEFERRED_RETRY_HOURS


def lrc_name(filename: str) -> str:
    """Sidecar name for a song file.

    Example: lrc_name("A - B.opus") -> "A - B.lrc"
    """
    return os.path.splitext(filename)[0] + ".lrc"


def due_songs(index: Dict[str, str], checked: Dict[str, dict], now: float) -> List[str]:
    """Video ids never looked up, or whose miss (or deferral) is old enough to try again."""
    retry_after = {"none": RETRY_MISSES_DAYS * 86400, "deferred": DEFERRED_RETRY_HOURS * 3600}
    return [vid for vid in index if vid not in checked
            or now - checked[vid].get("at", 0) > retry_after.get(checked[vid].get("result"), float("inf"))]


def add_missing_lyrics(folder: str, jellyfin, lookup: Callable[[SongMeta], Optional[Lyrics]],
                       write_file: Callable[[str, str], None], limit: int,
                       clock: Callable[[], float] = time.time) -> LyricsOutcome:
    """Look up and write lyrics for up to `limit` songs; stops early if lrclib is unreachable.

    Example: add_missing_lyrics(folder, jf, LrclibClient().find, writer, 150).written -> ["A - B.opus"]
    """
    outcome = LyricsOutcome()
    index: Dict[str, str] = load_state(folder, INDEX_NAME, {})
    checked: Dict[str, dict] = load_state(folder, LYRICS_STATE_NAME, {})
    due = due_songs(index, checked, clock())
    songs = jellyfin.audio_metadata() if due else {}  # one Jellyfin call, skipped when nothing is due
    for done, vid in enumerate([v for v in due if index[v] in songs][:limit], start=1):
        if not _lyrics_for_song(index[vid], songs[index[vid]], (jellyfin, lookup, write_file), checked, vid, outcome):
            break
        checked[vid]["at"] = clock()
        if done % SAVE_EVERY == 0:
            save_state(folder, LYRICS_STATE_NAME, checked)
    save_state(folder, LYRICS_STATE_NAME, checked)
    return outcome


def _lyrics_for_song(name: str, song: dict, tools: tuple, checked: Dict[str, dict], vid: str,
                     outcome: LyricsOutcome) -> bool:
    """One song; False when the lookup service failed (stop the run, nothing recorded)."""
    jellyfin, lookup, write_file = tools
    if song["has_lyrics"]:
        checked[vid] = {"result": "present"}
        return True
    try:
        result = _fetch_and_write(name, song, jellyfin, lookup, write_file)
    except (HttpJsonError, OSError, RuntimeError) as error:
        outcome.errors.append(f"lyrics for {name!r}: {error}")
        return False
    checked[vid] = {"result": result}
    _count(result, name, outcome)
    return True


def _fetch_and_write(name: str, song: dict, jellyfin, lookup: Callable[[SongMeta], Optional[Lyrics]],
                     write_file: Callable[[str, str], None]) -> str:
    """'synced', 'plain', 'none' or 'deferred'; on a hit writes the .lrc and refreshes Jellyfin."""
    try:
        found = lookup(SongMeta(song["title"], song["artist"], song["album"], song["seconds"]))
    except LookupIncomplete:
        return "deferred"
    if not found:
        return "none"
    write_file(lrc_name(name), found.text)
    jellyfin.refresh_item(song["id"])
    return "synced" if found.synced else "plain"


def _count(result: str, name: str, outcome: LyricsOutcome) -> None:
    if result in ("synced", "plain"):
        outcome.written.append(name)
    elif result == "deferred":
        outcome.deferred += 1
    else:
        outcome.misses += 1


def backfill(folder: str, run_batch: Callable[[int], LyricsOutcome], take_lock: Callable[[], object],
             limit: int, sleep: Callable[[float], None] = time.sleep) -> LyricsOutcome:
    """Work through the library in batches, cooling down while lrclib is busy; stops when nothing is due.

    Example: backfill(folder, lambda n: lyrics_step(...), lambda: try_sync_lock(folder), 5000)
    """
    total, failures = LyricsOutcome(), 0
    while len(total.written) + total.misses < limit and failures < MAX_FAILED_BATCHES:
        batch = _locked_batch(run_batch, take_lock, sleep)
        if batch is None:
            continue
        _add_batch(total, batch)
        failures = failures + 1 if batch.errors else 0
        if batch.errors:
            total.errors = batch.errors
            sleep(BUSY_COOLDOWN_SECONDS)
        elif not batch.written and not batch.misses and not batch.deferred:
            break
    return total


def _add_batch(total: LyricsOutcome, batch: LyricsOutcome) -> None:
    total.written += batch.written
    total.misses += batch.misses
    total.deferred += batch.deferred
    print(f"lyrics: {len(total.written)} written, {total.misses} not found, {total.deferred} deferred", flush=True)


def _locked_batch(run_batch: Callable[[int], LyricsOutcome], take_lock: Callable[[], object],
                  sleep: Callable[[float], None]) -> Optional[LyricsOutcome]:
    """One batch under the sync lock (held per batch, so the agent's downloads run in between)."""
    lock = take_lock()
    if lock is None:  # a sync is running; let it finish
        sleep(60)
        return None
    with lock:
        return run_batch(BACKFILL_BATCH)


def main() -> None:
    """Backfill: python3 -m homelab_monitor.liked_music_lyrics monitor.env [--limit N]"""
    from .config import Config, load_env_file
    from .liked_music import settings_from_config, try_sync_lock
    from .liked_music_servers import lyrics_step

    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        load_env_file(sys.argv[1])
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 5000
    settings = settings_from_config(Config())
    if settings is None or settings.unlike.jellyfin is None or not settings.push_target:
        sys.exit("lyrics need LIKED_MUSIC_DIR, LIKED_MUSIC_PUSH_TARGET and the LIKED_MUSIC_JELLYFIN_* settings")
    outcome = backfill(settings.folder, lambda n: lyrics_step(settings.folder, settings.unlike.jellyfin,
                                                              settings.push_target, n),
                       lambda: try_sync_lock(settings.folder), limit)
    print(f"done: lyrics written: {len(outcome.written)}, not found: {outcome.misses}")
    for error in outcome.errors:
        print(f"last error: {error}")


if __name__ == "__main__":
    main()
