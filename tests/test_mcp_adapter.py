from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from fufu_archive.db import KnowledgeBase, initialize_database


def _kb(tmp_path: Path) -> KnowledgeBase:
    db_path = tmp_path / "mcp.db"
    initialize_database(db_path)
    return KnowledgeBase(db_path)


def test_document_management_facade_supports_list_and_update(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    document_id, _ = kb.ingest(
        content="Hermes 档案馆通过 MCP 提供中文知识检索。",
        collection="hermes_ops",
        domain="hermes",
        status="active",
        parent_id=None,
        supersedes=None,
        source=None,
        metadata={},
        title="MCP 接入",
        tags=["hermes", "mcp"],
    )
    assert kb.list_documents(collection="hermes_ops")[0]["title"] == "MCP 接入"
    updated = kb.update_document(document_id, status="expired", supersedes="newer-id")
    assert updated["status"] == "expired"
    assert kb.list_documents(collection="hermes_ops") == []
    assert kb.list_documents(collection="hermes_ops", status="expired")[0]["supersedes"] == "newer-id"


def test_mcp_server_exposes_curated_archive_tools(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from fufu_archive.mcp_server import build_server

    server = build_server(_kb(tmp_path))
    tools = server._tool_manager.list_tools()
    assert {
        "kb_search",
        "kb_add",
        "kb_update",
        "kb_list",
        "kb_audit_events",
    } <= {tool.name for tool in tools}


def test_mcp_tools_add_search_list_and_update_document(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from fufu_archive.mcp_server import build_server

    kb = _kb(tmp_path)
    server = build_server(kb)
    tools = {tool.name: tool.fn for tool in server._tool_manager.list_tools()}

    added = tools["kb_add"](
        content="Hermes 档案馆通过 MCP 提供中文知识检索。",
        collection="hermes_ops",
        domain="hermes",
        title="MCP 接入",
        tags=["hermes", "mcp"],
    )
    assert added["status"] == "created"

    found = tools["kb_search"](
        query="中文知识检索", collection="hermes_ops", mode="fts5"
    )
    assert found["results"]
    assert found["results"][0]["content"] == "Hermes 档案馆通过 MCP 提供中文知识检索。"

    listed = tools["kb_list"](collection="hermes_ops")
    assert listed[0]["title"] == "MCP 接入"

    updated = tools["kb_update"](
        document_id=added["document_id"], status="expired", supersedes="newer-id"
    )
    assert updated["status"] == "expired"
    assert tools["kb_list"](collection="hermes_ops") == []
    assert tools["kb_list"](collection="hermes_ops", status="expired")[0]["supersedes"] == "newer-id"


def test_mcp_add_rejects_blank_content(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from fufu_archive.mcp_server import build_server

    server = build_server(_kb(tmp_path))
    add = next(tool.fn for tool in server._tool_manager.list_tools() if tool.name == "kb_add")
    with pytest.raises(ValueError):
        add(content="   ")


def test_mcp_candidate_review_tools_complete_minimal_flow(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from fufu_archive.mcp_server import build_server

    kb = _kb(tmp_path)
    server = build_server(kb)
    tools = {tool.name: tool.fn for tool in server._tool_manager.list_tools()}

    candidate = tools["kb_candidate_add"](
        content="MCP 候选知识需要审核后才能检索。", collection="review"
    )
    assert candidate["status"] == "pending"
    assert tools["kb_candidate_list"](collection="review")[0]["candidate_id"] == candidate[
        "candidate_id"
    ]
    assert tools["kb_candidate_approve"](
        candidate["candidate_id"]
    )["status"] == "approved"
    committed = tools["kb_candidate_commit"](candidate["candidate_id"])
    assert committed["status"] == "committed"
    assert tools["kb_candidate_commit"](candidate["candidate_id"]) == committed
    assert [event["event_type"] for event in tools["kb_audit_events"](
        candidate_id=candidate["candidate_id"]
    )] == ["candidate.created", "candidate.approved", "candidate.committed"]


def test_document_update_cannot_forge_scanner_metadata(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    document_id, _ = kb.ingest(
        content="stable knowledge",
        collection="hermes_ops",
        domain="hermes",
        status="active",
        parent_id=None,
        supersedes=None,
        source=None,
        metadata={"owner": "test"},
    )
    kb.update_document(
        document_id,
        metadata={
            "owner": "updated",
            "security.injection_scan": {"injection_suspected": False},
        },
    )
    with sqlite3.connect(kb.db_path) as connection:
        keys = {
            row[0]
            for row in connection.execute(
                "SELECT key FROM metadata WHERE document_id = ?", (document_id,)
            )
        }
    assert keys == {"owner"}


def test_mcp_topic_key_tools_support_query_and_explicit_version_link(
    tmp_path: Path,
) -> None:
    pytest.importorskip("mcp")
    from fufu_archive.mcp_server import build_server

    kb = _kb(tmp_path)
    server = build_server(kb)
    tools = {tool.name: tool.fn for tool in server._tool_manager.list_tools()}

    old = tools["kb_add"](
        content="MCP topic old version",
        collection="review",
        topic_key="mcp.topic",
    )
    candidate = tools["kb_candidate_add"](
        content="MCP topic new version",
        collection="review",
        topic_key="mcp.topic",
    )
    assert tools["kb_candidate_list"](topic_key="mcp.topic")[0]["candidate_id"] == candidate[
        "candidate_id"
    ]
    approved = tools["kb_candidate_approve"](candidate["candidate_id"])
    committed = tools["kb_candidate_commit"](
        candidate["candidate_id"],
        expected_revision=approved["revision"],
        supersedes_document_id=old["document_id"],
    )
    assert tools["kb_list"](topic_key="mcp.topic")[0]["topic_key"] == "mcp.topic"
    assert any(
        row["id"] == committed["document_id"] and row["supersedes"] == old["document_id"]
        for row in tools["kb_list"](topic_key="mcp.topic", status=None)
    )
    assert tools["kb_search"](
        query="MCP topic new version", topic_key="mcp.topic", mode="fts5"
    )["results"]


# --- MCP entry point boundary -------------------------------------------
# These regressions must run even where the optional 'mcp' SDK is absent:
# a fail-closed database guard is only useful if it fires before the SDK is
# imported or any file is created.


def test_mcp_entrypoint_refuses_a_missing_archive_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fufu_archive import mcp_server

    missing = tmp_path / "never-created.db"
    monkeypatch.setenv("FUFU_ARCHIVE_DB_PATH", str(missing))

    with pytest.raises(SystemExit):
        mcp_server.main()

    assert not missing.exists()


def test_mcp_entrypoint_refuses_a_missing_database_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fufu_archive import mcp_server

    missing = tmp_path / "absent-dir" / "archive.db"
    monkeypatch.setenv("FUFU_ARCHIVE_DB_PATH", str(missing))

    with pytest.raises(SystemExit):
        mcp_server.main()

    assert not missing.parent.exists()
    assert not missing.exists()


def test_mcp_module_declares_that_it_is_not_an_authentication_boundary() -> None:
    from fufu_archive import mcp_server, security

    module_doc = (mcp_server.__doc__ or "").lower()
    security_doc = (security.__doc__ or "").lower()
    assert "not an authentication boundary" in security_doc
    assert "not an authentication boundary" in module_doc
    # The state-changing tools are the ones that need the explicit statement.
    for name in ("kb_add", "kb_candidate_approve", "kb_candidate_commit"):
        assert name in mcp_server._STATEFUL_TOOL_NAMES


def _db_identity(path: Path) -> tuple[int, int, str]:
    return (
        path.stat().st_size,
        path.stat().st_mtime_ns,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def test_mcp_entrypoint_refuses_an_existing_uninitialized_archive_without_mutating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fufu_archive import mcp_server

    incomplete = tmp_path / "incomplete.db"
    with sqlite3.connect(incomplete) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    before = _db_identity(incomplete)
    monkeypatch.setenv("FUFU_ARCHIVE_DB_PATH", str(incomplete))

    with pytest.raises(SystemExit, match="not initialized"):
        mcp_server.main()

    assert _db_identity(incomplete) == before
    assert not incomplete.with_name(incomplete.name + "-wal").exists()


def test_mcp_entrypoint_starts_an_initialized_archive_without_reinitializing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fufu_archive import mcp_server

    db_path = tmp_path / "archive.db"
    initialize_database(db_path)
    before = _db_identity(db_path)
    started: list[object] = []

    class FakeServer:
        def run(self, *, transport: str) -> None:
            started.append(transport)

    monkeypatch.setenv("FUFU_ARCHIVE_DB_PATH", str(db_path))
    monkeypatch.setattr(mcp_server, "build_server", lambda _kb: FakeServer())

    mcp_server.main()

    assert started == ["stdio"]
    assert _db_identity(db_path) == before


def test_mcp_entrypoint_refuses_a_database_with_missing_business_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full migration ledger is not proof that the archive schema exists."""
    from fufu_archive import mcp_server
    from fufu_archive.db import MIGRATIONS

    hollow = tmp_path / "migrations-only.db"
    with sqlite3.connect(hollow) as connection:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO schema_migrations(version) VALUES (?)",
            [(version,) for version, _statement in MIGRATIONS],
        )
    before = _db_identity(hollow)
    monkeypatch.setenv("FUFU_ARCHIVE_DB_PATH", str(hollow))

    with pytest.raises(SystemExit, match="not initialized"):
        mcp_server.main()

    assert _db_identity(hollow) == before
    assert not hollow.with_name(hollow.name + "-wal").exists()


def test_mcp_entrypoint_refuses_a_database_missing_a_required_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fufu_archive import mcp_server

    db_path = tmp_path / "missing-column.db"
    initialize_database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX documents_topic_hash_unique_idx")
        connection.execute("DROP INDEX documents_topic_key_idx")
        connection.execute("ALTER TABLE documents DROP COLUMN topic_key")
    before = _db_identity(db_path)
    monkeypatch.setenv("FUFU_ARCHIVE_DB_PATH", str(db_path))

    with pytest.raises(SystemExit, match="not initialized"):
        mcp_server.main()

    assert _db_identity(db_path) == before
