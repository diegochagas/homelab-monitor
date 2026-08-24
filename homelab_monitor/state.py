"""Persistent state in SQLite: registered nodes, snapshots, alerts, known containers."""

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    token       TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'node',
    hostname    TEXT,
    registered  INTEGER NOT NULL,
    last_seen   INTEGER,
    snapshot    TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    key            TEXT PRIMARY KEY,
    node           TEXT NOT NULL,
    failures       INTEGER NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 0,
    first_seen     INTEGER,
    last_notified  INTEGER,
    detail         TEXT
);
CREATE TABLE IF NOT EXISTS known_containers (
    node        TEXT NOT NULL,
    name        TEXT NOT NULL,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    PRIMARY KEY (node, name)
);
"""


class State:
    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "monitor.sqlite3")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ----------------------------------------------------------- nodes

    def upsert_central(self, name, hostname):
        with self._lock:
            row = self._conn.execute("SELECT id FROM nodes WHERE role='central'").fetchone()
            if row:
                self._conn.execute(
                    "UPDATE nodes SET name=?, hostname=? WHERE id=?", (name, hostname, row["id"])
                )
            else:
                self._conn.execute(
                    "INSERT INTO nodes (id, name, token, role, hostname, registered) VALUES (?,?,?,?,?,?)",
                    ("central", name, "", "central", hostname, int(time.time())),
                )
            self._conn.commit()

    def register_node(self, node_id, name, token, hostname):
        with self._lock:
            existing = self._conn.execute("SELECT id FROM nodes WHERE id=?", (node_id,)).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE nodes SET token=?, hostname=? WHERE id=?", (token, hostname, node_id)
                )
            else:
                # Avoid a name clash with another node: suffix the id.
                clash = self._conn.execute("SELECT id FROM nodes WHERE name=?", (name,)).fetchone()
                if clash:
                    name = f"{name}-{node_id[:4]}"
                self._conn.execute(
                    "INSERT INTO nodes (id, name, token, role, hostname, registered) VALUES (?,?,?,?,?,?)",
                    (node_id, name, token, "node", hostname, int(time.time())),
                )
            self._conn.commit()

    def node_by_id(self, node_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return dict(row) if row else None

    def node_by_name(self, name):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nodes WHERE lower(name)=lower(?)", (name,)
            ).fetchone()
        return dict(row) if row else None

    def nodes(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM nodes ORDER BY role='central' DESC, name"
            ).fetchall()
        return [dict(row) for row in rows]

    def rename_node(self, old, new):
        with self._lock:
            if self._conn.execute("SELECT 1 FROM nodes WHERE lower(name)=lower(?)", (new,)).fetchone():
                return False
            cursor = self._conn.execute(
                "UPDATE nodes SET name=? WHERE lower(name)=lower(?)", (new, old)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def remove_node(self, name):
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM nodes WHERE lower(name)=lower(?) AND role!='central'", (name,)
            ).fetchone()
            if not row:
                return False
            self._conn.execute("DELETE FROM nodes WHERE id=?", (row["id"],))
            self._conn.execute("DELETE FROM alerts WHERE node=?", (row["id"],))
            self._conn.execute("DELETE FROM known_containers WHERE node=?", (row["id"],))
            self._conn.commit()
            return True

    def store_snapshot(self, node_id, snapshot):
        with self._lock:
            self._conn.execute(
                "UPDATE nodes SET snapshot=?, last_seen=?, hostname=COALESCE(?, hostname) WHERE id=?",
                (json.dumps(snapshot), int(time.time()), snapshot.get("hostname"), node_id),
            )
            self._conn.commit()

    @staticmethod
    def snapshot_of(node):
        raw = node.get("snapshot") if node else None
        return json.loads(raw) if raw else None

    # ---------------------------------------------------------- alerts

    def alert(self, key):
        with self._lock:
            row = self._conn.execute("SELECT * FROM alerts WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def save_alert(self, key, node, failures, active, first_seen, last_notified, detail):
        with self._lock:
            self._conn.execute(
                """INSERT INTO alerts (key, node, failures, active, first_seen, last_notified, detail)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET failures=excluded.failures, active=excluded.active,
                   first_seen=excluded.first_seen, last_notified=excluded.last_notified,
                   detail=excluded.detail""",
                (key, node, failures, int(active), first_seen, last_notified, detail),
            )
            self._conn.commit()

    def clear_alerts_except_backup(self, node_id):
        with self._lock:
            self._conn.execute(
                "DELETE FROM alerts WHERE node=? AND key NOT LIKE ?", (node_id, f"{node_id}:backup:%")
            )
            self._conn.commit()

    def active_alerts(self, node_id=None):
        with self._lock:
            if node_id:
                rows = self._conn.execute(
                    "SELECT * FROM alerts WHERE active=1 AND node=? ORDER BY key", (node_id,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM alerts WHERE active=1 ORDER BY key").fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------ known containers

    def known_containers(self, node_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM known_containers WHERE node=? ORDER BY name", (node_id,)
            ).fetchall()
        return [row["name"] for row in rows]

    def remember_containers(self, node_id, names):
        now = int(time.time())
        with self._lock:
            for name in names:
                self._conn.execute(
                    """INSERT INTO known_containers (node, name, first_seen, last_seen) VALUES (?,?,?,?)
                       ON CONFLICT(node, name) DO UPDATE SET last_seen=excluded.last_seen""",
                    (node_id, name, now, now),
                )
            self._conn.commit()

    def forget_container(self, node_id, name):
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM known_containers WHERE node=? AND name=?", (node_id, name)
            )
            self._conn.execute("DELETE FROM alerts WHERE key=?", (f"{node_id}:container:{name}",))
            self._conn.commit()
            return cursor.rowcount > 0
