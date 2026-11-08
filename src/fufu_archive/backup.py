"""SQLite-consistent backup and isolated restore verification.

This module deliberately accepts database paths rather than reading application
or Hermes configuration.  The source connection is always opened with SQLite
URI ``mode=ro`` and ``query_only`` enabled.  Restore verification is read-only;
it does not migrate, repair, or rebuild optional vector indexes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

from fufu_archive.db import MIGRATIONS, KnowledgeBase

EXPECTED_SCHEMA_VERSIONS = list(range(1, max(version for version, _ in MIGRATIONS) + 1))
REQUIRED_TABLES = (
    "schema_migrations",
    "documents",
    "chunks",
    "metadata",
    "parent_contexts",
    "document_tags",
    "candidates",
    "audit_events",
    "chunks_fts",
)


class BackupError(ValueError):
    """Raised when a requested backup or restore precondition is invalid."""


def _resolved_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _validate_paths(source: Path | str, destination: Path | str) -> tuple[Path, Path]:
    source_path = _resolved_path(source)
    destination_path = _resolved_path(destination)
    if source_path == destination_path:
        raise BackupError("source and destination must be different paths")
    if not source_path.is_file():
        raise BackupError(f"source database does not exist: {source_path}")
    if destination_path.exists():
        try:
            if os.path.samefile(source_path, destination_path):
                raise BackupError("source and destination must be different paths")
        except OSError:
            pass
    if destination_path.exists() and destination_path.is_dir():
        raise BackupError(f"destination is a directory: {destination_path}")
    return source_path, destination_path


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    # Opening a missing source through this URI fails instead of creating it.
    connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_evidence(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _page_evidence(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        "page_size": int(connection.execute("PRAGMA page_size").fetchone()[0]),
        "page_count": int(connection.execute("PRAGMA page_count").fetchone()[0]),
        "freelist_count": int(
            connection.execute("PRAGMA freelist_count").fetchone()[0]
        ),
    }


def _database_evidence(path: Path) -> dict[str, Any]:
    evidence = _file_evidence(path)
    try:
        connection = _connect_read_only(path)
    except (OSError, sqlite3.Error):
        return evidence
    try:
        try:
            evidence["pages"] = _page_evidence(connection)
        except sqlite3.Error:
            pass
    finally:
        connection.close()
    return evidence


def _same_evidence(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left.get("bytes") == right.get("bytes") and left.get("sha256") == right.get(
        "sha256"
    )


def _logical_content_sha256(path: Path) -> str:
    """Hash canonical rows from the P4 core tables for restore equivalence."""
    connection = _connect_read_only(path)
    try:
        digest = hashlib.sha256()
        for table in REQUIRED_TABLES:
            if table == "chunks_fts":
                continue
            columns = [
                str(row["name"])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            if not columns:
                raise sqlite3.OperationalError(f"missing required table: {table}")
            quoted_columns = ", ".join(f'"{column}"' for column in columns)
            digest.update(table.encode("utf-8"))
            digest.update(json.dumps(columns, separators=(",", ":")).encode("utf-8"))
            for row in connection.execute(
                f'SELECT {quoted_columns} FROM "{table}" ORDER BY rowid'
            ):
                values = [
                    value.hex() if isinstance(value, bytes) else value
                    for value in tuple(row)
                ]
                digest.update(
                    json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
            digest.update(b"\n")
        return digest.hexdigest()
    finally:
        connection.close()


def _backup_to_temp(source_path: Path, destination_path: Path) -> Path:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.", suffix=".sqlite3.tmp", dir=destination_path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = _connect_read_only(source_path)
        destination_connection = sqlite3.connect(temporary_path, timeout=30.0)
        source_connection.backup(destination_connection)
        destination_connection.commit()
        return temporary_path
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()


def backup_database(source: Path | str, destination: Path | str) -> dict[str, Any]:
    """Create a consistent SQLite backup at an explicitly supplied path."""
    source_path, destination_path = _validate_paths(source, destination)
    source_before = _database_evidence(source_path)
    temporary_path: Path | None = None
    try:
        temporary_path = _backup_to_temp(source_path, destination_path)
        source_after = _database_evidence(source_path)
        source_unchanged = _same_evidence(source_before, source_after)
        if not source_unchanged:
            raise BackupError("source bytes or hash changed during backup")
        os.replace(temporary_path, destination_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    destination_evidence = _database_evidence(destination_path)
    pages_match = source_before.get("pages") == destination_evidence.get("pages")
    return {
        "operation": "backup",
        "ok": True,
        "source": source_before,
        "destination": destination_evidence,
        "evidence": {
            "source_unchanged": True,
            "same_page_evidence": pages_match,
            "method": "sqlite3.Connection.backup",
        },
        "verification": {"status": "not_run"},
    }


def _check(name: str, ok: bool, **details: Any) -> dict[str, Any]:
    return {"name": name, "ok": ok, **details}


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }


def _verify_database(connection: sqlite3.Connection, destination: Path) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    query_only = int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1
    checks["database_open"] = _check("database_open", query_only, query_only=query_only)

    integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    checks["integrity_check"] = _check(
        "integrity_check", integrity == ["ok"], result=integrity
    )
    foreign_keys = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
    checks["foreign_key_check"] = _check(
        "foreign_key_check",
        not foreign_keys,
        violation_count=len(foreign_keys),
        violations=foreign_keys[:20],
    )

    tables = _table_names(connection)
    missing_tables = [table for table in REQUIRED_TABLES if table not in tables]
    checks["table_presence"] = _check(
        "table_presence",
        not missing_tables,
        required=list(REQUIRED_TABLES),
        missing=missing_tables,
    )
    if missing_tables:
        checks["schema_migration"] = _check(
            "schema_migration", False, reason="required table is missing"
        )
        checks["initialization"] = {
            "status": "not_run",
            "reason": "incomplete restore is reported, not repaired",
        }
        for name in ("fts_query", "candidate_read", "audit_read"):
            checks[name] = _check(name, False, reason="required table is missing")
        checks["vector_checks"] = {
            "status": "skipped",
            "optional": True,
            "reason": "P4.1 does not rebuild vector indexes",
        }
        return checks

    versions = [
        int(row[0])
        for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
    ]
    checks["schema_migration"] = _check(
        "schema_migration",
        versions == EXPECTED_SCHEMA_VERSIONS,
        applied_versions=versions,
        expected_versions=EXPECTED_SCHEMA_VERSIONS,
    )
    checks["initialization"] = {
        "status": "not_needed",
        "reason": "restored SQLite copy already contains the current schema",
        "destination": str(destination),
    }

    # Use the existing FTS row search implementation against this read-only
    # connection.  The query is derived from indexed data, so this is a real
    # MATCH query rather than a table-presence check.
    fts_row = connection.execute(
        """SELECT f.indexed_text
           FROM chunks_fts AS f
           INNER JOIN chunks AS c ON c.id = CAST(f.chunk_id AS INTEGER)
           WHERE c.status = 'active' AND trim(f.indexed_text) <> ''
           ORDER BY c.id LIMIT 1"""
    ).fetchone()
    if fts_row is None:
        checks["fts_query"] = _check(
            "fts_query", False, reason="no active indexed chunk is available for probe"
        )
    else:
        query = str(fts_row[0]).split()[0]
        rows = KnowledgeBase(destination)._search_fts_rows(
            connection, query, None, None, 1
        )
        checks["fts_query"] = _check(
            "fts_query", bool(rows), query=query, match_count=len(rows)
        )

    candidates = connection.execute(
        """SELECT id FROM candidates
           ORDER BY updated_at ASC, id ASC LIMIT 20"""
    ).fetchall()
    candidate_ids = [str(row[0]) for row in candidates]
    checks["candidate_read"] = _check(
        "candidate_read", True, row_count=len(candidate_ids), candidate_ids=candidate_ids
    )

    if candidate_ids:
        audit_rows = connection.execute(
            """SELECT id, entity_type, entity_id, event_type, revision
               FROM audit_events
               WHERE entity_type = 'candidate' AND entity_id = ?
               ORDER BY id ASC LIMIT 100""",
            (candidate_ids[0],),
        ).fetchall()
        audit_scope = {"candidate_id": candidate_ids[0]}
    else:
        audit_rows = connection.execute(
            """SELECT id, entity_type, entity_id, event_type, revision
               FROM audit_events ORDER BY id ASC LIMIT 100"""
        ).fetchall()
        audit_scope = {"candidate_id": None}
    checks["audit_read"] = _check(
        "audit_read",
        True,
        row_count=len(audit_rows),
        **audit_scope,
    )
    checks["vector_checks"] = {
        "status": "skipped",
        "optional": True,
        "reason": "P4.1 does not rebuild vector indexes",
    }
    return checks


def verify_restore(source: Path | str, destination: Path | str) -> dict[str, Any]:
    """Verify an existing restore without creating, migrating, or repairing it."""
    source_path, destination_path = _validate_paths(source, destination)
    source_before = _database_evidence(source_path)
    destination_before = _database_evidence(destination_path)
    checks: dict[str, Any]
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_read_only(destination_path)
        checks = _verify_database(connection, destination_path)
    except (OSError, sqlite3.Error) as error:
        checks = {
            "database_open": _check(
                "database_open", False, error=type(error).__name__, message=str(error)
            )
        }
    finally:
        if connection is not None:
            connection.close()

    source_after = _database_evidence(source_path)
    destination_after = _database_evidence(destination_path)
    try:
        source_content_sha256 = _logical_content_sha256(source_path)
        destination_content_sha256 = _logical_content_sha256(destination_path)
        source_destination_match = source_content_sha256 == destination_content_sha256
        checks["source_destination_match"] = _check(
            "source_destination_match",
            source_destination_match,
            source_content_sha256=source_content_sha256,
            destination_content_sha256=destination_content_sha256,
        )
    except (OSError, sqlite3.Error) as error:
        checks["source_destination_match"] = _check(
            "source_destination_match",
            False,
            error=type(error).__name__,
            message="logical source/restore comparison failed",
        )
    checks["source_unchanged"] = _check(
        "source_unchanged", _same_evidence(source_before, source_after)
    )
    checks["destination_unchanged"] = _check(
        "destination_unchanged", _same_evidence(destination_before, destination_after)
    )
    required_checks = [
        check
        for name, check in checks.items()
        if name not in {"vector_checks"} and isinstance(check, dict) and "ok" in check
    ]
    ok = all(bool(check["ok"]) for check in required_checks)
    return {
        "operation": "verify",
        "ok": ok,
        "source": source_after,
        "destination": destination_after,
        "evidence": {
            "same_page_evidence": source_after.get("pages")
            == destination_after.get("pages"),
        },
        "verification": checks,
    }


def restore_check(source: Path | str, destination: Path | str) -> dict[str, Any]:
    """Restore to *destination* with SQLite backup API, then verify it."""
    backup = backup_database(source, destination)
    verification = verify_restore(source, destination)
    return {
        "operation": "restore-check",
        "ok": bool(backup["ok"] and verification["ok"]),
        "source": verification["source"],
        "destination": verification["destination"],
        "evidence": {
            "method": "sqlite3.Connection.backup",
            "same_page_evidence": verification["evidence"]["same_page_evidence"],
        },
        "backup": backup,
        "verification": verification["verification"],
    }


def _error_report(operation: str, source: Path | str, destination: Path | str, error: Exception) -> dict[str, Any]:
    return {
        "operation": operation,
        "ok": False,
        "source": str(_resolved_path(source)),
        "destination": str(_resolved_path(destination)),
        "error": {"type": type(error).__name__, "message": str(error)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SQLite-consistent Fufu Archive backup and isolated restore verification"
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for operation in ("backup", "restore-check", "verify"):
        subparser = subparsers.add_parser(operation)
        subparser.add_argument("source", type=Path, help="explicit source SQLite path")
        subparser.add_argument(
            "destination", type=Path, help="explicit destination SQLite path"
        )
    args = parser.parse_args(argv)
    try:
        if args.operation == "backup":
            report = backup_database(args.source, args.destination)
        elif args.operation == "restore-check":
            report = restore_check(args.source, args.destination)
        else:
            report = verify_restore(args.source, args.destination)
    except (BackupError, OSError, sqlite3.Error) as error:
        report = _error_report(args.operation, args.source, args.destination, error)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
