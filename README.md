# Bot Telegram: RSS → Seedr → Telegram

Bot en Python que:
1. Monitorea un feed RSS (por defecto `https://nyaa.si/?page=rss`).
2. Filtra items por **Encoder** y **Título** (formato `[Encoder] Título`), definidos por el usuario vía comandos de Telegram.
3. Encola las coincidencias y las sube a **Seedr v2** (respetando el espacio libre de la cuenta).
4. Cuando Seedr termina de descargar el torrent, descarga el archivo a disco local.
5. Borra el archivo de Seedr (libera espacio).
6. Sube el archivo a un canal/grupo de Telegram vía **Telethon** (cuenta bot).
7. Borra el archivo local.

## Estructura

```
config.py           Configuración (variables de entorno)
seedr_auth.py        OAuth Device Flow + refresco de token de Seedr
authenticate.py       Script de un solo uso para vincular la cuenta de Seedr
seedr_client.py       Cliente HTTP de la API de Seedr v2
db.py                 Persistencia SQLite (filtros, items vistos, cola)
rss_watcher.py         Polling del feed RSS y matching de filtros
worker.py              Procesa la cola: Seedr -> disco -> Telegram -> limpieza
telegram_bot.py        Comandos del bot (Telethon)
main.py                 Punto de entrada, arranca todo
```

## Instalación

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Completa `.env` con:
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`: desde https://my.telegram.org
- `TELEGRAM_BOT_TOKEN`: creado con @BotFather
- `TELEGRAM_TARGET_CHAT`: canal/grupo destino (el bot debe ser admin o tener permiso de publicar)
- `TELEGRAM_ADMIN_IDS`: IDs numéricos de Telegram autorizados a administrar filtros
- `SEEDR_CLIENT_ID`: tu client_id de la app OAuth de Seedr

## Vincular la cuenta de Seedr (una sola vez)

```bash
python authenticate.py
```

Te dará una URL y un código para autorizar desde cualquier dispositivo con navegador.
El token queda guardado en `seedr_token.json` y el bot lo refresca solo mientras corre.

## Ejecutar el bot

```bash
python main.py
```

## Uso desde Telegram

- `/addfilter ASW | Mushoku Tensei` — sigue solo ese título de ese encoder.
- `/addfilter ASW` — sigue **todo** lo que suba el encoder `ASW` (sin especificar título).
- `/listfilters` — lista los filtros activos con su id.
- `/removefilter <id>` — elimina un filtro.
- `/status` — muestra qué hay en cola / procesándose (pending, waiting_space, adding_torrent, downloading_seedr, downloading_local, uploading_telegram, done, error).

## ⚠️ Nota sobre la API de Seedr v2

Ya se incorporó la documentación oficial que compartiste (API Documentation -
Seedr API Console) y el código usa los endpoints reales documentados:

- `GET /user`, `GET /me/quota`
- `GET /fs/root`, `GET /fs/root/contents`, `GET /fs/folder/{id}`, `GET /fs/folder/{id}/contents`, `GET /fs/file/{id}`
- `POST /fs/folder`, `DELETE /fs/folder/{id}`, `DELETE /fs/file/{id}`
- `GET /tasks`, `GET /tasks/{id}`, `GET /tasks/{id}/contents`, `POST /tasks`, `DELETE /tasks/{id}` (no borra archivos)
- `GET /download/file/{id}` (descarga directa), `GET /download/file/{id}/url`
- `POST /scrape/html/torrents` (fallback para resolver el link real cuando se agrega la página de `<guid>`)
- OAuth: `POST /oauth/device/code`, `POST /oauth/device/token` (polling), `POST /oauth/token` (refresh/exchange)

Aun así, la documentación **no detalla el body exacto** de algunos POST
(`POST /tasks`, `POST /fs/folder`, `POST /scrape/html/torrents`) ni los
nombres de campo de algunas respuestas JSON (`/me/quota`, el id de tarea
devuelto por `POST /tasks`). Esos puntos siguen marcados con **`AJUSTAR`** en
el código:

- `seedr_client.py :: get_free_space_bytes()` — nombres de campo de `/me/quota`.
- `seedr_client.py :: add_torrent_by_url()` — nombre del body param (se asume `url` + `folder_id`).
- `seedr_client.py :: create_folder()` — nombre del body param (se asume `name` + `parent_id`).
- `seedr_client.py :: scrape_html_torrents()` / `add_torrent_from_page()` — nombres de campo de la respuesta del scraper.
- `worker.py :: _process_item()` — nombre del campo `id` en la respuesta de `POST /tasks`.

La primera corrida, revisa los logs y ajusta esos puntos según la respuesta
real de tu cuenta — quedan incluidos en los mensajes de error para que sea
rápido de depurar.

### Sobre el uso de `<guid>` como URL a subir

Tal como pediste, el bot toma la URL desde la etiqueta `<guid>` del item RSS
(no `<link>`). En nyaa.si el `<guid>` es la página de información del
torrent (p. ej. `https://nyaa.si/view/2132355`), no un `.torrent` directo. Por
eso `add_torrent_from_page()`:

1. Intenta agregarla directamente con `POST /tasks` (por si Seedr acepta
   páginas y las resuelve internamente).
2. Si falla, usa `POST /scrape/html/torrents` (documentado como "Scrapes a
   webpage for torrent files or magnet links") para extraer el enlace
   magnet/torrent real de esa página, y reintenta con ese enlace.

Si en la práctica el paso 1 nunca funciona, no pasa nada: el fallback al
scraper se activa automáticamente y el flujo sigue igual.

## Diseño de la cola y el espacio en disco de Seedr

- `QUEUE_CONCURRENCY` (por defecto `1`) controla cuántas descargas se procesan en
  paralelo dentro de Seedr. Se recomienda dejarlo en 1 para no saturar los límites
  de la cuenta.
- Antes de agregar un torrent, el worker calcula el espacio libre restante
  (`/me/quota`) y espera (sin bloquear otros procesos del bot) hasta que haya
  espacio suficiente para el tamaño del torrent + un margen de seguridad
  configurable (`SEEDR_SPACE_SAFETY_MARGIN_BYTES`).
- En cuanto el archivo termina de bajar a disco local, se borra inmediatamente
  de Seedr para liberar espacio para el siguiente item de la cola.
- El estado de cada item queda persistido en SQLite (`bot_data.sqlite3`), así que
  si el bot se reinicia, los items pendientes se vuelven a encolar automáticamente.

## Notas adicionales

- El feed de nyaa.si trae títulos en distintos idiomas/scripts; el matching de
  encoder es exacto (case-insensitive) y el de título es "contiene" (case-insensitive),
  así que puedes usar solo una parte del nombre del release.
- Los archivos temporales se guardan en `DOWNLOAD_DIR` (por defecto `downloads/`)
  y se borran automáticamente después de subirse a Telegram.
- Telegram/Telethon con cuenta bot tiene un límite de subida de 2 GB por archivo
  (o 4 GB en algunos casos con Telegram Premium en el bot); si tus releases superan
  ese límite tendrás que dividir el archivo o usar una cuenta de usuario en vez de bot.
