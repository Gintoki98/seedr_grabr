"""
Comandos del bot de Telegram (Telethon, cuenta bot) para administrar los
filtros que determinan qué releases del RSS se descargan.

Comandos:
  /start, /help
  /addfilter <Encoder> | <Título>   -> agrega un filtro
  /listfilters                      -> lista los filtros activos
  /removefilter <id>                -> elimina un filtro por id
  /status                           -> muestra items en cola / procesándose
"""
import asyncio
import logging

from telethon import TelegramClient, events

import config
import db

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "🤖 **Bot de descargas RSS → Seedr → Telegram**\n\n"
    "Los releases del feed tienen el formato `[Encoder] Título`.\n"
    "Agrega un filtro para que cada vez que aparezca ese Encoder (+ opcionalmente "
    "un Título) en el feed, se descargue y suba automáticamente a este canal.\n\n"
    "**Comandos:**\n"
    "`/addfilter Encoder | Título` — sigue solo ese título de ese encoder\n"
    "  Ejemplo: `/addfilter ASW | Mushoku Tensei`\n"
    "`/addfilter Encoder` — sigue **todo** lo que suba ese encoder\n"
    "  Ejemplo: `/addfilter ASW`\n\n"
    "`/listfilters` — lista los filtros activos con su id\n"
    "`/removefilter <id>` — elimina un filtro\n"
    "`/status` — muestra el estado de la cola de descargas\n"
)


def _is_admin(user_id: int) -> bool:
    if not config.TELEGRAM_ADMIN_IDS:
        return True
    return user_id in config.TELEGRAM_ADMIN_IDS


def register_handlers(client: TelegramClient) -> None:
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
            if "UNIQUE" in str(e):
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
        active = await asyncio.to_thread(db.get_active_items)
        if not active:
            await event.respond("No hay descargas en curso ni pendientes.")
            return
        lines = []
        for it in active[:20]:
            lines.append(f"`{it['id']}` [{it['status']}] {it['title'][:60]}")
        extra = f"\n… y {len(active) - 20} más" if len(active) > 20 else ""
        await event.respond("**Cola actual:**\n" + "\n".join(lines) + extra, parse_mode="markdown")
