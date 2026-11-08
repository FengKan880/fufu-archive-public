from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from fufu_archive.db import KnowledgeBase, initialize_database
from fufu_archive.doctor import doctor_database, main


def _checks(report: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        check["name"]: check
        for check in report["checks"]  # type: ignore[index]
    }


def test_doctor_reports_healthy_initialized_database(tmp_path: Path) -> None:
    db_path = tmp_path / "healthy.sqlite3"
    initialize_database(db_path)
    kb = KnowledgeBase(db_path)
    kb.ingest(
        content="healthy doctor document",
        collection="doctor",
        domain=None,
        status="active",
        parent_id=None,
        supersedes=None,
        source=None,
        metadata={"fixture": "doctor"},
        tags=["healthy"],
    )
    kb.create_candidate(content="healthy doctor candidate", collection="doctor")

    report = doctor_database(db_path)
    checks = _checks(report)

    assert report["ok"] is True
    assert checks["schema_migration"]["status"] == "ok"
    assert checks["integrity_check"]["status"] == "ok"
    assert checks["foreign_key_check"]["status"] == "ok"
    assert checks["table_presence"]["status"] == "ok"
    assert checks["fts_coverage"]["status"] == "ok"
    assert checks["candidate_status_counts"]["details"] == {"counts": {"pending": 1}}
    assert checks["audit_chain"]["status"] == "ok"


def test_doctor_missing_database_is_read_only(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "does-not-exist.sqlite3"

    assert main([str(db_path), "--json"]) != 0
    output = json.loads(capsys.readouterr().out)

    assert output["ok"] is False
    assert not db_path.exists()
    assert output["checks"][0]["name"] == "database_open"


def test_doctor_detects_incomplete_database_without_changing_it(tmp_path: Path) -> None:
    db_path = tmp_path / "incomplete.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'fixture')"
        )
        connection.execute("CREATE TABLE documents(id TEXT PRIMARY KEY, content TEXT)")

    before = hashlib.sha256(db_path.read_bytes()).digest()
    report = doctor_database(db_path)
    after = hashlib.sha256(db_path.read_bytes()).digest()
    checks = _checks(report)

    assert report["ok"] is False
    assert checks["schema_migration"]["status"] == "fail"
    assert checks["table_presence"]["status"] == "fail"
    assert before == after



