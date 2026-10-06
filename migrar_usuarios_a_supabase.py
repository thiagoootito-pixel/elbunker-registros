import argparse
import getpass
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime
from pathlib import Path

from database import (
    ITERACIONES_HASH_CONTRASENA,
    USUARIOS_PREDEFINIDOS,
    conectar,
    guardar_usuarios_en_conexion,
    inicializar_base_datos,
)


VERSION_MIGRACION_USUARIOS = 3
ROLES_VALIDOS = {"Administrador", "Supervisor"}


def _hashear_contrasena(contrasena):
    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        contrasena.encode("utf-8"),
        salt,
        ITERACIONES_HASH_CONTRASENA,
    ).hex()
    return password_hash, salt.hex()


def leer_usuarios_legacy(ruta, contrasena_predefinida=None):
    with ruta.open("r", encoding="utf-8") as archivo:
        datos = json.load(archivo)
    if not isinstance(datos, dict) or not isinstance(datos.get("usuarios"), dict):
        raise ValueError("usuarios.json no tiene el formato esperado.")

    usuarios_legacy = datos["usuarios"]
    eliminados = datos.get("eliminados_definitivamente", [])
    if not isinstance(eliminados, list) or any(
        not isinstance(nombre, str) or not nombre.strip()
        for nombre in eliminados
    ):
        raise ValueError("La lista de usuarios eliminados no es válida.")
    eliminados = {nombre.strip().casefold() for nombre in eliminados}
    eliminados.update(
        nombre.casefold()
        for nombre in USUARIOS_PREDEFINIDOS
        if nombre.casefold() not in {
            name.casefold() for name in usuarios_legacy if isinstance(name, str)
        }
    )

    usuarios = {}
    nombres_vistos = set()
    for username, registro in usuarios_legacy.items():
        if not isinstance(username, str) or not isinstance(registro, dict):
            raise ValueError("usuarios.json contiene una cuenta inválida.")
        if not username.strip() or username.casefold() in nombres_vistos:
            raise ValueError(
                "usuarios.json contiene un nombre de usuario vacío o duplicado."
            )
        nombres_vistos.add(username.casefold())
        if username.casefold() in eliminados:
            continue

        role = registro.get("rol")
        active = registro.get("activo")
        last_login = registro.get("ultimo_inicio_sesion")
        if not isinstance(active, bool) or (
            last_login is not None and not isinstance(last_login, str)
        ):
            raise ValueError(f"La cuenta {username!r} está incompleta.")
        if last_login:
            try:
                last_login = datetime.fromisoformat(last_login)
            except ValueError as error:
                raise ValueError(
                    f"La fecha de inicio de sesión de {username!r} no es válida."
                ) from error
        else:
            last_login = None

        if username.casefold() in {
            name.casefold() for name in USUARIOS_PREDEFINIDOS
        }:
            expected_role = "Administrador"
            if role != expected_role:
                raise ValueError(
                    f"La cuenta predefinida {username!r} no tiene rol "
                    f"{expected_role!r}."
                )
            if not contrasena_predefinida:
                raise ValueError(
                    "Se requiere la contraseña actual de la cuenta "
                    f"{username!r} para migrarla de forma segura."
                )
            password_hash, salt = _hashear_contrasena(contrasena_predefinida)
            role = expected_role
            active = True
            predefinido = True
        else:
            if role not in ROLES_VALIDOS and not re.fullmatch(
                r"Supervisor [1-4]", str(role)
            ):
                raise ValueError(f"El rol de {username!r} no es válido.")
            if role not in ROLES_VALIDOS:
                role = "Supervisor"
            salt = registro.get("salt")
            password_hash = registro.get("contrasena_hash")
            try:
                salt_bytes = bytes.fromhex(salt) if isinstance(salt, str) else b""
                hash_bytes = (
                    bytes.fromhex(password_hash)
                    if isinstance(password_hash, str)
                    else b""
                )
            except ValueError as error:
                raise ValueError(
                    f"El hash de contraseña de {username!r} no es válido."
                ) from error
            if len(salt_bytes) != 16 or len(hash_bytes) != 32:
                raise ValueError(
                    f"La cuenta {username!r} no tiene un hash PBKDF2 válido."
                )
            predefinido = False

        usuarios[username] = {
            "contrasena": None,
            "contrasena_hash": password_hash,
            "salt": salt,
            "rol": role,
            "ultimo_inicio_sesion": last_login,
            "activo": active,
            "predefinido": predefinido,
        }
    return usuarios, eliminados


def migrar_usuarios(usuarios, eliminados):
    with conectar() as conexion:
        cursor = conexion.cursor()
        cursor.execute("LOCK TABLE usuarios IN SHARE ROW EXCLUSIVE MODE")
        cursor.execute("SELECT COUNT(*) FROM usuarios")
        if cursor.fetchone()[0]:
            raise ValueError(
                "Supabase ya tiene usuarios; se cancela para evitar "
                "sobrescribir cuentas existentes."
            )
        cursor.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s",
            (VERSION_MIGRACION_USUARIOS,),
        )
        if cursor.fetchone():
            raise ValueError("La migración de usuarios ya fue aplicada.")

        guardar_usuarios_en_conexion(conexion, usuarios, eliminados)
        cursor.execute(
            """
            INSERT INTO schema_migrations (version)
            VALUES (%s)
            ON CONFLICT (version) DO NOTHING
            """,
            (VERSION_MIGRACION_USUARIOS,),
        )
    return len(usuarios)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Importa usuarios.json a Supabase. La contraseña de las cuentas "
            "se guarda como hash PBKDF2, nunca en texto plano."
        )
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=Path(__file__).resolve().parent / "usuarios.json",
        help="ruta al archivo de usuarios existente",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="escribe los usuarios en Supabase; por defecto solo valida",
    )
    argumentos = parser.parse_args()
    ruta = argumentos.json.resolve()
    if not ruta.is_file():
        raise FileNotFoundError(f"No se encontró el archivo: {ruta}")

    nombres_en_json = set()
    with ruta.open("r", encoding="utf-8") as archivo:
        estructura = json.load(archivo)
    if isinstance(estructura, dict) and isinstance(
        estructura.get("usuarios"), dict
    ):
        nombres_en_json = {
            username.casefold()
            for username in estructura["usuarios"]
            if isinstance(username, str)
        }

    password_predefinido = None
    admin_en_json = any(
        username.casefold() in {
            name.casefold() for name in USUARIOS_PREDEFINIDOS
        }
        for username in nombres_en_json
    )
    if admin_en_json:
        password_predefinido = getpass.getpass(
            "Contraseña que usará la cuenta administradora predefinida: "
        )
        if not password_predefinido:
            raise ValueError("La contraseña no puede estar vacía.")
        confirmar_password = getpass.getpass("Confirme esa contraseña: ")
        if not hmac.compare_digest(password_predefinido, confirmar_password):
            raise ValueError("Las contraseñas no coinciden.")

    usuarios, eliminados = leer_usuarios_legacy(
        ruta,
        password_predefinido,
    )
    print(f"Cuentas válidas para migrar: {len(usuarios)}")
    print(f"Nombres bloqueados conservados: {len(eliminados)}")
    if not argumentos.apply:
        print("Simulación: Supabase no fue modificado.")
        print("Para aplicar: python migrar_usuarios_a_supabase.py --apply")
        return

    inicializar_base_datos()
    total = migrar_usuarios(usuarios, eliminados)
    print(f"Migración de usuarios completada: {total} cuentas.")


if __name__ == "__main__":
    main()
