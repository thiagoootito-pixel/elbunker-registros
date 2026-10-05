from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import secrets

import psycopg2
import streamlit as st


LOCALES_VALIDOS = ("Búnker 1", "Búnker 2", "Búnker 3")
LOCAL_GLOBAL = "Todos los locales"
USUARIOS_PREDEFINIDOS = frozenset({"Titovallejo"})
ITERACIONES_HASH_CONTRASENA = 600_000

COLUMNAS_REGISTRO = {
    "ID": "id",
    "Tipo": "tipo",
    "Placa": "placa",
    "Servicio": "servicio",
    "Pago": "pago",
    "Monto": "monto",
    "Fecha": "fecha",
    "Lavador 1": "lavador1",
    "Lavador 2": "lavador2",
    "Lavador 3": "lavador3",
    "Motivo Gasto": "motivo_gasto",
    "Precio Gasto": "precio_gasto",
    "Método Gasto": "metodo_gasto",
    "Local": "local",
    "RUC/DNI": "ruc_dni",
    "Razón Social/Nombre": "razon_social_nombre",
    "Número Cliente": "numero_cliente",
    "Correo Cliente": "correo_cliente",
    "Registrado por": "registrado_por",
}

_FORMATOS_FECHA = (
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y",
)

_COLUMNAS_SQL = ", ".join(COLUMNAS_REGISTRO.values())


@contextmanager
def conectar():
    try:
        database_url = st.secrets["postgres"]["url"]
    except (KeyError, FileNotFoundError) as error:
        raise ValueError(
            'Configura la URL de PostgreSQL en st.secrets["postgres"]["url"].'
        ) from error

    conexion = psycopg2.connect(database_url)
    try:
        yield conexion
        conexion.commit()
    except Exception:
        conexion.rollback()
        raise
    finally:
        conexion.close()


def _ejecutar(conexion, consulta, parametros=None):
    cursor = conexion.cursor()
    cursor.execute(consulta, parametros)
    return cursor


def _normalizar_fecha(valor):
    texto = str(valor or "").strip()
    for formato in _FORMATOS_FECHA:
        try:
            fecha = datetime.strptime(texto, formato)
            return fecha.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return texto


def inicializar_base_datos():
    with conectar() as conexion:
        _ejecutar(
            conexion,
            """
            CREATE TABLE IF NOT EXISTS registros (
                id BIGSERIAL PRIMARY KEY,
                tipo TEXT NOT NULL DEFAULT '',
                placa TEXT NOT NULL DEFAULT '',
                servicio TEXT NOT NULL DEFAULT '',
                pago TEXT NOT NULL DEFAULT '',
                monto TEXT NOT NULL DEFAULT '',
                fecha TEXT NOT NULL DEFAULT '',
                lavador1 TEXT NOT NULL DEFAULT '',
                lavador2 TEXT NOT NULL DEFAULT '',
                lavador3 TEXT NOT NULL DEFAULT '',
                motivo_gasto TEXT NOT NULL DEFAULT '',
                precio_gasto TEXT NOT NULL DEFAULT '',
                metodo_gasto TEXT NOT NULL DEFAULT '',
                local TEXT NOT NULL DEFAULT 'Búnker 1'
                    CHECK (local IN ('Búnker 1', 'Búnker 2', 'Búnker 3')),
                ruc_dni TEXT NOT NULL DEFAULT '',
                razon_social_nombre TEXT NOT NULL DEFAULT '',
                numero_cliente TEXT NOT NULL DEFAULT '',
                correo_cliente TEXT NOT NULL DEFAULT '',
                registrado_por TEXT NOT NULL DEFAULT ''
            )
            """,
        )
        _ejecutar(
            conexion,
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                aplicado_en TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
        _ejecutar(
            conexion,
            """
            CREATE TABLE IF NOT EXISTS usuarios (
                id BIGSERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL,
                salt TEXT NOT NULL,
                last_login TIMESTAMPTZ,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                local TEXT NOT NULL DEFAULT 'Todos los locales',
                predefinido BOOLEAN NOT NULL DEFAULT FALSE
            )
            """,
        )
        _ejecutar(
            conexion,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_username "
            "ON usuarios (username)",
        )
        _ejecutar(
            conexion,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_username_ci "
            "ON usuarios (lower(username))",
        )
        _ejecutar(
            conexion,
            """
            CREATE TABLE IF NOT EXISTS usuarios_eliminados_definitivamente (
                username TEXT PRIMARY KEY
            )
            """,
        )
        _ejecutar(
            conexion,
            "CREATE INDEX IF NOT EXISTS idx_registros_local_fecha "
            "ON registros(local, fecha)",
        )
        _ejecutar(
            conexion,
            "CREATE INDEX IF NOT EXISTS idx_registros_local_responsable_fecha "
            "ON registros(local, registrado_por, fecha)",
        )
        _ejecutar(
            conexion,
            """
            INSERT INTO schema_migrations (version)
            VALUES (1)
            ON CONFLICT (version) DO NOTHING
            """,
        )


def consultar_registros(local=None, fecha=None, mes=None, supervisor=None):
    condiciones = []
    parametros = []
    if local and local != "Todos los locales":
        condiciones.append("local = %s")
        parametros.append(local)
    if fecha is not None:
        siguiente_dia = fecha + timedelta(days=1)
        condiciones.extend(("fecha >= %s", "fecha < %s"))
        parametros.extend(
            (fecha.strftime("%Y-%m-%d"), siguiente_dia.strftime("%Y-%m-%d"))
        )
    if mes is not None:
        inicio = mes.replace(day=1)
        if inicio.month == 12:
            fin = inicio.replace(year=inicio.year + 1, month=1)
        else:
            fin = inicio.replace(month=inicio.month + 1)
        condiciones.extend(("fecha >= %s", "fecha < %s"))
        parametros.extend(
            (inicio.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d"))
        )
    if supervisor is not None:
        condiciones.append("lower(trim(registrado_por)) = lower(%s)")
        parametros.append(supervisor)
    where_sql = f"WHERE {' AND '.join(condiciones)}" if condiciones else ""
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            f"SELECT {_COLUMNAS_SQL} FROM registros {where_sql} ORDER BY id",
            parametros,
        )
        columnas = [columna.name for columna in cursor.description]
        return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


def consultar_usuarios():
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            """
            SELECT username, password, role, salt, last_login,
                   active, local, predefinido
            FROM usuarios
            ORDER BY username
            """,
        )
        usuarios = {}
        for (
            username,
            password_hash,
            role,
            salt,
            last_login,
            active,
            local,
            predefinido,
        ) in cursor.fetchall():
            if isinstance(last_login, datetime):
                last_login = last_login.isoformat(timespec="seconds")
            usuarios[username] = {
                "contrasena": None,
                "contrasena_hash": password_hash,
                "salt": salt,
                "rol": role,
                "ultimo_inicio_sesion": last_login,
                "activo": active,
                "local": local,
                "predefinido": predefinido,
            }
        return usuarios


def consultar_usuarios_eliminados():
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            "SELECT username FROM usuarios_eliminados_definitivamente",
        )
        return {fila[0].casefold() for fila in cursor.fetchall()}


def _credenciales_persistentes(cuenta):
    salt = cuenta.get("salt")
    password_hash = cuenta.get("contrasena_hash")
    if isinstance(salt, str) and isinstance(password_hash, str):
        try:
            salt_bytes = bytes.fromhex(salt)
            hash_bytes = bytes.fromhex(password_hash)
        except ValueError:
            salt_bytes = b""
            hash_bytes = b""
        if len(salt_bytes) == 16 and len(hash_bytes) == 32:
            return password_hash, salt

    contrasena = cuenta.get("contrasena")
    if not isinstance(contrasena, str) or not contrasena:
        raise ValueError("La cuenta no tiene credenciales válidas para guardar.")
    salt_bytes = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        contrasena.encode("utf-8"),
        salt_bytes,
        ITERACIONES_HASH_CONTRASENA,
    ).hex()
    return password_hash, salt_bytes.hex()


def guardar_usuarios_en_conexion(
    conexion, usuarios, usuarios_eliminados_definitivamente=()
):
    cursor = _ejecutar(conexion, "SELECT username FROM usuarios")
    existentes = {fila[0] for fila in cursor.fetchall()}
    deseados = set(usuarios)

    for username in existentes - deseados:
        _ejecutar(
            conexion,
            "DELETE FROM usuarios WHERE username = %s",
            (username,),
        )

    for username, cuenta in usuarios.items():
        password_hash, salt = _credenciales_persistentes(cuenta)
        ultimo_inicio = cuenta.get("ultimo_inicio_sesion")
        if isinstance(ultimo_inicio, str):
            ultimo_inicio = (
                datetime.fromisoformat(ultimo_inicio)
                if ultimo_inicio
                else None
            )
        _ejecutar(
            conexion,
            """
            INSERT INTO usuarios (
                username, password, role, salt, last_login,
                active, local, predefinido
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (username) DO UPDATE SET
                username = EXCLUDED.username,
                password = EXCLUDED.password,
                role = EXCLUDED.role,
                salt = EXCLUDED.salt,
                last_login = EXCLUDED.last_login,
                active = EXCLUDED.active,
                local = EXCLUDED.local,
                predefinido = EXCLUDED.predefinido
            """,
            (
                username,
                password_hash,
                cuenta["rol"],
                salt,
                ultimo_inicio,
                bool(cuenta["activo"]),
                cuenta["local"],
                bool(cuenta.get("predefinido", False)),
            ),
        )

    _ejecutar(conexion, "DELETE FROM usuarios_eliminados_definitivamente")
    for username in usuarios_eliminados_definitivamente:
        _ejecutar(
            conexion,
            """
            INSERT INTO usuarios_eliminados_definitivamente (username)
            VALUES (%s)
            ON CONFLICT (username) DO NOTHING
            """,
            (str(username).casefold(),),
        )


def guardar_usuarios(usuarios, usuarios_eliminados_definitivamente=()):
    with conectar() as conexion:
        guardar_usuarios_en_conexion(
            conexion,
            usuarios,
            usuarios_eliminados_definitivamente,
        )
def actualizar_ultimo_inicio_sesion(username, ultimo_inicio_sesion):
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            "UPDATE usuarios SET last_login = %s WHERE username = %s",
            (datetime.fromisoformat(ultimo_inicio_sesion), username),
        )
        actualizado = cursor.rowcount == 1
    return actualizado


def insertar_registro(registro):
    campos = [
        nombre for nombre in COLUMNAS_REGISTRO if nombre != "ID"
    ]
    valores = []
    for nombre in campos:
        valor = registro.get(nombre, "")
        if nombre == "Fecha":
            valor = _normalizar_fecha(valor)
        valores.append("" if valor is None else str(valor))
    columnas = ", ".join(COLUMNAS_REGISTRO[nombre] for nombre in campos)
    parametros = ", ".join("%s" for _ in campos)
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            f"INSERT INTO registros ({columnas}) VALUES ({parametros}) "
            "RETURNING id",
            valores,
        )
        registro_id = cursor.fetchone()[0]
    return registro_id


def actualizar_registro(registro_id, local, cambios):
    campos = [
        nombre
        for nombre in cambios
        if nombre in COLUMNAS_REGISTRO and nombre not in {"ID", "Local"}
    ]
    if not campos:
        return False
    asignaciones = ", ".join(
        f"{COLUMNAS_REGISTRO[nombre]} = %s" for nombre in campos
    )
    valores = [
        _normalizar_fecha(cambios[nombre])
        if nombre == "Fecha"
        else str(cambios[nombre] or "")
        for nombre in campos
    ]
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            f"UPDATE registros SET {asignaciones} WHERE id = %s AND local = %s",
            (*valores, int(registro_id), local),
        )
        actualizado = cursor.rowcount == 1
    return actualizado


def eliminar_registro(registro_id, local):
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            "DELETE FROM registros WHERE id = %s AND local = %s",
            (int(registro_id), local),
        )
        eliminado = cursor.rowcount == 1
    return eliminado


def eliminar_registros_supervisor(nombre):
    nombre_normalizado = (
        nombre.casefold().replace(" ", "").replace("_", "").replace("-", "")
    )
    with conectar() as conexion:
        eliminados = eliminar_registros_supervisor_en_conexion(
            conexion, nombre_normalizado
        )
    return eliminados


def eliminar_registros_supervisor_en_conexion(conexion, nombre_o_normalizado):
    nombre_normalizado = (
        nombre_o_normalizado.casefold()
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
    )
    cursor = _ejecutar(
        conexion,
        """
        DELETE FROM registros
        WHERE replace(replace(replace(lower(trim(registrado_por)), ' ', ''),
                              '_', ''), '-', '') = %s
        """,
        (nombre_normalizado,),
    )
    return cursor.rowcount
