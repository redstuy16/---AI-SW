import sqlite3

import pytest

from probe import database


def test_old_database_upgrades_without_losing_rows(tmp_path):
    path = tmp_path / "old.sqlite"
    raw = sqlite3.connect(path)
    raw.executescript(database.SCHEMA_PATH.read_text(encoding="utf-8"))
    raw.execute("INSERT INTO research_runs VALUES ('R-old','legacy',0,'2026-01-01T00:00:00+00:00')")
    raw.commit()
    raw.close()
    db = database.initialize(path)
    assert [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")] == ["001", "002", "003", "004", "005", "006"]
    assert db.execute("SELECT goal FROM research_runs WHERE research_id='R-old'").fetchone()[0] == "legacy"
    assert db.execute("SELECT name FROM sqlite_master WHERE name='datasets'").fetchone() is not None
    database.migrate(db)
    assert db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 6
    db.close()


def test_migration_failure_rolls_back(tmp_path, monkeypatch):
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    text = "CREATE TABLE must_rollback(id INTEGER);\nINVALID SQL;\n"
    text.encode("utf-8", errors="strict")
    (migration_dir / "001_bad.sql").write_text(text, encoding="utf-8")
    monkeypatch.setattr(database, "MIGRATIONS_PATH", migration_dir)
    db = database.connect(tmp_path / "bad.sqlite")
    with pytest.raises(database.MigrationError):
        database.migrate(db)
    assert db.execute("SELECT name FROM sqlite_master WHERE name='must_rollback'").fetchone() is None
    db.close()
