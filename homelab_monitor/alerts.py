"""Periodic checks with confirmation, rate limiting and recovery notifications."""

import logging
import time

from . import metrics
from .formatting import GREEN, backup_line
from .telegram import escape as e

log = logging.getLogger("alerts")


class AlertEngine:
    def __init__(self, cfg, state, notify):
        self.cfg = cfg
        self.state = state
        self.notify = notify  # callable(text)

    # ------------------------------------------------------- core logic

    def evaluate(self, key, node_id, failing, detail, problem_text, recovery_text):
        """Feed one reading for an alert key. Sends messages when appropriate."""
        now = int(time.time())
        alert = self.state.alert(key) or {
            "failures": 0, "active": 0, "first_seen": None, "last_notified": None, "detail": None,
        }
        if failing:
            failures = alert["failures"] + 1
            active = bool(alert["active"])
            first_seen = alert["first_seen"] or now
            last_notified = alert["last_notified"]
            if not active and failures >= self.cfg.alert_confirmations:
                active = True
                last_notified = now
                self.notify(problem_text)
            elif active and now - (last_notified or 0) >= self.cfg.alert_repeat_seconds:
                last_notified = now
                self.notify(problem_text + f"\n\n(still ongoing, since {_stamp(first_seen)})")
            self.state.save_alert(key, node_id, failures, active, first_seen, last_notified, detail)
        else:
            if alert["active"]:
                self.notify(recovery_text)
            if alert["active"] or alert["failures"]:
                self.state.save_alert(key, node_id, 0, False, None, None, None)

    # ----------------------------------------------------------- checks

    def check_node(self, node, snapshot):
        cfg = self.cfg
        node_id, name = node["id"], node["name"]
        heading = e(name)

        cpu = snapshot["cpu"].get("percent")
        self.evaluate(
            f"{node_id}:cpu", node_id,
            cpu is not None and cpu >= cfg.cpu_percent,
            f"CPU {cpu}%",
            f"🚨 <b>HIGH CPU — {heading}</b>\n\nCPU at {cpu}% for {cfg.alert_confirmations} consecutive checks (limit {cfg.cpu_percent:.0f}%).",
            f"{GREEN} <b>CPU RECOVERED — {heading}</b>\n\nCPU is back below {cfg.cpu_percent:.0f}% ({cpu}%).",
        )

        mem = snapshot["memory"].get("percent")
        self.evaluate(
            f"{node_id}:memory", node_id,
            mem is not None and mem >= cfg.memory_percent,
            f"Memory {mem}%",
            f"🚨 <b>HIGH MEMORY — {heading}</b>\n\nMemory at {mem}% for {cfg.alert_confirmations} consecutive checks (limit {cfg.memory_percent:.0f}%).",
            f"{GREEN} <b>MEMORY RECOVERED — {heading}</b>\n\nMemory is back below {cfg.memory_percent:.0f}% ({mem}%).",
        )

        for disk in snapshot["disks"]:
            if disk["mount"] in cfg.disk_ignore:
                continue
            mount = disk["mount"]
            self.evaluate(
                f"{node_id}:disk:{mount}", node_id,
                disk["percent"] >= cfg.disk_percent,
                f"Disk {mount} {disk['percent']}%",
                f"🚨 <b>DISK FULL — {heading}</b>\n\n<code>{e(mount)}</code> at {disk['percent']}% (limit {cfg.disk_percent:.0f}%).",
                f"{GREEN} <b>DISK RECOVERED — {heading}</b>\n\n<code>{e(mount)}</code> is back to {disk['percent']}%.",
            )

        temp = metrics.max_temperature(snapshot["temps"])
        self.evaluate(
            f"{node_id}:temp", node_id,
            temp is not None and temp >= cfg.temp_c,
            f"Temperature {temp} °C",
            f"🚨 <b>HIGH TEMPERATURE — {heading}</b>\n\n{temp} °C for {cfg.alert_confirmations} consecutive checks (limit {cfg.temp_c:.0f} °C).",
            f"{GREEN} <b>TEMPERATURE RECOVERED — {heading}</b>\n\nBack to {temp} °C.",
        )

        self.check_containers(node, snapshot)
        self.check_backups(node, snapshot)

    def check_containers(self, node, snapshot):
        docker = snapshot["docker"]
        node_id, heading = node["id"], e(node["name"])
        if not docker.get("available"):
            return
        present = {c["name"]: c for c in docker["containers"]}
        self.state.remember_containers(node_id, present.keys())
        for known in self.state.known_containers(node_id):
            container = present.get(known)
            if container is None:
                failing, reason = True, "not found"
            elif container["state"] != "running":
                failing, reason = True, container["status"] or container["state"]
            elif container["health"] == "unhealthy":
                failing, reason = True, "unhealthy"
            else:
                failing, reason = False, "running"
            self.evaluate(
                f"{node_id}:container:{known}", node_id,
                failing,
                f"Container {known}: {reason}",
                f"🚨 <b>CONTAINER FAILED — {heading}</b>\n\n<code>{e(known)}: {e(reason)}</code>\n\nIf it was removed on purpose: /forget {e(known)} {e(node['name'])}",
                f"{GREEN} <b>CONTAINER RECOVERED — {heading}</b>\n\n<code>{e(known)}</code> is running again.",
            )

    def check_backups(self, node, snapshot):
        node_id, heading = node["id"], e(node["name"])
        for stage_key, label in (("stage1", "SERVER BACKUP"), ("stage2", "OFFSITE BACKUP")):
            stage = (snapshot.get("backup") or {}).get(stage_key)
            if not stage:
                continue
            icon, text = backup_line(stage, self.cfg)
            failing = icon not in (GREEN, "🟡")  # stale, failed or unavailable
            hint = ""
            if stage_key == "stage1":
                hint = "\n\nCheck that the server's backup job is still scheduled (e.g. <code>sudo crontab -l</code>)."
            self.evaluate(
                f"{node_id}:backup:{stage_key}", node_id,
                failing,
                f"{label.title()}: {text}",
                f"🚨 <b>{label} — {heading}</b>\n\n{text}{hint}",
                f"{GREEN} <b>{label} OK — {heading}</b>\n\n{text}",
            )

    def backup_only(self, node):
        return node["name"].lower() in self.cfg.backup_only_nodes

    def check_offline(self, node):
        cfg = self.cfg
        node_id, heading = node["id"], e(node["name"])
        last_seen = node.get("last_seen") or 0
        silent_for = time.time() - last_seen
        offline = silent_for > cfg.heartbeat_interval * cfg.offline_after_missed
        self.evaluate(
            f"{node_id}:offline", node_id,
            offline,
            "Offline",
            f"🚨 <b>SERVER OFFLINE</b>\n\n<code>{heading}</code> has not responded for {int(silent_for // 60)} minutes.",
            f"{GREEN} <b>SERVER ONLINE</b>\n\n<code>{heading}</code> is responding again.",
        )
        return offline

    # ------------------------------------------------------------- loop

    def run_cycle(self, central_snapshot):
        for node in self.state.nodes():
            try:
                if node["role"] == "central":
                    self.check_node(node, central_snapshot)
                    continue
                if self.backup_only(node):
                    # Laptops and other intermittently-online machines: skip
                    # offline/CPU/memory/disk/temp/container checks entirely,
                    # just watch backup freshness off whatever was last reported.
                    snapshot = self.state.snapshot_of(node)
                    if snapshot:
                        self.check_backups(node, snapshot)
                    continue
                if self.check_offline(node):
                    continue
                snapshot = self.state.snapshot_of(node)
                if snapshot:
                    self.check_node(node, snapshot)
            except Exception:  # noqa: BLE001 - one node must not break the cycle
                log.exception("alert check failed for %s", node.get("name"))


def _stamp(timestamp):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
