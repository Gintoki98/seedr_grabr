"""
Comandos del bot de Telegram (Telethon, cuenta bot) para administrar los
filtros que determinan qué releases del RSS se descargan, y para vincular
la cuenta de Seedr sin tener que subir ningún token/secreto a GitHub.

Comandos:
  /start, /help
  /auth                              -> vincula (o re-vincula) la cuenta de Seedr
  /addfilter <Encoder> | <Título>    -> agrega un filtro
  /listfilters                       -> lista los filtros activos
  /removefilter <id>                 -> elimina un filtro por id
  /status                            -> muestra items en cola / procesándose + estado de Seedr
"""
import asyncio
import logging
from typing import Optional

from telethon import TelegramClient, events

import config
import db
import seedr_auth

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "🤖 **Bot de descargas RSS → Seedr → Telegram**\n\n"
    "Los releases del feed tienen el formato `[Encoder] Título`.\n"
    "Agrega un filtro para que cada vez que aparezca ese Encoder (+ opcionalmente "
    "un Título) en el feed, se descargue y suba automáticamente a este canal.\n\n"
    "**Primero:** vinculá tu cuenta de Seedr con `/auth` (una sola vez).\n\n"
    "**Comandos:**\n"
    "`/auth` — vincula (o re-vincula) la cuenta de Seedr\n"
    "`/addfilter Encoder | Título` — sigue solo ese título de ese encoder\n"
    "  Ejemplo: `/addfilter ASW | Mushoku Tensei`\n"
    "`/addfilter Encoder` — sigue **todo** lo que suba ese encoder\n"
    "  Ejemplo: `/addfilter ASW`\n\n"
    "`/listfilters` — lista los filtros activos con su id\n"
    "`/removefilter <id>` — elimina un filtro\n"
    "`/status` — muestra el estado de la cola y si Seedr está vinculado\n"
)


def _is_admin(user_id: int) -> bool:
    if not config.TELEGRAM_ADMIN_IDS:
        return True
    return user_id in config.TELEGRAM_ADMIN_IDS


def register_handlers(client: TelegramClient) -> None:
    # Referencia a la vinculación de Seedr en curso (si hay una), para evitar
    # que dos /auth corran en paralelo. Vive en el closure de esta función.
    auth_task: Optional[asyncio.Task] = None

    async def _run_device_flow(event) -> None:
        nonlocal auth_task
        try:
            try:
                code_data = await asyncio.to_thread(seedr_auth.request_device_code)
            except seedr_auth.SeedrAuthError as e:
                await event.respond(f"❌ No se pudo iniciar la vinculación con Seedr: {e}")
                return

            user_code = code_data.get("user_code")
            verification_uri = code_data.get("verification_uri_complete") or code_data.get("verification_uri")
            expires_in = int(code_data.get("expires_in") or config.SEEDR_DEVICE_FLOW_MAX_WAIT_SECONDS)
            interval = int(code_data.get("interval") or 5)
            device_code = code_data.get("device_code")

            if not (user_code and verification_uri and device_code):
                await event.respond(f"❌ Respuesta inesperada de Seedr al pedir el device code: {code_data}")
                return

            await event.respond(
                "🔗 **Vinculación de Seedr**\n\n"
                f"1️⃣ Abrí este enlace (en cualquier dispositivo, no hace falta que sea este): {verification_uri}\n"
                f"2️⃣ Si te pide un código, ingresá: `{user_code}`\n"
                f"3️⃣ Aprobá el acceso a la app.\n\n"
                f"Tenés {max(expires_in // 60, 1)} minutos. Te aviso apenas quede vinculado ✅",
                parse_mode="markdown",
                link_preview=False,
            )

            token_data = await asyncio.to_thread(
                seedr_auth.poll_for_token_blocking, device_code, interval, expires_in
            )

            if not token_data:
                await event.respond("⌛ Tiempo agotado o autorización rechazada. Enviá /auth de nuevo para reintentar.")
                return

            try:
                await asyncio.to_thread(seedr_auth.save_token_response, token_data, event.sender_id)
                await event.respond("✅ Cuenta de Seedr vinculada correctamente. Ya se pueden procesar descargas.")
            except seedr_auth.SeedrAuthError as e:
                await event.respond(f"❌ Se obtuvo el token pero no se pudo guardar: {e}")

        except Exception:
            logger.exception("Error inesperado durante /auth")
            try:
                await event.respond("❌ Ocurrió un error inesperado vinculando la cuenta. Revisá los logs del bot.")
            except Exception:
                pass
        finally:
            auth_task = None

    @client.on(events.NewMessage(pattern=r"^/auth$"))
    async def auth_handler(event):
        nonlocal auth_task
        if not _is_admin(event.sender_id):
            await event.respond("No tienes permiso para vincular la cuenta de Seedr.")
            return

        if auth_task is not None and not auth_task.done():
            await event.respond("Ya hay una vinculación en curso. Esperá a que termine o expire (unos minutos).")
            return

        already_linked = await asyncio.to_thread(seedr_auth.has_saved_token)
        if already_linked:
            await event.respond(
                "Ya había una cuenta de Seedr vinculada; voy a iniciar una nueva vinculación "
                "(al aprobarla, reemplaza a la anterior)."
            )

        auth_task = asyncio.create_task(_run_device_flow(event))

    @client.on(events.NewMessage(pattern=r"^/start$"))
    async def start_handler(event):
        await event.respond(HELP_TEXT, parse_mode="markdown")

    @client.on(events.NewMessage(pattern=r"^/help$"))
    async def help_handler(event):
        await event.respond(HELP_TEXT, parse_mode="markdown")

    @client.on(events.NewMessage(pattern=r"^/addfilter(?:\s+(.*))?$"))
    async def addfilter_handler(event):
        if not _is_admin(event.sender_id):
            await event.respond("No tienes permiso para administrar filtros.")
            return

        raw = event.pattern_match.group(1)
        if not raw or not raw.strip():
            await event.respond(
                "Uso:\n"
                "`/addfilter Encoder | Título` (título específico)\n"
                "`/addfilter Encoder` (todo el encoder)\n"
                "Ejemplos: `/addfilter ASW | Mushoku Tensei`  ·  `/addfilter ASW`",
                parse_mode="markdown",
            )
            return

        if "|" in raw:
            encoder, title = raw.split("|", 1)
            encoder, title = encoder.strip(), title.strip()
        else:
            encoder, title = raw.strip(), ""

        if not encoder:
            await event.respond("El encoder no puede estar vacío.")
            return

        try:
            filter_id = await asyncio.to_thread(db.add_filter, encoder, title, event.sender_id)
            if title:
                await event.respond(f"✅ Filtro agregado (id `{filter_id}`): `[{encoder}]` — `{title}`", parse_mode="markdown")
            else:
                await event.respond(
                    f"✅ Filtro agregado (id `{filter_id}`): `[{encoder}]` — **todo** lo que suba este encoder",
                    parse_mode="markdown",
                )
        except Exception as e:
            if "duplicate key" in str(e).lower() or "unique constraint" in str(e).lower():
                await event.respond("Ese filtro ya existe.")
            else:
                logger.exception("Error agregando filtro")
                await event.respond(f"Error agregando el filtro: {e}")

    @client.on(events.NewMessage(pattern=r"^/listfilters$"))
    async def listfilters_handler(event):
        filters = await asyncio.to_thread(db.list_filters)
        if not filters:
            await event.respond("No hay filtros configurados. Usa /addfilter para agregar uno.")
            return
        lines = []
        for f in filters:
            if f["title"]:
                lines.append(f"`{f['id']}` — `[{f['encoder']}]` `{f['title']}`")
            else:
                lines.append(f"`{f['id']}` — `[{f['encoder']}]` (todo el encoder)")
        await event.respond("**Filtros activos:**\n" + "\n".join(lines), parse_mode="markdown")

    @client.on(events.NewMessage(pattern=r"^/removefilter(?:\s+(\d+))?$"))
    async def removefilter_handler(event):
        if not _is_admin(event.sender_id):
            await event.respond("No tienes permiso para administrar filtros.")
            return

        raw_id = event.pattern_match.group(1)
        if not raw_id:
            await event.respond("Uso: `/removefilter <id>` (ver /listfilters)", parse_mode="markdown")
            return

        removed = await asyncio.to_thread(db.remove_filter, int(raw_id))
        if removed:
            await event.respond(f"🗑️ Filtro `{raw_id}` eliminado.", parse_mode="markdown")
        else:
            await event.respond(f"No existe un filtro con id `{raw_id}`.", parse_mode="markdown")

    @client.on(events.NewMessage(pattern=r"^/status$"))
    async def status_handler(event):
        linked = await asyncio.to_thread(seedr_auth.has_saved_token)
        header = "🔗 Seedr: **vinculado** ✅" if linked else "🔗 Seedr: **no vinculado** ❌ (enviá /auth)"

        active = await asyncio.to_thread(db.get_active_items)
        if not active:
            await event.respond(f"{header}\n\nNo hay descargas en curso ni pendientes.", parse_mode="markdown")
            return
        lines = []
        for it in active[:20]:
            lines.append(f"`{it['id']}` [{it['status']}] {it['title'][:60]}")
        extra = f"\n… y {len(active) - 20} más" if len(active) > 20 else ""
        await event.respond(
            f"{header}\n\n**Cola actual:**\n" + "\n".join(lines) + extra, parse_mode="markdown"
        )
