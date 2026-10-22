"""Central side of the unlike confirmation (Telegram <-> agent heartbeat).

An agent's heartbeat snapshot lists the songs that left YouTube Music's Liked
Music and wait in its queue (snapshot["liked_music"]["pending_removals"]).
The first time a song shows up, the bot sends one Telegram message listing
what is waiting. /unliked_delete or /unliked_keep records the answer for the
songs listed at that moment, and every heartbeat reply to that agent carries
it ({"liked_music_decisions": {"delete": [...], "keep": [...]}}) until the
agent's snapshot no longer lists those songs.

Stored in DATA_DIR/unlike-decisions.json: {node_id: {"notified": [ids],
"delete": [ids], "keep": [ids]}}.
"""

import json
import logging
import os
import threading
from typing import Callable, Dict, List, Optional

from .telegram import escape as e

log = logging.getLogger("unlikes")

DECISIONS_FILE = "unlike-decisions.json"
MAX_LISTED = 30  # Telegram messages stop at 4096 characters
ACTIONS = ("delete", "keep")


class UnlikeConfirmations:
    """Tracks waiting unlikes per agent and the owner's delete/keep answers."""

    def __init__(self, data_dir: str, state, notify: Callable[[str], None]):
        self.path = os.path.join(data_dir, DECISIONS_FILE)
        self.state = state  # State: nodes(), snapshot_of(node)
        self.notify = notify
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, List[str]]] = self._load()

    def _load(self) -> Dict[str, Dict[str, List[str]]]:
        if not os.path.isfile(self.path):
            return {}
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    def _save(self) -> None:
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self._entries, handle)
        os.replace(temporary, self.path)

    def on_snapshot(self, node: dict, snapshot: dict) -> None:
        """Notify about newly waiting songs; forget answers the agent has carried out."""
        waiting = waiting_songs(snapshot)
        ids = [song["id"] for song in waiting]
        with self._lock:
            entry = self._entries.setdefault(node["id"], {"notified": [], "delete": [], "keep": []})
            fresh = [vid for vid in ids if vid not in entry["notified"]]
            before = json.dumps(entry, sort_keys=True)
            entry["notified"] = ids
            for action in ACTIONS:
                entry[action] = [vid for vid in entry[action] if vid in ids]
            if json.dumps(entry, sort_keys=True) != before:
                self._save()
        if fresh:
            self.notify(confirmation_message(node["name"], waiting))

    def reply_for(self, node_id: str) -> dict:
        """Extra heartbeat-reply fields for this agent ({} when nothing was decided)."""
        with self._lock:
            entry = self._entries.get(node_id) or {}
            decisions = {action: list(entry.get(action, [])) for action in ACTIONS}
        if not decisions["delete"] and not decisions["keep"]:
            return {}
        return {"liked_music_decisions": decisions}

    def decide(self, action: str, server: Optional[str] = None) -> str:
        """Record delete/keep for every song waiting right now (optionally one server); returns the reply."""
        lines = []
        for node, waiting in self.waiting_by_node(server):
            ids = [song["id"] for song in waiting]
            with self._lock:
                entry = self._entries.setdefault(node["id"], {"notified": ids, "delete": [], "keep": []})
                other = "keep" if action == "delete" else "delete"
                entry[action] = list(dict.fromkeys(entry[action] + ids))
                entry[other] = [vid for vid in entry[other] if vid not in ids]
                self._save()
            lines.append(song_list(node["name"], waiting))
        if not lines:
            return "🎵 No unliked songs are waiting for a decision."
        verb = "🗑 <b>Deleting</b> everywhere" if action == "delete" else "📁 <b>Keeping</b> the files of"
        return f"{verb} (applied on the next heartbeat, within a minute):\n\n" + "\n\n".join(lines)

    def waiting_by_node(self, server: Optional[str] = None) -> List[tuple]:
        """[(node, [songs])] for agents with songs waiting, optionally one server by name."""
        found = []
        for node in self.state.nodes():
            if server and node["name"].lower() != server.lower():
                continue
            waiting = waiting_songs(self.state.snapshot_of(node) or {})
            if waiting:
                found.append((node, waiting))
        return found

    def status(self) -> str:
        """Text for /unliked."""
        groups = self.waiting_by_node()
        if not groups:
            return "🎵 No unliked songs are waiting for a decision."
        body = "\n\n".join(song_list(node["name"], waiting) for node, waiting in groups)
        return f"🎵 <b>Waiting for your decision</b>\n\n{body}\n\n{COMMAND_HINT}"


COMMAND_HINT = ("/unliked_delete removes them from the playlists and deletes the files\n"
                "/unliked_keep keeps the files (add a server name to answer for one server)")


def waiting_songs(snapshot: dict) -> List[dict]:
    """[{"id", "title"}] from an agent snapshot; malformed entries are skipped.

    Example: waiting_songs({"liked_music": {"pending_removals": [{"id": "a", "title": "A"}]}})
    """
    songs = (snapshot.get("liked_music") or {}).get("pending_removals") or []
    return [{"id": str(s["id"]), "title": str(s.get("title", s["id"]))}
            for s in songs if isinstance(s, dict) and s.get("id")]


def song_list(server: str, waiting: List[dict]) -> str:
    shown = "\n".join(f"• {e(song['title'])}" for song in waiting[:MAX_LISTED])
    more = f"\n… and {len(waiting) - MAX_LISTED} more" if len(waiting) > MAX_LISTED else ""
    return f"<code>{e(server)}</code>:\n{shown}{more}"


def confirmation_message(server: str, waiting: List[dict]) -> str:
    return ("🎵 <b>UNLIKED SONGS</b>\n\nThese songs left your YouTube Music likes. "
            "Nothing is deleted until you answer.\n\n" + song_list(server, waiting) + "\n\n" + COMMAND_HINT)
