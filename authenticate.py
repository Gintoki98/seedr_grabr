"""
Ejecuta este script UNA VEZ (de forma interactiva, con acceso a otro dispositivo
con navegador) para vincular tu cuenta de Seedr mediante OAuth Device Flow.

Uso:
    python authenticate.py

El token resultante se guarda en el archivo configurado por SEEDR_TOKEN_FILE
(por defecto: seedr_token.json). El bot lo leerá y refrescará automáticamente
en tiempo de ejecución.
"""
import config
from seedr_auth import (
    SeedrAuthError,
    TokenStore,
    poll_for_token,
    request_device_code,
)


def main():
    if not config.SEEDR_CLIENT_ID:
        print("Error: define SEEDR_CLIENT_ID en tu .env antes de continuar.")
        return

    store = TokenStore()

    try:
        print("Solicitando device code a Seedr...")
        code_data = request_device_code(scope="files.read files.write profile account.read tasks.write tasks.read archives.manage")

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

        token_data = poll_for_token(device_code, interval, expires_in)

        if token_data:
            store.save_token_response(token_data)
            print(f"\nToken guardado en: {store.path}")
            print("Ya puedes iniciar el bot con `python main.py`.")
        else:
            print("\nNo se pudo obtener el token (timeout o cancelado).")

    except SeedrAuthError as e:
        print(f"\nError de autenticación: {e}")
    except KeyboardInterrupt:
        print("\nCancelado por el usuario.")


if __name__ == "__main__":
    main()
