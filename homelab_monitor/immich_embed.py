"""`scripts/immich-embed <step>`: put the reviewed date/GPS into the library files on the server.

Steps: plan (new run folder) -> stage -> write -> verify -> push (dry run) -> push-apply.
Each run lives in IMMICH_EMBED_WORK_DIR/<YYYY-MM-DD_HHMM>/ with the untouched
originals as backup; photos already pushed by an earlier run are left out of a new plan.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .config import load_env_file
from .immich_client import ImmichClient
from .immich_embed_plan import EmbedItem, plan_items
from .immich_embed_steps import EmbedConfig, push_command, read_back, run, save_plan, stage, write
from .immich_embed_verify import check_item
from .immich_settings import EMBED_KEYS, ImmichSettings, immich_settings, require

STEPS = ("plan", "stage", "write", "verify", "push", "push-apply")


def embed_config(settings: ImmichSettings, new_run: bool) -> EmbedConfig:
    """Paths for this run: a fresh timestamped folder for `plan`, else the latest one."""
    keys = require(settings.raw, EMBED_KEYS)
    root = Path(keys["IMMICH_EMBED_WORK_DIR"])
    runs = sorted(p for p in root.glob("20*") if p.is_dir()) if root.is_dir() else []
    if not new_run and not runs:
        raise SystemExit(f"no run in {root}: start with `scripts/immich-embed plan`")
    work = root / datetime.now(settings.tz).strftime("%Y-%m-%d_%H%M") if new_run else runs[-1]
    return EmbedConfig(keys["IMMICH_EMBED_SSH"], keys["IMMICH_LIBRARY_ROOT"].rstrip("/"),
                       keys["IMMICH_CONTAINER_ROOT"].rstrip("/"), work)


def build_plan(settings: ImmichSettings, embed: EmbedConfig) -> list[EmbedItem]:
    """Reviewed photos whose date or location changed and that no earlier run pushed."""
    client = ImmichClient(settings.url, settings.api_key)
    since = datetime.now(settings.tz) - timedelta(days=settings.review_days + 1)
    assets = {a["id"]: a for a in client.search_assets(since.isoformat(timespec="seconds"))}
    decisions = json.loads((settings.review_dir / "decisions.json").read_text())
    done = set(load_embedded(settings.review_dir))
    pending = {key: value for key, value in decisions.items() if key not in done}
    items = plan_items(assets, pending, settings.tz, embed.container_root)
    save_plan(items, embed.work_dir)
    return items


def load_embedded(review_dir: Path) -> list[str]:
    path = review_dir / "embedded.json"
    return json.loads(path.read_text()) if path.is_file() else []


def remember_pushed(review_dir: Path, embed: EmbedConfig) -> int:
    """After push-apply: add this run's verified photos to embedded.json."""
    verified = set((embed.work_dir / "verified.txt").read_text().splitlines())
    pushed = [item.asset_id for item in load_items(embed.work_dir) if item.rel_path in verified]
    merged = sorted(set(load_embedded(review_dir)) | set(pushed))
    (review_dir / "embedded.json").write_text(json.dumps(merged, indent=1))
    return len(pushed)


def load_items(work: Path) -> list[EmbedItem]:
    rows = json.loads((work / "plan.json").read_text())
    return [
        EmbedItem(**{**r, "taken": datetime.fromisoformat(r["taken"]) if r["taken"] else None})
        for r in rows
    ]


def verify(work: Path, items: list[EmbedItem]) -> int:
    """Print every problem found, save the passing files to verified.txt; returns failures."""
    passed = []
    for item in items:
        edited = work / "edited" / item.rel_path
        found = read_back(edited)
        problems = check_item(item, found, work / "original" / item.rel_path, edited)
        passed += [] if problems else [item.rel_path]
        for problem in problems:
            print(f"{item.rel_path}: {problem}")
    (work / "verified.txt").write_text("".join(f"{rel}\n" for rel in passed))
    print(f"verified {len(passed)}/{len(items)} files; only those are pushed")
    return len(items) - len(passed)


def run_step(step: str, settings: ImmichSettings) -> int:
    embed = embed_config(settings, new_run=step == "plan")
    if step == "plan":
        print(f"planned {len(build_plan(settings, embed))} files in {embed.work_dir}")
    elif step == "stage":
        stage(embed)
    elif step == "write":
        write(embed, load_items(embed.work_dir))
    elif step == "verify":
        return 1 if verify(embed.work_dir, load_items(embed.work_dir)) else 0
    else:
        print(run(push_command(embed, apply=step == "push-apply")).stdout)
        if step == "push-apply":
            print(f"remembered {remember_pushed(settings.review_dir, embed)} pushed photos")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3) or argv[1] not in STEPS:
        raise SystemExit(f"usage: scripts/immich-embed <{'|'.join(STEPS)}> [monitor.env], got {argv[1:]!r}")
    if len(argv) == 3:
        load_env_file(argv[2])
    return run_step(argv[1], immich_settings())


if __name__ == "__main__":
    sys.exit(main(sys.argv))
