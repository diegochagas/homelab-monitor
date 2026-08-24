"""Central server: Telegram bot + agent endpoint + alert loop.

Run with:  python3 -m homelab_monitor.bot [path/to/monitor.env]
"""

import logging
import socket
import sys
import threading
import time
import urllib.error

from . import VERSION, metrics
from .alerts import AlertEngine
from .commands import COMMANDS, Commands
from .config import Config, load_env_file
from .state import State
from .telegram import Telegram
from .telegram import escape as e

log = logging.getLogger("bot")


class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = State(cfg.data_dir)
        self.telegram = Telegram(cfg.bot_token)
        self.commands = Commands(cfg, self.state, self.central_snapshot)
        self.alerts = AlertEngine(cfg, self.state, self.notify)
        self._snapshot_lock = threading.Lock()
        self._snapshot = None
        self._snapshot_at = 0
        self.state.upsert_central(cfg.node_name, socket.gethostname())
        self._bot_username = ""

    # -------------------------------------------------------- snapshots

    def central_snapshot(self, max_age=5):
        """Central metrics, cached briefly so bursts of commands stay cheap."""
        with self._snapshot_lock:
            if self._snapshot is None or time.time() - self._snapshot_at > max_age:
                self._snapshot = metrics.snapshot(self.cfg)
                self._snapshot_at = time.time()
            return self._snapshot

    # ----------------------------------------------------- notifications

    def notify(self, text):
        try:
            message = self.telegram.send(self.cfg.chat_id, text)
            if message:
                self.commands.sent_messages.append((message["chat"]["id"], message["message_id"]))
        except Exception as error:  # noqa: BLE001
            log.error("notify failed: %s", error)

    def on_registered(self, node):
        self.notify(f"➕ <b>SERVER REGISTERED</b>\n\n<code>{e(node['name'])}</code> joined the monitor.")

    # ------------------------------------------------------ authorization

    def authorized(self, message):
        user_id = (message.get("from") or {}).get("id")
        chat_id = (message.get("chat") or {}).get("id")
        if user_id in self.cfg.allowed_user_ids:
            return True
        if chat_id in self.cfg.allowed_chat_ids:
            return True
        return False

    # ---------------------------------------------------------- polling

    def handle_message(self, message):
        text = (message.get("text") or "").strip()
        chat_id = message["chat"]["id"]
        if not text.startswith("/"):
            return
        if not self.authorized(message):
            user = message.get("from") or {}
            log.warning("unauthorized command from user %s (%s)", user.get("id"), user.get("username"))
            return

        parts = text.split()
        command = parts[0][1:].lower()
        if "@" in command:
            command, target = command.split("@", 1)
            if self._bot_username and target != self._bot_username.lower():
                return
        args = parts[1:]
        log.info("/%s %s from user %s", command, " ".join(args), (message.get("from") or {}).get("id"))

        try:
            reply = self.commands.handle(command, args)
        except Exception as error:  # noqa: BLE001
            log.exception("command %s failed", command)
            reply = f"❌ Command failed: <code>{e(error)}</code>"

        if reply == "__clear__":
            self.clear(chat_id, message.get("message_id"))
            return
        if reply is None:
            reply = f"❓ Unknown command /{e(command)}. Try /help"
        sent = self.telegram.send(chat_id, reply)
        if sent:
            self.commands.sent_messages.append((chat_id, sent["message_id"]))

    def clear(self, chat_id, request_message_id):
        deleted = 0
        remaining = []
        for stored_chat, message_id in self.commands.sent_messages:
            if stored_chat == chat_id:
                if self.telegram.delete_message(chat_id, message_id):
                    deleted += 1
            else:
                remaining.append((stored_chat, message_id))
        self.commands.sent_messages = remaining
        if request_message_id:
            self.telegram.delete_message(chat_id, request_message_id)
        log.info("cleared %d messages in chat %s", deleted, chat_id)

    def poll_forever(self):
        backoff = 2
        while True:
            try:
                for update in self.telegram.get_updates():
                    message = update.get("message")
                    if message:
                        self.handle_message(message)
                backoff = 2
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as error:
                log.warning("polling error: %s — retrying in %ss", error, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception:  # noqa: BLE001
                log.exception("unexpected polling error")
                time.sleep(backoff)

    # ------------------------------------------------------------ alerts

    def alert_loop(self):
        while True:
            started = time.time()
            try:
                self.alerts.run_cycle(self.central_snapshot(max_age=0))
            except Exception:  # noqa: BLE001
                log.exception("alert cycle failed")
            elapsed = time.time() - started
            time.sleep(max(5, self.cfg.check_interval - elapsed))

    # -------------------------------------------------------------- main

    def run(self):
        from .server import AgentServer

        me = self.telegram.get_me()
        self._bot_username = me.get("username", "")
        log.info("connected to Telegram as @%s", self._bot_username)
        self.telegram.set_commands(COMMANDS)

        for node in self.state.nodes():
            if node["role"] != "central" and node["name"].lower() in self.cfg.backup_only_nodes:
                self.state.clear_alerts_except_backup(node["id"])

        server = AgentServer(self.cfg, self.state, self.on_registered)
        server.start()
        log.info("agent endpoint listening on %s:%s", self.cfg.listen_host, self.cfg.listen_port)

        threading.Thread(target=self.alert_loop, name="alerts", daemon=True).start()
        self.notify(f"🤖 <b>Homelab Monitor v{e(VERSION)}</b> started on <code>{e(self.cfg.node_name)}</code>")
        self.poll_forever()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if len(sys.argv) > 1:
        load_env_file(sys.argv[1])
    cfg = Config()
    problems = cfg.validate_central()
    if problems:
        for problem in problems:
            log.error(problem)
        sys.exit(1)
    Bot(cfg).run()


if __name__ == "__main__":
    main()
