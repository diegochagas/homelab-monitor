"""Telegram command handlers."""

import logging

from . import VERSION, formatting, metrics
from .telegram import escape as e

log = logging.getLogger("commands")

COMMANDS = [
    ("servers", "Registered servers"),
    ("status", "Quick status [server]"),
    ("health", "Detailed diagnosis [server]"),
    ("backups", "Last backup results"),
    ("apps", "Docker containers [server]"),
    ("docker", "Docker summary [server]"),
    ("cpu", "CPU usage [server]"),
    ("memory", "Memory usage [server]"),
    ("disk", "Disk usage [server]"),
    ("temperature", "Temperatures [server]"),
    ("network", "Interfaces and IPs [server]"),
    ("ports", "Listening TCP ports"),
    ("rename", "Rename a server"),
    ("remove", "Unregister a server"),
    ("forget", "Stop tracking a removed container"),
    ("integrate", "How to add a server"),
    ("unliked", "Unliked songs waiting for a decision"),
    ("unliked_delete", "Delete the waiting unliked songs [server]"),
    ("unliked_keep", "Keep the waiting unliked songs [server]"),
    ("restart", "Restart the bot"),
    ("clear", "Delete recent bot messages"),
    ("version", "Bot version"),
    ("help", "All commands"),
]


class Commands:
    def __init__(self, cfg, state, fresh_central_snapshot):
        self.cfg = cfg
        self.state = state
        self.fresh_central_snapshot = fresh_central_snapshot  # callable() -> snapshot
        self.sent_messages = []  # (chat_id, message_id) for /clear
        self.unlikes = None  # UnlikeConfirmations, set by the bot

    # ---------------------------------------------------------- helpers

    def resolve(self, arg):
        """Return (node, snapshot) for a server name, defaulting to central."""
        if not arg:
            node = next((n for n in self.state.nodes() if n["role"] == "central"), None)
        else:
            node = self.state.node_by_name(arg)
        if node is None:
            return None, None
        if node["role"] == "central":
            return node, self.fresh_central_snapshot()
        return node, self.state.snapshot_of(node)

    def snapshot_for(self, node):
        if node["role"] == "central":
            return self.fresh_central_snapshot()
        return self.state.snapshot_of(node)

    def unknown_server(self, arg):
        names = ", ".join(e(n["name"]) for n in self.state.nodes())
        return f"❓ Unknown server <b>{e(arg)}</b>\n\nRegistered: {names}"

    # --------------------------------------------------------- dispatch

    def handle(self, command, args):
        handler = getattr(self, f"cmd_{command}", None)
        if handler is None:
            return None
        return handler(args)

    def per_node(self, args, renderer, **extra):
        arg = args[0] if args else ""
        node, snapshot = self.resolve(arg)
        if node is None:
            return self.unknown_server(arg)
        return renderer(node, snapshot, self.cfg, **extra)

    # --------------------------------------------------------- commands

    def cmd_start(self, args):
        return self.cmd_help(args)

    def cmd_help(self, _args):
        return formatting.help_text(VERSION)

    def cmd_version(self, _args):
        return f"🤖 Homelab Monitor v{e(VERSION)}"

    def cmd_servers(self, _args):
        return formatting.servers(self.state.nodes(), self.cfg, self.snapshot_for)

    def cmd_status(self, args):
        return self.per_node(args, formatting.status)

    def cmd_health(self, args):
        arg = args[0] if args else ""
        node, snapshot = self.resolve(arg)
        if node is None:
            return self.unknown_server(arg)
        return formatting.health(node, snapshot, self.cfg, self.state.active_alerts(node["id"]))

    def cmd_backups(self, _args):
        pairs = []
        for node in self.state.nodes():
            pairs.append((node, self.snapshot_for(node)))
        return formatting.backups(pairs, self.cfg)

    def cmd_apps(self, args):
        return self.per_node(args, formatting.apps)

    def cmd_docker(self, args):
        arg = args[0] if args else ""
        node, snapshot = self.resolve(arg)
        if node is None:
            return self.unknown_server(arg)
        return formatting.docker_summary(node, snapshot, self.cfg, self.state.known_containers(node["id"]))

    def cmd_cpu(self, args):
        return self.per_node(args, formatting.cpu)

    def cmd_memory(self, args):
        return self.per_node(args, formatting.memory)

    def cmd_disk(self, args):
        return self.per_node(args, formatting.disk)

    def cmd_temperature(self, args):
        return self.per_node(args, formatting.temperature)

    def cmd_temp(self, args):
        return self.cmd_temperature(args)

    def cmd_network(self, args):
        return self.per_node(args, formatting.network)

    def cmd_ports(self, args):
        arg = args[0] if args else ""
        node, _ = self.resolve(arg)
        if node is None:
            return self.unknown_server(arg)
        ports = metrics.listening_ports() if node["role"] == "central" else None
        return formatting.ports(node, ports)

    def cmd_rename(self, args):
        if len(args) != 2:
            return "Usage: /rename current-name new-name"
        old, new = args
        if self.state.rename_node(old, new):
            return f"✅ Renamed <b>{e(old)}</b> → <b>{e(new)}</b>"
        return f"❌ Could not rename: <b>{e(old)}</b> not found or <b>{e(new)}</b> already used"

    def cmd_remove(self, args):
        if len(args) != 1:
            return "Usage: /remove server-name"
        if self.state.remove_node(args[0]):
            return f"✅ Removed <b>{e(args[0])}</b>. Stop its agent too, or it will re-register."
        return f"❌ <b>{e(args[0])}</b> not found (the central server cannot be removed)"

    def cmd_forget(self, args):
        if not args:
            return "Usage: /forget container-name [server]"
        container = args[0]
        node, _ = self.resolve(args[1] if len(args) > 1 else "")
        if node is None:
            return self.unknown_server(args[1])
        if self.state.forget_container(node["id"], container):
            return f"✅ No longer tracking <code>{e(container)}</code> on <b>{e(node['name'])}</b>"
        return f"❌ <code>{e(container)}</code> is not a known container on <b>{e(node['name'])}</b>"

    def cmd_integrate(self, _args):
        addresses = [ip for ip in metrics.ips() if not ip["iface"].startswith(("docker", "br-", "virbr"))]
        lan = [ip for ip in addresses if not ip["iface"].startswith(("tailscale", "zt", "tun", "wg"))]
        central_ip = (lan or addresses or [{"ip": "CENTRAL-IP"}])[0]["ip"]
        return "\n".join([
            "➕ <b>Add a server</b>",
            "",
            "On the new machine:",
            "<code>git clone https://github.com/diegochagas/homelab-monitor.git</code>",
            "<code>cd homelab-monitor</code>",
            "<code>./install-agent.sh</code>",
            "",
            "When asked, use:",
            f"Central URL: <code>http://{e(central_ip)}:{self.cfg.listen_port}</code>",
            "Registration key: the REGISTRATION_KEY from the central monitor.env",
            "",
            "The agent registers itself on its first heartbeat and shows up in /servers.",
        ])

    def cmd_unliked(self, _args):
        return self.unlikes.status() if self.unlikes else None

    def cmd_unliked_delete(self, args):
        return self.unlikes.decide("delete", args[0] if args else None) if self.unlikes else None

    def cmd_unliked_keep(self, args):
        return self.unlikes.decide("keep", args[0] if args else None) if self.unlikes else None

    def cmd_restart(self, _args):
        return "__restart__"

    def cmd_clear(self, _args):
        return "__clear__"
