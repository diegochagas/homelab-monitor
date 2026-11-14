"""Remember what was reviewed, so a restart resumes where it stopped."""

import json
import os
from pathlib import Path


class DecisionStore:
    """decisions.json: {asset id: {action, applied fields, previous values}}."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        return json.loads(self.path.read_text())

    def record(self, asset_id: str, decision: dict) -> None:
        """Save one decision; write to a temp file first so a crash never corrupts the log."""
        decisions = self.load()
        decisions[asset_id] = decision
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(decisions, indent=1))
        os.replace(tmp, self.path)
