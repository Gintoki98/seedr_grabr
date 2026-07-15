"""
Autenticación OAuth 2.0 Device Flow contra la API de Seedr v2.

Este módulo se basa en el ejemplo `seedr_device_flow_auth.py` provisto,
reorganizado para:
  1) Poder ejecutarse una sola vez de forma interactiva (ver `authenticate.py`)
     para vincular la cuenta y guardar el token en disco.
  2) Ser reutilizado en tiempo de ejecución por el bot para refrescar el
     access_token automáticamente cuando expira, sin intervención humana.

NOTA:
Según la documentación oficial de la API (API Documentation - Seedr API Console)
el flujo de device code usa DOS endpoints distintos, algo fácil de pasar por
alto:
  - POST /oauth/device/code   -> solicita device_code + user_code
  - POST /oauth/device/token  -> polling para obtener el access_token
    (grant_type = 'urn:ietf:params:oauth:grant-type:device_code')

El endpoint POST /oauth/token es un endpoint DISTINTO que se usa para:
  - Intercambiar un authorization_code (grant_type=authorization_code)
  - Refrescar un token (grant_type=refresh_token)
  - Client credentials (grant_type=client_credentials)

Este módulo ya refleja esa separación. El prefijo de path (`API_PATH_PREFIX`)
sigue sin estar confirmado en la documentación (los ejemplos que diste usan
`/api/v0.1/p`), así que se mantiene como constante fácil de ajustar si hiciera
falta.
"""
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

import config

logger = logging.getLogger(__name__)

# AJUSTAR si tu base URL no necesita este prefijo (ver nota arriba).
API_PATH_PREFIX = "/api/v0.1/p"

DEVICE_CODE_ENDPOINT = f"{config.SEEDR_API_BASE_URL}{API_PATH_PREFIX}/oauth/device/code"
DEVICE_TOKEN_ENDPOINT = f"{config.SEEDR_API_BASE_URL}{API_PATH_PREFIX}/oauth/device/token"
TOKEN_ENDPOINT = f"{config.SEEDR_API_BASE_URL}{API_PATH_PREFIX}/oauth/token"

# Campos donde se espera encontrar el token de refresco / expiración.
# AJUSTAR si la respuesta real de la API usa otros nombres.
FIELD_ACCESS_TOKEN = "access_token"
FIELD_REFRESH_TOKEN = "refresh_token"
FIELD_EXPIRES_IN = "expires_in"


class SeedrAuthError(Exception):
    def __init__(self, message: str, error_code: Optional[str] = None):
        super().__init__(message)
        self.error_code = error_code


class TokenStore:
    """Persiste y refresca el token de acceso de Seedr en disco (JSON)."""

    def __init__(self, path: Path = config.SEEDR_TOKEN_FILE):
        self.path = Path(path)
        self._data: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                logger.warning("No se pudo leer el archivo de token %s, se recreará.", self.path)
                self._data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2))

    def save_token_response(self, token_data: Dict[str, Any]) -> None:
        access_token = token_data.get(FIELD_ACCESS_TOKEN)
        if not access_token:
            raise SeedrAuthError(f"La respuesta de token no contiene '{FIELD_ACCESS_TOKEN}': {token_data}")

        refresh_token = token_data.get(FIELD_REFRESH_TOKEN, self._data.get(FIELD_REFRESH_TOKEN))
        expires_in = token_data.get(FIELD_EXPIRES_IN, 3600)

        self._data = {
            FIELD_ACCESS_TOKEN: access_token,
            FIELD_REFRESH_TOKEN: refresh_token,
            "obtained_at": time.time(),
            "expires_at": time.time() + int(expires_in) - 60,  # 60s de margen
        }
        self._save()

    @property
    def access_token(self) -> Optional[str]:
        return self._data.get(FIELD_ACCESS_TOKEN)

    @property
    def refresh_token(self) -> Optional[str]:
        return self._data.get(FIELD_REFRESH_TOKEN)

    @property
    def is_expired(self) -> bool:
        expires_at = self._data.get("expires_at")
        if not expires_at:
            return True
        return time.time() >= expires_at

    @property
    def has_token(self) -> bool:
        return bool(self.access_token)


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


def poll_for_token(device_code: str, interval: int, expires_in: int) -> Optional[Dict[str, Any]]:
    """Paso 3: hace polling al endpoint de token hasta que el usuario autoriza."""
    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "client_id": config.SEEDR_CLIENT_ID,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    expiry_time = time.time() + expires_in
    current_interval = interval

    print(f"Esperando autorización... (cada {current_interval}s, expira en {expires_in}s)")

    while time.time() < expiry_time:
        time.sleep(current_interval)
        if time.time() >= expiry_time:
            print("\nEl tiempo de espera expiró.")
            return None
        try:
            resp = requests.post(DEVICE_TOKEN_ENDPOINT, data=payload, headers=headers, timeout=30)
            data = resp.json()
            if resp.ok:
                print("\n¡Autenticación exitosa!")
                return data
            error = data.get("error")
            error_description = data.get("error_description", error)
            if error == "authorization_pending":
                sys.stdout.write(".")
                sys.stdout.flush()
            elif error == "slow_down":
                current_interval = min(int(current_interval * 1.5), 60)
                print(f"[slow down - nuevo intervalo: {current_interval}s]")
            elif error in ("expired_token", "access_denied"):
                raise SeedrAuthError(error_description or f"Autorización fallida: {error}", error_code=error)
            else:
                raise SeedrAuthError(f"Error de polling: {error_description}", error_code=error)
        except requests.exceptions.Timeout:
            print("\nTimeout en el polling. Reintentando...")
        except requests.exceptions.RequestException as e:
            print(f"\nError de red en polling: {e}. Reintentando...")
            time.sleep(max(current_interval, 5))
        except json.JSONDecodeError:
            print("\nError decodificando respuesta de polling. Reintentando...")

    print("\nEl device flow terminó sin obtener token.")
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


def get_valid_access_token(store: TokenStore) -> str:
    """Devuelve un access_token válido, refrescándolo si hace falta.

    Lanza SeedrAuthError si no hay token guardado (hay que correr authenticate.py primero)
    o si el refresco falla (puede requerir re-autenticación manual).
    """
    if not store.has_token:
        raise SeedrAuthError(
            "No hay token de Seedr guardado. Ejecuta `python authenticate.py` primero "
            "para vincular la cuenta."
        )
    if store.is_expired:
        if not store.refresh_token:
            raise SeedrAuthError(
                "El token de Seedr expiró y no hay refresh_token disponible. "
                "Ejecuta `python authenticate.py` para re-autenticar."
            )
        logger.info("Access token de Seedr expirado, refrescando...")
        new_token_data = refresh_access_token(store.refresh_token)
        store.save_token_response(new_token_data)
    return store.access_token
