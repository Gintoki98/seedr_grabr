"""
Autenticación OAuth 2.0 Device Flow contra la API de Seedr v2.

El token (access_token + refresh_token) se guarda en PostgreSQL (tabla
`seedr_token`, ver db.py) en vez de un archivo local — así no hace falta
subir ningún secreto a GitHub ni configurar un volumen persistente en
Coolify: mientras la base de datos exista, el token sobrevive a cualquier
redeploy.

El flujo se puede disparar desde:
  - El comando /auth del bot de Telegram (telegram_bot.py) — recomendado.
  - El script CLI `authenticate.py` (alternativa para setup sin Telegram).

Endpoints (según la documentación oficial de la API):
  POST /oauth/device/code   -> solicita device_code + user_code
  POST /oauth/device/token  -> polling para obtener el access_token
  POST /oauth/token         -> refrescar un token (grant_type=refresh_token)
"""
import json
import logging
import time
from typing import Any, Dict, Optional

import requests

import config
import db

logger = logging.getLogger(__name__)

# AJUSTAR si tu base URL no necesita este prefijo.
API_PATH_PREFIX = "/api/v0.1/p"

DEVICE_CODE_ENDPOINT = f"{config.SEEDR_API_BASE_URL}{API_PATH_PREFIX}/oauth/device/code"
DEVICE_TOKEN_ENDPOINT = f"{config.SEEDR_API_BASE_URL}{API_PATH_PREFIX}/oauth/device/token"
TOKEN_ENDPOINT = f"{config.SEEDR_API_BASE_URL}{API_PATH_PREFIX}/oauth/token"

FIELD_ACCESS_TOKEN = "access_token"
FIELD_REFRESH_TOKEN = "refresh_token"
FIELD_EXPIRES_IN = "expires_in"


class SeedrAuthError(Exception):
    def __init__(self, message: str, error_code: Optional[str] = None):
        super().__init__(message)
        self.error_code = error_code


class SeedrAuthPending(Exception):
    """Señal interna: el usuario todavía no aprobó el device code."""


# --------------------------------------------------------- HTTP (síncrono)

def request_device_code(scope: str = "files.read files.write profile account.read tasks.write tasks.read archives.manage") -> Dict[str, Any]:
    """Paso 1: solicita device_code + user_code."""
    payload = {"client_id": config.SEEDR_CLIENT_ID, "scope": scope}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        response = requests.post(DEVICE_CODE_ENDPOINT, data=payload, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        if "device_code" not in data or "user_code" not in data:
            raise SeedrAuthError(f"Respuesta inválida del endpoint de device code: {data}")
        return data
    except requests.exceptions.RequestException as e:
        message = f"Error de red solicitando device code: {e}"
        error_code = None
        if e.response is not None:
            try:
                error_data = e.response.json()
                error_code = error_data.get("error")
                message = error_data.get("error_description", message)
            except json.JSONDecodeError:
                message = f"Error API {e.response.status_code}: {e.response.text[:200]}"
        raise SeedrAuthError(message, error_code=error_code) from e


def poll_device_token_once(device_code: str) -> Optional[Dict[str, Any]]:
    """Un solo intento de polling. Devuelve el token si ya fue aprobado,
    lanza SeedrAuthPending si sigue pendiente, o SeedrAuthError si hubo un
    error terminal (denegado, expirado, etc.)."""
    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "client_id": config.SEEDR_CLIENT_ID,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    resp = requests.post(DEVICE_TOKEN_ENDPOINT, data=payload, headers=headers, timeout=30)
    try:
        data = resp.json()
    except json.JSONDecodeError:
        raise SeedrAuthError(f"Respuesta no-JSON del polling: {resp.text[:200]}")

    if resp.ok:
        return data

    error = data.get("error")
    error_description = data.get("error_description", error)
    if error in ("authorization_pending", "slow_down"):
        raise SeedrAuthPending()
    raise SeedrAuthError(error_description or f"Autorización fallida: {error}", error_code=error)


def poll_for_token_blocking(device_code: str, interval: int, expires_in: int) -> Optional[Dict[str, Any]]:
    """Hace polling de forma bloqueante (para usar en un hilo aparte vía
    asyncio.to_thread, o desde el script CLI authenticate.py)."""
    deadline = time.time() + expires_in
    current_interval = interval
    while time.time() < deadline:
        time.sleep(current_interval)
        try:
            token_data = poll_device_token_once(device_code)
            if token_data:
                return token_data
        except SeedrAuthPending:
            continue
        except requests.exceptions.RequestException as e:
            logger.warning("Error de red en polling de Seedr: %s. Reintentando...", e)
            time.sleep(max(current_interval, 5))
    return None


def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    """Usa el refresh_token para obtener un nuevo access_token."""
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": config.SEEDR_CLIENT_ID,
    }
    if config.SEEDR_CLIENT_SECRET:
        payload["client_secret"] = config.SEEDR_CLIENT_SECRET
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        resp = requests.post(TOKEN_ENDPOINT, data=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        message = f"Error refrescando token: {e}"
        error_code = None
        if e.response is not None:
            try:
                error_data = e.response.json()
                error_code = error_data.get("error")
                message = error_data.get("error_description", message)
            except json.JSONDecodeError:
                message = f"Error API {e.response.status_code}: {e.response.text[:200]}"
        raise SeedrAuthError(message, error_code=error_code) from e


# ------------------------------------------------------------ persistencia

def save_token_response(token_data: Dict[str, Any], updated_by: Optional[int] = None) -> None:
    access_token = token_data.get(FIELD_ACCESS_TOKEN)
    if not access_token:
        raise SeedrAuthError(f"La respuesta de token no contiene '{FIELD_ACCESS_TOKEN}': {token_data}")

    existing = db.get_seedr_token()
    refresh_token = token_data.get(FIELD_REFRESH_TOKEN) or (existing.get("refresh_token") if existing else None)
    expires_in = token_data.get(FIELD_EXPIRES_IN, 3600)
    expires_at = time.time() + int(expires_in) - 60  # 60s de margen

    db.save_seedr_token(access_token, refresh_token, expires_at, updated_by)


def has_saved_token() -> bool:
    return db.get_seedr_token() is not None


def get_valid_access_token() -> str:
    """Devuelve un access_token válido, refrescándolo si hace falta.

    Lanza SeedrAuthError si no hay token guardado (hay que vincular la cuenta
    con /auth en Telegram, o `python authenticate.py`) o si el refresco falla
    (puede requerir re-vincular la cuenta)."""
    row = db.get_seedr_token()
    if not row:
        raise SeedrAuthError(
            "No hay ninguna cuenta de Seedr vinculada. Un administrador debe "
            "enviarle /auth al bot en Telegram para vincularla."
        )

    if time.time() < row["expires_at"]:
        return row["access_token"]

    if not row.get("refresh_token"):
        raise SeedrAuthError(
            "El token de Seedr expiró y no hay refresh_token disponible. "
            "Volvé a vincular la cuenta con /auth."
        )

    logger.info("Access token de Seedr expirado, refrescando...")
    new_token_data = refresh_access_token(row["refresh_token"])
    save_token_response(new_token_data)
    return db.get_seedr_token()["access_token"]
