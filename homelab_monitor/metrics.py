"""Metrics collection from /proc, /sys and the Docker socket (stdlib only).

Every collector returns something usable even when its data source is missing:
the bot reports a metric as unavailable instead of crashing.
"""

import fcntl
import os
import re
import socket
import struct
import time

from . import dockerapi

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

REAL_FILESYSTEMS = {
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs",
    "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "hfsplus", "squashfs",
}
SKIP_MOUNT_PREFIXES = ("/var/lib/docker", "/snap/", "/boot", "/mnt/overlay", "/proc", "/sys", "/dev", "/run")


def _read(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return default


# ---------------------------------------------------------------- CPU

def _cpu_times():
    line = _read("/proc/stat").split("\n", 1)[0]
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None
    values = [int(v) for v in parts[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return idle, sum(values)


def _proc_pids():
    try:
        return [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return []


def _proc_stat(pid):
    """Return (comm, cpu_ticks) for a pid, or None."""
    raw = _read(f"/proc/{pid}/stat")
    if not raw:
        return None
    start = raw.find("(")
    end = raw.rfind(")")
    if start < 0 or end < 0:
        return None
    comm = raw[start + 1:end]
    fields = raw[end + 2:].split()
    try:
        utime = int(fields[11])
        stime = int(fields[12])
    except (IndexError, ValueError):
        return None
    return comm, utime + stime


def cpu(interval=0.5, top=5):
    """CPU usage sampled over `interval` seconds, plus the top processes."""
    first = _cpu_times()
    procs_first = {}
    for pid in _proc_pids():
        stat = _proc_stat(pid)
        if stat:
            procs_first[pid] = stat

    time.sleep(interval)

    second = _cpu_times()
    percent = None
    if first and second:
        idle = second[0] - first[0]
        total = second[1] - first[1]
        if total > 0:
            percent = round(100.0 * (total - idle) / total, 1)

    cores = os.cpu_count() or 1
    top_processes = []
    if first and second and second[1] - first[1] > 0:
        total_ticks = second[1] - first[1]
        deltas = []
        for pid, (comm, ticks) in procs_first.items():
            stat = _proc_stat(pid)
            if stat:
                delta = stat[1] - ticks
                if delta > 0:
                    deltas.append((delta, comm, pid))
        deltas.sort(reverse=True)
        for delta, comm, pid in deltas[:top]:
            # total_ticks covers all cores, so this is share of whole machine
            top_processes.append({
                "name": comm,
                "pid": int(pid),
                "percent": round(100.0 * delta / total_ticks, 1),
            })

    load = None
    loadavg = _read("/proc/loadavg").split()
    if len(loadavg) >= 3:
        try:
            load = [float(loadavg[0]), float(loadavg[1]), float(loadavg[2])]
        except ValueError:
            load = None

    return {"percent": percent, "load": load, "cores": cores, "top": top_processes}


# ------------------------------------------------------------- Memory

def memory(top=5):
    info = {}
    for line in _read("/proc/meminfo").splitlines():
        match = re.match(r"^(\w+):\s+(\d+)", line)
        if match:
            info[match.group(1)] = int(match.group(2)) * 1024

    total = info.get("MemTotal")
    if not total:
        return {"total": None, "used": None, "available": None, "percent": None, "top": []}
    available = info.get("MemAvailable", info.get("MemFree", 0))
    used = total - available

    processes = []
    for pid in _proc_pids():
        statm = _read(f"/proc/{pid}/statm").split()
        if len(statm) < 2:
            continue
        try:
            rss = int(statm[1]) * _PAGE_SIZE
        except ValueError:
            continue
        if rss <= 0:
            continue
        comm = _read(f"/proc/{pid}/comm").strip() or "?"
        processes.append({"name": comm, "pid": int(pid), "rss": rss})
    processes.sort(key=lambda p: p["rss"], reverse=True)

    return {
        "total": total,
        "used": used,
        "available": available,
        "percent": round(100.0 * used / total, 1),
        "top": processes[:top],
    }


# --------------------------------------------------------------- Disks

def disks(host_root=""):
    """Mounted real filesystems, deduplicated by device."""
    mounts = _read("/proc/1/mounts") or _read("/proc/mounts")
    seen_devices = {}
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mountpoint, fstype = parts[0], parts[1], parts[2]
        mountpoint = mountpoint.replace("\\040", " ")
        if fstype not in REAL_FILESYSTEMS:
            continue
        if any(mountpoint.startswith(prefix) for prefix in SKIP_MOUNT_PREFIXES) and mountpoint != "/":
            continue
        if host_root and mountpoint.startswith(host_root):
            continue
        current = seen_devices.get(device)
        if current is None or _mount_rank(mountpoint) < _mount_rank(current):
            seen_devices[device] = mountpoint

    result = []
    for device, mountpoint in seen_devices.items():
        path = (host_root + mountpoint) if host_root else mountpoint
        try:
            stats = os.statvfs(path)
        except OSError:
            continue
        total = stats.f_blocks * stats.f_frsize
        if total == 0:
            continue
        free = stats.f_bavail * stats.f_frsize
        used = total - stats.f_bfree * stats.f_frsize
        percent = round(100.0 * used / (used + free), 1) if (used + free) else 0.0
        result.append({
            "mount": mountpoint,
            "device": device,
            "fstype": fstype_of(mounts, device, mountpoint),
            "total": total,
            "used": used,
            "free": free,
            "percent": percent,
        })
    result.sort(key=lambda d: d["mount"])
    return result


def _mount_rank(mountpoint):
    """Prefer user-facing mountpoints (/DATA over /opt, /var/lib/...) for a device."""
    system_like = mountpoint.startswith(("/var", "/opt", "/usr", "/etc"))
    return (system_like, len(mountpoint))


def fstype_of(mounts, device, mountpoint):
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == device and parts[1] == mountpoint:
            return parts[2]
    return "?"


# --------------------------------------------------------- Temperature

def temperatures():
    """Hardware sensors from /sys/class/hwmon. Empty list when none exist."""
    result = []
    base = "/sys/class/hwmon"
    try:
        chips = sorted(os.listdir(base))
    except OSError:
        return result
    for chip in chips:
        chip_dir = os.path.join(base, chip)
        chip_name = _read(os.path.join(chip_dir, "name")).strip() or chip
        try:
            files = sorted(os.listdir(chip_dir))
        except OSError:
            continue
        for file in files:
            match = re.match(r"^temp(\d+)_input$", file)
            if not match:
                continue
            raw = _read(os.path.join(chip_dir, file)).strip()
            try:
                celsius = int(raw) / 1000.0
            except ValueError:
                continue
            label = _read(os.path.join(chip_dir, f"temp{match.group(1)}_label")).strip()
            result.append({
                "chip": chip_name,
                "label": label or f"temp{match.group(1)}",
                "celsius": round(celsius, 1),
            })
    return result


def max_temperature(temps):
    """Most relevant temperature: the hottest CPU-ish sensor, else the hottest overall."""
    if not temps:
        return None
    cpu_like = [t for t in temps if re.search(r"(core|package|cpu|tctl|tdie|k10temp|coretemp)", (t["chip"] + t["label"]).lower())]
    pool = cpu_like or temps
    return max(t["celsius"] for t in pool)


# -------------------------------------------------------------- Uptime

def uptime():
    raw = _read("/proc/uptime").split()
    try:
        return int(float(raw[0]))
    except (IndexError, ValueError):
        return None


# ------------------------------------------------------------- Network

def ips():
    """IPv4 address per interface (excluding loopback and veth pairs)."""
    result = []
    try:
        interfaces = socket.if_nameindex()
    except OSError:
        return result
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _, name in sorted(interfaces, key=lambda i: i[1]):
            if name == "lo" or name.startswith("veth"):
                continue
            try:
                packed = fcntl.ioctl(
                    sock.fileno(),
                    0x8915,  # SIOCGIFADDR
                    struct.pack("256s", name.encode()[:15]),
                )
                address = socket.inet_ntoa(packed[20:24])
            except OSError:
                continue
            result.append({"iface": name, "ip": address})
    finally:
        sock.close()
    return result


# --------------------------------------------------------------- Ports

_PORT_NAMES = {
    22: "SSH", 53: "DNS", 80: "HTTP", 443: "HTTPS", 445: "SMB", 139: "NetBIOS",
    2283: "Immich", 8096: "Jellyfin", 8080: "Nextcloud", 8000: "Vaultwarden",
    5678: "n8n", 5432: "PostgreSQL", 3306: "MySQL", 6379: "Redis",
    8787: "Homelab Monitor",
}


def _socket_inodes_by_pid():
    mapping = {}
    for pid in _proc_pids():
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target.startswith("socket:["):
                mapping[target[8:-1]] = pid
    return mapping


def listening_ports():
    """TCP ports in LISTEN state, with the owning process when visible."""
    ports = {}
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in _read(table).splitlines()[1:]:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "0A":
                continue
            try:
                port = int(parts[1].rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            ports.setdefault(port, parts[9])

    inode_to_pid = _socket_inodes_by_pid() if ports else {}
    result = []
    for port in sorted(ports):
        pid = inode_to_pid.get(ports[port])
        process = _read(f"/proc/{pid}/comm").strip() if pid else ""
        result.append({
            "port": port,
            "process": process or _PORT_NAMES.get(port, ""),
        })
    return result


# -------------------------------------------------------------- Docker

def docker():
    ok, data = dockerapi.list_containers()
    if not ok:
        return {"available": False, "error": data, "containers": [], "version": None}
    summary = dockerapi.summarize(data)
    summary.update({"available": True, "containers": data, "version": dockerapi.version()})
    return summary


# ------------------------------------------------------------- Backups

_STAGE1_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$")


def server_backup_status(log_path):
    """Parse zimaos/backup.sh's log: last started/finished markers."""
    if not log_path or not os.path.isfile(log_path):
        return {"available": False, "error": "log not found"}
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError as error:
        return {"available": False, "error": str(error)}

    started = finished = aborted = None
    for line in tail.splitlines():
        match = _STAGE1_LINE.match(line)
        if not match:
            continue
        stamp, message = match.groups()
        if "Backup started" in message:
            started, finished, aborted = stamp, None, None
        elif "Backup finished" in message:
            finished = stamp
        elif message.startswith("ABORT"):
            aborted = f"{stamp} {message}"

    if not started:
        return {"available": False, "error": "no backup runs in log"}

    mtime = int(os.path.getmtime(log_path))
    status = "running"
    if finished:
        status = "ok"
    elif aborted:
        status = "failed"
    elif time.time() - mtime > 12 * 3600:
        # Started, never finished, log untouched for hours: the run died.
        status = "failed"
        aborted = f"started {started} and never finished"
    return {
        "available": True,
        "started": started,
        "finished": finished,
        "status": status,
        "detail": aborted,
        "mtime": mtime,
    }


_STAGE2_LOG = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.log$")


def _parse_stage2_log(path, name):
    text = _read(path)
    result = {
        "available": True,
        "log": name,
        "mtime": int(os.path.getmtime(path)),
        "mode": None,
        "started": None,
        "status": "running",
        "elapsed": None,
        "folders": [],
    }
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Mode:") and result["mode"] is None:
            result["mode"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Date:") and result["started"] is None:
            result["started"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Elapsed:"):
            result["elapsed"] = stripped.split(":", 1)[1].strip()
        elif "Backup failed" in stripped:
            result["status"] = "failed"
        elif stripped.startswith("• "):
            current = {"name": stripped[2:].strip(), "size": None, "status": None}
            result["folders"].append(current)
        elif current and stripped.startswith("Size:"):
            current["size"] = stripped.split(":", 1)[1].strip()
        elif current and stripped.startswith("Status:"):
            current["status"] = stripped.split(":", 1)[1].strip()
            current = None
        elif stripped.startswith("Status:") and "SUCCESS" in stripped:
            result["status"] = "ok"

    if result["status"] == "running" and time.time() - result["mtime"] > 86400:
        # A log without a footer that is older than a day is a crashed run.
        result["status"] = "failed"
    return result


def local_backup_status(log_dir):
    """Parse the latest real (non dry-run) backup.sh log in `log_dir`."""
    if not log_dir or not os.path.isdir(log_dir):
        return {"available": False, "error": "log dir not found"}
    try:
        candidates = sorted(name for name in os.listdir(log_dir) if _STAGE2_LOG.match(name))
    except OSError as error:
        return {"available": False, "error": str(error)}
    if not candidates:
        return {"available": False, "error": "no logs yet"}

    latest = None
    for name in reversed(candidates):
        parsed = _parse_stage2_log(os.path.join(log_dir, name), name)
        if latest is None:
            latest = parsed
        if parsed["mode"] != "Simulation":
            return parsed
    return latest


# ------------------------------------------------------------ Snapshot

def snapshot(cfg):
    """Everything the bot needs about this machine, JSON-serialisable."""
    data = {
        "collected_at": int(time.time()),
        "hostname": socket.gethostname(),
        "cpu": cpu(),
        "memory": memory(),
        "disks": disks(cfg.host_root),
        "temps": temperatures(),
        "uptime": uptime(),
        "ips": ips(),
        "docker": docker(),
        "backup": {},
    }
    if cfg.server_backup_log:
        data["backup"]["stage1"] = server_backup_status(cfg.host_path(cfg.server_backup_log))
    if cfg.backup_log_dir:
        data["backup"]["stage2"] = local_backup_status(cfg.host_path(cfg.backup_log_dir))
    return data
