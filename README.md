# Bot Telegram: RSS → Seedr → Telegram

Bot en Python que:
1. Monitorea un feed RSS (por defecto `https://nyaa.si/?page=rss`).
2. Filtra items por **Encoder** (y opcionalmente **Título**), definidos por el usuario vía comandos de Telegram.
3. Encola las coincidencias y las sube a **Seedr v2** (respetando el espacio libre de la cuenta).
4. Cuando Seedr termina de descargar el torrent, descarga el archivo a disco local (temporal).
5. Borra el archivo de Seedr (libera espacio).
6. Sube el archivo a un canal/grupo de Telegram vía **Telethon** (cuenta bot).
7. Borra el archivo local.

La cuenta de Seedr se vincula **desde el propio bot** (comando `/auth`, OAuth Device Flow) — no hace falta correr nada por consola ni subir ningún token a GitHub.

## Sin estado local: todo vive en PostgreSQL

No hay SQLite, ni archivos de sesión, ni JSON de tokens. Todo el estado que
necesita sobrevivir a un redeploy (filtros, cola de descargas, y el token
OAuth de Seedr) se guarda en **PostgreSQL**. Los archivos descargados son
temporales y se borran apenas se suben a Telegram, así que ni siquiera
necesitan un volumen persistente. Esto hace que el despliegue en Coolify sea
trivial: una app + una base de datos Postgres, sin volúmenes que configurar.

## Estructura

```
config.py           Configuración (variables de entorno)
db.py                 Persistencia PostgreSQL (filtros, cola, token de Seedr)
seedr_auth.py          OAuth Device Flow de Seedr (guarda/refresca el token en la DB)
seedr_client.py         Cliente HTTP de la API de Seedr v2
rss_watcher.py           Polling del feed RSS y matching de filtros
worker.py                 Procesa la cola: Seedr -> disco -> Telegram -> limpieza
telegram_bot.py            Comandos del bot (Telethon), incluye /auth
main.py                     Punto de entrada, arranca todo
authenticate.py              Alternativa CLI a /auth (opcional, no hace falta usarla)
Dockerfile                    Imagen del bot
docker-compose.yml              Para desplegar como stack en Coolify
```

## Variables de entorno

Ver `.env.example` para la lista completa con comentarios. Las importantes:

| Variable | Para qué |
|---|---|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | Credenciales de tu app en https://my.telegram.org |
| `TELEGRAM_BOT_TOKEN` | Token del bot, de @BotFather |
| `TELEGRAM_TARGET_CHAT` | Canal/grupo donde se suben los archivos |
| `TELEGRAM_ADMIN_IDS` | IDs autorizados a usar `/auth` y `/addfilter` (**recomendado configurarlo**) |
| `SEEDR_CLIENT_ID` (+ `SEEDR_CLIENT_SECRET` si aplica) | Tu app OAuth de Seedr |
| `DATABASE_URL` | Connection string de PostgreSQL |

---

## Guía paso a paso: de 0 a 100 en Coolify

### Orden de los pasos: ¿autenticar antes o después?

**No importa el orden.** El bot está diseñado para arrancar siempre, aunque
Seedr todavía no esté vinculado: el bot de Telegram, `/addfilter`, `/status`,
etc. funcionan de inmediato, y el worker que sube archivos a Seedr simplemente
**espera pacientemente** (revisa cada 30s) hasta que alguien vincule la cuenta
con `/auth`. No hay que reiniciar nada. Recomendación: seguí estos pasos en
orden, `/auth` queda para el final, cuando el bot ya esté corriendo.

### Paso 0 — Reunir credenciales

Antes de tocar Coolify, conseguí:

1. **Telegram API ID/HASH**: entrá a https://my.telegram.org → API Development Tools → creá una app.
2. **Bot de Telegram**: hablá con [@BotFather](https://t.me/BotFather), `/newbot`, guardá el token.
3. **Canal/grupo destino**: creá el canal/grupo, agregá el bot como administrador (con permiso de publicar).
4. **Tu Telegram user ID**: hablá con [@userinfobot](https://t.me/userinfobot) para saber tu ID numérico (para `TELEGRAM_ADMIN_IDS`).
5. **Seedr Client ID** (y secret si tu app es confidencial): desde el panel de desarrollador/API console de Seedr donde ya tenés registrada tu app OAuth.

### Paso 1 — Subir el código a GitHub

```bash
cd seedr_telegram_bot
git init
git add .
git commit -m "Bot RSS -> Seedr -> Telegram"
git branch -M main
git remote add origin https://github.com/tu-usuario/tu-repo.git
git push -u origin main
```

El `.gitignore` ya excluye `.env`, `__pycache__/`, sesiones, sqlite, etc. —
no hay ningún secreto en el repo porque el token de Seedr vive en la base de
datos, no en un archivo.

### Paso 2 — Crear la base de datos PostgreSQL en Coolify

1. En el dashboard de Coolify: **Projects** → elegí (o creá) un proyecto → **New Resource** → **Database** → **PostgreSQL**.
2. Dejá que Coolify genere usuario/password, o personalizalos.
3. Deployá el recurso de base de datos.
4. Una vez arriba, andá a su página de detalle y copiá el **connection string** (formato `postgresql://usuario:password@host:puerto/nombre_db`). Vas a necesitarlo en el paso 4.
   - Si la app y la base están en el mismo proyecto/red de Coolify, normalmente podés usar el nombre interno del servicio como host en vez de una IP pública.

### Paso 3 — Crear la app del bot en Coolify

1. **New Resource** → **Application** (despliegue basado en Git).
2. Conectá tu cuenta de GitHub (o pegá la URL si el repo es público) y seleccioná el repositorio.
3. Build Pack: Coolify debería detectar el `Dockerfile` automáticamente (si no, seleccionalo manualmente en vez de Nixpacks).
4. **Ports Exposes**: dejalo **vacío**. Este bot no expone ningún puerto HTTP (solo hace conexiones salientes a Telegram/Seedr/Postgres), así que no necesita uno.
5. En la sección **Health Checks** de la configuración de la app: **desactivalos**. Al no haber puerto HTTP, un health check por URL siempre va a fallar y Coolify va a marcar la app como no saludable innecesariamente.

   *(Alternativa: si preferís desplegar con `docker-compose.yml` en vez de "Application" + Dockerfile, elegí **New Resource → Docker Compose**, apuntá al `docker-compose.yml` del repo, y aplicá los mismos criterios: sin `ports:`, sin `networks:` custom — el archivo que te dejé ya cumple ambas cosas.)*

### Paso 4 — Configurar las variables de entorno

En la pestaña **Environment Variables** de la app, cargá todas las de `.env.example`, con estos puntos clave:

- `DATABASE_URL`: el connection string que copiaste en el paso 2.
- `TELEGRAM_ADMIN_IDS`: tu ID de Telegram (del paso 0.4). **No lo dejes vacío** — `/auth` vincula tu cuenta de Seedr, no debe quedar abierto a cualquiera que le escriba al bot.
- Resto de variables según `.env.example`.

No hace falta `SEEDR_TOKEN_FILE`, `DB_PATH` ni `DOWNLOAD_DIR` — ya no existen, todo quedó resuelto vía `DATABASE_URL` y un directorio temporal interno.

### Paso 5 — Deploy

Click en **Deploy**. Mirá los logs: deberías ver algo así:

```
Base de datos (PostgreSQL) lista.
Bot de Telegram conectado.
```

Si ves un error de conexión a la base, revisá que `DATABASE_URL` sea accesible desde la red de la app (host interno vs. público, credenciales, etc.).

### Paso 6 — Vincular Seedr desde Telegram

Abrí un chat con tu bot y enviá:

```
/auth
```

El bot te va a responder con un link y (si aplica) un código. Abrilo desde
cualquier dispositivo con navegador, aprobá el acceso, y el bot te va a
confirmar automáticamente:

```
✅ Cuenta de Seedr vinculada correctamente.
```

Este paso podés hacerlo en cualquier momento — antes, durante o mucho después
del deploy — sin reiniciar la app.

### Paso 7 — Configurar filtros y listo

```
/addfilter ASW | Mushoku Tensei     -> sigue solo ese título de ese encoder
/addfilter ASW                      -> sigue todo lo que suba ese encoder
/listfilters
/status                             -> muestra si Seedr está vinculado + la cola actual
```

A partir de acá el bot revisa el RSS cada `RSS_POLL_INTERVAL_SECONDS`
(5 minutos por defecto), encola lo que matchee, y va subiendo a Seedr →
Telegram automáticamente.

---

## Comandos de Telegram

- `/auth` — vincula (o re-vincula) la cuenta de Seedr.
- `/addfilter Encoder | Título` — sigue solo ese título de ese encoder.
- `/addfilter Encoder` — sigue **todo** lo que suba ese encoder.
- `/listfilters` — lista los filtros activos con su id.
- `/removefilter <id>` — elimina un filtro.
- `/status` — estado de Seedr (vinculado o no) + cola actual (pending, waiting_space, adding_torrent, downloading_seedr, downloading_local, uploading_telegram, done, error).

## ⚠️ Nota sobre la API de Seedr v2

El código usa los endpoints reales documentados en la "API Documentation -
Seedr API Console" que compartiste: `GET /user`, `GET /me/quota`,
`GET /fs/root(/contents)`, `GET /fs/folder/{id}(/contents)`, `POST /fs/folder`,
`DELETE /fs/folder|file/{id}`, `GET/POST /tasks`, `GET /tasks/{id}(/contents)`,
`DELETE /tasks/{id}` (no borra archivos), `GET /download/file/{id}(/url)`,
`POST /scrape/html/torrents`, y el flujo OAuth (`/oauth/device/code`,
`/oauth/device/token` para polling, `/oauth/token` para refresh).

La documentación no detalla el **body exacto** de algunos POST
(`POST /tasks`, `POST /fs/folder`, `POST /scrape/html/torrents`) ni algunos
nombres de campo de respuesta (`/me/quota`, el id devuelto por `POST /tasks`).
Esos puntos están marcados **`AJUSTAR`** en `seedr_client.py` y `worker.py` —
las respuestas crudas de la API quedan incluidas en los mensajes de error
para poder ajustarlos rápido contra tu cuenta real si hiciera falta.

### Sobre el uso de `<guid>` como URL a subir

El bot toma la URL desde `<guid>` (no `<link>`) del item RSS. En nyaa.si eso
es la página de info del torrent, no un `.torrent` directo. Por eso
`add_torrent_from_page()` primero intenta agregarla tal cual con `POST /tasks`,
y si Seedr la rechaza, usa `POST /scrape/html/torrents` para resolver el
enlace magnet/torrent real de esa página y reintenta.

## Diseño de la cola y el espacio en disco de Seedr

- `QUEUE_CONCURRENCY` (por defecto `1`) controla cuántas descargas se procesan
  en paralelo dentro de Seedr. Se recomienda dejarlo en 1.
- Antes de agregar un torrent, el worker calcula el espacio libre restante
  (`/me/quota`) y espera hasta que haya espacio suficiente (tamaño del
  torrent + margen de seguridad `SEEDR_SPACE_SAFETY_MARGIN_BYTES`).
- En cuanto el archivo termina de bajar a disco local, se borra inmediatamente
  de Seedr para liberar espacio para el siguiente item.
- El estado de cada item queda persistido en PostgreSQL; si el bot se
  reinicia, los items que seguían `pending` se re-encolan automáticamente.
  Items que quedaron a mitad de proceso (p. ej. `downloading_seedr`) no se
  retoman solos — quedarían visibles en `/status` con ese estado; podés volver
  a correr `/addfilter` o esperar a que el propio nuevo match del RSS los
  reemplace en la próxima vuelta.

## Notas adicionales

- El feed de nyaa.si trae títulos en distintos idiomas/scripts; el matching de
  encoder es exacto (case-insensitive) y el de título es "contiene"
  (case-insensitive).
- `DOWNLOAD_DIR` es un directorio temporal (`/tmp/...` dentro del contenedor);
  no necesita volumen persistente, los archivos se borran automáticamente.
- Telegram/Telethon con cuenta bot tiene un límite de subida de 2 GB por
  archivo (4 GB con Telegram Premium en el bot); releases más grandes
  necesitarían dividirse o usar una cuenta de usuario en vez de bot.
