"""
Persistencia en PostgreSQL. Reemplaza el SQLite original porque Coolify no
ofrece SQLite como servicio ni almacenamiento persistente por defecto —
en cambio sí provee PostgreSQL con volumen persistente administrado.

Tablas:
  - filters: pares (encoder, título) que el usuario quiere seguir
             (título vacío = "cualquier título de este encoder")
  - seen_items: guids del RSS ya procesados (para no reencolar duplicados)
  - queue_items: cola de descargas con su estado
  - seedr_token: fila única con el access_token/refresh_token de Seedr,
                 vinculado desde Telegram con /auth (nunca se sube a git)

Usa un pool de conexiones simple (psycopg2) y funciones síncronas, llamadas
desde el código async vía `asyncio.to_thread(...)`.
"""
import logging
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

import config

logger = logging.getLogger(__name__)

_pool: Optional[ThreadedConnectionPool] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS filters (
    id SERIAL PRIMARY KEY,
    encoder TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    added_by BIGINT,
    created_at DOUBLE PRECISION NOT NULL,
    UNIQUE(encoder, title)
);

CREATE TABLE IF NOT EXISTS seen_items (
    guid TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    seen_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS queue_items (
    id SERIAL PRIMARY KEY,
    guid TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    torrent_url TEXT NOT NULL,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    seedr_task_id TEXT,
    error_message TEXT,
    added_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);

-- Fila única (id siempre 1) con el token OAuth vigente de Seedr.
CREATE TABLE IF NOT EXISTS seedr_token (
    id INTEGER PRIMARY KEY DEFAULT 1,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    obtained_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    updated_by BIGINT,
    CONSTRAINT seedr_token_singleton CHECK (id = 1)
);
"""


def init_db() -> None:
    global _pool
    if not config.DATABASE_URL:
        raise SystemExit(
            "Falta DATABASE_URL. En Coolify: creá un recurso de PostgreSQL, "
            "copiá su connection string y ponela como variable de entorno "
            "DATABASE_URL de esta app (formato postgresql://user:pass@host:puerto/db)."
        )
    _pool = ThreadedConnectionPool(minconn=1, maxconn=10, dsn=config.DATABASE_URL)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)


@contextmanager
def _conn():
    if _pool is None:
        raise RuntimeError("La base de datos no fue inicializada. Llama a db.init_db() primero.")
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def _dictcur(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# ------------------------------------------------------------------ filters

def add_filter(encoder: str, title: Optional[str], added_by: Optional[int]) -> int:
    """title puede ser None o '' para indicar "cualquier título de este encoder"."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO filters (encoder, title, added_by, created_at) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (encoder.strip(), (title or "").strip(), added_by, time.time()),
            )
            return cur.fetchone()[0]


def remove_filter(filter_id: int) -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM filters WHERE id = %s", (filter_id,))
            return cur.rowcount > 0


def list_filters() -> List[Dict[str, Any]]:
    with _conn() as conn:
        with _dictcur(conn) as cur:
            cur.execute("SELECT * FROM filters ORDER BY encoder, title")
            return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------- seen_items

def is_seen(guid: str) -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM seen_items WHERE guid = %s", (guid,))
            return cur.fetchone() is not None


def mark_seen(guid: str, title: str) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO seen_items (guid, title, seen_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (guid) DO NOTHING",
                (guid, title, time.time()),
            )


# -------------------------------------------------------------- queue_items

def enqueue_item(guid: str, title: str, torrent_url: str, size_bytes: int) -> Optional[int]:
    with _conn() as conn:
        with conn.cursor() as cur:
            now = time.time()
            cur.execute(
                """INSERT INTO queue_items
                   (guid, title, torrent_url, size_bytes, status, added_at, updated_at)
                   VALUES (%s, %s, %s, %s, 'pending', %s, %s)
                   ON CONFLICT (guid) DO NOTHING
                   RETURNING id""",
                (guid, title, torrent_url, size_bytes, now, now),
            )
            row = cur.fetchone()
            return row[0] if row else None


def update_item_status(
    item_id: int,
    status: str,
    seedr_task_id: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE queue_items
                   SET status = %s,
                       seedr_task_id = COALESCE(%s, seedr_task_id),
                       error_message = %s,
                       updated_at = %s
                   WHERE id = %s""",
                (status, seedr_task_id, error_message, time.time(), item_id),
            )


def get_pending_items() -> List[Dict[str, Any]]:
    with _conn() as conn:
        with _dictcur(conn) as cur:
            cur.execute("SELECT * FROM queue_items WHERE status = 'pending' ORDER BY added_at")
            return [dict(r) for r in cur.fetchall()]


def get_active_items() -> List[Dict[str, Any]]:
    """Items que no están en un estado terminal (done/error)."""
    with _conn() as conn:
        with _dictcur(conn) as cur:
            cur.execute(
                "SELECT * FROM queue_items WHERE status NOT IN ('done', 'error') ORDER BY added_at"
            )
            return [dict(r) for r in cur.fetchall()]


def get_item(item_id: int) -> Optional[Dict[str, Any]]:
    with _conn() as conn:
        with _dictcur(conn) as cur:
            cur.execute("SELECT * FROM queue_items WHERE id = %s", (item_id,))
            row = cur.fetchone()
            return dict(row) if row else None


# ---------------------------------------------------------------- seedr_token

def get_seedr_token() -> Optional[Dict[str, Any]]:
    with _conn() as conn:
        with _dictcur(conn) as cur:
            cur.execute("SELECT * FROM seedr_token WHERE id = 1")
            row = cur.fetchone()
            return dict(row) if row else None


def save_seedr_token(
    access_token: str,
    refresh_token: Optional[str],
    expires_at: float,
    updated_by: Optional[int] = None,
) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO seedr_token (id, access_token, refresh_token, obtained_at, expires_at, updated_by)
                   VALUES (1, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET
                       access_token = EXCLUDED.access_token,
                       refresh_token = COALESCE(EXCLUDED.refresh_token, seedr_token.refresh_token),
                       obtained_at = EXCLUDED.obtained_at,
                       expires_at = EXCLUDED.expires_at,
                       updated_by = EXCLUDED.updated_by""",
                (access_token, refresh_token, time.time(), expires_at, updated_by),
            )


def clear_seedr_token() -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seedr_token WHERE id = 1")
