"""What the review page does: list the photos to review, apply or skip a decision."""

from dataclasses import dataclass
from datetime import datetime, tzinfo

from .immich_review_store import DecisionStore
from .immich_settings import window_start
from .immich_client import ImmichClient
from .immich_queue import LOCAL_FORMAT, build_queue

LOCAL_FORMAT_NO_SECONDS = "%Y-%m-%dT%H:%M"


@dataclass(frozen=True)
class ReviewSettings:
    tz: tzinfo
    days: int  # 0 = the whole library
    presets: list[dict]
    web_url: str = ""  # Immich's own pages (duplicate review)


class ReviewDesk:
    def __init__(
        self,
        client: ImmichClient,
        store: DecisionStore,
        classifications: DecisionStore,
        settings: ReviewSettings,
    ) -> None:
        self.client = client
        self.store = store
        self.classifications = classifications
        self.tz = settings.tz
        self.days = settings.days
        self.presets = settings.presets
        self.web_url = settings.web_url

    def queue(self) -> list[dict]:
        """Cards still needing review plus the saved decision for each."""
        assets = self.client.search_assets(window_start(self.days, datetime.now(self.tz)))
        decisions = self.store.load()
        guesses = self.classifications.load()
        cards = build_queue(assets, self.tz)
        return [self._decorate(card, decisions, guesses) for card in cards]

    def _decorate(self, card: dict, decisions: dict, guesses: dict) -> dict:
        guess = guesses.get(card["id"], {})
        preset = next((p for p in self.presets if p["name"] == guess.get("place")), None)
        place = {**preset, "reason": guess.get("reason", "")} if preset else None
        return {**card, "decision": decisions.get(card["id"]), "aiPlace": place}

    def apply(self, asset_id: str, taken_at: str | None, location: dict | None) -> dict:
        """Write the chosen date and/or location to Immich (if any changed) and remember it."""
        fields = self._fields(taken_at, location)
        if fields:  # empty = the reviewer confirmed the values Immich already has
            self.client.update_asset(asset_id, fields)
        decision = {"action": "applied", "fields": fields}
        self.store.record(asset_id, decision)
        return decision

    def duplicates(self) -> dict:
        """How many duplicate groups Immich has and where to review them."""
        groups = self.client.duplicate_groups()
        return {"groups": len(groups), "url": f"{self.web_url}/utilities/duplicates"}

    def skip(self, asset_id: str) -> dict:
        decision = {"action": "skipped"}
        self.store.record(asset_id, decision)
        return decision

    def _fields(self, taken_at: str | None, location: dict | None) -> dict:
        fields: dict = {}
        if taken_at:
            fields["dateTimeOriginal"] = self.to_iso(taken_at)
        if location:
            fields["latitude"] = _coordinate(location, "lat", 90)
            fields["longitude"] = _coordinate(location, "lon", 180)
        return fields

    def to_iso(self, local_text: str) -> str:
        """ "2026-11-11T10:27" or "...T10:27:12" read as local time in the review zone -> ISO + offset.

        Browsers drop the seconds from datetime-local when they are 00.
        """
        for pattern in (LOCAL_FORMAT, LOCAL_FORMAT_NO_SECONDS):
            try:
                local = datetime.strptime(local_text, pattern).replace(tzinfo=self.tz)
            except ValueError:
                continue
            return local.isoformat(timespec="milliseconds")
        raise ValueError(
            f"bad date {local_text!r}, expected {LOCAL_FORMAT} or {LOCAL_FORMAT_NO_SECONDS}"
        )


def _coordinate(location: dict, key: str, limit: int) -> float:
    try:
        value = float(location[key])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"bad {key} in {location!r}: expected a number") from error
    if abs(value) > limit:
        raise ValueError(f"{key} {value} out of range, expected -{limit}..{limit}")
    return value
