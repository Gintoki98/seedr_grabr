"""
Worker de la cola de subidas.

Por cada item encolado (torrent que hizo match con un filtro del usuario):
  1. Si Seedr todavía no fue vinculado (no hay token en la base), espera
     pacientemente sin marcar el item como error — apenas un admin lo vincule
     con /auth, retoma solo.
  2. Espera a que haya espacio suficiente en la cuenta de Seedr (con margen).
  3. Agrega el torrent (POST /tasks) a partir de la URL tomada de <guid>.
  4. Espera a que Seedr termine de descargarlo (polling de /tasks/{id},
     luego /tasks/{id}/contents para obtener los archivos resultantes).
  5. Descarga cada archivo resultante a disco local (GET /download/file/{id}).
  6. Borra cada archivo en Seedr ni bien está en local (libera espacio).
  7. Sube el archivo a Telegram (canal/grupo configurado) vía Telethon.
  8. Borra el archivo local.

Se procesa de a un item por vez por "slot" de concurrencia (QUEUE_CONCURRENCY),
para no saturar la cuenta de Seedr ni el ancho de banda.
"""
import asyncio
import logging
import re
import requests

from telethon import TelegramClient

import config
import db
from seedr_auth import SeedrAuthError, get_valid_access_token
from seedr_client import SeedrApiError, SeedrClient, SeedrTaskTimeoutError

logger = logging.getLogger(__name__)

_INVALID_CHARS_RE = re.compile(r'[\\/:*?"<>|]')


def _sanitize_filename(name: str, max_len: int = 150) -> str:
    name = _INVALID_CHARS_RE.sub("_", name).strip()
    return name[:max_len] if len(name) > max_len else name


def _get_seedr_client() -> SeedrClient:
    access_token = get_valid_access_token()
    return SeedrClient(access_token=access_token, base_url=config.SEEDR_API_BASE_URL)


async def _wait_for_seedr_auth(stop_event: asyncio.Event) -> None:
    """Bloquea este slot de la cola (sin tocar el item) hasta que haya una
    cuenta de Seedr vinculada. No consume el item de la cola todavía."""
    warned = False
    while not stop_event.is_set():
        try:
            get_valid_access_token()
            return
        except SeedrAuthError:
            if not warned:
                logger.warning(
                    "Hay releases esperando pero Seedr no está vinculado todavía. "
                    "Un admin debe enviarle /auth al bot en Telegram. Reintentando cada 30s..."
                )
                warned = True
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass


async def _wait_for_space(client: SeedrClient, needed_bytes: int, stop_event: asyncio.Event) -> None:
    """Espera (bloqueando este slot de la cola) hasta que haya espacio libre
    suficiente para `needed_bytes` + margen de seguridad."""
    required = needed_bytes + config.SEEDR_SPACE_SAFETY_MARGIN_BYTES
    while not stop_event.is_set():
        free_space = await asyncio.to_thread(client.get_free_space_bytes)
        if free_space >= required:
            return
        logger.info(
            "Espacio insuficiente en Seedr (libre=%.2f MiB, requerido=%.2f MiB). Esperando...",
            free_space / 1024**2, required / 1024**2,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.SEEDR_POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _process_item(
    item_id: int,
    telegram_client: TelegramClient,
    stop_event: asyncio.Event,
) -> None:
    item = await asyncio.to_thread(db.get_item, item_id)
    if item is None:
        logger.warning("Item %s ya no existe en la base de datos, se omite.", item_id)
        return
    if item["status"] in ("done", "error"):
        return

    title = item["title"]
    torrent_url = item["torrent_url"]  # viene de <guid> del RSS
    size_bytes = item["size_bytes"]

    logger.info("Procesando item %s: %s", item_id, title)

    try:
        client = await asyncio.to_thread(_get_seedr_client)

        # 1) Esperar espacio suficiente.
        await asyncio.to_thread(db.update_item_status, item_id, "waiting_space")
        await _wait_for_space(client, size_bytes, stop_event)
        if stop_event.is_set():
            return

        # 2) Descargar el .torrent (desde <link>) y subirlo a Seedr como archivo.
        await asyncio.to_thread(db.update_item_status, item_id, "adding_torrent")
        torrent_file_path = config.DOWNLOAD_DIR / f"{item_id}.torrent"

        def _download_torrent_file():
            resp = requests.get(torrent_url, timeout=60)
            resp.raise_for_status()
            torrent_file_path.parent.mkdir(parents=True, exist_ok=True)
            torrent_file_path.write_bytes(resp.content)

        try:
            await asyncio.to_thread(_download_torrent_file)
            add_resp = await asyncio.to_thread(client.add_torrent_by_file, torrent_file_path, 0)
        finally:
            try:
                torrent_file_path.unlink(missing_ok=True)
            except OSError:
                pass

        # AJUSTAR: nombre de campo con el id de la tarea creada.
        task_id = add_resp.get("id") or add_resp.get("task_id")
        if task_id is None:
            raise SeedrApiError(f"No se pudo determinar el id de la tarea creada: {add_resp}")

        await asyncio.to_thread(db.update_item_status, item_id, "downloading_seedr", str(task_id))

        # 3) Esperar a que Seedr termine de descargar el torrent.
        files = await asyncio.to_thread(
            client.wait_for_task_files,
            task_id,
            config.SEEDR_TASK_TIMEOUT_SECONDS,
            config.SEEDR_POLL_INTERVAL_SECONDS,
        )

        if not files:
            raise SeedrApiError(f"La tarea {task_id} terminó sin archivos.")

        await asyncio.to_thread(db.update_item_status, item_id, "downloading_local")

        for f in files:
            logger.info("Procesando entrada de archivo cruda: %s", f)
            file_id = f.get("id") or f.get("file_id") or f.get("folder_file_id")
            if file_id is None:
                raise SeedrApiError(f"No se pudo determinar el id de archivo en la entrada: {f}")

            file_name = _sanitize_filename(f.get("name") or f.get("file_name") or f"file_{file_id}")
            dest = config.DOWNLOAD_DIR / f"{item_id}_{file_name}"

            logger.info("Descargando %s (file_id=%s) a %s", file_name, file_id, dest)
            await asyncio.to_thread(client.download_file_to_path, file_id, dest)

            # 6) Borrar el archivo de Seedr ni bien está en local, para liberar espacio.
            try:
                await asyncio.to_thread(client.delete_file, file_id)
            except SeedrApiError:
                logger.exception("No se pudo borrar el archivo %s en Seedr (se continúa igual).", file_id)

            # 7) Subir a Telegram.
            await asyncio.to_thread(db.update_item_status, item_id, "uploading_telegram")
            logger.info("Subiendo %s a Telegram (%s)", dest.name, config.TELEGRAM_TARGET_CHAT)
            try:
                await telegram_client.send_file(
                    config.TELEGRAM_TARGET_CHAT,
                    str(dest),
                    caption=title[:1024],
                    force_document=True,
                )
            finally:
                # 8) Borrar archivo local (siempre, haya fallado o no la subida).
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    logger.warning("No se pudo borrar el archivo local %s", dest)

        # Limpieza final de la tarea en Seedr (no borra archivos, ya los borramos arriba).
        try:
            await asyncio.to_thread(client.delete_task, task_id)
        except SeedrApiError:
            logger.debug("No se pudo borrar la tarea %s en Seedr (no crítico).", task_id)

        await asyncio.to_thread(db.update_item_status, item_id, "done")
        logger.info("Item %s completado: %s", item_id, title)

    except SeedrTaskTimeoutError as e:
        logger.error("Timeout procesando item %s: %s", item_id, e)
        await asyncio.to_thread(db.update_item_status, item_id, "error", error_message=str(e))
    except SeedrApiError as e:
        logger.error("Error de Seedr procesando item %s: %s", item_id, e)
        await asyncio.to_thread(db.update_item_status, item_id, "error", error_message=str(e))
    except Exception as e:
        logger.exception("Error inesperado procesando item %s", item_id)
        await asyncio.to_thread(db.update_item_status, item_id, "error", error_message=str(e))


async def worker_loop(
    worker_name: str,
    upload_queue: "asyncio.Queue",
    telegram_client: TelegramClient,
    stop_event: asyncio.Event,
):
    logger.info("Worker '%s' iniciado.", worker_name)
    while not stop_event.is_set():
        try:
            item_id = await asyncio.wait_for(upload_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        try:
            # No consumir/fallar el item si todavía no hay cuenta de Seedr
            # vinculada: esperar acá y recién después procesarlo.
            await _wait_for_seedr_auth(stop_event)
            if stop_event.is_set():
                continue
            await _process_item(item_id, telegram_client, stop_event)
        finally:
            upload_queue.task_done()

    logger.info("Worker '%s' detenido.", worker_name)
