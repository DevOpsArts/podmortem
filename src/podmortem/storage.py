"""SQLite-backed storage for pod restart events."""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel


class RestartRecord(BaseModel):
    """A single pod restart event record."""

    pod_name: str
    namespace: str
    container_name: str
    restart_count: int
    reason: str
    exit_code: Optional[int] = None
    last_logs: str
    events: str
    node_name: Optional[str] = None
    timestamp: str


def _default_db_path() -> Path:
    """Return a sensible default DB path depending on environment."""
    # In-cluster: use /data volume mount
    # Local: use ~/.local/share/podmortem/
    data_dir = Path("/data")
    if data_dir.exists() and os.access(data_dir, os.W_OK):
        return data_dir / "podmortem.db"
    local_dir = Path.home() / ".local" / "share" / "podmortem"
    local_dir.mkdir(parents=True, exist_ok=True)
    return local_dir / "podmortem.db"


def get_db_path() -> Path:
    """Return the database path, respecting PODMORTEM_DB_PATH env var."""
    import os as _os

    env_path = _os.environ.get("PODMORTEM_DB_PATH")
    if env_path:
        path = Path(env_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return _default_db_path()


def init_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Initialize the SQLite database and return a connection."""
    if db_path is None:
        db_path = get_db_path()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS restarts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pod_name TEXT NOT NULL,
            namespace TEXT NOT NULL,
            container_name TEXT NOT NULL,
            restart_count INTEGER NOT NULL,
            reason TEXT NOT NULL,
            exit_code INTEGER,
            last_logs TEXT,
            events TEXT,
            node_name TEXT,
            timestamp TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_restarts_namespace
        ON restarts(namespace)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_restarts_pod
        ON restarts(pod_name)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_restarts_timestamp
        ON restarts(timestamp)
    """)
    conn.commit()
    return conn


def store_restart(conn: sqlite3.Connection, record: RestartRecord) -> int:
    """Store a restart record and return the row ID."""
    cursor = conn.execute(
        """
        INSERT INTO restarts
            (pod_name, namespace, container_name, restart_count, reason,
             exit_code, last_logs, events, node_name, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.pod_name,
            record.namespace,
            record.container_name,
            record.restart_count,
            record.reason,
            record.exit_code,
            record.last_logs,
            record.events,
            record.node_name,
            record.timestamp,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def query_restarts(
    conn: sqlite3.Connection,
    namespace: Optional[str] = None,
    pod_name: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Query restart records with optional filters."""
    conditions = []
    params = []

    if namespace:
        conditions.append("namespace = ?")
        params.append(namespace)
    if pod_name:
        conditions.append("pod_name LIKE ?")
        params.append(f"%{pod_name}%")
    if since:
        conditions.append("timestamp >= ?")
        params.append(since)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT * FROM restarts
        {where_clause}
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    return [dict(row) for row in rows]


def delete_restarts(
    conn: sqlite3.Connection,
    record_id: Optional[int] = None,
    namespace: Optional[str] = None,
    pod_name: Optional[str] = None,
    before: Optional[str] = None,
    all_records: bool = False,
) -> int:
    """Delete restart records matching filters. Returns count of deleted rows."""
    if not any([record_id, namespace, pod_name, before, all_records]):
        return 0

    if all_records:
        cursor = conn.execute("DELETE FROM restarts")
        conn.commit()
        return cursor.rowcount

    conditions = []
    params: list = []

    if record_id is not None:
        conditions.append("id = ?")
        params.append(record_id)
    if namespace:
        conditions.append("namespace = ?")
        params.append(namespace)
    if pod_name:
        conditions.append("pod_name LIKE ?")
        params.append(f"%{pod_name}%")
    if before:
        conditions.append("timestamp < ?")
        params.append(before)

    where_clause = "WHERE " + " AND ".join(conditions)
    cursor = conn.execute(f"DELETE FROM restarts {where_clause}", params)
    conn.commit()
    return cursor.rowcount
