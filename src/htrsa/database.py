"""SQLite 초기화와 결정적 JSON 직렬화."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel


_REPOSITORY_DB = Path(__file__).resolve().parents[2] / "db"
MIGRATIONS_PATH = _REPOSITORY_DB / "migrations"
if not MIGRATIONS_PATH.is_dir():
    MIGRATIONS_PATH = Path(sys.prefix) / "share" / "htrsa" / "migrations"
SCHEMA_PATH = _REPOSITORY_DB / "schema.sql"
if not SCHEMA_PATH.is_file():
    SCHEMA_PATH = MIGRATIONS_PATH / "001_initial.sql"


class MigrationError(Exception):
    """SQLite 마이그레이션이 실패하여 원자적으로 되돌려졌다."""


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
                      default=lambda item: item.model_dump(mode="json") if isinstance(item, BaseModel) else _unsupported(item))


def _unsupported(value: Any) -> None:
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def from_json(value: str) -> Any:
    return json.loads(value)


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def initialize(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    try:
        migrate(connection)
    except Exception:
        connection.close()
        raise
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    files = sorted(MIGRATIONS_PATH.glob("[0-9][0-9][0-9]_*.sql"))
    if not files:
        raise MigrationError(f"no migration files found in {MIGRATIONS_PATH}")
    versions = [file.name.split("_", 1)[0] for file in files]
    if len(versions) != len(set(versions)):
        raise MigrationError("duplicate migration version")
    from .schemas import utc_now
    for file in files:
        version = file.name.split("_", 1)[0]
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
            continue
        script = file.read_text(encoding="utf-8", errors="strict")
        try:
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + script + "\n"
                + "INSERT INTO schema_migrations(version,applied_at) VALUES ("
                + "'" + version + "','" + utc_now().isoformat() + "');\nCOMMIT;"
            )
        except sqlite3.Error as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise MigrationError(f"migration {file.name} failed") from exc
