"""
Punto de entrada del bot.

Arranca:
  - El cliente de Telegram (Telethon, cuenta bot) con los comandos registrados.
  - El monitor de RSS (revisa el feed periódicamente y encola matches).
  - N workers que procesan la cola (Seedr -> disco local -> Telegram -> limpieza).

Uso:
    python authenticate.py   # una sola vez, para vincular la cuenta de Seedr
    python main.py
"""
import asyncio
import logging
import signal

from telethon import TelegramClient

import config
import db
import rss_watcher
import telegram_bot
import worker
from seedr_auth import TokenStore

logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _validate_config() -> None:
    missing = []
    if not config.TELEGRAM_API_ID:
        missing.append("TELEGRAM_API_ID")
    if not config.TELEGRAM_API_HASH:
        missing.append("TELEGRAM_API_HASH")
    if not config.TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not config.TELEGRAM_TARGET_CHAT:
        missing.append("TELEGRAM_TARGET_CHAT")
    if not config.SEEDR_CLIENT_ID:
        missing.append("SEEDR_CLIENT_ID")
    if missing:
        raise SystemExit(
            "Faltan variables de entorno requeridas: " + ", ".join(missing) + "\n"
            "Copia .env.example a .env y completa los valores."
        )


async def main():
    _validate_config()
    db.init_db()

    token_store = TokenStore()
    if not token_store.has_token:
        raise SystemExit(
            "No hay token de Seedr guardado. Ejecuta `python authenticate.py` primero."
        )

    telegram_client = TelegramClient("bot_session", config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    telegram_bot.register_handlers(telegram_client)

    await telegram_client.start(bot_token=config.TELEGRAM_BOT_TOKEN)
    logger.info("Bot de Telegram conectado.")

    upload_queue: asyncio.Queue = asyncio.Queue()
    stop_event = asyncio.Event()

    def _handle_stop(*_):
        logger.info("Señal de apagado recibida, deteniendo...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_stop)
        except NotImplementedError:
            # add_signal_handler no está disponible en algunas plataformas (p. ej. Windows)
            pass

    await rss_watcher.requeue_pending_on_startup(upload_queue)

    tasks = [
        asyncio.create_task(rss_watcher.watch_loop(upload_queue, stop_event), name="rss_watcher"),
    ]
    for i in range(max(1, config.QUEUE_CONCURRENCY)):
        tasks.append(
            asyncio.create_task(
                worker.worker_loop(f"worker-{i}", upload_queue, token_store, telegram_client, stop_event),
                name=f"worker-{i}",
            )
        )

    async def _disconnect_on_stop():
        await stop_event.wait()
        await telegram_client.disconnect()

    try:
        stop_watcher = asyncio.create_task(_disconnect_on_stop(), name="stop_watcher")
        await telegram_client.run_until_disconnected()
        stop_watcher.cancel()
    finally:
        stop_event.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await telegram_client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
