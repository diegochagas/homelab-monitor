"""Tests for the central unlike confirmation. Telegram and SQLite state are fakes."""

import tempfile
import unittest
from types import SimpleNamespace
from typing import List

from homelab_monitor.commands import Commands
from homelab_monitor.server import AgentServer
from homelab_monitor.unlike_confirmations import UnlikeConfirmations

NODE = {"id": "48b8fd97-0000", "name": "mint", "role": "agent"}


def snapshot_with(*songs) -> dict:
    return {"liked_music": {"pending_removals": [{"id": vid, "title": title} for vid, title in songs]}}


class FakeState:
    """Keeps the latest snapshot per node, like State.store_snapshot / snapshot_of."""

    def __init__(self):
        self.snapshots = {}

    def nodes(self) -> List[dict]:
        return [NODE]

    def snapshot_of(self, node: dict) -> dict:
        return self.snapshots.get(node["id"])


class FakeTelegram:
    def __init__(self):
        self.sent: List[str] = []

    def __call__(self, text: str) -> None:
        self.sent.append(text)


class UnlikeConfirmationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state, self.telegram = FakeState(), FakeTelegram()
        self.unlikes = UnlikeConfirmations(self.tmp.name, self.state, self.telegram)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def heartbeat(self, snapshot: dict) -> dict:
        self.state.snapshots[NODE["id"]] = snapshot
        self.unlikes.on_snapshot(NODE, snapshot)
        return self.unlikes.reply_for(NODE["id"])

    def test_new_unlike_is_announced_once_and_nothing_decided(self) -> None:
        self.assertEqual(self.heartbeat(snapshot_with(("a", "Artist - Song"))), {})
        self.heartbeat(snapshot_with(("a", "Artist - Song")))
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertIn("Artist - Song", self.telegram.sent[0])
        self.assertIn("/unliked_delete", self.telegram.sent[0])

    def test_delete_answer_travels_until_the_agent_applied_it(self) -> None:
        self.heartbeat(snapshot_with(("a", "A"), ("b", "B")))
        reply = self.unlikes.decide("delete")
        self.assertIn("Deleting", reply)
        self.assertEqual(self.heartbeat(snapshot_with(("a", "A"), ("b", "B"))),
                         {"liked_music_decisions": {"delete": ["a", "b"], "keep": []}})
        self.assertEqual(self.heartbeat(snapshot_with()), {})

    def test_keep_replaces_an_earlier_delete(self) -> None:
        self.heartbeat(snapshot_with(("a", "A")))
        self.unlikes.decide("delete")
        self.unlikes.decide("keep")
        self.assertEqual(self.heartbeat(snapshot_with(("a", "A"))),
                         {"liked_music_decisions": {"delete": [], "keep": ["a"]}})

    def test_song_added_after_the_answer_is_not_included(self) -> None:
        self.heartbeat(snapshot_with(("a", "A")))
        self.unlikes.decide("delete")
        reply = self.heartbeat(snapshot_with(("a", "A"), ("late", "Late")))
        self.assertEqual(reply["liked_music_decisions"]["delete"], ["a"])
        self.assertEqual(len(self.telegram.sent), 2)

    def test_answers_survive_a_bot_restart(self) -> None:
        self.heartbeat(snapshot_with(("a", "A")))
        self.unlikes.decide("delete")
        restarted = UnlikeConfirmations(self.tmp.name, self.state, self.telegram)
        self.assertEqual(restarted.reply_for(NODE["id"])["liked_music_decisions"]["delete"], ["a"])

    def test_nothing_waiting_and_unknown_server(self) -> None:
        self.assertIn("No unliked songs", self.unlikes.decide("delete"))
        self.heartbeat(snapshot_with(("a", "A")))
        self.assertIn("No unliked songs", self.unlikes.decide("delete", "zimaos"))

    def test_titles_are_html_escaped(self) -> None:
        self.heartbeat(snapshot_with(("a", "<b>Rock & Roll</b>")))
        self.assertIn("&lt;b&gt;Rock &amp; Roll&lt;/b&gt;", self.telegram.sent[0])

    def test_commands_reach_the_confirmations(self) -> None:
        self.heartbeat(snapshot_with(("a", "A")))
        commands = Commands(cfg=None, state=self.state, fresh_central_snapshot=dict)
        commands.unlikes = self.unlikes
        self.assertIn("Waiting for your decision", commands.handle("unliked", []))
        self.assertIn("Keeping", commands.handle("unliked_keep", ["mint"]))


class ExplodingConfirmations:
    def on_snapshot(self, node, snapshot):
        raise ValueError("bad snapshot")


class HeartbeatReplyTest(unittest.TestCase):
    def test_a_failing_unlike_step_never_rejects_the_heartbeat(self) -> None:
        server = SimpleNamespace(unlikes=ExplodingConfirmations())
        with self.assertLogs("server", "ERROR"):
            self.assertEqual(AgentServer.after_snapshot(server, NODE, {}), {})


if __name__ == "__main__":
    unittest.main()
