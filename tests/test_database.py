from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import fufu_archive.db as db_module
from fufu_archive.db import initialize_database


def test_database_initialization_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    initialize_database(db_path)
    initialize_database(db_path)

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        versions = connection.execute("SELECT version FROM schema_migrations").fetchall()

    assert {
        "documents",
        "chunks",
        "metadata",
        "parent_contexts",
        "document_tags",
        "schema_migrations",
    } <= tables
    assert versions == [(1,), (2,), (3,), (4,), (5,)]


def test_database_initialization_is_safe_under_concurrency(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent.db"
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: initialize_database(db_path), range(8)))

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,)]


def test_failed_migration_rolls_back_schema_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "failed.db"
    monkeypatch.setattr(
        db_module,
        "MIGRATIONS",
        ((99, "CREATE TABLE partial(value TEXT);\nTHIS IS NOT SQL;"),),
    )

    with pytest.raises(sqlite3.OperationalError):
        initialize_database(db_path)

    with sqlite3.connect(db_path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        versions = connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    assert "partial" not in names
    assert versions == []
