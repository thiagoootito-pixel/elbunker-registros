import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from database import COLUMNAS_REGISTRO, conectar, inicializar_base_datos


VERSION_MIGRACION_DATOS = 2
TAMANO_LOTE = 1000


def _abrir_sqlite_solo_lectura(ruta):
    uri = f"{ruta.resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def contar_registros_sqlite(ruta):
    with closing(_abrir_sqlite_solo_lectura(ruta)) as conexion:
        tabla = conexion.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("registros",),
        ).fetchone()
        if tabla is None:
            raise ValueError(f"No existe la tabla registros en {ruta}.")
        return conexion.execute("SELECT COUNT(*) FROM registros").fetchone()[0]


def migrar_registros(ruta_sqlite):
    columnas = list(COLUMNAS_REGISTRO.values())
    columnas_sql = ", ".join(columnas)
    marcadores = ", ".join("%s" for _ in columnas)
    insert_sql = (
        f"INSERT INTO registros ({columnas_sql}) VALUES ({marcadores})"
    )

    with conectar() as destino:
        cursor_destino = destino.cursor()
        cursor_destino.execute("LOCK TABLE registros IN SHARE ROW EXCLUSIVE MODE")
        cursor_destino.execute("SELECT COUNT(*) FROM registros")
        if cursor_destino.fetchone()[0]:
            raise ValueError(
                "Supabase ya contiene registros; se cancela para evitar "
                "duplicados. Migra solo a una base de datos vacía."
            )
        cursor_destino.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s",
            (VERSION_MIGRACION_DATOS,),
        )
        if cursor_destino.fetchone():
            raise ValueError("La migración desde SQLite ya fue aplicada.")

        with closing(_abrir_sqlite_solo_lectura(ruta_sqlite)) as origen:
            columnas_origen = {
                fila[1]
                for fila in origen.execute("PRAGMA table_info(registros)")
            }
            faltantes = set(columnas) - columnas_origen
            if faltantes:
                raise ValueError(
                    "La tabla SQLite no contiene las columnas requeridas: "
                    + ", ".join(sorted(faltantes))
                )

            cursor_origen = origen.execute(
                f"SELECT {columnas_sql} FROM registros ORDER BY id"
            )
            total = 0
            while lote := cursor_origen.fetchmany(TAMANO_LOTE):
                cursor_destino.executemany(insert_sql, lote)
                total += len(lote)

        cursor_destino.execute(
            """
            SELECT setval(
                pg_get_serial_sequence('registros', 'id'),
                COALESCE(MAX(id), 1),
                MAX(id) IS NOT NULL
            )
            FROM registros
            """
        )
        cursor_destino.execute(
            """
            INSERT INTO schema_migrations (version)
            VALUES (%s)
            ON CONFLICT (version) DO NOTHING
            """,
            (VERSION_MIGRACION_DATOS,),
        )
    return total


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Copia los registros de bunker.db a una tabla registros vacía "
            "en Supabase. Por defecto solo muestra una simulación."
        )
    )
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=Path(__file__).resolve().parent / "bunker.db",
        help="ruta de la base SQLite de origen",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="realiza la copia; sin esta opción no modifica Supabase",
    )
    argumentos = parser.parse_args()
    ruta_sqlite = argumentos.sqlite.resolve()
    if not ruta_sqlite.is_file():
        raise FileNotFoundError(f"No se encontró la base SQLite: {ruta_sqlite}")

    total = contar_registros_sqlite(ruta_sqlite)
    print(f"Registros encontrados en SQLite: {total}")
    if not argumentos.apply:
        print("Simulación: Supabase no fue modificado.")
        print("Para aplicar: python migrar_sqlite_a_supabase.py --apply")
        return

    inicializar_base_datos()
    migrados = migrar_registros(ruta_sqlite)
    print(f"Migración completada: {migrados} registros copiados a Supabase.")


if __name__ == "__main__":
    main()
