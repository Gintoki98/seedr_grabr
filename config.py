"""
Configuración central del bot.
Todos los valores se cargan desde variables de entorno (o un archivo .env).

Diseñado para correr en Coolify (o cualquier plataforma sin almacenamiento
persistente por defecto): el único estado que necesita sobrevivir a un
redeploy vive en PostgreSQL (filtros, cola, y el token OAuth de Seedr,
vinculado desde el propio bot con /auth). No se depende de ningún archivo
local ni volumen.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# --- Telegram ---
TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Canal/grupo destino donde se subirán los archivos ya descargados.
# Puede ser un username (@canal), un id numérico (-100...) o un invite link.
def _normalize_chat_id(value: str):
    """Telethon, al ser bot, interpreta un string compuesto solo de dígitos
    como número de teléfono y falla ('Cannot get entity by phone number as
    a bot'). Si el valor es puramente numérico (con o sin '-' inicial, como
    los IDs de canal -100...), lo convertimos a int para que lo trate como
    ID de chat y no como teléfono. Si es un @username o invite link, se
    deja como string tal cual."""
    if not value:
        return value
    v = value.strip()
    if v.lstrip("-").isdigit():
        return int(v)
    return value


TELEGRAM_TARGET_CHAT = _normalize_chat_id(os.environ.get("TELEGRAM_TARGET_CHAT", ""))

# IDs de usuario de Telegram autorizados a administrar filtros/bot y a vincular
# la cuenta de Seedr (/auth), separados por coma. MUY recomendado definir esto:
# /auth expone credenciales de tu cuenta de Seedr, no debe quedar abierto a cualquiera.
_admin_ids = os.environ.get("TELEGRAM_ADMIN_IDS", "")
TELEGRAM_ADMIN_IDS = {int(x) for x in _admin_ids.split(",") if x.strip()}

# --- Seedr ---
SEEDR_API_BASE_URL = os.environ.get("SEEDR_API_BASE_URL", "https://api.seedr.cc")
SEEDR_CLIENT_ID = os.environ.get("SEEDR_CLIENT_ID", "")
# Solo necesario si tu app OAuth de Seedr es de tipo "confidencial" (tiene client_secret).
SEEDR_CLIENT_SECRET = os.environ.get("SEEDR_CLIENT_SECRET", "")
# Margen de seguridad sobre el espacio libre de Seedr (para no llenar el 100%).
SEEDR_SPACE_SAFETY_MARGIN_BYTES = int(os.environ.get("SEEDR_SPACE_SAFETY_MARGIN_BYTES", 200 * 1024 * 1024))  # 200 MiB
# Tiempo máximo (segundos) que se espera a que un torrent termine de descargar dentro de Seedr.
SEEDR_TASK_TIMEOUT_SECONDS = int(os.environ.get("SEEDR_TASK_TIMEOUT_SECONDS", 3 * 3600))
SEEDR_POLL_INTERVAL_SECONDS = int(os.environ.get("SEEDR_POLL_INTERVAL_SECONDS", 20))
# Cuánto esperar (segundos) por la aprobación del usuario durante /auth antes de darla por vencida.
SEEDR_DEVICE_FLOW_MAX_WAIT_SECONDS = int(os.environ.get("SEEDR_DEVICE_FLOW_MAX_WAIT_SECONDS", 900))

# --- RSS ---
RSS_FEED_URL = os.environ.get("RSS_FEED_URL", "https://nyaa.si/?page=rss")
RSS_POLL_INTERVAL_SECONDS = int(os.environ.get("RSS_POLL_INTERVAL_SECONDS", 300))  # 5 min

# --- Almacenamiento local temporal (NO necesita persistir entre redeploys) ---
# Los archivos se borran automáticamente apenas se suben a Telegram, así que
# usar un directorio efímero (p. ej. /tmp) es intencional y seguro.
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/seedr_bot_downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# --- Base de datos: PostgreSQL ---
# Coolify provee esto automáticamente si agregás un recurso de PostgreSQL y lo
# enlazás a esta app (o lo copiás manualmente desde el recurso de la base).
# Formato: postgresql://usuario:password@host:puerto/nombre_db
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# --- Cola de subidas a Seedr ---
# Número de descargas que se procesan en paralelo dentro de Seedr.
# Se recomienda dejarlo en 1 para no saturar los límites de la cuenta.
QUEUE_CONCURRENCY = int(os.environ.get("QUEUE_CONCURRENCY", 1))

# --- Logging ---
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
