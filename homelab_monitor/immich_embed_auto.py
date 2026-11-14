"""`scripts/immich-embed-auto [--dry-run]`: daily copy of Immich date/location edits into the originals.

Runs from a laptop systemd timer (scripts/install-immich-embed-timer). Each run:
lists the library's XMP sidecars, takes the ones written since the last run, stages
those photos, writes the sidecar's date/GPS with exiftool, verifies, pushes only the
verified files, then sends a Telegram summary. Failed photos stay unrecorded so the
next run retries them. Run folders older than KEEP_DAYS are deleted.
"""

import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import load_env_file
from .immich_embed import embed_config, verify
from .immich_embed_plan import EmbedItem, exiftool_args
from .immich_embed_steps import EmbedConfig, push_command, run, save_plan, stage
from .immich_settings import ImmichSettings, immich_settings
from .immich_sidecars import changed_sidecars, item_from_sidecar, media_for, parse_listing
from .telegram import Telegram
from .telegram import escape as e

KEEP_DAYS = 30  # run folders (with their original/ backups) kept this long
MAX_FILES = 300  # more changed sidecars than this (e.g. Immich rewrote them all) -> ask for a manual run
STATE_FILE = "embedded-sidecars.json"


@dataclass
class SyncState:
    baseline: datetime  # sidecars written before this were handled by hand (or predate the sync)
    done: dict[str, str] = field(default_factory=dict)  # sidecar path -> mtime it was embedded at


@dataclass
class SyncReport:
    pushed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # no date/GPS in the sidecar, or no photo found


def load_state(review_dir: Path, now: datetime) -> SyncState:
    """State file, created with baseline=now on the very first run (only later edits are synced)."""
    path = review_dir / STATE_FILE
    if not path.is_file():
        state = SyncState(baseline=now.astimezone(UTC))
        save_state(review_dir, state)
        return state
    raw = json.loads(path.read_text())
    return SyncState(datetime.fromisoformat(raw["baseline"]), raw.get("done", {}))


def save_state(review_dir: Path, state: SyncState) -> None:
    payload = {"baseline": state.baseline.isoformat(), "done": state.done}
    temp = review_dir / f"{STATE_FILE}.tmp"
    temp.write_text(json.dumps(payload, indent=1, sort_keys=True))
    temp.replace(review_dir / STATE_FILE)


def list_library(embed: EmbedConfig) -> dict[str, datetime]:
    """Every file under the library root with its mtime (TZ=UTC so the listing is in UTC)."""
    command = ["env", "TZ=UTC", "rsync", "--list-only", "-r", "-s", "--rsync-path=sudo rsync"]
    return parse_listing(run([*command, f"{embed.ssh}:{embed.library_root}/"]).stdout)


def fetch_sidecars(embed: EmbedConfig, sidecars: list[str]) -> Path:
    target = embed.work_dir / "sidecars"
    target.mkdir(parents=True, exist_ok=True)
    (embed.work_dir / "sidecars.txt").write_text("".join(f"{rel}\n" for rel in sidecars))
    command = ["rsync", "-a", "-s", "--no-owner", "--no-group", "--rsync-path=sudo rsync"]
    files_from = f"--files-from={embed.work_dir / 'sidecars.txt'}"
    run([*command, files_from, f"{embed.ssh}:{embed.library_root}/", f"{target}/"])
    return target


def plan_from_sidecars(
    folder: Path, sidecars: list[str], files: dict[str, datetime], settings: ImmichSettings
) -> tuple[dict[str, EmbedItem], list[str]]:
    """{sidecar: item} for sidecars with something to write, plus the ones skipped."""
    planned, skipped = {}, []
    for sidecar in sidecars:
        media = media_for(sidecar, files)
        item = item_from_sidecar((folder / sidecar).read_text(), media, settings.tz) if media else None
        if item is None:
            skipped.append(sidecar)
        else:
            planned[sidecar] = item
    return planned, skipped


def write_each(embed: EmbedConfig, items: list[EmbedItem]) -> list[str]:
    """exiftool on every edited copy; a file exiftool rejects is reported, not fatal to the run."""
    rejected = []
    for item in items:
        try:
            run(["exiftool", *exiftool_args(item), str(embed.work_dir / "edited" / item.rel_path)])
        except RuntimeError as error:
            print(f"{item.rel_path}: exiftool failed: {error}")
            rejected.append(item.rel_path)
    return rejected


def embed_planned(embed: EmbedConfig, planned: dict[str, EmbedItem]) -> set[str]:
    """Stage, write, verify and push; returns the photos (rel paths) now on the server."""
    items = list(planned.values())
    save_plan(items, embed.work_dir)
    stage(embed)
    rejected = set(write_each(embed, items))
    verify(embed.work_dir, items)
    verified_file = embed.work_dir / "verified.txt"
    passed = [rel for rel in verified_file.read_text().splitlines() if rel and rel not in rejected]
    verified_file.write_text("".join(f"{rel}\n" for rel in passed))
    if passed:
        print(run(push_command(embed, apply=True)).stdout)
    return set(passed)


def sync(settings: ImmichSettings, now: datetime, dry_run: bool) -> SyncReport | None:
    """One daily run; None when nothing changed (or a dry run)."""
    embed = embed_config(settings, new_run=True)
    state = load_state(settings.review_dir, now)
    files = list_library(embed)
    sidecars = changed_sidecars(files, state.baseline, state.done)
    print(f"{len(sidecars)} sidecars changed since the last run")
    if not sidecars or dry_run:
        print("\n".join(sidecars[:50]))
        return None
    if len(sidecars) > MAX_FILES:
        raise SystemExit(f"{len(sidecars)} changed sidecars (limit {MAX_FILES}): run the steps by hand")
    planned, skipped = plan_from_sidecars(fetch_sidecars(embed, sidecars), sidecars, files, settings)
    pushed = embed_planned(embed, planned) if planned else set()
    report = record(state, sidecars, files, planned, pushed)
    report.skipped = skipped
    save_state(settings.review_dir, state)
    return report


def record(
    state: SyncState, sidecars: list[str], files: dict[str, datetime],
    planned: dict[str, EmbedItem], pushed: set[str],
) -> SyncReport:
    """Mark pushed (and nothing-to-write) sidecars done; failed ones stay open for tomorrow."""
    report = SyncReport()
    for sidecar in sidecars:
        item = planned.get(sidecar)
        if item is not None and item.rel_path not in pushed:
            report.failed.append(item.rel_path)
            continue
        state.done[sidecar] = files[sidecar].isoformat()
        report.pushed += [item.rel_path] if item else []
    return report


def prune_runs(work_root: Path, now: datetime, keep_days: int = KEEP_DAYS) -> list[Path]:
    """Delete run folders (YYYY-MM-DD_HHMM) older than keep_days; returns what was removed."""
    cutoff = now.replace(tzinfo=None) - timedelta(days=keep_days)
    removed = []
    for folder in sorted(work_root.glob("20*")):
        try:
            started = datetime.strptime(folder.name[:15], "%Y-%m-%d_%H%M")
        except ValueError:
            continue
        if folder.is_dir() and started < cutoff:
            shutil.rmtree(folder)
            removed.append(folder)
    return removed


def describe_sync(report: SyncReport) -> str:
    """Telegram HTML summary of one run."""
    lines = [f"🗂️ <b>Immich metadata sync</b>: {len(report.pushed)} photos updated on the server"]
    for rel in report.pushed[:10]:
        lines.append(f"• <code>{e(rel)}</code>")
    if report.failed:
        lines += ["", f"⚠️ {len(report.failed)} did not pass the check (retried tomorrow):"]
        lines += [f"• <code>{e(rel)}</code>" for rel in report.failed[:10]]
    return "\n".join(lines)


def notify(settings: ImmichSettings, text: str) -> None:
    token, chat = settings.raw.get("TELEGRAM_BOT_TOKEN"), settings.raw.get("TELEGRAM_CHAT_ID")
    if token and chat:
        Telegram(token).send(chat, text)


def main(argv: list[str]) -> int:
    args = [arg for arg in argv[1:] if arg != "--dry-run"]
    if len(args) > 1:
        raise SystemExit(f"usage: scripts/immich-embed-auto [--dry-run] [monitor.env], got {argv[1:]!r}")
    if args:
        load_env_file(args[0])
    settings = immich_settings()
    now = datetime.now(settings.tz)
    report = sync(settings, now, dry_run="--dry-run" in argv)
    if report and (report.pushed or report.failed):
        notify(settings, describe_sync(report))
    if "--dry-run" not in argv:
        removed = prune_runs(embed_config(settings, new_run=True).work_dir.parent, now)
        print(f"removed {len(removed)} run folders older than {KEEP_DAYS} days")
    return 1 if report and report.failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
