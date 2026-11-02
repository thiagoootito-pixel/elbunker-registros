from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import secrets

import psycopg2
import streamlit as st


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
    "%Y/%m/%d %I:%M:%S %p",
    "%Y/%m/%d %I:%M %p",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%d/%m/%Y %I:%M:%S %p",
    "%d/%m/%Y %I:%M %p",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%Y-%m-%d %I:%M:%S %p",
    "%Y-%m-%d %I:%M %p",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d-%m-%Y %I:%M:%S %p",
    "%d-%m-%Y %I:%M %p",
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
            CREATE TABLE IF NOT EXISTS personal_trabajadores (
                id BIGSERIAL PRIMARY KEY,
                nombre TEXT NOT NULL,
                cargo TEXT,
                sueldo_base NUMERIC(12, 2)
                    CHECK (sueldo_base IS NULL OR sueldo_base >= 0),
                modalidad_pago TEXT
                    CHECK (modalidad_pago IS NULL OR modalidad_pago IN (
                        'Diario', 'Quincenal', 'Mensual', 'Semanal'
                    )),
                dias_libres TEXT[] NOT NULL DEFAULT '{}',
                fecha_ingreso DATE,
                activo BOOLEAN NOT NULL DEFAULT TRUE,
                creado_en TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                actualizado_en TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
        _ejecutar(
            conexion,
            "ALTER TABLE personal_trabajadores "
            "DROP CONSTRAINT IF EXISTS personal_trabajadores_modalidad_pago_check",
        )
        _ejecutar(
            conexion,
            """
            ALTER TABLE personal_trabajadores
            ADD CONSTRAINT personal_trabajadores_modalidad_pago_check
            CHECK (modalidad_pago IS NULL OR modalidad_pago IN (
                'Diario', 'Quincenal', 'Mensual', 'Semanal'
            ))
            """,
        )
        _ejecutar(
            conexion,
            "ALTER TABLE personal_trabajadores "
            "ADD COLUMN IF NOT EXISTS dias_libres TEXT[] "
            "NOT NULL DEFAULT '{}'",
        )
        _ejecutar(
            conexion,
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_personal_trabajadores_nombre_ci "
            "ON personal_trabajadores (lower(trim(nombre)))",
        )
        _ejecutar(
            conexion,
            """
            CREATE TABLE IF NOT EXISTS personal_asistencias (
                id BIGSERIAL PRIMARY KEY,
                trabajador_id BIGINT NOT NULL
                    REFERENCES personal_trabajadores(id),
                fecha DATE NOT NULL,
                estado TEXT NOT NULL
                    CHECK (estado IN ('A', 'F', 'M')),
                observacion TEXT NOT NULL DEFAULT '',
                registrado_por TEXT NOT NULL DEFAULT '',
                creado_en TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                actualizado_en TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (trabajador_id, fecha)
            )
            """,
        )
        _ejecutar(
            conexion,
            """
            CREATE TABLE IF NOT EXISTS personal_liquidaciones (
                id BIGSERIAL PRIMARY KEY,
                trabajador_id BIGINT NOT NULL
                    REFERENCES personal_trabajadores(id),
                modalidad_pago TEXT NOT NULL
                    CHECK (modalidad_pago IN (
                        'Diario', 'Quincenal', 'Mensual', 'Semanal'
                    )),
                fecha_inicio DATE NOT NULL,
                fecha_fin DATE NOT NULL,
                sueldo_base NUMERIC(12, 2) NOT NULL CHECK (sueldo_base >= 0),
                bonificacion NUMERIC(12, 2) NOT NULL DEFAULT 0
                    CHECK (bonificacion >= 0),
                descuento NUMERIC(12, 2) NOT NULL DEFAULT 0
                    CHECK (descuento >= 0),
                adelantos_aplicados NUMERIC(12, 2) NOT NULL DEFAULT 0
                    CHECK (adelantos_aplicados >= 0),
                creado_por TEXT NOT NULL DEFAULT '',
                creado_en TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (fecha_fin >= fecha_inicio),
                UNIQUE (trabajador_id, fecha_inicio, fecha_fin)
            )
            """,
        )
        _ejecutar(
            conexion,
            "ALTER TABLE personal_liquidaciones "
            "ADD COLUMN IF NOT EXISTS descuento NUMERIC(12, 2) "
            "NOT NULL DEFAULT 0 CHECK (descuento >= 0)",
        )
        _ejecutar(
            conexion,
            "ALTER TABLE personal_liquidaciones "
            "DROP CONSTRAINT IF EXISTS personal_liquidaciones_modalidad_pago_check",
        )
        _ejecutar(
            conexion,
            """
            ALTER TABLE personal_liquidaciones
            ADD CONSTRAINT personal_liquidaciones_modalidad_pago_check
            CHECK (modalidad_pago IN (
                'Diario', 'Quincenal', 'Mensual', 'Semanal'
            ))
            """,
        )
        _ejecutar(
            conexion,
            """
            CREATE TABLE IF NOT EXISTS personal_adelantos (
                id BIGSERIAL PRIMARY KEY,
                trabajador_id BIGINT NOT NULL
                    REFERENCES personal_trabajadores(id),
                importe NUMERIC(12, 2) NOT NULL CHECK (importe > 0),
                fecha_entrega DATE NOT NULL,
                observacion TEXT NOT NULL DEFAULT '',
                registrado_por TEXT NOT NULL DEFAULT '',
                estado TEXT NOT NULL DEFAULT 'Pendiente de descontar'
                    CHECK (estado IN (
                        'Pendiente de descontar', 'Aplicado', 'Anulado'
                    )),
                clave_idempotencia TEXT,
                creado_en TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                actualizado_en TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
        _ejecutar(
            conexion,
            "ALTER TABLE personal_adelantos "
            "ADD COLUMN IF NOT EXISTS clave_idempotencia TEXT",
        )
        _ejecutar(
            conexion,
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_personal_adelantos_clave_idempotencia "
            "ON personal_adelantos (clave_idempotencia) "
            "WHERE clave_idempotencia IS NOT NULL",
        )
        _ejecutar(
            conexion,
            """
            CREATE TABLE IF NOT EXISTS personal_adelantos_aplicados (
                id BIGSERIAL PRIMARY KEY,
                adelanto_id BIGINT NOT NULL
                    REFERENCES personal_adelantos(id),
                liquidacion_id BIGINT NOT NULL
                    REFERENCES personal_liquidaciones(id),
                importe NUMERIC(12, 2) NOT NULL CHECK (importe > 0),
                aplicado_por TEXT NOT NULL DEFAULT '',
                aplicado_en TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (adelanto_id, liquidacion_id)
            )
            """,
        )
        _ejecutar(
            conexion,
            """
            CREATE TABLE IF NOT EXISTS personal_pagos (
                id BIGSERIAL PRIMARY KEY,
                liquidacion_id BIGINT NOT NULL
                    REFERENCES personal_liquidaciones(id),
                importe NUMERIC(12, 2) NOT NULL CHECK (importe > 0),
                fecha_pago DATE NOT NULL,
                metodo_pago TEXT NOT NULL DEFAULT '',
                observacion TEXT NOT NULL DEFAULT '',
                registrado_por TEXT NOT NULL DEFAULT '',
                creado_en TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
        _ejecutar(
            conexion,
            "ALTER TABLE personal_pagos "
            "ADD COLUMN IF NOT EXISTS anulado_en TIMESTAMPTZ",
        )
        _ejecutar(
            conexion,
            "ALTER TABLE personal_pagos "
            "ADD COLUMN IF NOT EXISTS anulado_por TEXT NOT NULL DEFAULT ''",
        )
        _ejecutar(
            conexion,
            """
            CREATE TABLE IF NOT EXISTS personal_historial (
                id BIGSERIAL PRIMARY KEY,
                trabajador_id BIGINT NOT NULL
                    REFERENCES personal_trabajadores(id),
                tipo_movimiento TEXT NOT NULL,
                referencia_id BIGINT,
                accion TEXT NOT NULL,
                detalle JSONB NOT NULL DEFAULT '{}'::jsonb,
                registrado_por TEXT NOT NULL DEFAULT '',
                creado_en TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
        _ejecutar(
            conexion,
            "CREATE INDEX IF NOT EXISTS idx_personal_asistencias_fecha "
            "ON personal_asistencias (fecha, trabajador_id)",
        )
        _ejecutar(
            conexion,
            "CREATE INDEX IF NOT EXISTS idx_personal_adelantos_trabajador_estado "
            "ON personal_adelantos (trabajador_id, estado, fecha_entrega)",
        )
        _ejecutar(
            conexion,
            "CREATE INDEX IF NOT EXISTS idx_personal_liquidaciones_periodo "
            "ON personal_liquidaciones (fecha_inicio, fecha_fin, trabajador_id)",
        )
        _ejecutar(
            conexion,
            "CREATE INDEX IF NOT EXISTS idx_personal_pagos_liquidacion "
            "ON personal_pagos (liquidacion_id, fecha_pago)",
        )
        _ejecutar(
            conexion,
            """
            INSERT INTO schema_migrations (version)
            VALUES (1), (2)
            ON CONFLICT (version) DO NOTHING
            """,
        )


def consultar_registros(fecha=None, mes=None, supervisor=None):
    condiciones = []
    parametros = []
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


@st.cache_data(ttl=300, show_spinner=False)
def consultar_resumen_servicios_personal(fecha_inicio, fecha_fin):
    fecha_fin_exclusiva = fecha_fin + timedelta(days=1)
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            r"""
            WITH servicios AS (
                SELECT id,
                       CASE
                           WHEN btrim(replace(monto, 'S/', ''))
                               ~ '^[+-]?[0-9]+([.][0-9]+)?$'
                           THEN btrim(replace(monto, 'S/', ''))::numeric
                           ELSE 0::numeric
                       END AS monto,
                       lavador1, lavador2, lavador3
                FROM registros
                WHERE fecha >= %s AND fecha < %s
                    AND tipo !~* '(^|[^[:alnum:]_])(gasto|egreso|expense)([^[:alnum:]_]|$)'
            ), participantes AS (
                SELECT s.id, s.monto, n.trabajador
                FROM servicios s
                CROSS JOIN LATERAL (
                    SELECT DISTINCT lower(trim(nombre)) AS trabajador
                    FROM (VALUES (s.lavador1), (s.lavador2), (s.lavador3))
                        AS nombres(nombre)
                    WHERE lower(trim(nombre)) NOT IN ('', '-', 'nan', 'none')
                ) n
            ), cantidades AS (
                SELECT id, COUNT(*) AS cantidad
                FROM participantes
                GROUP BY id
            )
            SELECT p.trabajador,
                   COUNT(*) FILTER (WHERE c.cantidad = 1) AS solitarios,
                   COUNT(*) FILTER (WHERE c.cantidad = 2) AS compartidos,
                   COUNT(*) FILTER (WHERE c.cantidad = 3) AS grupales,
                   COALESCE(SUM(p.monto / c.cantidad), 0) AS monto_total,
                   COALESCE(SUM(p.monto / c.cantidad)
                       FILTER (WHERE c.cantidad = 1), 0) AS monto_solitario,
                   COALESCE(SUM(p.monto / c.cantidad)
                       FILTER (WHERE c.cantidad = 2), 0) AS monto_compartido,
                   COALESCE(SUM(p.monto / c.cantidad)
                       FILTER (WHERE c.cantidad = 3), 0) AS monto_grupal
            FROM participantes p
            JOIN cantidades c ON c.id = p.id
            WHERE c.cantidad BETWEEN 1 AND 3
            GROUP BY p.trabajador
            """,
            (
                fecha_inicio.strftime("%Y-%m-%d"),
                fecha_fin_exclusiva.strftime("%Y-%m-%d"),
            ),
        )
        columnas = [columna.name for columna in cursor.description]
        return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


@st.cache_data(ttl=300, show_spinner=False)
def consultar_resumen_adelantos_registros_personal(fecha_inicio, fecha_fin):
    fecha_fin_exclusiva = fecha_fin + timedelta(days=1)
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            r"""
            SELECT lower(regexp_replace(
                       trim(substring(trim(motivo_gasto) FROM 5)),
                       '[[:space:]]+', ' ', 'g'
                   )) AS trabajador,
                   left(fecha, 10)::date AS fecha,
                   COUNT(*) AS solicitudes,
                   COALESCE(SUM(
                       CASE
                           WHEN btrim(replace(precio_gasto, 'S/', ''))
                               ~ '^[+-]?[0-9]+([.][0-9]+)?$'
                           THEN btrim(replace(precio_gasto, 'S/', ''))::numeric
                           ELSE 0::numeric
                       END
                   ), 0) AS importe
            FROM registros
            WHERE lower(left(trim(motivo_gasto), 4)) = 'ad. '
                AND fecha >= %s AND fecha < %s
            GROUP BY lower(regexp_replace(
                         trim(substring(trim(motivo_gasto) FROM 5)),
                         '[[:space:]]+', ' ', 'g'
                     )),
                     left(fecha, 10)::date
            ORDER BY fecha
            """,
            (
                fecha_inicio.strftime("%Y-%m-%d"),
                fecha_fin_exclusiva.strftime("%Y-%m-%d"),
            ),
        )
        columnas = [columna.name for columna in cursor.description]
        return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


def consultar_detalle_adelantos_registros_personal(
    nombre, fecha_inicio, fecha_fin
):
    fecha_fin_exclusiva = fecha_fin + timedelta(days=1)
    trabajador = " ".join(str(nombre or "").casefold().split())
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            r"""
            SELECT id, fecha,
                   CASE
                       WHEN btrim(replace(precio_gasto, 'S/', ''))
                           ~ '^[+-]?[0-9]+([.][0-9]+)?$'
                       THEN btrim(replace(precio_gasto, 'S/', ''))::numeric
                       ELSE 0::numeric
                   END AS importe,
                   metodo_gasto AS metodo_pago,
                   registrado_por
            FROM registros
            WHERE lower(left(trim(motivo_gasto), 4)) = 'ad. '
                AND lower(regexp_replace(
                    trim(substring(trim(motivo_gasto) FROM 5)),
                    '[[:space:]]+', ' ', 'g'
                )) = %s
                AND fecha >= %s AND fecha < %s
            ORDER BY fecha DESC, id DESC
            """,
            (
                trabajador,
                fecha_inicio.strftime("%Y-%m-%d"),
                fecha_fin_exclusiva.strftime("%Y-%m-%d"),
            ),
        )
        columnas = [columna.name for columna in cursor.description]
        return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


def consultar_detalle_servicios_personal(
    nombre, fecha_inicio, fecha_fin
):
    fecha_fin_exclusiva = fecha_fin + timedelta(days=1)
    trabajador = str(nombre or "").strip().casefold()
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            r"""
            WITH servicios AS (
                SELECT id, fecha, tipo, servicio, registrado_por,
                       CASE
                           WHEN btrim(replace(monto, 'S/', ''))
                               ~ '^[+-]?[0-9]+([.][0-9]+)?$'
                           THEN btrim(replace(monto, 'S/', ''))::numeric
                           ELSE 0::numeric
                       END AS monto,
                       lavador1, lavador2, lavador3
                FROM registros
                WHERE fecha >= %s AND fecha < %s
                    AND tipo !~* '(^|[^[:alnum:]_])(gasto|egreso|expense)([^[:alnum:]_]|$)'
            ), participantes AS (
                SELECT s.id, s.fecha, s.tipo, s.servicio, s.monto,
                       s.registrado_por, n.trabajador
                FROM servicios s
                CROSS JOIN LATERAL (
                    SELECT DISTINCT lower(trim(nombre)) AS trabajador
                    FROM (VALUES (s.lavador1), (s.lavador2), (s.lavador3))
                        AS nombres(nombre)
                    WHERE lower(trim(nombre)) NOT IN ('', '-', 'nan', 'none')
                ) n
            ), cantidades AS (
                SELECT id, COUNT(*) AS participantes
                FROM participantes
                GROUP BY id
            )
            SELECT p.id, p.fecha, p.tipo, p.servicio, p.monto,
                   c.participantes, p.registrado_por
            FROM participantes p
            JOIN cantidades c ON c.id = p.id
            WHERE c.participantes BETWEEN 1 AND 3
                AND p.trabajador = %s
            ORDER BY p.fecha DESC, p.id DESC
            """,
            (
                fecha_inicio.strftime("%Y-%m-%d"),
                fecha_fin_exclusiva.strftime("%Y-%m-%d"),
                trabajador,
            ),
        )
        columnas = [columna.name for columna in cursor.description]
        return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


def invalidar_cache_resumenes_personal():
    consultar_resumen_servicios_personal.clear()
    consultar_resumen_adelantos_registros_personal.clear()


def consultar_usuarios():
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            """
            SELECT username, password, role, salt, last_login,
                   active, predefinido
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


def normalizar_usuarios(usuarios):
    if not isinstance(usuarios, Mapping):
        raise ValueError("La lista de usuarios no tiene un formato válido.")

    usuarios_normalizados = {}
    for username, cuenta in usuarios.items():
        if not isinstance(cuenta, Mapping):
            raise ValueError(f"La cuenta {username!r} tiene un formato inválido.")

        rol = cuenta.get("rol")
        activo = cuenta.get("activo")
        if not isinstance(rol, str) or not rol.strip():
            raise ValueError(f"La cuenta {username!r} no tiene un rol válido.")
        if not isinstance(activo, bool):
            raise ValueError(f"La cuenta {username!r} no tiene un estado válido.")

        usuarios_normalizados[username] = {
            "contrasena": cuenta.get("contrasena"),
            "contrasena_hash": cuenta.get("contrasena_hash"),
            "salt": cuenta.get("salt"),
            "rol": rol,
            "ultimo_inicio_sesion": cuenta.get("ultimo_inicio_sesion"),
            "activo": activo,
            "predefinido": bool(cuenta.get("predefinido", False)),
        }
    return usuarios_normalizados


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
    usuarios = normalizar_usuarios(usuarios)
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
                active, predefinido
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (username) DO UPDATE SET
                username = EXCLUDED.username,
                password = EXCLUDED.password,
                role = EXCLUDED.role,
                salt = EXCLUDED.salt,
                last_login = EXCLUDED.last_login,
                active = EXCLUDED.active,
                predefinido = EXCLUDED.predefinido
            """,
            (
                username,
                password_hash,
                cuenta.get("rol"),
                salt,
                ultimo_inicio,
                cuenta.get("activo"),
                cuenta.get("predefinido", False),
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


def actualizar_registro(registro_id, cambios):
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
            f"UPDATE registros SET {asignaciones} WHERE id = %s",
            (*valores, int(registro_id)),
        )
        actualizado = cursor.rowcount == 1
    return actualizado


def eliminar_registro(registro_id):
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            "DELETE FROM registros WHERE id = %s",
            (int(registro_id),),
        )
        eliminado = cursor.rowcount == 1
    return eliminado


def actualizar_fecha_registros(registro_ids, fecha):
    ids = list(dict.fromkeys(int(registro_id) for registro_id in registro_ids))
    if not ids:
        return 0

    fecha_nueva = fecha.strftime("%Y/%m/%d")
    patron_fecha = (
        r"^[[:space:]]*([0-9]{4}/[0-9]{2}/[0-9]{2}|[0-9]{4}-[0-9]{2}-[0-9]{2}|"
        r"[0-9]{2}/[0-9]{2}/[0-9]{4}|[0-9]{2}-[0-9]{2}-[0-9]{4})"
    )
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            """
            UPDATE registros
            SET fecha = regexp_replace(fecha, %s, %s)
            WHERE id = ANY(%s)
            RETURNING id
            """,
            (patron_fecha, fecha_nueva, ids),
        )
        actualizados = len(cursor.fetchall())
        if actualizados != len(ids):
            raise ValueError(
                "No se pudo actualizar la fecha de todos los registros seleccionados."
            )
    return actualizados


def eliminar_registros(registro_ids):
    ids = list(dict.fromkeys(int(registro_id) for registro_id in registro_ids))
    if not ids:
        return 0

    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            "DELETE FROM registros WHERE id = ANY(%s) RETURNING id",
            (ids,),
        )
        eliminados = len(cursor.fetchall())
    return eliminados


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


def _obtener_id_trabajador(conexion, nombre, crear=False):
    nombre_limpio = str(nombre or "").strip()
    if not nombre_limpio:
        raise ValueError("Selecciona un trabajador válido.")
    cursor = _ejecutar(
        conexion,
        """
        SELECT id FROM personal_trabajadores
        WHERE lower(trim(nombre)) = lower(trim(%s))
        """,
        (nombre_limpio,),
    )
    fila = cursor.fetchone()
    if fila:
        return fila[0]
    if not crear:
        raise ValueError("El trabajador ya no está disponible.")
    cursor = _ejecutar(
        conexion,
        """
        INSERT INTO personal_trabajadores (nombre)
        VALUES (%s)
        ON CONFLICT (lower(trim(nombre))) DO NOTHING
        RETURNING id
        """,
        (nombre_limpio,),
    )
    fila = cursor.fetchone()
    if fila:
        return fila[0]
    cursor = _ejecutar(
        conexion,
        """
        SELECT id FROM personal_trabajadores
        WHERE lower(trim(nombre)) = lower(trim(%s))
        """,
        (nombre_limpio,),
    )
    fila = cursor.fetchone()
    if not fila:
        raise RuntimeError(
            "No se pudo encontrar o crear el perfil del trabajador."
        )
    return fila[0]


def _registrar_historial(
    conexion, trabajador_id, tipo, referencia_id, accion, detalle, registrado_por
):
    _ejecutar(
        conexion,
        """
        INSERT INTO personal_historial (
            trabajador_id, tipo_movimiento, referencia_id, accion,
            detalle, registrado_por
        )
        VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        """,
        (
            trabajador_id,
            tipo,
            referencia_id,
            accion,
            json.dumps(detalle, ensure_ascii=False, default=str),
            str(registrado_por or ""),
        ),
    )


def consultar_perfiles_personal(nombres):
    nombres_limpios = list(
        dict.fromkeys(
            str(nombre).strip()
            for nombre in nombres
            if str(nombre).strip()
        )
    )
    if not nombres_limpios:
        return []
    with conectar() as conexion:
        _ejecutar(
            conexion,
            """
            INSERT INTO personal_trabajadores (nombre)
            SELECT DISTINCT trim(n.nombre)
            FROM unnest(%s::text[]) AS n(nombre)
            WHERE trim(n.nombre) <> ''
            ON CONFLICT (lower(trim(nombre))) DO NOTHING
            """,
            (nombres_limpios,),
        )
        cursor = _ejecutar(
            conexion,
            """
            SELECT id, nombre, cargo, sueldo_base, modalidad_pago,
                   dias_libres, fecha_ingreso, activo
            FROM personal_trabajadores
            WHERE lower(trim(nombre)) = ANY(%s::text[])
            ORDER BY lower(nombre)
            """,
            ([nombre.lower() for nombre in nombres_limpios],),
        )
        columnas = [columna.name for columna in cursor.description]
        return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


def guardar_perfil_personal(
    nombre,
    cargo,
    sueldo_base,
    modalidad_pago,
    dias_libres,
    fecha_ingreso,
    activo,
    registrado_por,
):
    modalidades = {"Diario", "Quincenal", "Mensual", "Semanal"}
    nombre_limpio = str(nombre or "").strip()
    cargo_limpio = str(cargo or "").strip()
    if not nombre_limpio:
        raise ValueError("El nombre del trabajador es obligatorio.")
    if modalidad_pago not in modalidades:
        raise ValueError("Selecciona una modalidad de pago válida.")
    dias_semana = {
        "Lunes",
        "Martes",
        "Miércoles",
        "Jueves",
        "Viernes",
        "Sábado",
        "Domingo",
    }
    dias_libres_limpios = list(dict.fromkeys(dias_libres or []))
    if any(dia not in dias_semana for dia in dias_libres_limpios):
        raise ValueError("Selecciona días libres de la semana válidos.")
    try:
        sueldo = Decimal(str(sueldo_base)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Ingresa un sueldo base válido.") from error
    if not sueldo.is_finite() or sueldo < 0:
        raise ValueError("El sueldo base no puede ser negativo.")
    if not isinstance(fecha_ingreso, date):
        raise ValueError("Selecciona una fecha de ingreso válida.")
    with conectar() as conexion:
        trabajador_id = _obtener_id_trabajador(
            conexion, nombre_limpio, crear=True
        )
        cursor = _ejecutar(
            conexion,
            """
            SELECT cargo, sueldo_base, modalidad_pago, dias_libres,
                   fecha_ingreso, activo
            FROM personal_trabajadores
            WHERE id = %s
            FOR UPDATE
            """,
            (trabajador_id,),
        )
        anterior = cursor.fetchone()
        nuevo = (
            cargo_limpio,
            sueldo,
            modalidad_pago,
            dias_libres_limpios,
            fecha_ingreso,
            bool(activo),
        )
        _ejecutar(
            conexion,
            """
            UPDATE personal_trabajadores
            SET cargo = %s, sueldo_base = %s, modalidad_pago = %s,
                dias_libres = %s, fecha_ingreso = %s, activo = %s,
                actualizado_en = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (*nuevo, trabajador_id),
        )
        _registrar_historial(
            conexion,
            trabajador_id,
            "perfil",
            trabajador_id,
            "actualizado",
            {"antes": anterior, "despues": nuevo},
            registrado_por,
        )
    return trabajador_id


def guardar_asistencia_personal(
    nombre, fecha, estado, observacion="", registrado_por=""
):
    if estado not in {"A", "F", "M"}:
        raise ValueError("El estado de asistencia debe ser A, F o M.")
    if not isinstance(fecha, date):
        raise ValueError("Selecciona una fecha de asistencia válida.")
    with conectar() as conexion:
        trabajador_id = _obtener_id_trabajador(
            conexion, nombre, crear=True
        )
        return _guardar_asistencia_personal_en_transaccion(
            conexion,
            trabajador_id,
            fecha,
            estado,
            observacion,
            registrado_por,
        )


def _guardar_asistencia_personal_en_transaccion(
    conexion, trabajador_id, fecha, estado, observacion, registrado_por
):
    cursor = _ejecutar(
        conexion,
        """
        SELECT estado, observacion FROM personal_asistencias
        WHERE trabajador_id = %s AND fecha = %s
        FOR UPDATE
        """,
        (trabajador_id, fecha),
    )
    anterior = cursor.fetchone()
    observacion_limpia = str(observacion or "").strip()
    cursor = _ejecutar(
        conexion,
        """
        INSERT INTO personal_asistencias (
            trabajador_id, fecha, estado, observacion, registrado_por
        )
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (trabajador_id, fecha) DO UPDATE SET
            estado = EXCLUDED.estado,
            observacion = EXCLUDED.observacion,
            registrado_por = EXCLUDED.registrado_por,
            actualizado_en = CURRENT_TIMESTAMP
        RETURNING id
        """,
        (
            trabajador_id,
            fecha,
            estado,
            observacion_limpia,
            str(registrado_por or ""),
        ),
    )
    asistencia_id = cursor.fetchone()[0]
    _registrar_historial(
        conexion,
        trabajador_id,
        "asistencia",
        asistencia_id,
        "creado" if anterior is None else "actualizado",
        {
            "fecha": fecha,
            "antes": anterior,
            "despues": (estado, observacion_limpia),
        },
        registrado_por,
    )
    return asistencia_id


def consultar_asistencias_personal_fecha(fecha):
    if not isinstance(fecha, date):
        raise ValueError("Selecciona una fecha de asistencia válida.")
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            """
            SELECT trabajador_id, estado, observacion
            FROM personal_asistencias
            WHERE fecha = %s
            """,
            (fecha,),
        )
        columnas = [columna.name for columna in cursor.description]
        return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


def consultar_asistencias_personal_periodo(fecha_inicio, fecha_fin):
    if (
        not isinstance(fecha_inicio, date)
        or not isinstance(fecha_fin, date)
    ):
        raise ValueError("Selecciona un periodo de asistencia válido.")
    if fecha_fin < fecha_inicio:
        raise ValueError("La fecha final no puede ser anterior a la inicial.")
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            """
            SELECT t.id AS trabajador_id, t.nombre, a.fecha, a.estado,
                   a.observacion,
                   a.registrado_por,
                   COALESCE(a.actualizado_en, a.creado_en)
                       AS fecha_hora_registro
            FROM personal_asistencias a
            JOIN personal_trabajadores t ON t.id = a.trabajador_id
            WHERE a.fecha BETWEEN %s AND %s
            ORDER BY lower(t.nombre), a.fecha DESC
            """,
            (fecha_inicio, fecha_fin),
        )
        columnas = [columna.name for columna in cursor.description]
        return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


def guardar_asistencias_personal(registros, fecha, registrado_por=""):
    if not isinstance(fecha, date):
        raise ValueError("Selecciona una fecha de asistencia válida.")
    registros_limpios = []
    trabajadores_vistos = set()
    for trabajador_id, estado, observacion in registros:
        if (
            isinstance(trabajador_id, bool)
            or not isinstance(trabajador_id, int)
            or trabajador_id <= 0
        ):
            raise ValueError("Se encontró un trabajador no válido.")
        if trabajador_id in trabajadores_vistos:
            raise ValueError("Un trabajador aparece más de una vez.")
        if estado not in {"A", "F", "M"}:
            raise ValueError("Selecciona A, F o M para cada trabajador.")
        trabajadores_vistos.add(trabajador_id)
        registros_limpios.append(
            (trabajador_id, estado, str(observacion or "").strip())
        )
    if not registros_limpios:
        raise ValueError("Selecciona al menos un trabajador.")

    with conectar() as conexion:
        for trabajador_id, estado, observacion in registros_limpios:
            cursor = _ejecutar(
                conexion,
                """
                SELECT id FROM personal_trabajadores
                WHERE id = %s
                FOR SHARE
                """,
                (trabajador_id,),
            )
            if cursor.fetchone() is None:
                raise ValueError("El trabajador ya no está disponible.")
            _guardar_asistencia_personal_en_transaccion(
                conexion,
                trabajador_id,
                fecha,
                estado,
                observacion,
                registrado_por,
            )


def consultar_asistencias_personal(
    nombre, fecha_inicio=None, fecha_fin=None
):
    with conectar() as conexion:
        trabajador_id = _obtener_id_trabajador(conexion, nombre)
        condiciones = ["trabajador_id = %s"]
        parametros = [trabajador_id]
        if fecha_inicio is not None:
            condiciones.append("fecha >= %s")
            parametros.append(fecha_inicio)
        if fecha_fin is not None:
            condiciones.append("fecha <= %s")
            parametros.append(fecha_fin)
        cursor = _ejecutar(
            conexion,
            f"""
            SELECT fecha, estado, observacion, registrado_por,
                   COALESCE(actualizado_en, creado_en) AS fecha_hora_registro
            FROM personal_asistencias
            WHERE {" AND ".join(condiciones)}
            ORDER BY fecha DESC
            """,
            parametros,
        )
        columnas = [columna.name for columna in cursor.description]
        return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


def registrar_adelanto_personal(
    nombre,
    importe,
    fecha_entrega,
    observacion="",
    registrado_por="",
    clave_idempotencia=None,
):
    try:
        monto = Decimal(str(importe)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Ingresa un importe de adelanto válido.") from error
    if not monto.is_finite() or monto <= 0:
        raise ValueError("El importe del adelanto debe ser mayor que cero.")
    if not isinstance(fecha_entrega, date):
        raise ValueError("Selecciona una fecha de entrega válida.")
    clave = (
        str(clave_idempotencia).strip()
        if clave_idempotencia is not None
        else None
    )
    if clave_idempotencia is not None and not clave:
        raise ValueError("La clave de registro del adelanto no es válida.")
    with conectar() as conexion:
        trabajador_id = _obtener_id_trabajador(
            conexion, nombre, crear=True
        )
        if clave is None:
            cursor = _ejecutar(
                conexion,
                """
                INSERT INTO personal_adelantos (
                    trabajador_id, importe, fecha_entrega, observacion,
                    registrado_por
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    trabajador_id,
                    monto,
                    fecha_entrega,
                    str(observacion or "").strip(),
                    str(registrado_por or ""),
                ),
            )
            fila = cursor.fetchone()
        else:
            cursor = _ejecutar(
                conexion,
                """
                INSERT INTO personal_adelantos (
                    trabajador_id, importe, fecha_entrega, observacion,
                    registrado_por, clave_idempotencia
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (clave_idempotencia)
                    WHERE clave_idempotencia IS NOT NULL
                DO NOTHING
                RETURNING id
                """,
                (
                    trabajador_id,
                    monto,
                    fecha_entrega,
                    str(observacion or "").strip(),
                    str(registrado_por or ""),
                    clave,
                ),
            )
            fila = cursor.fetchone()
            if fila is None:
                cursor = _ejecutar(
                    conexion,
                    """
                    SELECT id, trabajador_id, importe, fecha_entrega,
                           observacion
                    FROM personal_adelantos
                    WHERE clave_idempotencia = %s
                    """,
                    (clave,),
                )
                existente = cursor.fetchone()
                if not existente:
                    raise RuntimeError(
                        "No se pudo recuperar el adelanto ya registrado."
                    )
                if (
                    existente[1] != trabajador_id
                    or Decimal(existente[2]) != monto
                    or existente[3] != fecha_entrega
                    or existente[4] != str(observacion or "").strip()
                ):
                    raise ValueError(
                        "La clave de registro ya se utilizó para otro adelanto."
                    )
                return existente[0]

        adelanto_id = fila[0]
        _registrar_historial(
            conexion,
            trabajador_id,
            "adelanto",
            adelanto_id,
            "creado",
            {
                "importe": monto,
                "fecha_entrega": fecha_entrega,
                "observacion": str(observacion or "").strip(),
            },
            registrado_por,
        )
    return adelanto_id


def consultar_adelantos_personal(
    nombre=None,
    fecha_inicio=None,
    fecha_fin=None,
    incluir_anulados=True,
):
    with conectar() as conexion:
        trabajador_id = (
            _obtener_id_trabajador(conexion, nombre) if nombre else None
        )
        condiciones = []
        parametros = []
        if trabajador_id is not None:
            condiciones.append("a.trabajador_id = %s")
            parametros.append(trabajador_id)
        if fecha_inicio is not None:
            condiciones.append("a.fecha_entrega >= %s")
            parametros.append(fecha_inicio)
        if fecha_fin is not None:
            condiciones.append("a.fecha_entrega <= %s")
            parametros.append(fecha_fin)
        if not incluir_anulados:
            condiciones.append("a.estado <> 'Anulado'")
        where_sql = f"WHERE {' AND '.join(condiciones)}" if condiciones else ""
        cursor = _ejecutar(
            conexion,
            f"""
            SELECT a.id, p.nombre, a.importe, a.fecha_entrega,
                   a.observacion, a.registrado_por, a.estado,
                   a.creado_en AS fecha_registro,
                   COALESCE(SUM(ap.importe), 0) AS aplicado,
                   COALESCE(
                       json_agg(json_build_object(
                           'inicio', l.fecha_inicio,
                           'fin', l.fecha_fin,
                           'importe', ap.importe
                       )) FILTER (WHERE ap.id IS NOT NULL),
                       '[]'::json
                   ) AS aplicaciones
            FROM personal_adelantos a
            JOIN personal_trabajadores p ON p.id = a.trabajador_id
            LEFT JOIN personal_adelantos_aplicados ap ON ap.adelanto_id = a.id
            LEFT JOIN personal_liquidaciones l ON l.id = ap.liquidacion_id
            {where_sql}
            GROUP BY a.id, p.nombre
            ORDER BY a.fecha_entrega DESC, a.id DESC
            """,
            parametros,
        )
        columnas = [columna.name for columna in cursor.description]
        resultados = []
        for fila in cursor.fetchall():
            resultado = dict(zip(columnas, fila))
            resultado["saldo_pendiente"] = max(
                Decimal(resultado["importe"])
                - Decimal(resultado["aplicado"]),
                Decimal("0.00"),
            )
            resultados.append(resultado)
        return resultados


def anular_adelanto_personal(adelanto_id, registrado_por=""):
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            """
            SELECT a.trabajador_id, a.estado
            FROM personal_adelantos a
            WHERE a.id = %s
            FOR UPDATE OF a
            """,
            (int(adelanto_id),),
        )
        fila = cursor.fetchone()
        if not fila:
            raise ValueError("El adelanto ya no está disponible.")
        trabajador_id, estado = fila
        if estado == "Anulado":
            return False
        cursor = _ejecutar(
            conexion,
            """
            SELECT COALESCE(SUM(importe), 0)
            FROM personal_adelantos_aplicados
            WHERE adelanto_id = %s
            """,
            (int(adelanto_id),),
        )
        aplicado = cursor.fetchone()[0]
        if Decimal(aplicado) > 0:
            raise ValueError(
                "No se puede anular un adelanto que ya se descontó "
                "en una liquidación."
            )
        _ejecutar(
            conexion,
            """
            UPDATE personal_adelantos
            SET estado = 'Anulado', actualizado_en = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (int(adelanto_id),),
        )
        _registrar_historial(
            conexion,
            trabajador_id,
            "adelanto",
            int(adelanto_id),
            "anulado",
            {},
            registrado_por,
        )
    return True


def crear_liquidacion_personal(
    nombre,
    modalidad_pago,
    fecha_inicio,
    fecha_fin,
    bonificacion=0,
    registrado_por="",
    descuento=0,
):
    if modalidad_pago not in {"Diario", "Quincenal", "Mensual", "Semanal"}:
        raise ValueError("La modalidad de pago no es válida.")
    if not isinstance(fecha_inicio, date) or not isinstance(fecha_fin, date):
        raise ValueError("Selecciona un periodo de liquidación válido.")
    if fecha_fin < fecha_inicio:
        raise ValueError("La fecha final no puede ser anterior a la inicial.")
    if modalidad_pago == "Diario" and fecha_inicio != fecha_fin:
        raise ValueError("Una liquidación diaria debe corresponder a un solo día.")
    if modalidad_pago == "Semanal" and (
        fecha_inicio.weekday() != 0
        or (fecha_fin - fecha_inicio).days != 6
    ):
        raise ValueError(
            "Una liquidación semanal debe abarcar de lunes a domingo."
        )
    if modalidad_pago != "Mensual" and Decimal(str(bonificacion)) != 0:
        raise ValueError(
            "La bonificación acumulada solo se aplica a liquidaciones mensuales."
        )
    try:
        monto_bonificacion = Decimal(str(bonificacion)).quantize(
            Decimal("0.01")
        )
    except (InvalidOperation, ValueError) as error:
        raise ValueError("La bonificación no es válida.") from error
    if not monto_bonificacion.is_finite() or monto_bonificacion < 0:
        raise ValueError("La bonificación no puede ser negativa.")
    try:
        monto_descuento = Decimal(str(descuento)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("El descuento no es válido.") from error
    if not monto_descuento.is_finite() or monto_descuento < 0:
        raise ValueError("El descuento no puede ser negativo.")

    with conectar() as conexion:
        trabajador_id = _obtener_id_trabajador(conexion, nombre)
        cursor = _ejecutar(
            conexion,
            """
            SELECT sueldo_base, modalidad_pago
            FROM personal_trabajadores
            WHERE id = %s AND activo
            FOR UPDATE
            """,
            (trabajador_id,),
        )
        perfil = cursor.fetchone()
        if not perfil or perfil[0] is None or perfil[1] is None:
            raise ValueError(
                "Configura el sueldo y la modalidad de pago del trabajador."
            )
        sueldo_base, modalidad_configurada = perfil
        if modalidad_configurada != modalidad_pago:
            raise ValueError(
                "La modalidad no coincide con la configurada para el trabajador."
            )
        cursor = _ejecutar(
            conexion,
            """
            INSERT INTO personal_liquidaciones (
                trabajador_id, modalidad_pago, fecha_inicio, fecha_fin,
                sueldo_base, bonificacion, descuento, creado_por
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (trabajador_id, fecha_inicio, fecha_fin) DO NOTHING
            RETURNING id
            """,
            (
                trabajador_id,
                modalidad_pago,
                fecha_inicio,
                fecha_fin,
                sueldo_base,
                monto_bonificacion,
                monto_descuento,
                str(registrado_por or ""),
            ),
        )
        fila = cursor.fetchone()
        if not fila:
            cursor = _ejecutar(
                conexion,
                """
                SELECT id FROM personal_liquidaciones
                WHERE trabajador_id = %s
                    AND fecha_inicio = %s AND fecha_fin = %s
                """,
                (trabajador_id, fecha_inicio, fecha_fin),
            )
            existente = cursor.fetchone()
            if not existente:
                raise RuntimeError(
                    "No se pudo recuperar la liquidación existente."
                )
            return existente[0], False

        liquidacion_id = fila[0]
        _registrar_historial(
            conexion,
            trabajador_id,
            "liquidacion",
            liquidacion_id,
            "creada",
            {
                "modalidad": modalidad_pago,
                "inicio": fecha_inicio,
                "fin": fecha_fin,
                "sueldo_base": sueldo_base,
                "bonificacion": monto_bonificacion,
                "descuento": monto_descuento,
            },
            registrado_por,
        )
    return liquidacion_id, True


def consultar_liquidaciones_personal(nombre):
    with conectar() as conexion:
        trabajador_id = _obtener_id_trabajador(conexion, nombre)
        cursor = _ejecutar(
            conexion,
            """
            SELECT l.id, l.modalidad_pago, l.fecha_inicio, l.fecha_fin,
                   l.sueldo_base, l.bonificacion, l.descuento,
                   l.adelantos_aplicados,
                   COALESCE(SUM(p.importe), 0) AS pagado,
                   l.creado_por, l.creado_en
            FROM personal_liquidaciones l
            LEFT JOIN personal_pagos p
                ON p.liquidacion_id = l.id AND p.anulado_en IS NULL
            WHERE l.trabajador_id = %s
            GROUP BY l.id
            ORDER BY l.fecha_inicio DESC, l.id DESC
            """,
            (trabajador_id,),
        )
        columnas = [columna.name for columna in cursor.description]
        return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


def consultar_liquidacion_personal_periodo(
    nombre, fecha_inicio, fecha_fin
):
    with conectar() as conexion:
        trabajador_id = _obtener_id_trabajador(conexion, nombre)
        cursor = _ejecutar(
            conexion,
            """
            SELECT l.id, l.modalidad_pago, l.fecha_inicio, l.fecha_fin,
                   l.sueldo_base, l.bonificacion, l.descuento,
                   l.adelantos_aplicados,
                   COALESCE(SUM(p.importe), 0) AS pagado,
                   l.creado_por, l.creado_en
            FROM personal_liquidaciones l
            LEFT JOIN personal_pagos p
                ON p.liquidacion_id = l.id AND p.anulado_en IS NULL
            WHERE l.trabajador_id = %s
                AND l.fecha_inicio = %s
                AND l.fecha_fin = %s
            GROUP BY l.id
            LIMIT 1
            """,
            (trabajador_id, fecha_inicio, fecha_fin),
        )
        fila = cursor.fetchone()
        if fila is None:
            return None
        columnas = [columna.name for columna in cursor.description]
        return dict(zip(columnas, fila))


def registrar_pago_personal(
    liquidacion_id,
    importe,
    fecha_pago,
    metodo_pago="",
    observacion="",
    registrado_por="",
    adelantos_registrados=0,
):
    try:
        monto = Decimal(str(importe)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Ingresa un importe de pago válido.") from error
    if not monto.is_finite() or monto <= 0:
        raise ValueError("El importe pagado debe ser mayor que cero.")
    if not isinstance(fecha_pago, date):
        raise ValueError("Selecciona una fecha de pago válida.")
    try:
        monto_adelantos = Decimal(str(adelantos_registrados)).quantize(
            Decimal("0.01")
        )
    except (InvalidOperation, ValueError) as error:
        raise ValueError("El importe de adelantos no es válido.") from error
    if not monto_adelantos.is_finite() or monto_adelantos < 0:
        raise ValueError("El importe de adelantos no puede ser negativo.")
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            """
            SELECT trabajador_id,
                   sueldo_base + bonificacion - descuento AS bruto
            FROM personal_liquidaciones
            WHERE id = %s
            FOR UPDATE
            """,
            (int(liquidacion_id),),
        )
        liquidacion = cursor.fetchone()
        if not liquidacion:
            raise ValueError("La liquidación ya no está disponible.")
        trabajador_id, bruto = liquidacion
        cursor = _ejecutar(
            conexion,
            """
            SELECT COALESCE(SUM(importe), 0)
            FROM personal_pagos
            WHERE liquidacion_id = %s AND anulado_en IS NULL
            """,
            (int(liquidacion_id),),
        )
        pagado = Decimal(cursor.fetchone()[0])
        saldo = max(
            Decimal(bruto) - monto_adelantos - pagado,
            Decimal("0.00"),
        )
        if monto > saldo:
            raise ValueError(
                f"El importe excede el saldo pendiente de S/ {saldo:.2f}."
            )
        cursor = _ejecutar(
            conexion,
            """
            INSERT INTO personal_pagos (
                liquidacion_id, importe, fecha_pago, metodo_pago,
                observacion, registrado_por
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                int(liquidacion_id),
                monto,
                fecha_pago,
                str(metodo_pago or "").strip(),
                str(observacion or "").strip(),
                str(registrado_por or ""),
            ),
        )
        pago_id = cursor.fetchone()[0]
        _registrar_historial(
            conexion,
            trabajador_id,
            "pago",
            pago_id,
            "registrado",
            {
                "liquidacion_id": int(liquidacion_id),
                "importe": monto,
                "fecha_pago": fecha_pago,
                "metodo_pago": str(metodo_pago or "").strip(),
                "observacion": str(observacion or "").strip(),
            },
            registrado_por,
        )
    return pago_id


def consultar_pagos_personal(nombre):
    with conectar() as conexion:
        trabajador_id = _obtener_id_trabajador(conexion, nombre)
        cursor = _ejecutar(
            conexion,
            """
            SELECT p.id, l.modalidad_pago, l.fecha_inicio, l.fecha_fin,
                   p.importe, p.fecha_pago, p.metodo_pago,
                   p.observacion, p.registrado_por,
                   p.creado_en AS fecha_registro
            FROM personal_pagos p
            JOIN personal_liquidaciones l ON l.id = p.liquidacion_id
            WHERE l.trabajador_id = %s AND p.anulado_en IS NULL
            ORDER BY p.fecha_pago DESC, p.id DESC
            """,
            (trabajador_id,),
        )
        columnas = [columna.name for columna in cursor.description]
        return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


def revertir_pagos_liquidacion_personal(liquidacion_id, registrado_por):
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            """
            SELECT trabajador_id, fecha_inicio, fecha_fin
            FROM personal_liquidaciones
            WHERE id = %s
            FOR UPDATE
            """,
            (int(liquidacion_id),),
        )
        liquidacion = cursor.fetchone()
        if not liquidacion:
            raise ValueError("La liquidación ya no está disponible.")
        trabajador_id, fecha_inicio, fecha_fin = liquidacion
        cursor = _ejecutar(
            conexion,
            """
            UPDATE personal_pagos
            SET anulado_en = CURRENT_TIMESTAMP, anulado_por = %s
            WHERE liquidacion_id = %s AND anulado_en IS NULL
            RETURNING id, importe
            """,
            (str(registrado_por or ""), int(liquidacion_id)),
        )
        pagos_revertidos = cursor.fetchall()
        if not pagos_revertidos:
            raise ValueError("La liquidación no tiene pagos activos para revertir.")
        importe_revertido = sum(
            (Decimal(importe) for _, importe in pagos_revertidos),
            Decimal("0.00"),
        )
        _registrar_historial(
            conexion,
            trabajador_id,
            "pago",
            int(liquidacion_id),
            "pagos_revertidos",
            {
                "liquidacion_id": int(liquidacion_id),
                "fecha_inicio": fecha_inicio,
                "fecha_fin": fecha_fin,
                "cantidad_pagos": len(pagos_revertidos),
                "importe_total": importe_revertido,
                "pago_ids": [pago_id for pago_id, _ in pagos_revertidos],
            },
            registrado_por,
        )
    return importe_revertido


def consultar_historial_personal(nombre):
    with conectar() as conexion:
        trabajador_id = _obtener_id_trabajador(conexion, nombre)
        cursor = _ejecutar(
            conexion,
            """
            SELECT h.tipo_movimiento, h.accion, h.detalle,
                   h.registrado_por, h.creado_en,
                   (
                       h.tipo_movimiento = 'pago'
                       AND h.accion = 'registrado'
                       AND EXISTS (
                           SELECT 1
                           FROM personal_pagos p
                           WHERE p.id = h.referencia_id
                             AND p.anulado_en IS NOT NULL
                       )
                   ) AS pago_anulado
            FROM personal_historial h
            WHERE h.trabajador_id = %s
            ORDER BY h.creado_en DESC, h.id DESC
            """,
            (trabajador_id,),
        )
        columnas = [columna.name for columna in cursor.description]
        return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


def consultar_resumen_personal_periodo(fecha_inicio, fecha_fin):
    with conectar() as conexion:
        cursor = _ejecutar(
            conexion,
            """
            WITH asistencia AS (
                SELECT trabajador_id,
                    COUNT(*) FILTER (WHERE estado = 'A') AS asistio,
                    COUNT(*) FILTER (WHERE estado = 'F') AS falto,
                    COUNT(*) FILTER (WHERE estado = 'M') AS medio_dia,
                    COUNT(DISTINCT date_trunc('week', fecha::timestamp))
                        FILTER (WHERE estado = 'F') AS semanas_con_falta
                FROM personal_asistencias
                WHERE fecha BETWEEN %s AND %s
                GROUP BY trabajador_id
            ), pagos AS (
                SELECT liquidacion_id, SUM(importe) AS importe
                FROM personal_pagos
                WHERE anulado_en IS NULL
                GROUP BY liquidacion_id
            )
            SELECT t.id, t.nombre, t.cargo, t.sueldo_base,
                   t.modalidad_pago, t.fecha_ingreso, t.activo,
                   COALESCE(a.asistio, 0) AS asistio,
                   COALESCE(a.falto, 0) AS falto,
                   COALESCE(a.medio_dia, 0) AS medio_dia,
                   COALESCE(a.semanas_con_falta, 0) AS semanas_con_falta,
                   0::numeric AS adelantos_pendientes,
                   l.id AS liquidacion_id,
                   l.sueldo_base AS sueldo_liquidado,
                   l.bonificacion AS bonificacion_liquidada,
                   l.descuento AS descuento_liquidado,
                   l.adelantos_aplicados,
                   COALESCE(pg.importe, 0) AS pagado
            FROM personal_trabajadores t
            LEFT JOIN asistencia a ON a.trabajador_id = t.id
            LEFT JOIN personal_liquidaciones l
                ON l.trabajador_id = t.id
                AND l.fecha_inicio = %s AND l.fecha_fin = %s
            LEFT JOIN pagos pg ON pg.liquidacion_id = l.id
            WHERE t.activo
            ORDER BY lower(t.nombre)
            """,
            (
                fecha_inicio,
                fecha_fin,
                fecha_inicio,
                fecha_fin,
            ),
        )
        columnas = [columna.name for columna in cursor.description]
        filas = [dict(zip(columnas, fila)) for fila in cursor.fetchall()]
    return filas
