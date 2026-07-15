"""
Monitorea el feed RSS (por defecto nyaa.si) y encola para descarga los items
que coincidan con los filtros (encoder + título) configurados por el usuario.

Formato de título esperado: "[Encoder] Nombre del release ..."
Ejemplo: "[ASW] Mushoku Tensei - 12 [1080p][ABCD1234].mkv"
  -> encoder = "ASW"
  -> resto   = "Mushoku Tensei - 12 [1080p][ABCD1234].mkv"

Un filtro (encoder="ASW", title="Mushoku Tensei") hace match si:
  - el encoder es exactamente igual (case-insensitive)
  - el título del filtro está contenido en el resto del título del item (case-insensitive)
"""
import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

import feedparser

import config
import db

logger = logging.getLogger(__name__)

TITLE_RE = re.compile(r"^\[(?P<encoder>[^\]]+)\]\s*(?P<rest>.+)$")

# Conversión de tamaños tipo "1.3 GiB" / "528.4 MiB" a bytes.
_SIZE_UNITS = {
    "B": 1,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
}
SIZE_RE = re.compile(r"([\d.]+)\s*([A-Za-z]+)")


def parse_size_to_bytes(size_str: Optional[str]) -> int:
    if not size_str:
        return 0
    match = SIZE_RE.match(size_str.strip())
    if not match:
        return 0
    value, unit = match.groups()
    multiplier = _SIZE_UNITS.get(unit.upper(), 0)
    try:
        return int(float(value) * multiplier)
    except ValueError:
        return 0


def parse_title(title: str) -> Optional[Dict[str, str]]:
    match = TITLE_RE.match(title.strip())
    if not match:
        return None
    return {"encoder": match.group("encoder").strip(), "rest": match.group("rest").strip()}


def item_matches_filter(parsed: Dict[str, str], flt: Dict[str, Any]) -> bool:
    if parsed["encoder"].lower() != flt["encoder"].strip().lower():
        return False
    filter_title = (flt.get("title") or "").strip().lower()
    if not filter_title:
        # Filtro solo-encoder: cualquier release de este encoder hace match.
        return True
    return filter_title in parsed["rest"].lower()


async def fetch_feed_entries() -> List[Dict[str, Any]]:
    """Descarga y parsea el feed RSS. feedparser es bloqueante -> a un hilo aparte."""
    def _parse():
        return feedparser.parse(config.RSS_FEED_URL)

    feed = await asyncio.to_thread(_parse)
    if feed.bozo and not feed.entries:
        logger.warning("Error parseando el feed RSS: %s", feed.bozo_exception)
    return feed.entries


async def poll_once(upload_queue: "asyncio.Queue") -> int:
    """Revisa el feed una vez, encola los nuevos items que hagan match con
    algún filtro y devuelve cuántos se encolaron."""
    entries = await fetch_feed_entries()
    filters = await asyncio.to_thread(db.list_filters)
    if not filters:
        return 0

    enqueued = 0
    for entry in entries:
        guid = entry.get("id") or entry.get("link")
        title = entry.get("title", "")
        if not guid or not title:
            continue

        already_seen = await asyncio.to_thread(db.is_seen, guid)
        if already_seen:
            continue

        parsed = parse_title(title)
        if parsed is None:
            await asyncio.to_thread(db.mark_seen, guid, title)
            continue

        matched = any(item_matches_filter(parsed, f) for f in filters)
        await asyncio.to_thread(db.mark_seen, guid, title)

        if not matched:
            continue

        # Se usa el <guid> del item (no el <link>) como URL a enviar a Seedr,
        # tal como indicaste. En nyaa.si el guid es la página de info del
        # torrent (https://nyaa.si/view/<id>), no un .torrent directo; por
        # eso el cliente de Seedr (add_torrent_from_page) intenta agregarlo
        # tal cual y, si la API lo rechaza, cae en /scrape/html/torrents
        # para resolver el enlace magnet/torrent real desde esa página.
        torrent_url = guid
        # nyaa expone el tamaño en el namespace nyaa:size
        size_str = entry.get("nyaa_size") or entry.get("size")
        size_bytes = parse_size_to_bytes(size_str)

        item_id = await asyncio.to_thread(db.enqueue_item, guid, title, torrent_url, size_bytes)
        if item_id:
            logger.info("Encolado: %s (%s)", title, size_str)
            await upload_queue.put(item_id)
            enqueued += 1

    return enqueued


async def watch_loop(upload_queue: "asyncio.Queue", stop_event: asyncio.Event):
    """Bucle infinito que revisa el RSS cada RSS_POLL_INTERVAL_SECONDS."""
    logger.info("Iniciando monitor de RSS: %s (cada %ss)", config.RSS_FEED_URL, config.RSS_POLL_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            count = await poll_once(upload_queue)
            if count:
                logger.info("RSS: %d nuevo(s) item(s) encolado(s).", count)
        except Exception:
            logger.exception("Error revisando el feed RSS")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.RSS_POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def requeue_pending_on_startup(upload_queue: "asyncio.Queue") -> None:
    """Al iniciar el bot, vuelve a encolar en memoria los items que quedaron
    pendientes de una corrida anterior (persistidos en sqlite)."""
    pending = await asyncio.to_thread(db.get_pending_items)
    for item in pending:
        await upload_queue.put(item["id"])
    if pending:
        logger.info("%d item(s) pendiente(s) re-encolado(s) desde la base de datos.", len(pending))
