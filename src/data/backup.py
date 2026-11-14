from __future__ import annotations

import os
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
def _sqlite_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


def _check_integrity(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if result is None or str(result[0]).lower() != "ok":
        raise sqlite3.DatabaseError("SQLite integrity_check failed")
    foreign_key_error = connection.execute("PRAGMA foreign_key_check").fetchone()
    if foreign_key_error is not None:
        raise sqlite3.DatabaseError("SQLite foreign_key_check failed")


def _copy_database(
    source: str | Path, destination: str | Path, *, replace_destination: bool
) -> Path:
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if source_path == destination_path:
        raise ValueError("source and destination must differ")
    if destination_path.exists() and not replace_destination:
        raise FileExistsError(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.with_name(destination_path.name + ".tmp")
    if temporary_path.exists():
        raise FileExistsError(temporary_path)
    try:
        with closing(_sqlite_readonly(source_path)) as source_connection:
            with closing(sqlite3.connect(temporary_path, timeout=5.0)) as destination_connection:
                source_connection.backup(destination_connection, pages=256, sleep=0.05)
                _check_integrity(destination_connection)
        os.replace(temporary_path, destination_path)
        return destination_path
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def backup_database(source: str | Path, destination: str | Path) -> Path:
    """Create a consistent SQLite snapshot, including committed WAL transactions."""
    return _copy_database(source, destination, replace_destination=False)


def restore_database(backup: str | Path, destination: str | Path) -> Path:
    """Restore a verified snapshot atomically; the service must be stopped by caller."""
    return _copy_database(backup, destination, replace_destination=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Consistent SQLite backup and restore")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="create and verify a consistent snapshot")
    backup.add_argument("source")
    backup.add_argument("destination")
    restore = commands.add_parser("restore", help="restore a verified snapshot atomically")
    restore.add_argument("backup")
    restore.add_argument("destination")
    args = parser.parse_args()
    if args.command == "backup":
        path = backup_database(args.source, args.destination)
    else:
        path = restore_database(args.backup, args.destination)
    with closing(_sqlite_readonly(path)) as connection:
        _check_integrity(connection)
        version_row = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
    version = version_row[0] if version_row else None
    print(f"{args.command} verified: {path}; schema_version={version}; bytes={path.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
