FROM python:3.12-slim

WORKDIR /app

# psycopg2-binary no necesita libpq-dev, pero dejamos build-essential por si
# alguna dependencia futura requiere compilar.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Directorio de descargas temporales (efímero, no necesita volumen: los
# archivos se borran automáticamente apenas se suben a Telegram).
RUN mkdir -p /tmp/seedr_bot_downloads
ENV DOWNLOAD_DIR=/tmp/seedr_bot_downloads

# No exponemos ningún puerto: el bot solo hace conexiones salientes
# (Telegram, Seedr, PostgreSQL, RSS). Coolify no necesita un healthcheck
# HTTP para este tipo de servicio "worker".

CMD ["python", "main.py"]
