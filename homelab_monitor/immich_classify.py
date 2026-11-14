"""Sort photos into the favourite places (home, gym...) with a local vision model.

Run: `scripts/immich-classify` — resumable, writes classifications.json in IMMICH_REVIEW_DIR.
"""

import sys
from collections.abc import Callable

from .config import load_env_file
from .immich_review_store import DecisionStore
from .immich_client import ImmichClient
from .immich_ollama import OllamaClient
from .immich_presets import load_presets
from .immich_review import open_desk
from .immich_settings import OLLAMA_KEYS, immich_settings, require


def build_prompt(presets: list[dict]) -> str:
    """Instruction listing each candidate place with its description (`hint`)."""
    lines = [
        f'- "{p["name"]}": {p.get("hint", "a place the photographer often is")}' for p in presets
    ]
    return (
        "You sort personal photos by where they were taken. Candidate places:\n"
        + "\n".join(lines)
        + '\nAnswer with JSON {"place": "<exact candidate name, or none>", "reason": "<short>"}.'
        + " Answer none unless the photo clearly shows one of these places."
    )


def classify_image(ollama: OllamaClient, image: bytes, presets: list[dict]) -> dict:
    """{"place": preset name or None, "reason": str} for one photo."""
    answer = ollama.ask_json(build_prompt(presets), image)
    names = {preset["name"] for preset in presets}
    place = answer.get("place")
    return {"place": place if place in names else None, "reason": str(answer.get("reason", ""))}


def classify_all(
    cards: list[dict],
    fetch_image: Callable[[str], bytes],
    ollama: OllamaClient,
    presets: list[dict],
    store: DecisionStore,
) -> int:
    """Classify every card not in `store` yet; returns how many were classified."""
    done = store.load()
    todo = [card for card in cards if card["id"] not in done]
    for number, card in enumerate(todo, 1):
        try:
            result = classify_image(ollama, fetch_image(card["id"]), presets)
        except (ValueError, OSError) as error:  # not recorded, so a re-run retries it
            print(f"{number}/{len(todo)} {card['name']}: FAILED {error!r}")
            continue
        store.record(card["id"], result)
        print(f"{number}/{len(todo)} {card['name']}: {result['place']} ({result['reason']})")
    return len([card for card in todo if card["id"] in store.load()])


def main(argv: list[str]) -> None:
    if len(argv) > 1:
        load_env_file(argv[1])
    settings = immich_settings()
    desk = open_desk(settings)
    if not desk.presets:
        raise SystemExit(f"no places in {settings.review_dir / 'places.json'}: add some first")
    keys = require(settings.raw, OLLAMA_KEYS)
    ollama = OllamaClient(keys["IMMICH_OLLAMA_URL"].rstrip("/"), keys["IMMICH_OLLAMA_MODEL"])
    fetch = lambda asset_id: desk.client.fetch_image(asset_id, "preview")[0]  # noqa: E731
    count = classify_all(desk.queue(), fetch, ollama, desk.presets, desk.classifications)
    print(f"classified {count} photos")


if __name__ == "__main__":
    main(sys.argv)
