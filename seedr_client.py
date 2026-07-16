"""
Cliente de la API de Seedr v2.

Endpoints alineados con la documentación oficial (API Documentation - Seedr
API Console) que proporcionaste. Puntos que la documentación NO detalla
(nombres de campos de la respuesta JSON, o el body exacto de POST /tasks y
POST /fs/folder) siguen marcados con "AJUSTAR".

Endpoints usados:
  GET    /user                          -> perfil del usuario
  GET    /me/quota                      -> cuota de espacio/ancho de banda
  GET    /fs/root                       -> detalles carpeta raíz
  GET    /fs/root/contents              -> contenido de la carpeta raíz
  GET    /fs/folder/{id}                -> detalles de una carpeta
  GET    /fs/folder/{id}/contents       -> contenido de una carpeta
  GET    /fs/file/{id}                  -> detalles de un archivo
  POST   /fs/folder                     -> crea una carpeta
  DELETE /fs/folder/{id}                -> borra una carpeta
  DELETE /fs/file/{id}                  -> borra un archivo
  GET    /tasks                         -> lista tareas (torrents) activas
  GET    /tasks/{id}                    -> detalle de una tarea
  GET    /tasks/{id}/contents           -> archivos resultantes de una tarea
  POST   /tasks                         -> agrega un torrent (url/magnet/archivo)
  DELETE /tasks/{id}                    -> borra la tarea (NO borra los archivos)
  GET    /download/file/{id}            -> descarga directa del contenido del archivo
  GET    /download/file/{id}/url        -> URL de descarga directa temporal
  POST   /scrape/html/torrents          -> extrae enlaces magnet/torrent de una página
"""
import json
import logging
import time
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests

logger = logging.getLogger(__name__)

# AJUSTAR si tu base URL no necesita este prefijo.
API_PATH_PREFIX = "/api/v0.1/p"


class SeedrApiError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, response_data: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data

    def __str__(self):
        details = f"Status: {self.status_code}" if self.status_code else "N/A"
        if self.response_data:
            details += f", Data: {json.dumps(self.response_data)}"
        return f"{super().__str__()} ({details})"


class SeedrTaskTimeoutError(Exception):
    """El torrent no terminó de descargarse dentro de Seedr en el tiempo esperado."""


class SeedrClient:
    def __init__(self, access_token: str, base_url: str = "https://api.seedr.cc"):
        if not access_token:
            raise ValueError("Access token cannot be empty.")
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
                "User-Agent": "SeedrTelegramBot/1.0",
            }
        )

    # ---------------------------------------------------------------- core

    def request(
        self,
        endpoint: str,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        raw: bool = False,
        stream: bool = False,
        **kwargs,
    ) -> Any:
        """Realiza una petición a la API. Si raw=True devuelve el objeto Response
        crudo (usado para streaming de descargas binarias)."""
        url = f"{self.base_url}{API_PATH_PREFIX}{endpoint}"
        method = method.upper()

        request_args: Dict[str, Any] = {"params": params, "stream": stream, **kwargs}

        headers = self.session.headers.copy()
        if "headers" in kwargs:
            headers.update(kwargs["headers"])

        if files:
            request_args["files"] = files
            if data:
                request_args["data"] = data
            headers.pop("Content-Type", None)
        elif data:
            content_type = headers.get("Content-Type", "application/json")
            if "application/json" in content_type:
                request_args["json"] = data
                headers.setdefault("Content-Type", "application/json")
            else:
                request_args["data"] = data
                headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

        request_args["headers"] = headers

        try:
            response = self.session.request(method, url, **request_args)
            response.raise_for_status()

            if raw:
                return response
            if response.status_code == 204:
                return None
            try:
                return response.json()
            except json.JSONDecodeError:
                return response.text

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            try:
                error_data = e.response.json()
                message = error_data.get(
                    "error_description", error_data.get("error", error_data.get("message", "API Error"))
                )
            except json.JSONDecodeError:
                error_data = {"raw_body": e.response.text[:500]}
                message = f"API request failed with status {status_code}"
            raise SeedrApiError(message, status_code=status_code, response_data=error_data) from e
        except requests.exceptions.RequestException as e:
            raise SeedrApiError(f"Request failed: {e}") from e

    # ------------------------------------------------------------ endpoints

    def get_user_profile(self) -> Dict[str, Any]:
        return self.request("/user")

    def get_quota_usage(self) -> Dict[str, Any]:
        return self.request("/me/quota")

    # --- Files & Folders ---

    def get_folder_contents(self, folder_id: Union[int, str] = 0) -> Dict[str, Any]:
        """Lista el contenido (subcarpetas, archivos, torrents activos) de una carpeta.
        folder_id=0 (o "root") usa el endpoint dedicado /fs/root/contents."""
        if folder_id in (0, "0", None, "root"):
            return self.request("/fs/root/contents")
        return self.request(f"/fs/folder/{folder_id}/contents")

    def get_folder_details(self, folder_id: Union[int, str] = 0) -> Dict[str, Any]:
        if folder_id in (0, "0", None, "root"):
            return self.request("/fs/root")
        return self.request(f"/fs/folder/{folder_id}")

    def get_file_details(self, file_id: int) -> Dict[str, Any]:
        return self.request(f"/fs/file/{file_id}")

    def get_by_path(self, path: str, contents: bool = False) -> Dict[str, Any]:
        return self.request("/fs/path", params={"path": path, "contents": str(contents).lower()})

    def create_folder(self, name: str, parent_id: Union[int, str] = 0) -> Dict[str, Any]:
        # AJUSTAR: el body exacto de POST /fs/folder no está detallado en la
        # documentación; se asume name + parent_id como en la mayoría de APIs
        # de este estilo.
        return self.request("/fs/folder", method="POST", data={"name": name, "parent_id": parent_id})

    def delete_file(self, file_id: int) -> None:
        return self.request(f"/fs/file/{file_id}", method="DELETE")

    def delete_folder(self, folder_id: Union[int, str]) -> None:
        return self.request(f"/fs/folder/{folder_id}", method="DELETE")

    def search_fs(self, query: str) -> Dict[str, Any]:
        return self.request("/search/fs", params={"query": query})

    # --- Tasks (torrents) ---

    def list_tasks(self) -> Any:
        return self.request("/tasks")

    def get_task(self, task_id: Union[int, str]) -> Dict[str, Any]:
        return self.request(f"/tasks/{task_id}")

    def get_task_contents(self, task_id: Union[int, str]) -> Any:
        """Archivos generados por una tarea de torrent (según la documentación,
        sigue siendo consultable incluso si la tarea ya no aparece en /tasks)."""
        return self.request(f"/tasks/{task_id}/contents")

    def add_torrent_by_url(self, torrent_or_magnet_url: str, folder_id: Union[int, str] = 0) -> Dict[str, Any]:
        """Agrega una nueva tarea de torrent a partir de una URL de .torrent o un
        magnet link. AJUSTAR: la documentación no detalla el body exacto; se
        asume 'url' (o 'magnet') + 'folder_id' según el ejemplo original."""
        if not torrent_or_magnet_url:
            raise ValueError("torrent_or_magnet_url no puede estar vacío.")
        data = {"url": torrent_or_magnet_url}
        if folder_id not in (0, "0", None):
            data["folder_id"] = folder_id
        return self.request("/tasks", method="POST", data=data)

    def add_torrent_by_file(self, torrent_file_path: Path, folder_id: Union[int, str] = 0) -> Dict[str, Any]:
        """Sube un archivo .torrent ya descargado en disco (multipart/form-data)."""
        file_name = os.path.basename(torrent_file_path)
        data = {}
        if folder_id not in (0, "0", None):
            data["folder_id"] = folder_id
        with open(torrent_file_path, "rb") as f:
            files = {"file": (file_name, f, "application/x-bittorrent")}
            return self.request("/tasks", method="POST", files=files, data=data or None)

    def add_torrent_from_page(self, page_url: str, folder_id: Union[int, str] = 0) -> Dict[str, Any]:
        """Intenta agregar un torrent directamente a partir de `page_url`
        (p. ej. la URL de <guid> de nyaa.si, que es una página de info, no un
        .torrent directo). Si Seedr no puede procesarla como está, usa
        POST /scrape/html/torrents para extraer el enlace magnet/torrent real
        de esa página y reintenta."""
        try:
            return self.add_torrent_by_url(page_url, folder_id)
        except SeedrApiError as first_error:
            logger.info(
                "add_torrent_by_url falló para %s (%s); intentando resolver vía /scrape/html/torrents.",
                page_url, first_error,
            )
            scraped = self.scrape_html_torrents(page_url)
            # AJUSTAR: nombre de campo con los enlaces encontrados en la respuesta del scraper.
            links = scraped.get("torrents") or scraped.get("links") or scraped.get("results") or []
            if not links:
                raise SeedrApiError(
                    f"No se pudo agregar el torrent desde {page_url} ni resolverlo vía scrape. "
                    f"Error original: {first_error}. Respuesta de scrape: {scraped}"
                )
            resolved_url = links[0].get("url") if isinstance(links[0], dict) else links[0]
            return self.add_torrent_by_url(resolved_url, folder_id)

    def scrape_html_torrents(self, page_url: str) -> Dict[str, Any]:
        # AJUSTAR: nombre del campo de body no confirmado; se asume 'url'.
        return self.request("/scrape/html/torrents", method="POST", data={"url": page_url})

    def pause_task(self, task_id: Union[int, str]) -> Any:
        return self.request(f"/tasks/{task_id}/pause", method="POST")

    def resume_task(self, task_id: Union[int, str]) -> Any:
        return self.request(f"/tasks/{task_id}/resume", method="POST")

    def delete_task(self, task_id: Union[int, str]) -> None:
        """Borra la tarea. La documentación aclara que esto NO borra los
        archivos ya descargados; hay que borrarlos aparte con delete_file()."""
        return self.request(f"/tasks/{task_id}", method="DELETE")

    # --- Download ---

    def get_file_download_url(self, file_id: int) -> str:
        resp = self.request(f"/download/file/{file_id}/url")
        # AJUSTAR: nombre del campo con la URL si difiere.
        for key in ("url", "download_url", "link"):
            if isinstance(resp, dict) and resp.get(key):
                return resp[key]
        if isinstance(resp, str) and resp.startswith("http"):
            return resp
        raise SeedrApiError(f"No se encontró URL de descarga en la respuesta: {resp}")

    def download_file_to_path(self, file_id: int, destination: Path, chunk_size: int = 1024 * 1024) -> Path:
        """Descarga un archivo resolviendo primero la URL firmada temporal
        (GET /download/file/{id}/url) y bajando esa URL directamente, sin
        pasarle el Authorization header de Seedr (las URLs firmadas suelen
        llevar su propia autenticación en query params)."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        download_url = self.get_file_download_url(file_id)
        logger.info("URL de descarga resuelta para file_id=%s: %s", file_id, download_url)

        with requests.get(download_url, stream=True, timeout=120) as response:
            logger.info("Descarga file_id=%s -> status=%s content-length=%s",
                        file_id, response.status_code, response.headers.get("Content-Length"))
            response.raise_for_status()
            with open(destination, "wb") as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)

        size_on_disk = destination.stat().st_size
        logger.info("Descarga completa: %s (%d bytes)", destination, size_on_disk)
        if size_on_disk == 0:
            raise SeedrApiError(f"El archivo descargado {destination} quedó en 0 bytes.")
        return destination

    # ------------------------------------------------------- funcionalidad de alto nivel

    def get_free_space_bytes(self) -> int:
        """Devuelve el espacio libre en bytes. AJUSTAR los nombres de campo
        si la respuesta real de /me/quota usa otras claves."""
        quota = self.get_quota_usage()

        if "space_used" in quota and "space_max" in quota:
            return int(quota["space_max"]) - int(quota["space_used"])

        space = quota.get("space")
        if isinstance(space, dict) and "used" in space and "max" in space:
            return int(space["max"]) - int(space["used"])

        if "used" in quota and "max_space" in quota:
            return int(quota["max_space"]) - int(quota["used"])

        raise SeedrApiError(
            "No se pudo interpretar la respuesta de /me/quota para calcular espacio libre. "
            f"Ajusta get_free_space_bytes() en seedr_client.py. Respuesta cruda: {quota}",
        )

    def wait_for_task_files(
        self,
        task_id: Union[int, str],
        timeout_seconds: int,
        poll_interval_seconds: int = 20,
    ) -> List[Dict[str, Any]]:
        """Espera a que la tarea `task_id` termine de descargar dentro de Seedr
        y devuelve la lista de archivos resultantes (GET /tasks/{id}/contents).

        Estrategia:
          - Hace polling de GET /tasks/{id}. Si responde 404 (la tarea suele
            desaparecer de /tasks una vez que termina y sus archivos ya están
            en el sistema de archivos), se considera terminada.
          - Si sigue activa, revisa el campo de progreso/estado (nombres no
            confirmados por la documentación, se intentan varias variantes) y
            sigue esperando si no llegó al 100% / estado final.
          - En cualquier caso, al terminar consulta GET /tasks/{id}/contents
            para obtener los archivos (la documentación indica que este
            endpoint sigue funcionando incluso después de que la tarea se
            complete, ya que DELETE /tasks/{id} explícitamente NO borra los
            archivos, lo que implica que quedan asociados a la tarea).
        """
        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            try:
                task = self.get_task(task_id)
            except SeedrApiError as e:
                if e.status_code == 404:
                    break  # la tarea ya no existe -> se asume completada
                raise

            status = str(task.get("status", "")).lower()
            progress = task.get("progress")
            logger.info("Tarea Seedr %s: status=%s progress=%s", task_id, status, progress)

            if status in ("error", "failed", "dead"):
                raise SeedrApiError(f"La tarea de Seedr terminó con error: {task}")
            if status in ("finished", "completed", "done") or progress in (100, "100", 100.0):
                break

            time.sleep(poll_interval_seconds)
        else:
            raise SeedrTaskTimeoutError(
                f"La tarea {task_id} no terminó de descargar dentro de Seedr en {timeout_seconds}s."
            )

        raw = self.get_task_contents(task_id)
        logger.info("Contenido crudo de la tarea %s (/tasks/%s/contents): %s", task_id, task_id, raw)

        if isinstance(raw, dict):
            files = raw.get("files") or raw.get("contents") or raw.get("items") or []
        elif isinstance(raw, list):
            files = raw
        else:
            files = []

        logger.info("Archivos detectados para la tarea %s: %s", task_id, files)
        return files
