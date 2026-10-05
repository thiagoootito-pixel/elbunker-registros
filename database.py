import csv
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta


LOCALES_VALIDOS = ("Búnker 1", "Búnker 2", "Búnker 3")

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
def conectar(ruta_db):
    conexion = sqlite3.connect(ruta_db, timeout=30)
    try:
        conexion.row_factory = sqlite3.Row
        conexion.execute("PRAGMA busy_timeout = 30000")
        yield conexion
        conexion.commit()
    except Exception:
        conexion.rollback()
        raise
    finally:
        conexion.close()


def _normalizar_fecha(valor):
    texto = str(valor or "").strip()
    for formato in _FORMATOS_FECHA:
        try:
            fecha = datetime.strptime(texto, formato)
            return fecha.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return texto


def _valor_fila(fila, nombre):
    alias = "RUC Cliente" if nombre == "RUC/DNI" else nombre
    valor = fila.get(nombre, fila.get(alias, ""))
    if valor is None:
        return ""
    return str(valor).strip()


def inicializar_base_datos(ruta_db, ruta_csv):
    os.makedirs(os.path.dirname(os.path.abspath(ruta_db)), exist_ok=True)
    with conectar(ruta_db) as conexion:
        conexion.execute("PRAGMA journal_mode = WAL")
        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS registros (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            """
        )
        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                aplicado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conexion.execute(
            "CREATE INDEX IF NOT EXISTS idx_registros_local_fecha "
            "ON registros(local, fecha)"
        )
        conexion.execute(
            "CREATE INDEX IF NOT EXISTS idx_registros_local_responsable_fecha "
            "ON registros(local, registrado_por, fecha)"
        )
        migracion_aplicada = conexion.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 1"
        ).fetchone()
        if migracion_aplicada:
            return

        cantidad_registros = conexion.execute(
            "SELECT COUNT(*) FROM registros"
        ).fetchone()[0]
        if cantidad_registros == 0 and os.path.exists(ruta_csv):
            with open(ruta_csv, "r", encoding="utf-8-sig", newline="") as archivo:
                lector = csv.DictReader(archivo)
                columnas_requeridas = {
                    "Tipo",
                    "Placa",
                    "Servicio",
                    "Pago",
                    "Monto",
                    "Fecha",
                }
                if not lector.fieldnames or not columnas_requeridas.issubset(
                    lector.fieldnames
                ):
                    raise ValueError(
                        "El archivo CSV no contiene las columnas requeridas "
                        "para la migración a SQLite."
                    )
                registros = []
                for fila in lector:
                    registro = {
                        nombre: _valor_fila(fila, nombre)
                        for nombre in COLUMNAS_REGISTRO
                        if nombre != "ID"
                    }
                    registro["Fecha"] = _normalizar_fecha(registro["Fecha"])
                    registro["Local"] = registro["Local"] or LOCALES_VALIDOS[0]
                    if registro["Local"] not in LOCALES_VALIDOS:
                        raise ValueError(
                            "El archivo CSV contiene un local desconocido: "
                            f"{registro['Local']!r}."
                        )
                    registros.append((fila.get("ID", ""), registro))

            campos = [
                nombre
                for nombre in COLUMNAS_REGISTRO
                if nombre != "ID"
            ]
            columnas = ", ".join(COLUMNAS_REGISTRO[nombre] for nombre in campos)
            parametros = ", ".join("?" for _ in campos)
            sql = f"INSERT INTO registros ({columnas}) VALUES ({parametros})"
            for id_csv, registro in registros:
                try:
                    id_existente = int(id_csv) if id_csv else None
                except (TypeError, ValueError):
                    id_existente = None
                valores = tuple(registro[nombre] for nombre in campos)
                if id_existente is None:
                    conexion.execute(sql, valores)
                else:
                    conexion.execute(
                        f"INSERT INTO registros (id, {columnas}) "
                        f"VALUES (?, {parametros})",
                        (id_existente, *valores),
                    )

        conexion.execute(
            "INSERT INTO schema_migrations (version) VALUES (1)"
        )


def consultar_registros(
    ruta_db, local=None, fecha=None, mes=None, supervisor=None
):
    condiciones = []
    parametros = []
    if local and local != "Todos los locales":
        condiciones.append("local = ?")
        parametros.append(local)
    if fecha is not None:
        siguiente_dia = fecha + timedelta(days=1)
        condiciones.extend(("fecha >= ?", "fecha < ?"))
        parametros.extend(
            (fecha.strftime("%Y-%m-%d"), siguiente_dia.strftime("%Y-%m-%d"))
        )
    if mes is not None:
        inicio = mes.replace(day=1)
        if inicio.month == 12:
            fin = inicio.replace(year=inicio.year + 1, month=1)
        else:
            fin = inicio.replace(month=inicio.month + 1)
        condiciones.extend(("fecha >= ?", "fecha < ?"))
        parametros.extend(
            (inicio.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d"))
        )
    if supervisor is not None:
        condiciones.append("lower(trim(registrado_por)) = lower(?)")
        parametros.append(supervisor)
    where_sql = f"WHERE {' AND '.join(condiciones)}" if condiciones else ""
    with conectar(ruta_db) as conexion:
        filas = conexion.execute(
            f"SELECT {_COLUMNAS_SQL} FROM registros {where_sql} ORDER BY id",
            parametros,
        ).fetchall()
    return [dict(fila) for fila in filas]


def insertar_registro(ruta_db, registro):
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
    parametros = ", ".join("?" for _ in campos)
    with conectar(ruta_db) as conexion:
        cursor = conexion.execute(
            f"INSERT INTO registros ({columnas}) VALUES ({parametros})",
            valores,
        )
        return cursor.lastrowid


def actualizar_registro(ruta_db, registro_id, local, cambios):
    campos = [
        nombre
        for nombre in cambios
        if nombre in COLUMNAS_REGISTRO and nombre not in {"ID", "Local"}
    ]
    if not campos:
        return False
    asignaciones = ", ".join(
        f"{COLUMNAS_REGISTRO[nombre]} = ?" for nombre in campos
    )
    valores = [
        _normalizar_fecha(cambios[nombre])
        if nombre == "Fecha"
        else str(cambios[nombre] or "")
        for nombre in campos
    ]
    with conectar(ruta_db) as conexion:
        cursor = conexion.execute(
            f"UPDATE registros SET {asignaciones} WHERE id = ? AND local = ?",
            (*valores, int(registro_id), local),
        )
        return cursor.rowcount == 1


def eliminar_registro(ruta_db, registro_id, local):
    with conectar(ruta_db) as conexion:
        cursor = conexion.execute(
            "DELETE FROM registros WHERE id = ? AND local = ?",
            (int(registro_id), local),
        )
        return cursor.rowcount == 1


def eliminar_registros_supervisor(ruta_db, nombre):
    nombre_normalizado = (
        nombre.casefold().replace(" ", "").replace("_", "").replace("-", "")
    )
    with conectar(ruta_db) as conexion:
        return eliminar_registros_supervisor_en_conexion(
            conexion, nombre_normalizado
        )


def eliminar_registros_supervisor_en_conexion(conexion, nombre_o_normalizado):
    nombre_normalizado = (
        nombre_o_normalizado.casefold()
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
    )
    cursor = conexion.execute(
        """
        DELETE FROM registros
        WHERE replace(replace(replace(lower(trim(registrado_por)), ' ', ''),
                              '_', ''), '-', '') = ?
        """,
        (nombre_normalizado,),
    )
    return cursor.rowcount
