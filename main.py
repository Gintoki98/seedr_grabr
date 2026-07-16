"""
Punto de entrada del bot.

Arranca:
  - El cliente de Telegram (Telethon, cuenta bot) con los comandos registrados.
  - El monitor de RSS (revisa el feed periódicamente y encola matches).
  - N workers que procesan la cola (Seedr -> disco local -> Telegram -> limpieza).

No depende de ningún archivo local ni volumen persistente: todo el estado
(filtros, cola, y el token OAuth de Seedr) vive en PostgreSQL. La cuenta de
Seedr se vincula desde el propio bot con el comando /auth — no hace falta
correr nada por consola ni subir secretos a GitHub.

Uso:
    python main.py
    # luego, en Telegram: enviarle /auth al bot para vincular Seedr
"""
import asyncio
import logging
import signal

from telethon import TelegramClient
from telethon.sessions import StringSession

import config
import db
import rss_watcher
import telegram_bot
import worker

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
    if not config.DATABASE_URL:
        missing.append("DATABASE_URL")
    if missing:
        raise SystemExit(
            "Faltan variables de entorno requeridas: " + ", ".join(missing) + "\n"
            "Revisá .env.example / las variables de entorno configuradas en Coolify."
        )
    if not config.TELEGRAM_ADMIN_IDS:
        logger.warning(
            "TELEGRAM_ADMIN_IDS está vacío: cualquier persona que le escriba al bot podrá "
            "usar /auth y vincular tu cuenta de Seedr. Se recomienda fuertemente configurarlo."
        )


async def main():
    _validate_config()
    db.init_db()
    logger.info("Base de datos (PostgreSQL) lista.")

    # Sesión de Telegram en memoria: no se persiste a disco, así el bot no
    # depende de ningún volumen. Al reiniciar simplemente vuelve a loguearse
    # con el bot_token (normal y sin restricciones para cuentas de bot).
    telegram_client = TelegramClient(StringSession(), config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    telegram_bot.register_handlers(telegram_client)

    await telegram_client.start(bot_token=config.TELEGRAM_BOT_TOKEN)
    logger.info("Bot de Telegram conectado.")

    if not config.TELEGRAM_ADMIN_IDS:
        logger.warning("Recordatorio: configurá TELEGRAM_ADMIN_IDS para restringir /auth y /addfilter.")

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
                worker.worker_loop(f"worker-{i}", upload_queue, telegram_client, stop_event),
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
