from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from fufu_archive.backup import (
    BackupError,
    backup_database,
    main,
    verify_restore,
)
from fufu_archive.db import KnowledgeBase, initialize_database


def _source_database(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source.sqlite3"
    initialize_database(source)
    knowledge_base = KnowledgeBase(source)
    knowledge_base.ingest(
        content="P4.1 isolated restore has a searchable document",
        collection="backup-tests",
        domain="reliability",
        status="active",
        parent_id=None,
        supersedes=None,
        source="temporary-test",
        metadata={"fixture": "backup"},
    )
    candidate = knowledge_base.create_candidate(
        content="P4.1 candidate lifecycle audit fixture",
        collection="backup-tests",
        topic_key="backup.candidate",
        metadata={"fixture": "backup"},
    )
    knowledge_base.approve_candidate(candidate["candidate_id"])
    return source, str(candidate["candidate_id"])


def test_sqlite_backup_restore_and_reads_leave_source_unchanged(tmp_path: Path) -> None:
    source, candidate_id = _source_database(tmp_path)
    destination = tmp_path / "isolated-restore.sqlite3"
    source_bytes = source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()

    report = backup_database(source, destination)

    assert report["ok"] is True
    assert report["evidence"]["method"] == "sqlite3.Connection.backup"
    assert report["source"]["path"] == str(source.resolve())
    assert report["destination"]["path"] == str(destination.resolve())
    assert report["source"]["bytes"] > 0
    assert report["source"]["pages"]["page_count"] > 0
    assert source.read_bytes() == source_bytes
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash

    # Initialization is permitted only on the isolated destination and remains
    # idempotent; the restore itself already has the current schema.
    initialize_database(destination)
    restored = KnowledgeBase(destination)
    assert restored.search(
        query="searchable document",
        collection="backup-tests",
        domain="reliability",
        limit=10,
        mode="fts5",
    )
    candidates = restored.list_candidates(status=None, collection="backup-tests")
    assert [candidate["candidate_id"] for candidate in candidates] == [candidate_id]
    events = restored.list_audit_events(candidate_id=candidate_id)
    assert [event["event_type"] for event in events] == [
        "candidate.created",
        "candidate.approved",
    ]

    verification = verify_restore(source, destination)
    assert verification["ok"] is True
    assert verification["verification"]["integrity_check"]["ok"] is True
    assert verification["verification"]["foreign_key_check"]["ok"] is True
    assert verification["verification"]["fts_query"]["ok"] is True
    assert verification["verification"]["candidate_read"]["row_count"] == 1
    assert verification["verification"]["audit_read"]["row_count"] == 2


def test_restore_check_reports_stable_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source, _candidate_id = _source_database(tmp_path)
    destination = tmp_path / "restore-check.sqlite3"

    assert main(["restore-check", str(source), str(destination)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["operation"] == "restore-check"
    assert output["ok"] is True
    assert output["source"]["path"] == str(source.resolve())
    assert output["destination"]["path"] == str(destination.resolve())
    assert output["verification"]["fts_query"]["ok"] is True


def test_missing_source_does_not_create_destination(tmp_path: Path) -> None:
    source = tmp_path / "missing.sqlite3"
    destination = tmp_path / "should-not-exist.sqlite3"

    with pytest.raises(BackupError, match="does not exist"):
        backup_database(source, destination)

    assert not destination.exists()


def test_same_source_and_destination_are_rejected(tmp_path: Path) -> None:
    source, _candidate_id = _source_database(tmp_path)

    with pytest.raises(BackupError, match="different paths"):
        backup_database(source, source)
    with pytest.raises(BackupError, match="different paths"):
        verify_restore(source, source)


def test_verify_restore_rejects_healthy_but_unrelated_database(tmp_path: Path) -> None:
    source, _candidate_id = _source_database(tmp_path)
    unrelated, _other_candidate_id = _source_database(tmp_path / "unrelated")

    report = verify_restore(source, unrelated)

    assert report["ok"] is False
    assert report["verification"]["source_destination_match"]["ok"] is False


def test_corrupted_or_incomplete_restore_reports_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, _candidate_id = _source_database(tmp_path)
    destination = tmp_path / "incomplete.sqlite3"
    backup_database(source, destination)
    destination.write_bytes(destination.read_bytes()[:100])

    report = verify_restore(source, destination)
    assert report["ok"] is False
    assert report["verification"]["database_open"]["ok"] is False

    assert main(["verify", str(source), str(destination)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["verification"]["database_open"]["ok"] is False
