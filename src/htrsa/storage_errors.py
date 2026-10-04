"""저장 실패를 경로나 비밀 값 없이 구분한다."""
from __future__ import annotations

import errno
import sqlite3


def storage_error_code(error: BaseException) -> str:
    if isinstance(error, sqlite3.Error):
        code = getattr(error, "sqlite_errorcode", None)
        primary = code & 0xFF if isinstance(code, int) else None
        message = str(error).casefold()
        if primary == sqlite3.SQLITE_FULL or message == "database or disk is full":
            return "STORAGE_FULL"
        if primary in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or message in {
            "database is locked", "database table is locked", "database schema is locked"
        }:
            return "DATABASE_BUSY"
        if primary in {sqlite3.SQLITE_READONLY, sqlite3.SQLITE_PERM}:
            return "STORAGE_ACCESS_DENIED"
        if primary in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
            return "DATABASE_INVALID"
        return "DATABASE_UNAVAILABLE"
    if isinstance(error, OSError):
        if error.errno == errno.ENOSPC or getattr(error, "winerror", None) in {39, 112}:
            return "STORAGE_FULL"
        if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS} or getattr(error, "winerror", None) == 5:
            return "STORAGE_ACCESS_DENIED"
    return "STORAGE_UNAVAILABLE"
