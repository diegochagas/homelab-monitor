"""Review page for Immich photos without a location or with a date that disagrees with the file name.

Run on the laptop: `scripts/immich-review` (reads monitor.env), then open
http://127.0.0.1:<IMMICH_REVIEW_PORT>. The daily check on the server tells you on
Telegram when there is something to review.
"""

import sys

from .config import load_env_file
from .immich_client import ImmichClient
from .immich_presets import load_presets
from .immich_review_desk import ReviewDesk, ReviewSettings
from .immich_review_server import serve
from .immich_review_store import DecisionStore
from .immich_settings import ImmichSettings, immich_settings


def open_desk(settings: ImmichSettings) -> ReviewDesk:
    """The review desk with its stores in IMMICH_REVIEW_DIR (decisions, AI guesses, places)."""
    folder = settings.review_dir
    folder.mkdir(parents=True, exist_ok=True)
    client = ImmichClient(settings.url, settings.api_key)
    review = ReviewSettings(settings.tz, settings.review_days, load_presets(folder / "places.json"))
    stores = (DecisionStore(folder / "decisions.json"), DecisionStore(folder / "classifications.json"))
    return ReviewDesk(client, stores[0], stores[1], review)


def main(argv: list[str]) -> None:
    if len(argv) > 1:
        load_env_file(argv[1])
    settings = immich_settings()
    serve(open_desk(settings), settings.review_port)


if __name__ == "__main__":
    main(sys.argv)
