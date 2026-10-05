import argparse
import csv
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from database import COLUMNAS_REGISTRO


ADMIN_USERNAME = "Titovallejo"
MIGRATION_TABLE = "schema_migrations"


def _quote_identifier(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _load_user_data(path):
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict) or not isinstance(data.get("usuarios"), dict):
        raise ValueError("usuarios.json no tiene el formato esperado.")

    users = data["usuarios"]
    admin_matches = [
        (name, account)
        for name, account in users.items()
        if isinstance(name, str) and name.casefold() == ADMIN_USERNAME.casefold()
    ]
    if len(admin_matches) != 1:
        raise ValueError(
            f"Se esperaba una sola cuenta {ADMIN_USERNAME!r} en usuarios.json."
        )
    admin_name, admin_account = admin_matches[0]
    if (
        not isinstance(admin_account, dict)
        or admin_account.get("rol") != "Administrador"
    ):
        raise ValueError(
            f"La cuenta {ADMIN_USERNAME!r} no tiene el rol Administrador."
        )

    deleted = data.get("eliminados_definitivamente", [])
    if not isinstance(deleted, list) or any(
        not isinstance(name, str) or not name.strip() for name in deleted
    ):
        raise ValueError("La lista de usuarios eliminados no es válida.")

    removed_names = {
        name for name in deleted if name.casefold() != admin_name.casefold()
    }
    removed_names.update(
        name
        for name in users
        if isinstance(name, str) and name.casefold() != admin_name.casefold()
    )
    data["usuarios"] = {admin_name: admin_account}
    data["eliminados_definitivamente"] = sorted(
        removed_names, key=str.casefold
    )
    return data


def _get_table_row_counts(connection):
    tables = [
        row[0]
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]
    if "registros" not in tables or MIGRATION_TABLE not in tables:
        raise ValueError("bunker.db no contiene el esquema esperado.")

    counts = {}
    for table in tables:
        if table == MIGRATION_TABLE:
            continue
        quoted_table = _quote_identifier(table)
        counts[table] = connection.execute(
            f"SELECT COUNT(*) FROM {quoted_table}"
        ).fetchone()[0]
    return counts


def _write_staged_files(directory, user_data):
    user_temp = None
    csv_temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=directory,
            prefix=".usuarios-reset-",
            suffix=".tmp",
            delete=False,
        ) as file:
            user_temp = Path(file.name)
            json.dump(user_data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            dir=directory,
            prefix=".registros-reset-",
            suffix=".tmp",
            delete=False,
        ) as file:
            csv_temp = Path(file.name)
            writer = csv.writer(file, lineterminator="\n")
            writer.writerow(
                name for name in COLUMNAS_REGISTRO if name != "ID"
            )
            file.flush()
            os.fsync(file.fileno())
        return user_temp, csv_temp
    except Exception:
        for path in (user_temp, csv_temp):
            if path is not None and path.exists():
                path.unlink()
        raise


def _create_backup(database_path, users_path, csv_path, backup_root):
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_dir = backup_root / datetime.now().strftime(
        "reset-%Y%m%d-%H%M%S-%f"
    )
    backup_dir.mkdir()

    backup_database = backup_dir / database_path.name
    with closing(sqlite3.connect(database_path)) as source:
        with closing(sqlite3.connect(backup_database)) as destination:
            source.backup(destination)
    shutil.copy2(users_path, backup_dir / users_path.name)
    shutil.copy2(csv_path, backup_dir / csv_path.name)
    return backup_dir


def reset_application_data(directory, backup_root, apply=False):
    directory = Path(directory).resolve()
    database_path = directory / "bunker.db"
    users_path = directory / "usuarios.json"
    csv_path = directory / "registros_lavado.csv"

    for path in (database_path, users_path, csv_path):
        if not path.is_file():
            raise FileNotFoundError(f"No se encontró el archivo requerido: {path}")

    user_data = _load_user_data(users_path)
    with closing(sqlite3.connect(database_path, timeout=30)) as connection:
        connection.execute("PRAGMA busy_timeout = 30000")
        row_counts = _get_table_row_counts(connection)

    print(f"Cuenta que se conservará sin cambios: {ADMIN_USERNAME}")
    print(
        "Aviso: este script reinicia únicamente la base SQLite local; "
        "no modifica Supabase."
    )
    for table, count in row_counts.items():
        print(f"Filas que se vaciarán de {table}: {count}")
    print("El CSV de migración se conservará únicamente con sus cabeceras.")
    print("No se encontraron exportaciones Excel persistidas por la aplicación.")
    print("Detenga la aplicación antes de aplicar el reinicio.")

    if not apply:
        print("Simulación: no se modificó ningún archivo.")
        print("Para aplicar: ejecute de nuevo con --confirm-reset.")
        return None

    backup_dir = _create_backup(
        database_path, users_path, csv_path, Path(backup_root).resolve()
    )
    staged_users, staged_csv = _write_staged_files(directory, user_data)
    original_users = backup_dir / users_path.name
    original_csv = backup_dir / csv_path.name

    connection = None
    try:
        connection = sqlite3.connect(database_path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("BEGIN IMMEDIATE")
        tables = _get_table_row_counts(connection)
        for table in tables:
            connection.execute(f"DELETE FROM {_quote_identifier(table)}")
            connection.execute(
                "DELETE FROM sqlite_sequence WHERE name = ?",
                (table,),
            )

        os.replace(staged_users, users_path)
        os.replace(staged_csv, csv_path)
        connection.commit()
    except Exception as error:
        if connection is not None:
            connection.rollback()
        restoration_errors = []
        for backup, target in (
            (original_users, users_path),
            (original_csv, csv_path),
        ):
            try:
                shutil.copy2(backup, target)
            except OSError as restore_error:
                restoration_errors.append(f"{target}: {restore_error}")
        if restoration_errors:
            raise RuntimeError(
                "Falló el reinicio y también la restauración de archivos: "
                + "; ".join(restoration_errors)
                + f". La copia de respaldo está en {backup_dir}"
            ) from error
        raise
    try:
        checkpoint = connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        if checkpoint and checkpoint[0]:
            raise RuntimeError(
                "El reinicio se aplicó, pero SQLite no pudo truncar el WAL. "
                "Asegúrese de que la aplicación esté detenida."
            )
    except sqlite3.Error as error:
        raise RuntimeError(
            "El reinicio se aplicó, pero no se pudo limpiar el WAL. "
            f"La copia de respaldo está en {backup_dir}."
        ) from error
    finally:
        if connection is not None:
            connection.close()
        for path in (staged_users, staged_csv):
            if path.exists():
                path.unlink()

    print(f"Reinicio aplicado correctamente. Respaldo creado en: {backup_dir}")
    return backup_dir


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Vacía los datos de la antigua base SQLite local, no de Supabase, "
            f"y conserva la cuenta administradora {ADMIN_USERNAME}."
        )
    )
    parser.add_argument(
        "--confirm-reset",
        action="store_true",
        help="aplica el reinicio; sin esta opción solo muestra una simulación",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path.home() / "ElBunkerResetBackups",
        help="directorio seguro para copias previas al reinicio",
    )
    arguments = parser.parse_args()
    application_directory = Path(__file__).resolve().parent
    reset_application_data(
        application_directory,
        arguments.backup_dir,
        apply=arguments.confirm_reset,
    )


if __name__ == "__main__":
    main()
