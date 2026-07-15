"""
Alternativa por consola al comando /auth del bot de Telegram.

Normalmente NO hace falta usar este script: basta con enviarle /auth al bot
una vez esté desplegado. Este script sirve para vincular la cuenta de Seedr
sin depender de Telegram (por ejemplo, para dejarla lista antes del primer
deploy) — pero igual necesita poder conectarse a la misma base de datos
PostgreSQL que va a usar el bot (vía DATABASE_URL), porque el token se
guarda ahí, no en un archivo.

Uso:
    DATABASE_URL=postgresql://user:pass@host:puerto/db python authenticate.py
"""
import db
import config
from seedr_auth import (
    SeedrAuthError,
    poll_for_token_blocking,
    request_device_code,
    save_token_response,
)


def main():
    if not config.SEEDR_CLIENT_ID:
        print("Error: define SEEDR_CLIENT_ID en tu entorno antes de continuar.")
        return
    if not config.DATABASE_URL:
        print("Error: define DATABASE_URL (debe ser la misma base que usará el bot).")
        return

    db.init_db()

    try:
        print("Solicitando device code a Seedr...")
        code_data = request_device_code()

        user_code = code_data["user_code"]
        verification_uri = code_data.get("verification_uri_complete", code_data.get("verification_uri"))
        expires_in = code_data.get("expires_in", 900)
        interval = code_data.get("interval", 5)
        device_code = code_data["device_code"]

        print("\n" + "=" * 50)
        print("Visita esta URL (puede ser en otro dispositivo):")
        print(f"  {verification_uri}")
        print("E ingresa el código:")
        print(f"  {user_code}")
        print("=" * 50 + "\n")
        print(f"Esperando autorización... (hasta {expires_in}s)")

        token_data = poll_for_token_blocking(device_code, interval, expires_in)

        if token_data:
            save_token_response(token_data)
            print("\n✅ Token guardado en la base de datos. El bot ya puede usar Seedr.")
        else:
            print("\nNo se pudo obtener el token (timeout o cancelado).")

    except SeedrAuthError as e:
        print(f"\nError de autenticación: {e}")
    except KeyboardInterrupt:
        print("\nCancelado por el usuario.")


if __name__ == "__main__":
    main()
