"""Telegram message renderers — compact, emoji-coded, HTML-safe."""

import time

from . import metrics
from .telegram import escape as e

GREEN, YELLOW, RED, GREY = "🟢", "🟡", "🔴", "⚪"


def human_bytes(value):
    if value is None:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def duration(seconds):
    if seconds is None:
        return "?"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def ago(timestamp):
    if not timestamp:
        return "never"
    return duration(time.time() - timestamp) + " ago"


def level(value, limit, warn_ratio=0.85):
    """Traffic light for a percentage-like value against a limit."""
    if value is None:
        return GREY
    if value >= limit:
        return RED
    if value >= limit * warn_ratio:
        return YELLOW
    return GREEN


def worst(levels):
    for colour in (RED, YELLOW):
        if colour in levels:
            return colour
    return GREEN


def pct(value):
    return "?" if value is None else f"{value:.1f}%"


# ------------------------------------------------------------ helpers

def node_levels(snapshot, cfg):
    """Per-metric traffic lights for a snapshot."""
    levels = {}
    levels["cpu"] = level(snapshot["cpu"].get("percent"), cfg.cpu_percent)
    levels["memory"] = level(snapshot["memory"].get("percent"), cfg.memory_percent)
    disk_levels = [
        level(d["percent"], cfg.disk_percent)
        for d in snapshot["disks"] if d["mount"] not in cfg.disk_ignore
    ]
    levels["disk"] = worst(disk_levels) if disk_levels else GREY
    temp = metrics.max_temperature(snapshot["temps"])
    levels["temp"] = level(temp, cfg.temp_c) if temp is not None else GREY
    docker = snapshot["docker"]
    if not docker.get("available"):
        levels["docker"] = GREY
    elif docker["healthy"] < docker["total"]:
        levels["docker"] = YELLOW if docker["healthy"] else RED
    else:
        levels["docker"] = GREEN
    return levels


def overall(levels, extra=()):
    colour = worst(list(levels.values()) + list(extra))
    if colour == RED:
        return RED, "Problems detected"
    if colour == YELLOW:
        return YELLOW, "Attention required"
    return GREEN, "All good"


def stale(node, cfg):
    """True when an agent node has missed enough heartbeats to count as offline."""
    if node["role"] == "central":
        return False
    last_seen = node.get("last_seen") or 0
    return time.time() - last_seen > cfg.heartbeat_interval * cfg.offline_after_missed


def node_status_icon(node, cfg, snapshot=None):
    if stale(node, cfg):
        return RED
    if snapshot is None:
        return GREY
    colour, _ = overall(node_levels(snapshot, cfg))
    return colour


# ----------------------------------------------------------- commands

def status(node, snapshot, cfg):
    name = e(node["name"])
    if stale(node, cfg):
        return (f"🖥 <b>{name}</b>\n{RED} OFFLINE — last heartbeat {ago(node.get('last_seen'))}")
    if snapshot is None:
        return f"🖥 <b>{name}</b>\n{GREY} No data yet"

    levels = node_levels(snapshot, cfg)
    colour, text = overall(levels)
    cpu, mem, docker = snapshot["cpu"], snapshot["memory"], snapshot["docker"]

    worst_disk = None
    for disk in snapshot["disks"]:
        if disk["mount"] in cfg.disk_ignore:
            continue
        if worst_disk is None or disk["percent"] > worst_disk["percent"]:
            worst_disk = disk

    load = f" · load {cpu['load'][0]:.2f}" if cpu.get("load") else ""
    temp = metrics.max_temperature(snapshot["temps"])

    lines = [
        f"🖥 <b>{name}</b>",
        f"{colour} {text}",
        "",
        f"⚙️ CPU      {pct(cpu.get('percent'))}{load}",
        f"🧠 Memory   {pct(mem.get('percent'))} · {human_bytes(mem.get('used'))}/{human_bytes(mem.get('total'))}",
    ]
    if worst_disk:
        lines.append(f"💾 Disk     {pct(worst_disk['percent'])} · {e(worst_disk['mount'])}")
    else:
        lines.append("💾 Disk     unavailable")
    lines.append(f"🌡️ Temp     {f'{temp:.1f} °C' if temp is not None else 'no sensor'}")
    if docker.get("available"):
        lines.append(f"🐳 Docker   {docker['healthy']}/{docker['total']} healthy")
    else:
        lines.append("🐳 Docker   unavailable")
    lines.append(f"⏱️ Uptime   {duration(snapshot.get('uptime'))}")
    lan = [ip for ip in snapshot["ips"] if not ip["iface"].startswith(("docker", "br-", "virbr", "tailscale"))]
    lines.append(f"🌐 IP       http://{e(lan[0]['ip'])}" if lan else "🌐 IP       unavailable")
    if node["role"] != "central":
        lines.append(f"📡 Seen     {ago(node.get('last_seen'))}")
    return "\n".join(lines)


def health(node, snapshot, cfg, active_alerts):
    name = e(node["name"])
    if stale(node, cfg):
        return f"🩺 <b>HEALTH — {name}</b>\n\n{RED} OFFLINE — last heartbeat {ago(node.get('last_seen'))}"
    if snapshot is None:
        return f"🩺 <b>HEALTH — {name}</b>\n\n{GREY} No data yet"

    levels = node_levels(snapshot, cfg)
    cpu, mem, docker = snapshot["cpu"], snapshot["memory"], snapshot["docker"]
    lines = [f"🩺 <b>HEALTH — {name}</b>", ""]
    lines.append(f"{levels['cpu']} CPU: {pct(cpu.get('percent'))} (limit {cfg.cpu_percent:.0f}%)")
    lines.append(f"{levels['memory']} Memory: {pct(mem.get('percent'))} (limit {cfg.memory_percent:.0f}%)")
    for disk in snapshot["disks"]:
        ignored = disk["mount"] in cfg.disk_ignore
        icon = GREY if ignored else level(disk["percent"], cfg.disk_percent)
        suffix = " (ignored)" if ignored else ""
        lines.append(f"{icon} Disk {e(disk['mount'])}: {pct(disk['percent'])}{suffix}")
    temp = metrics.max_temperature(snapshot["temps"])
    if temp is None:
        lines.append(f"{GREY} Temperature: no sensor")
    else:
        lines.append(f"{levels['temp']} Temperature: {temp:.1f} °C (limit {cfg.temp_c:.0f} °C)")
    if docker.get("available"):
        lines.append(f"{levels['docker']} Docker: {docker['healthy']}/{docker['total']} healthy")
    else:
        lines.append(f"{GREY} Docker: unavailable")

    backups = snapshot.get("backup") or {}
    for stage_key, label in (("stage1", "Server backup"), ("stage2", "Offsite backup")):
        stage = backups.get(stage_key)
        if not stage:
            continue
        icon, text = backup_line(stage, cfg)
        lines.append(f"{icon} {label}: {text}")

    problems = list(docker.get("problems") or [])
    if problems:
        lines += ["", "Problems:"] + [f"• {e(p)}" for p in problems]
    if active_alerts:
        lines += ["", "Active alerts:"] + [f"• {e(a['detail'] or a['key'])}" for a in active_alerts]
    lines += ["", f"Automatic checks: every {cfg.check_interval}s"]
    return "\n".join(lines)


def backup_line(stage, cfg):
    """(icon, text) describing a backup stage result and its age."""
    if not stage.get("available"):
        return GREY, f"unavailable ({e(stage.get('error', '?'))})"
    age_hours = (time.time() - stage.get("mtime", 0)) / 3600
    when = stage.get("finished") or stage.get("started") or "?"
    if stage["status"] == "running":
        return YELLOW, f"running since {e(stage.get('started') or '?')}"
    if stage["status"] == "failed":
        return RED, f"FAILED — {e(stage.get('detail') or when)}"
    if age_hours > cfg.backup_max_age_hours:
        return RED, f"stale — last OK {e(when)} ({age_hours / 24:.1f} days ago)"
    return GREEN, f"OK — {e(when)}"


def backups(nodes_with_snapshots, cfg):
    lines = ["💾 <b>BACKUPS</b>", ""]
    found = False
    for node, snapshot in nodes_with_snapshots:
        stages = (snapshot or {}).get("backup") or {}
        for stage_key, label in (("stage1", "Server-side backup"), ("stage2", "Offsite backup")):
            stage = stages.get(stage_key)
            if not stage:
                continue
            found = True
            icon, text = backup_line(stage, cfg)
            lines.append(f"{icon} <b>{e(label)}</b>")
            lines.append(f"   on {e(node['name'])}: {text}")
            if stage.get("available") and stage_key == "stage2":
                for folder in stage.get("folders", []):
                    lines.append(f"   • {e(folder['name'])} {e(folder.get('size') or '')} {e(folder.get('status') or '')}")
                if stage.get("elapsed"):
                    lines.append(f"   elapsed {e(stage['elapsed'])}")
            lines.append("")
    if not found:
        lines.append("No backup information reported yet.")
    lines.append(f"Stale after {cfg.backup_max_age_hours}h without a successful run.")
    return "\n".join(lines)


def servers(nodes, cfg, snapshot_for):
    lines = ["🖥 <b>SERVERS</b>", ""]
    for node in nodes:
        snapshot = snapshot_for(node)
        icon = node_status_icon(node, cfg, snapshot)
        if node["role"] == "central":
            text = "central"
        elif stale(node, cfg):
            text = f"offline · last seen {ago(node.get('last_seen'))}"
        else:
            text = "online"
        lines.append(f"{icon} {e(node['name'])} — {text}")
    lines += ["", "Use /status server-name"]
    return "\n".join(lines)


def apps(node, snapshot, cfg):
    name = e(node["name"])
    if snapshot is None or stale(node, cfg):
        return f"📦 <b>APPS — {name}</b>\n\n{GREY} No data"
    docker = snapshot["docker"]
    if not docker.get("available"):
        return f"📦 <b>APPS — {name}</b>\n\n⚠️ Docker unavailable"
    lines = [f"📦 <b>APPS — {name}</b> ({docker['healthy']}/{docker['total']} healthy)", ""]
    for container in docker["containers"]:
        ok = container["state"] == "running" and container["health"] != "unhealthy"
        icon = GREEN if ok else (YELLOW if container["state"] == "running" else RED)
        ports = ", ".join(container["ports"]) if container["ports"] else "none published"
        lines.append(f"{icon} <b>{e(container['name'])}</b>")
        lines.append(f"{e(container['status'])}")
        lines.append(f"Ports: {e(ports)}")
        lines.append(f"Image: {e(container['image'])}")
        lines.append("")
    return "\n".join(lines).rstrip()


def docker_summary(node, snapshot, cfg, known):
    name = e(node["name"])
    if snapshot is None or stale(node, cfg):
        return f"🐳 <b>DOCKER — {name}</b>\n\n{GREY} No data"
    docker = snapshot["docker"]
    if not docker.get("available"):
        return f"🐳 <b>DOCKER — {name}</b>\n\n⚠️ Docker unavailable\n{e(docker.get('error') or '')}"
    present = {c["name"] for c in docker["containers"]}
    missing = [k for k in known if k not in present]
    stopped = [c["name"] for c in docker["containers"] if c["state"] != "running"]
    unhealthy = [c["name"] for c in docker["containers"] if c["health"] == "unhealthy"]
    lines = [
        f"🐳 <b>DOCKER — {name}</b>",
        "",
        f"Version: {e(docker.get('version') or '?')}",
        f"Containers: {docker['total']} total · {docker['running']} running · {len(stopped)} stopped",
        f"Unhealthy: {len(unhealthy)}",
        f"Known: {len(known)} · missing: {len(missing)}",
    ]
    if stopped:
        lines += ["", "Stopped:"] + [f"• {e(n)}" for n in stopped]
    if unhealthy:
        lines += ["", "Unhealthy:"] + [f"• {e(n)}" for n in unhealthy]
    if missing:
        lines += ["", "Missing (use /forget name if removed on purpose):"] + [f"• {e(n)}" for n in missing]
    return "\n".join(lines)


def cpu(node, snapshot, cfg):
    name = e(node["name"])
    if snapshot is None or stale(node, cfg):
        return f"⚙️ <b>CPU — {name}</b>\n\n{GREY} No data"
    data = snapshot["cpu"]
    load = " / ".join(f"{v:.2f}" for v in data["load"]) if data.get("load") else "?"
    lines = [
        f"⚙️ <b>CPU — {name}</b>",
        "",
        f"Usage: {pct(data.get('percent'))} (limit {cfg.cpu_percent:.0f}%)",
        f"Load: {load}",
        f"Cores: {data.get('cores', '?')}",
    ]
    if data.get("top"):
        lines += ["", "Top processes:"] + [f"{e(p['name'])} — {p['percent']:.1f}%" for p in data["top"]]
    return "\n".join(lines)


def memory(node, snapshot, cfg):
    name = e(node["name"])
    if snapshot is None or stale(node, cfg):
        return f"🧠 <b>MEMORY — {name}</b>\n\n{GREY} No data"
    data = snapshot["memory"]
    lines = [
        f"🧠 <b>MEMORY — {name}</b>",
        "",
        f"Total: {human_bytes(data.get('total'))}",
        f"Used: {human_bytes(data.get('used'))} ({pct(data.get('percent'))})",
        f"Available: {human_bytes(data.get('available'))}",
        f"Limit: {cfg.memory_percent:.0f}%",
    ]
    if data.get("top"):
        lines += ["", "Top processes:"] + [f"{e(p['name'])} — {human_bytes(p['rss'])}" for p in data["top"]]
    return "\n".join(lines)


def disk(node, snapshot, cfg):
    name = e(node["name"])
    if snapshot is None or stale(node, cfg):
        return f"💾 <b>DISK — {name}</b>\n\n{GREY} No data"
    lines = [f"💾 <b>DISK — {name}</b>", ""]
    if not snapshot["disks"]:
        lines.append("No filesystems found")
    for d in snapshot["disks"]:
        ignored = d["mount"] in cfg.disk_ignore
        icon = GREY if ignored else level(d["percent"], cfg.disk_percent)
        lines.append(f"{icon} {e(d['mount'])} — {pct(d['percent'])}")
        lines.append(f"   {human_bytes(d['used'])} used · {human_bytes(d['free'])} free of {human_bytes(d['total'])}")
    lines += ["", f"Limit: {cfg.disk_percent:.0f}%"]
    return "\n".join(lines)


def temperature(node, snapshot, cfg):
    name = e(node["name"])
    if snapshot is None or stale(node, cfg):
        return f"🌡️ <b>TEMPERATURE — {name}</b>\n\n{GREY} No data"
    temps = snapshot["temps"]
    lines = [f"🌡️ <b>TEMPERATURE — {name}</b>", ""]
    if not temps:
        lines.append("No sensor available")
        return "\n".join(lines)
    for t in temps:
        lines.append(f"{level(t['celsius'], cfg.temp_c)} {e(t['chip'])} {e(t['label'])}: {t['celsius']:.1f} °C")
    lines += ["", f"Limit: {cfg.temp_c:.0f} °C"]
    return "\n".join(lines)


def network(node, snapshot, cfg):
    name = e(node["name"])
    if snapshot is None or stale(node, cfg):
        return f"🌐 <b>NETWORK — {name}</b>\n\n{GREY} No data"
    lines = [f"🌐 <b>NETWORK — {name}</b>", ""]
    if not snapshot["ips"]:
        lines.append("No interfaces with IPv4 addresses")
    for ip in snapshot["ips"]:
        lines.append(f"<b>{e(ip['iface'])}</b>")
        lines.append(f"{e(ip['ip'])}")
        lines.append("")
    lines.append(f"Hostname: {e(snapshot.get('hostname') or '?')}")
    return "\n".join(lines)


def ports(node, ports_list):
    name = e(node["name"])
    lines = [f"🔌 <b>PORTS — {name}</b>", ""]
    if ports_list is None:
        lines.append("Port listing is only available for the central server.")
        return "\n".join(lines)
    if not ports_list:
        lines.append("No listening TCP ports")
    for p in ports_list:
        label = f" — {e(p['process'])}" if p["process"] else ""
        lines.append(f"{p['port']}{label}")
    return "\n".join(lines)


def help_text(version):
    return "\n".join([
        f"🤖 <b>Homelab Monitor v{e(version)}</b>",
        "",
        "<b>Overview</b>",
        "/servers — registered servers and their state",
        "/status [server] — quick status",
        "/health [server] — detailed diagnosis",
        "/backups — last result of each backup stage",
        "",
        "<b>Details</b>",
        "/apps [server] — Docker containers",
        "/docker [server] — Docker summary and missing containers",
        "/cpu [server] · /memory [server] · /disk [server]",
        "/temperature [server] · /network [server] · /ports",
        "",
        "<b>Manage</b>",
        "/rename old new — rename a server",
        "/remove server — unregister an agent",
        "/forget name [server] — stop tracking a removed container",
        "/integrate — how to add a server",
        "/clear — delete recent bot messages",
        "/version · /help",
    ])
