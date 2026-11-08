"""Local stdio MCP adapter for Hermes Agent.

The adapter deliberately talks to the local SQLite facade instead of calling
the HTTP API. This keeps the MCP process local and avoids copying the API
token into Hermes configuration.

IMPORTANT - this is NOT an authentication boundary.

The HTTP surface authenticates callers with a shared bearer token
(``FUFU_ARCHIVE_API_TOKEN``). This stdio surface has no principal at all: any
process able to spawn the server speaks the protocol, and the stateful tools
below (``kb_add``, ``kb_candidate_approve``, ``kb_candidate_commit``,
``kb_update``) write to the formal knowledge database with no identity check,
no role check and no local-operator interlock. The only real boundary here is
process locality: the transport is hard-wired to stdio, so there is no port,
no socket and no network exposure. Treat "Hermes can spawn this process" as the
entire trust decision. The ``reader``/``worker``/``admin`` labels in
:mod:`fufu_archive.security` are in-process misuse guards and grant nothing to
an MCP caller.

For the same reason the server never creates an archive: a mistyped
``FUFU_ARCHIVE_DB_PATH`` would otherwise silently produce an empty database
that looks legitimate.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any, TYPE_CHECKING

from fufu_archive.config import Settings
from fufu_archive.db import KnowledgeBase, MIGRATIONS
from fufu_archive.models import IngestRequest

#: Tools that can change the formal knowledge database. They carry no
#: authorization of their own; they are listed so the boundary is explicit.
_STATEFUL_TOOL_NAMES = (
    "kb_add",
    "kb_candidate_add",
    "kb_candidate_approve",
    "kb_candidate_reject",
    "kb_candidate_commit",
    "kb_update",
)

if TYPE_CHECKING:
    from mcp.server import MCPServer


def build_server(kb: KnowledgeBase) -> "MCPServer":
    try:
        # MCP 2.x renamed FastMCP to MCPServer. Keep the v1 fallback for
        # standalone installs that still use the older SDK line.
        from mcp.server import MCPServer
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as MCPServer
        except ImportError as error:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "MCP support requires the optional 'mcp' dependency"
            ) from error
    server = MCPServer("fufu-archive")

    @server.tool()
    def kb_search(
        query: str,
        collection: str | None = None,
        domain: str | None = None,
        topic_key: str | None = None,
        limit: int = 10,
        mode: str = "dense",
    ) -> dict[str, Any]:
        """Search archived knowledge with FTS, dense, or hybrid retrieval."""
        if not query.strip():
            raise ValueError("query must contain non-whitespace text")
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        return {
            "query": query,
            "results": kb.search(
                query=query,
                collection=collection,
                domain=domain,
                topic_key=topic_key,
                limit=limit,
                mode=mode,
            ),
        }

    @server.tool()
    def kb_add(
        content: str,
        collection: str = "default",
        domain: str | None = None,
        topic_key: str | None = None,
        source: str | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        chunking: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add a curated knowledge item to the archive.

        State-changing: writes directly to the formal knowledge database.
        This surface performs no authentication or authorization.
        """
        payload = IngestRequest(
            content=content,
            collection=collection,
            domain=domain,
            topic_key=topic_key,
            source=source,
            title=title,
            tags=[] if tags is None else tags,
            metadata={} if metadata is None else metadata,
            chunking=chunking,
        )
        document_id, chunk_count = kb.ingest(**payload.model_dump())
        return {
            "document_id": document_id,
            "chunk_count": chunk_count,
            "status": "created",
        }

    @server.tool()
    def kb_candidate_add(
        content: str,
        collection: str = "default",
        domain: str | None = None,
        topic_key: str | None = None,
        source: str | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        chunking: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a pending candidate for human or agent review."""
        return kb.create_candidate(
            content=content,
            collection=collection,
            domain=domain,
            topic_key=topic_key,
            source=source,
            title=title,
            tags=[] if tags is None else tags,
            metadata={} if metadata is None else metadata,
            chunking=chunking,
        )

    @server.tool()
    def kb_candidate_approve(
        candidate_id: str,
        expected_revision: int | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Approve a pending candidate so it can be committed.

        State-changing and unauthenticated: any MCP caller may approve.
        """
        return kb.approve_candidate(
            candidate_id, expected_revision=expected_revision, revision=revision
        )

    @server.tool()
    def kb_candidate_reject(
        candidate_id: str,
        expected_revision: int | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Reject a pending candidate."""
        return kb.reject_candidate(
            candidate_id, expected_revision=expected_revision, revision=revision
        )

    @server.tool()
    def kb_candidate_commit(
        candidate_id: str,
        expected_revision: int | None = None,
        revision: int | None = None,
        supersedes_document_id: str | None = None,
    ) -> dict[str, Any]:
        """Commit an approved candidate to the searchable knowledge base.

        State-changing and unauthenticated: this is the strongest write
        available and it has no principal, role or interlock check.
        """
        return kb.commit_candidate(
            candidate_id,
            expected_revision=expected_revision,
            revision=revision,
            supersedes_document_id=supersedes_document_id,
        )

    @server.tool()
    def kb_candidate_list(
        status: str | None = "pending",
        collection: str | None = None,
        topic_key: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List review candidates, defaulting to pending candidates."""
        return kb.list_candidates(
            status=status,
            collection=collection,
            topic_key=topic_key,
            limit=limit,
        )

    @server.tool()
    def kb_audit_events(
        candidate_id: str | None = None,
        document_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List ordered audit events for a candidate or formal document."""
        return kb.list_audit_events(
            candidate_id=candidate_id, document_id=document_id, limit=limit
        )

    @server.tool()
    def kb_update(
        document_id: str,
        status: str | None = None,
        source: str | None = None,
        title: str | None = None,
        supersedes: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        expected_revision: int | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Update lifecycle or descriptive fields without rewriting content.

        State-changing and unauthenticated.
        """
        return kb.update_document(
            document_id,
            status=status,
            source=source,
            title=title,
            supersedes=supersedes,
            metadata=metadata,
            tags=tags,
            expected_revision=expected_revision,
            revision=revision,
        )

    @server.tool()
    def kb_list(
        collection: str | None = None,
        domain: str | None = None,
        topic_key: str | None = None,
        status: str | None = "active",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List archived document summaries."""
        return kb.list_documents(
            collection=collection,
            domain=domain,
            topic_key=topic_key,
            status=status,
            limit=limit,
        )

    return server


def _expected_archive_schema() -> dict[str, frozenset[str]]:
    """Derive the required tables and columns from the migration source itself.

    A recorded migration version is not proof that the business schema exists:
    a database can carry the full ``schema_migrations`` ledger and still have no
    usable tables. Replaying the migrations in a throwaway in-memory database
    keeps the expectation in step with :data:`fufu_archive.db.MIGRATIONS`
    instead of hardcoding a second copy of the schema.
    """
    reference = sqlite3.connect(":memory:")
    try:
        for _version, statement in MIGRATIONS:
            reference.executescript(statement)
        names = [
            row[0]
            for row in reference.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if not str(row[0]).startswith("sqlite_")
        ]
        return {
            name: frozenset(
                row[1] for row in reference.execute(f'PRAGMA table_info("{name}")')
            )
            for name in names
        }
    finally:
        reference.close()


def _missing_schema_parts(
    connection: sqlite3.Connection, expected: dict[str, frozenset[str]]
) -> list[str]:
    """Return human-readable descriptions of every required part that is absent."""
    missing: list[str] = []
    for table in sorted(expected):
        actual_columns = {
            row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        if not actual_columns:
            missing.append(f"table {table}")
            continue
        absent = sorted(expected[table] - actual_columns)
        if absent:
            missing.append(f"{table}({', '.join(absent)})")
    return missing


def _require_initialized_archive_database(db_path: Path) -> None:
    """Fail closed unless an existing archive has every known migration.

    MCP startup must never bootstrap, migrate, backfill, or otherwise write a
    database. The explicit init-db command owns those state transitions. The
    check is read-only and verifies the required tables/columns as well as the
    migration ledger, so a truncated or partially restored database is refused
    at startup instead of failing later inside a tool call.
    """
    if not db_path.is_file():
        raise SystemExit(
            f"archive database does not exist: {db_path}. The MCP server never "
            "creates an archive; set FUFU_ARCHIVE_DB_PATH to an initialized "
            "database (run fufu-archive-init-db first)."
        )
    try:
        expected_schema = _expected_archive_schema()
    except sqlite3.Error as error:  # pragma: no cover - no FTS5 in this build
        raise SystemExit(
            f"cannot verify archive schema for {db_path}: {error}. This "
            "interpreter must provide the FTS5 extension used by the archive."
        ) from error
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=5
        )
        connection.execute("PRAGMA query_only = ON")
        applied = {
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        missing = _missing_schema_parts(connection, expected_schema)
    except (OSError, sqlite3.Error) as error:
        raise SystemExit(
            f"archive database is not initialized or readable: {db_path}. "
            "Run fufu-archive-init-db explicitly before starting MCP."
        ) from error
    finally:
        if connection is not None:
            connection.close()
    expected_versions = {version for version, _statement in MIGRATIONS}
    if applied != expected_versions or missing:
        detail = f" missing: {', '.join(missing[:8])}" if missing else ""
        raise SystemExit(
            f"archive database is not initialized for this version: {db_path}."
            f"{detail} Run fufu-archive-init-db explicitly before starting MCP."
        )


def main() -> None:
    """Run the stdio server against an existing archive database.

    The database check runs before MCP server creation, database initialization,
    or any filesystem write, so a mistyped path fails closed even on installs
    without the optional SDK.
    """
    settings = Settings.from_env()
    db_path = Path(settings.db_path)
    _require_initialized_archive_database(db_path)
    build_server(KnowledgeBase(db_path)).run(transport="stdio")


if __name__ == "__main__":
    main()
