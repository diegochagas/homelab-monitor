"""The steps of `scripts/immich-embed`: plan, stage, write, verify, push (each resumable and re-runnable)."""

import json
import shutil
import subprocess
from pathlib import Path

from dataclasses import dataclass

from .immich_embed_plan import EmbedItem, exiftool_args

RUN_TIMEOUT = 3600  # seconds; rsync of a few GB over Tailscale stays well under this


@dataclass(frozen=True)
class EmbedConfig:
    ssh: str  # user@server for rsync over SSH
    library_root: str  # Immich's upload folder on the server
    container_root: str  # the same folder inside the Immich container
    work_dir: Path  # this run's folder: plan, original/ backups, edited/ copies


def save_plan(items: list[EmbedItem], work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    rows = [
        {**item.__dict__, "taken": item.taken.isoformat() if item.taken else None} for item in items
    ]
    (work / "plan.json").write_text(json.dumps(rows, indent=1))
    (work / "files.txt").write_text("".join(f"{item.rel_path}\n" for item in items))


def load_paths(work: Path) -> list[str]:
    return [line for line in (work / "files.txt").read_text().splitlines() if line]


def run(command: list[str]) -> subprocess.CompletedProcess:
    """Run a command; the error carries the full command and its output."""
    done = subprocess.run(command, check=False, capture_output=True, text=True, timeout=RUN_TIMEOUT)
    if done.returncode != 0:
        raise RuntimeError(f"{command!r} exited {done.returncode}: {done.stderr or done.stdout}")
    return done


def stage(config: EmbedConfig) -> None:
    """Copy the originals from the server (read-only) and make an editable twin of each."""
    work = config.work_dir
    source = f"{config.ssh}:{config.library_root}/"
    # sudo rsync: some library files are root-only (mode 600); this is the passwordless rule the server grants
    command = ["rsync", "-a", "--no-owner", "--no-group", "--rsync-path=sudo rsync"]
    run([*command, f"--files-from={work / 'files.txt'}", source, f"{work / 'original'}/"])
    for rel in load_paths(work):
        target = work / "edited" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work / "original" / rel, target)


def write(config: EmbedConfig, items: list[EmbedItem]) -> None:
    """Write the metadata into the edited copies only (never the originals)."""
    for item in items:
        run(["exiftool", *exiftool_args(item), str(config.work_dir / "edited" / item.rel_path)])


def read_back(path: Path) -> dict:
    """Metadata of one file as exiftool reports it (numeric GPS, raw dates)."""
    fields = ["-j", "-n", "-GPSLatitude", "-GPSLongitude", "-DateTimeOriginal", "-CreateDate"]
    return json.loads(run(["exiftool", *fields, str(path)]).stdout)[0]


def push_command(config: EmbedConfig, apply: bool) -> list[str]:
    """rsync that replaces the server files as root through the allowed `sudo rsync`."""
    command = ["rsync", "-a", "--no-owner", "--no-group", "--chmod=F644", "-v"]
    command += [f"--files-from={config.work_dir / 'verified.txt'}", "--rsync-path=sudo rsync"]
    if not apply:
        command.append("--dry-run")
    return command + [f"{config.work_dir / 'edited'}/", f"{config.ssh}:{config.library_root}/"]
