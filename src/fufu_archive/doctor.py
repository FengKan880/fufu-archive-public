"""Read-only diagnostics for a Fufu Archive SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from fufu_archive.db import MIGRATIONS

EXPECTED_SCHEMA_VERSION = max(version for version, _statement in MIGRATIONS)
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
OPTIONAL_VECTOR_TABLE = "chunk_vec"


def _check(name: str, status: str, **details: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "details": details}


def _unavailable(name: str, reason: str, **details: Any) -> dict[str, Any]:
    return _check(name, "unavailable", reason=reason, **details)


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    # ``mode=ro`` is essential: sqlite3.connect(Path(...)) would create a
    # missing file before the doctor had a chance to inspect it.
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        import sqlite_vec  # type: ignore[import-not-found]

        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
    except Exception:
        # The vector extension is optional.  Its absence must not prevent
        # integrity, schema, foreign-key, or FTS diagnostics from running.
        pass
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }


def _schema_check(
    connection: sqlite3.Connection, tables: set[str]
) -> dict[str, Any]:
    if "schema_migrations" not in tables:
        return _check(
            "schema_migration",
            "fail",
            current_version=None,
            expected_version=EXPECTED_SCHEMA_VERSION,
            applied_versions=[],
            reason="schema_migrations table is missing",
        )
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    applied = [int(row[0]) for row in rows]
    expected = list(range(1, EXPECTED_SCHEMA_VERSION + 1))
    current = applied[-1] if applied else None
    status = "ok" if applied == expected else "fail"
    return _check(
        "schema_migration",
        status,
        current_version=current,
        expected_version=EXPECTED_SCHEMA_VERSION,
        applied_versions=applied,
    )


def _pragma_checks(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
    integrity = [str(row[0]) for row in integrity_rows]
    checks = [
        _check(
            "integrity_check",
            "ok" if integrity == ["ok"] else "fail",
            result=integrity,
        )
    ]
    foreign_keys = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
    checks.append(
        _check(
            "foreign_key_check",
            "ok" if not foreign_keys else "fail",
            violation_count=len(foreign_keys),
            violations=foreign_keys[:20],
        )
    )
    return checks


def _table_presence_check(tables: set[str]) -> dict[str, Any]:
    missing = [table for table in REQUIRED_TABLES if table not in tables]
    return _check(
        "table_presence",
        "ok" if not missing else "fail",
        required=list(REQUIRED_TABLES),
        present=[table for table in REQUIRED_TABLES if table in tables],
        missing=missing,
        vector_table_present=OPTIONAL_VECTOR_TABLE in tables,
    )


def _row_counts(connection: sqlite3.Connection) -> dict[str, Any]:
    names = ("documents", "chunks", "metadata", "candidates", "audit_events")
    counts = {
        table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        for table in names
    }
    return _check("row_counts", "ok", counts=counts)


def _fts_check(connection: sqlite3.Connection) -> dict[str, Any]:
    chunk_count = int(connection.execute("SELECT count(*) FROM chunks").fetchone()[0])
    fts_rows = int(connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0])
    covered = int(
        connection.execute(
            """SELECT count(DISTINCT CAST(f.chunk_id AS INTEGER))
               FROM chunks_fts AS f INNER JOIN chunks AS c
                 ON c.id = CAST(f.chunk_id AS INTEGER)"""
        ).fetchone()[0]
    )
    orphan_rows = int(
        connection.execute(
            """SELECT count(*) FROM chunks_fts AS f
               LEFT JOIN chunks AS c ON c.id = CAST(f.chunk_id AS INTEGER)
               WHERE c.id IS NULL"""
        ).fetchone()[0]
    )
    details = {
        "chunk_count": chunk_count,
        "fts_row_count": fts_rows,
        "covered_chunk_count": covered,
        "missing_chunk_count": max(chunk_count - covered, 0),
        "orphan_row_count": orphan_rows,
        "duplicate_row_count": max(fts_rows - covered - orphan_rows, 0),
    }
    return _check(
        "fts_coverage",
        "ok"
        if fts_rows == chunk_count and covered == chunk_count and orphan_rows == 0
        else "fail",
        **details,
    )


def _vector_checks(
    connection: sqlite3.Connection, tables: set[str]
) -> list[dict[str, Any]]:
    if OPTIONAL_VECTOR_TABLE not in tables:
        reason = "optional sqlite-vec table is not present"
        return [
            _unavailable("vector_table_presence", reason, optional=True),
            _unavailable("vector_coverage", reason, optional=True),
        ]
    presence = _check("vector_table_presence", "ok", present=True, optional=True)
    try:
        vector_rows = int(
            connection.execute(f"SELECT count(*) FROM {OPTIONAL_VECTOR_TABLE}").fetchone()[0]
        )
        covered = int(
            connection.execute(
                """SELECT count(*) FROM chunk_vec AS v
                   INNER JOIN chunks AS c ON c.id = v.rowid"""
            ).fetchone()[0]
        )
        orphan_rows = int(
            connection.execute(
                """SELECT count(*) FROM chunk_vec AS v
                   LEFT JOIN chunks AS c ON c.id = v.rowid
                   WHERE c.id IS NULL"""
            ).fetchone()[0]
        )
    except (OSError, sqlite3.Error) as error:
        return [
            presence,
            _unavailable(
                "vector_coverage",
                "sqlite-vec is unavailable for this connection",
                optional=True,
                error=type(error).__name__,
                message=str(error),
            ),
        ]
    chunk_count = int(connection.execute("SELECT count(*) FROM chunks").fetchone()[0])
    return [
        presence,
        _check(
            "vector_coverage",
            "ok"
            if vector_rows == chunk_count
            and covered == chunk_count
            and orphan_rows == 0
            else "fail",
            optional=True,
            chunk_count=chunk_count,
            vector_row_count=vector_rows,
            covered_chunk_count=covered,
            missing_chunk_count=max(chunk_count - covered, 0),
            orphan_row_count=orphan_rows,
        ),
    ]


def _orphan_checks(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    queries = {
        "orphan_chunks": """SELECT count(*) FROM chunks AS c
            LEFT JOIN documents AS d ON d.id = c.document_id WHERE d.id IS NULL""",
        "orphan_metadata": """SELECT count(*) FROM metadata AS m
            LEFT JOIN documents AS d ON d.id = m.document_id WHERE d.id IS NULL""",
        "orphan_tags": """SELECT count(*) FROM document_tags AS t
            LEFT JOIN documents AS d ON d.id = t.document_id WHERE d.id IS NULL""",
        "orphan_parent_contexts": """SELECT count(*) FROM parent_contexts AS p
            LEFT JOIN documents AS d ON d.id = p.document_id WHERE d.id IS NULL""",
        "orphan_audit_records": """SELECT count(*) FROM audit_events AS a
            LEFT JOIN candidates AS c ON a.entity_type = 'candidate' AND c.id = a.entity_id
            LEFT JOIN documents AS d ON a.entity_type = 'document' AND d.id = a.entity_id
            WHERE (a.entity_type = 'candidate' AND c.id IS NULL)
               OR (a.entity_type = 'document' AND d.id IS NULL)
               OR a.entity_type NOT IN ('candidate', 'document')""",
        "orphan_references": """SELECT
            (SELECT count(*) FROM documents AS x LEFT JOIN documents AS r ON r.id = x.parent_id
                WHERE x.parent_id IS NOT NULL AND r.id IS NULL) +
            (SELECT count(*) FROM documents AS x LEFT JOIN documents AS r ON r.id = x.supersedes
                WHERE x.supersedes IS NOT NULL AND r.id IS NULL) +
            (SELECT count(*) FROM chunks AS x LEFT JOIN documents AS r ON r.id = x.parent_id
                WHERE x.parent_id IS NOT NULL AND r.id IS NULL) +
            (SELECT count(*) FROM chunks AS x LEFT JOIN documents AS r ON r.id = x.supersedes
                WHERE x.supersedes IS NOT NULL AND r.id IS NULL) +
            (SELECT count(*) FROM candidates AS x
                LEFT JOIN documents AS r ON r.id = x.committed_document_id
                WHERE x.committed_document_id IS NOT NULL AND r.id IS NULL) +
            (SELECT count(*) FROM chunks AS x
                LEFT JOIN parent_contexts AS r ON r.id = x.parent_context_id
                WHERE x.parent_context_id IS NOT NULL AND r.id IS NULL)""",
    }
    checks: list[dict[str, Any]] = []
    for name, query in queries.items():
        count = int(connection.execute(query).fetchone()[0])
        checks.append(_check(name, "ok" if count == 0 else "fail", orphan_count=count))
    return checks


def _candidate_status_check(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT status, count(*) AS count FROM candidates GROUP BY status ORDER BY status"
    ).fetchall()
    return _check(
        "candidate_status_counts",
        "ok",
        counts={str(row[0]): int(row[1]) for row in rows},
    )


def _audit_chain_check(connection: sqlite3.Connection) -> dict[str, Any]:
    entities: dict[str, dict[str, tuple[int, str]]] = {
        "candidate": {
            str(row[0]): (int(row[1]), str(row[2]))
            for row in connection.execute("SELECT id, revision, status FROM candidates")
        },
        "document": {
            str(row[0]): (int(row[1]), str(row[2]))
            for row in connection.execute("SELECT id, revision, status FROM documents")
        },
    }
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
        """SELECT id, entity_type, entity_id, event_type, from_status, to_status, revision
           FROM audit_events ORDER BY id"""
    ):
        grouped[(str(row[1]), str(row[2]))].append(row)

    problems: list[dict[str, Any]] = []
    for key, events in grouped.items():
        entity_type, entity_id = key
        current = entities.get(entity_type, {}).get(entity_id)
        if current is None:
            problems.append({"entity_type": entity_type, "entity_id": entity_id, "reason": "missing entity"})
            continue
        revisions = [int(event[6]) for event in events]
        if revisions != list(range(len(events))):
            problems.append({"entity_type": entity_type, "entity_id": entity_id, "reason": "non-contiguous revisions", "revisions": revisions})
        previous_to: str | None = None
        for index, event in enumerate(events):
            if index == 0:
                valid = event[5] is not None and event[4] is None
            else:
                valid = event[4] == previous_to
            if not valid:
                problems.append({"entity_type": entity_type, "entity_id": entity_id, "reason": "invalid status transition", "event_id": int(event[0])})
            previous_to = event[5]
        if entity_type == "candidate":
            if events[0][3] != "candidate.created" or current[0] != revisions[-1] or current[1] != events[-1][5]:
                problems.append({"entity_type": entity_type, "entity_id": entity_id, "reason": "candidate state does not match audit tail"})
        elif events[0][3] != "document.created" or revisions[-1] > current[0]:
            problems.append({"entity_type": entity_type, "entity_id": entity_id, "reason": "document audit chain is inconsistent"})

    missing_candidate_events = [
        candidate_id
        for candidate_id in entities["candidate"]
        if ("candidate", candidate_id) not in grouped
    ]

    details = {
        "entity_count": sum(len(value) for value in entities.values()),
        "audited_entity_count": len(grouped),
        "missing_candidate_event_chains": missing_candidate_events,

        "problem_count": len(problems),
        "problems": problems[:20],
    }
    return _check("audit_chain", "ok" if not problems and not missing_candidate_events else "fail", **details)


def doctor_database(db_path: Path) -> dict[str, Any]:
    """Inspect *db_path* without creating, migrating, or modifying it."""
    resolved_path = db_path.resolve()
    report: dict[str, Any] = {
        "ok": False,
        "database": str(resolved_path),
        "checks": [],
    }
    try:
        connection = _connect_read_only(db_path)
    except (OSError, sqlite3.Error) as error:
        report["checks"] = [
            _check("database_open", "fail", error=type(error).__name__, message=str(error))
        ]
        return report

    try:
        tables = _table_names(connection)
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1
        checks: list[dict[str, Any]] = [
            _check("database_open", "ok" if query_only else "fail", query_only=query_only),
            _schema_check(connection, tables),
            *_pragma_checks(connection),
            _table_presence_check(tables),
        ]
        structural_ok = checks[-1]["status"] == "ok"
        if structural_ok:
            checks.extend(
                [
                    _row_counts(connection),
                    _fts_check(connection),
                    *_vector_checks(connection, tables),
                    *_orphan_checks(connection),
                    _candidate_status_check(connection),
                    _audit_chain_check(connection),
                ]
            )
        else:
            checks.extend(
                [
                    _unavailable(name, "required table is missing")
                    for name in (
                        "row_counts",
                        "fts_coverage",
                        "vector_table_presence",
                        "vector_coverage",
                        "orphan_chunks",
                        "orphan_metadata",
                        "orphan_tags",
                        "orphan_parent_contexts",
                        "orphan_audit_records",
                        "orphan_references",
                        "candidate_status_counts",
                        "audit_chain",
                    )
                ]
            )
        report["checks"] = checks
        report["ok"] = not any(
            check["status"] == "fail" and not check["details"].get("optional", False)
            for check in checks
        )
        return report
    except sqlite3.Error as error:
        report["checks"] = [
            _check("database_read", "fail", error=type(error).__name__, message=str(error))
        ]
        return report
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Fufu Archive SQLite diagnostics")
    parser.add_argument("db_path", type=Path, help="explicit SQLite database path")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit stable machine-readable JSON (the default output)",
    )
    args = parser.parse_args(argv)
    report = doctor_database(args.db_path)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
