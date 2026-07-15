"""
Persistencia en SQLite:
  - filters: pares (encoder, título) que el usuario quiere seguir
  - seen_items: guids del RSS ya procesados (para no reencolar duplicados)
  - queue_items: cola de descargas con su estado (pending/downloading/uploading/done/error)

Todas las funciones son síncronas; se llaman desde el código async vía
`asyncio.to_thread(...)` para no bloquear el event loop.
"""
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS filters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    encoder TEXT NOT NULL,
    -- title vacío ('') significa "cualquier título de este encoder".
    title TEXT NOT NULL DEFAULT '',
    added_by INTEGER,
    created_at REAL NOT NULL,
    UNIQUE(encoder, title)
);

CREATE TABLE IF NOT EXISTS seen_items (
    guid TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    seen_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS queue_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guid TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    torrent_url TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    seedr_task_id TEXT,
    seedr_folder_id TEXT,
    error_message TEXT,
    added_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


@contextmanager
def _conn():
    with _lock:
        conn = sqlite3.connect(config.DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(SCHEMA)


# ------------------------------------------------------------------ filters

def add_filter(encoder: str, title: Optional[str], added_by: Optional[int]) -> int:
    """title puede ser None o '' para indicar "cualquier título de este encoder"."""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO filters (encoder, title, added_by, created_at) VALUES (?, ?, ?, ?)",
            (encoder.strip(), (title or "").strip(), added_by, time.time()),
        )
        return cur.lastrowid


def remove_filter(filter_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM filters WHERE id = ?", (filter_id,))
        return cur.rowcount > 0


def list_filters() -> List[Dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM filters ORDER BY encoder, title").fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------- seen_items

def is_seen(guid: str) -> bool:
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM seen_items WHERE guid = ?", (guid,)).fetchone()
        return row is not None


def mark_seen(guid: str, title: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_items (guid, title, seen_at) VALUES (?, ?, ?)",
            (guid, title, time.time()),
        )


# -------------------------------------------------------------- queue_items

def enqueue_item(guid: str, title: str, torrent_url: str, size_bytes: int) -> int:
    with _conn() as conn:
        now = time.time()
        cur = conn.execute(
            """INSERT OR IGNORE INTO queue_items
               (guid, title, torrent_url, size_bytes, status, added_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
            (guid, title, torrent_url, size_bytes, now, now),
        )
        return cur.lastrowid


def update_item_status(
    item_id: int,
    status: str,
    seedr_task_id: Optional[str] = None,
    seedr_folder_id: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    with _conn() as conn:
        conn.execute(
            """UPDATE queue_items
               SET status = ?, seedr_task_id = COALESCE(?, seedr_task_id),
                   seedr_folder_id = COALESCE(?, seedr_folder_id),
                   error_message = ?, updated_at = ?
               WHERE id = ?""",
            (status, seedr_task_id, seedr_folder_id, error_message, time.time(), item_id),
        )


def get_pending_items() -> List[Dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM queue_items WHERE status = 'pending' ORDER BY added_at"
        ).fetchall()
        return [dict(r) for r in rows]


def get_active_items() -> List[Dict[str, Any]]:
    """Items que no están en un estado terminal (done/error)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM queue_items WHERE status NOT IN ('done', 'error') ORDER BY added_at"
        ).fetchall()
        return [dict(r) for r in rows]


def get_item(item_id: int) -> Optional[Dict[str, Any]]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None
