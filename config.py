"""
Configuración central del bot.
Todos los valores se cargan desde variables de entorno (o un archivo .env).
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
TELEGRAM_TARGET_CHAT = os.environ.get("TELEGRAM_TARGET_CHAT", "")
# IDs de usuario de Telegram autorizados a administrar filtros/bot (separados por coma).
# Si se deja vacío, cualquiera puede usar el bot (NO recomendado).
_admin_ids = os.environ.get("TELEGRAM_ADMIN_IDS", "")
TELEGRAM_ADMIN_IDS = {int(x) for x in _admin_ids.split(",") if x.strip()}

# --- Seedr ---
SEEDR_API_BASE_URL = os.environ.get("SEEDR_API_BASE_URL", "https://api.seedr.cc")
SEEDR_CLIENT_ID = os.environ.get("SEEDR_CLIENT_ID", "")
# Solo necesario si tu app OAuth de Seedr es de tipo "confidencial" (tiene client_secret).
SEEDR_CLIENT_SECRET = os.environ.get("SEEDR_CLIENT_SECRET", "")
SEEDR_TOKEN_FILE = Path(os.environ.get("SEEDR_TOKEN_FILE", str(BASE_DIR / "seedr_token.json")))
# Margen de seguridad sobre el espacio libre de Seedr (para no llenar el 100%).
SEEDR_SPACE_SAFETY_MARGIN_BYTES = int(os.environ.get("SEEDR_SPACE_SAFETY_MARGIN_BYTES", 200 * 1024 * 1024))  # 200 MiB
# Tiempo máximo (segundos) que se espera a que un torrent termine de descargar dentro de Seedr.
SEEDR_TASK_TIMEOUT_SECONDS = int(os.environ.get("SEEDR_TASK_TIMEOUT_SECONDS", 3 * 3600))
SEEDR_POLL_INTERVAL_SECONDS = int(os.environ.get("SEEDR_POLL_INTERVAL_SECONDS", 20))

# --- RSS ---
RSS_FEED_URL = os.environ.get("RSS_FEED_URL", "https://nyaa.si/?page=rss")
RSS_POLL_INTERVAL_SECONDS = int(os.environ.get("RSS_POLL_INTERVAL_SECONDS", 300))  # 5 min

# --- Almacenamiento local temporal ---
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", str(BASE_DIR / "downloads")))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# --- Base de datos (sqlite) ---
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "bot_data.sqlite3"))

# --- Cola de subidas a Seedr ---
# Número de descargas que se procesan en paralelo dentro de Seedr.
# Se recomienda dejarlo en 1 para no saturar los límites de la cuenta.
QUEUE_CONCURRENCY = int(os.environ.get("QUEUE_CONCURRENCY", 1))

# --- Logging ---
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
