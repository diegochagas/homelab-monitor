"""Daily Immich check on the central server: photos to review and duplicate groups.

Once a day after IMMICH_CHECK_HOUR it asks Immich for the photos of the review
window that have no location or a date that disagrees with the file name (the
same rule as the review page) and for Immich's duplicate groups, and sends one
Telegram message when anything is new since the last message. Seen ids live in
<DATA_DIR>/immich-check.json, so a photo is announced once.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .immich_client import ImmichClient
from .immich_queue import build_queue
from .immich_settings import ImmichSettings
from .telegram import escape as e

log = logging.getLogger("immich")
MAX_LISTED = 10


@dataclass
class ImmichFindings:
    review: dict = field(default_factory=dict)  # asset id -> "name — reasons"
    duplicates: dict = field(default_factory=dict)  # duplicate id -> "a.jpg, b.jpg"


def find_problems(client: ImmichClient, settings: ImmichSettings, now: datetime) -> ImmichFindings:
    """Everything that needs a look right now (read-only calls)."""
    cards = build_queue(client.search_assets(settings.window_start(now)), settings.tz)
    review = {c["id"]: f"{c['name']} — {', '.join(c['reasons'])}" for c in cards}
    groups = client.duplicate_groups()
    duplicates = {g["duplicateId"]: ", ".join(a["originalFileName"] for a in g["assets"]) for g in groups}
    return ImmichFindings(review, duplicates)


def describe(findings: ImmichFindings, new: ImmichFindings, settings: ImmichSettings,
             fresh: str = "new ") -> str:
    """Telegram text (HTML) for `new`, with totals and where to fix them.

    Example: describe(all, only_new, settings) -> "🖼️ IMMICH — daily check\n\n3 new photo(s)..."
    """
    lines = ["🖼️ <b>IMMICH — daily check</b>" if fresh else "🖼️ <b>IMMICH</b>"]
    if not new.review and not new.duplicates:
        lines.append("Nothing to review and no duplicates. ✅")
    if new.review:
        lines += ["", f"<b>{len(new.review)}</b> {fresh}photo(s) need a date or location "
                      f"({len(findings.review)} in total):"]
        lines += _listed(new.review)
        lines.append(f"Review on the laptop: <code>scripts/immich-review</code> → "
                     f"http://127.0.0.1:{settings.review_port}")
    if new.duplicates:
        lines += ["", f"<b>{len(new.duplicates)}</b> {fresh}duplicate group(s) "
                      f"({len(findings.duplicates)} in total):"]
        lines += _listed(new.duplicates)
        lines.append(f"Review: {e(settings.web_url)}/utilities/duplicates")
    return "\n".join(lines)


def _listed(entries: dict) -> list:
    shown = [f"• {e(text)}" for text in list(entries.values())[:MAX_LISTED]]
    hidden = len(entries) - MAX_LISTED
    return shown + ([f"… and {hidden} more"] if hidden > 0 else [])


class ImmichWatch:
    """Runs the check once a day and remembers what was already announced."""

    def __init__(self, client: ImmichClient, settings: ImmichSettings, data_dir: str,
                 notify: Callable[[str], None]) -> None:
        self.client = client
        self.settings = settings
        self.path = Path(data_dir) / "immich-check.json"
        self.notify = notify

    def run_if_due(self, now: datetime) -> Optional[str]:
        """Check (and notify) when today's check hasn't run and its hour has come."""
        seen = self._load()
        today = now.date().isoformat()
        if now.hour < self.settings.check_hour or seen.get("day") == today:
            return None
        return self.check(now, seen, today)

    def check(self, now: datetime, seen: dict, today: str) -> Optional[str]:
        findings = find_problems(self.client, self.settings, now)
        new = ImmichFindings(
            {k: v for k, v in findings.review.items() if k not in seen.get("review", [])},
            {k: v for k, v in findings.duplicates.items() if k not in seen.get("duplicates", [])},
        )
        self._save({"day": today, "review": sorted(findings.review), "duplicates": sorted(findings.duplicates)})
        if not new.review and not new.duplicates:
            log.info("immich check: nothing new (%d to review, %d duplicate groups)",
                     len(findings.review), len(findings.duplicates))
            return None
        text = describe(findings, new, self.settings)
        self.notify(text)
        return text

    def status(self, now: datetime) -> str:
        """Current totals for the /immich command (nothing is marked as seen)."""
        findings = find_problems(self.client, self.settings, now)
        return describe(findings, findings, self.settings, fresh="")

    def _load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.is_file() else {}

    def _save(self, seen: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(seen, indent=1))
        tmp.replace(self.path)
