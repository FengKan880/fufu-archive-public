"""SQLite storage, migrations, FTS5 + dense (sqlite-vec) retrieval.

P0 delivered FTS5 keyword retrieval; P1 adds a pluggable dense path backed by
an :class:`~fufu_archive.embedding.Embedder` (Ollama by default) and an
optional hybrid (RRF) fuse.  The vector extension is optional at runtime:
when sqlite-vec is unavailable or no embedder is configured, retrieval
gracefully degrades to FTS5 so the archive stays searchable.
"""

from __future__ import annotations

import json
import hashlib
import logging
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:  # optional native extension; vector features degrade without it
    import sqlite_vec  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only on bare installs
    sqlite_vec = None

from fufu_archive.chunking import chunk_text
from fufu_archive.config import Settings
from fufu_archive.embedding import Embedder
from fufu_archive.injection_scan import (
    INJECTION_SCAN_METADATA_KEY,
    scan_injection as scan_injection_text,
)
from fufu_archive.models import IngestRequest
from fufu_archive.tokenization import fts_document_text, safe_match_query

logger = logging.getLogger(__name__)


class RevisionConflict(ValueError):
    """Raised when a write uses a revision that is no longer current."""

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE documents (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            collection TEXT NOT NULL DEFAULT 'default',
            domain TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            parent_id TEXT,
            supersedes TEXT,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX documents_collection_status_idx ON documents(collection, status);
        CREATE INDEX documents_parent_id_idx ON documents(parent_id);

        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            collection TEXT NOT NULL,
            domain TEXT,
            status TEXT NOT NULL,
            parent_id TEXT,
            supersedes TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(document_id, chunk_index)
        );
        CREATE INDEX chunks_filters_idx ON chunks(collection, domain, status);

        CREATE TABLE metadata (
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(document_id, key)
        );

        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            chunk_id UNINDEXED,
            document_id UNINDEXED,
            indexed_text,
            tokenize='unicode61 remove_diacritics 2'
        );
        """,
    ),
    (
        2,
        """
        ALTER TABLE documents ADD COLUMN title TEXT;
        ALTER TABLE documents ADD COLUMN content_type TEXT;
        ALTER TABLE documents ADD COLUMN language TEXT;

        CREATE TABLE parent_contexts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL UNIQUE
                REFERENCES documents(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL
        );

        ALTER TABLE chunks ADD COLUMN parent_context_id INTEGER
            REFERENCES parent_contexts(id) ON DELETE SET NULL;
        CREATE INDEX chunks_parent_context_idx ON chunks(parent_context_id);

        CREATE TABLE document_tags (
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            tag_index INTEGER NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY(document_id, tag_index)
        );
        CREATE INDEX document_tags_value_idx ON document_tags(value);
        """,
    ),
    (
        3,
        """
        CREATE TABLE candidates (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            collection TEXT NOT NULL,
            domain TEXT,
            source TEXT,
            title TEXT,
            content_type TEXT,
            language TEXT,
            metadata_json TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            chunking_json TEXT,
            scan_injection_json TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected', 'committed')),
            committed_document_id TEXT UNIQUE
                REFERENCES documents(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX candidates_status_idx ON candidates(status, updated_at);
        CREATE INDEX candidates_collection_idx ON candidates(collection, status);
        """,
    ),
    (
        4,
        """
        ALTER TABLE documents ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;

        ALTER TABLE candidates ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;

        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT,
            revision INTEGER NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX audit_events_entity_idx
            ON audit_events(entity_type, entity_id, id);
        """,
    ),
    (
        5,
        """
        ALTER TABLE documents ADD COLUMN topic_key TEXT;
        ALTER TABLE documents ADD COLUMN content_hash TEXT;
        ALTER TABLE candidates ADD COLUMN topic_key TEXT;
        ALTER TABLE candidates ADD COLUMN content_hash TEXT;

        CREATE UNIQUE INDEX candidates_topic_hash_unique_idx
            ON candidates(topic_key, content_hash)
            WHERE topic_key IS NOT NULL AND content_hash IS NOT NULL;
        CREATE UNIQUE INDEX documents_topic_hash_unique_idx
            ON documents(topic_key, content_hash)
            WHERE topic_key IS NOT NULL AND content_hash IS NOT NULL;
        CREATE INDEX candidates_topic_key_idx ON candidates(topic_key, status);
        CREATE INDEX documents_topic_key_idx ON documents(topic_key, status);
        """,
    ),
)


def _vec_supported() -> bool:
    """True when the sqlite-vec native extension can be loaded."""
    return sqlite_vec is not None


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    for attempt in range(8):
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            break
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() or attempt == 7:
                connection.close()
                raise
            time.sleep(0.05 * (attempt + 1))
    if sqlite_vec is not None:
        try:
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
        except Exception as error:  # pragma: no cover - defensive
            logger.warning("sqlite-vec load failed: %s", error)
    return connection


def _execute_migration(connection: sqlite3.Connection, statement: str) -> None:
    """Execute a trusted migration script without ``executescript`` auto-commits."""
    pending = ""
    for line in statement.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            sql = pending.strip()
            if sql:
                connection.execute(sql)
            pending = ""
    if pending.strip():
        raise sqlite3.OperationalError("incomplete migration statement")


def initialize_database(db_path: Path, *, vector_dim: int = 1024) -> None:
    """Apply each ordered migration exactly once."""
    if isinstance(vector_dim, bool) or not isinstance(vector_dim, int) or vector_dim < 1:
        raise ValueError("vector_dim must be a positive integer")
    with _connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.commit()
        for version, statement in MIGRATIONS:
            connection.execute("BEGIN IMMEDIATE")
            try:
                applied = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone()
                if applied is None:
                    _execute_migration(connection, statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (version, _utc_now()),
                    )
                    logger.info("Applied database migration %s", version)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        _backfill_content_hashes(connection)
        _ensure_vec_table(connection, vector_dim)


def _backfill_content_hashes(connection: sqlite3.Connection) -> None:
    """Fill hashes for documents created before topic-key support."""
    for table in ("documents", "candidates"):
        rows = connection.execute(
            f"SELECT id, content FROM {table} WHERE content_hash IS NULL"
        ).fetchall()
        connection.executemany(
            f"UPDATE {table} SET content_hash = ? WHERE id = ?",
            [(_content_hash(row["content"]), row["id"]) for row in rows],
        )
    connection.commit()


def _ensure_vec_table(connection: sqlite3.Connection, vector_dim: int) -> None:
    """Best-effort creation of the vector table (needs sqlite-vec extension)."""
    if not _vec_supported():
        logger.warning(
            "sqlite-vec unavailable; vector retrieval disabled (FTS5 still works)"
        )
        return
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec "
            f"USING vec0(embedding float[{vector_dim}])"
        )
    except Exception as error:  # pragma: no cover - extension-specific failure
        logger.warning("chunk_vec creation failed: %s", error)


def init_database_from_env() -> None:
    """Console-script entry point."""
    settings = Settings.from_env()
    initialize_database(settings.db_path, vector_dim=settings.embed_dim)
    print(f"Initialised Fufu Archive database at {settings.db_path}")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _resolve_expected_revision(
    expected_revision: int | None, revision: int | None
) -> int | None:
    if expected_revision is not None and revision is not None and expected_revision != revision:
        raise ValueError("expected_revision and revision must match")
    resolved = expected_revision if expected_revision is not None else revision
    if resolved is not None and (
        isinstance(resolved, bool) or not isinstance(resolved, int) or resolved < 0
    ):
        raise ValueError("revision must be a non-negative integer")
    return resolved


def _rrf_fuse(
    ranked_lists: list[list[tuple[int, float]]], k: int = 60
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion over chunk-id rankings.

    Each input list holds ``(chunk_id, score)`` in rank order; scores are
    ignored for fusion (rank only). The returned score is the fused RRF score
    exposed by hybrid search, so it stays consistent with the returned order.
    """
    fused: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, (chunk_id, _score) in enumerate(ranked, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda item: item[1], reverse=True)


class KnowledgeBase:
    """Persistence facade for documents and their retrievable child chunks."""

    def __init__(self, db_path: Path, embedder: Embedder | None = None) -> None:
        self.db_path = db_path
        self.embedder = embedder

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = _connect(self.db_path)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _insert_document(
        self,
        connection: sqlite3.Connection,
        *,
        content: str,
        collection: str,
        domain: str | None,
        status: str,
        parent_id: str | None,
        supersedes: str | None,
        source: str | None,
        metadata: dict[str, Any],
        title: str | None,
        content_type: str | None,
        language: str | None,
        topic_key: str | None,
        document_tags: list[str],
        chunk_contents: list[str],
        injection_scan: dict[str, Any] | None,
        document_id: str | None = None,
    ) -> tuple[str, list[tuple[int, str]]]:
        """Insert a validated document and all of its searchable records."""
        document_id = document_id or str(uuid.uuid4())
        created_at = _utc_now()
        connection.execute(
            """
            INSERT INTO documents(
                id, content, collection, domain, status, parent_id, supersedes,
                source, created_at, updated_at, title, content_type, language,
                topic_key, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id, content, collection, domain, status, parent_id,
                supersedes, source, created_at, created_at, title, content_type,
                language, topic_key, _content_hash(content),
            ),
        )
        parent_context_id: int | None = None
        if len(chunk_contents) > 1:
            parent_cursor = connection.execute(
                "INSERT INTO parent_contexts(document_id, created_at) VALUES (?, ?)",
                (document_id, created_at),
            )
            parent_context_id = parent_cursor.lastrowid

        indexed_chunks: list[tuple[int, str]] = []
        for chunk_index, chunk_content in enumerate(chunk_contents):
            cursor = connection.execute(
                """
                INSERT INTO chunks(
                    document_id, chunk_index, content, collection, domain, status,
                    parent_id, supersedes, created_at, parent_context_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id, chunk_index, chunk_content, collection, domain,
                    status, parent_id, supersedes, created_at, parent_context_id,
                ),
            )
            chunk_id = cursor.lastrowid
            if chunk_id is None:  # pragma: no cover - SQLite always supplies it
                raise RuntimeError("SQLite did not return a chunk id")
            connection.execute(
                "INSERT INTO chunks_fts(chunk_id, document_id, indexed_text) "
                "VALUES (?, ?, ?)",
                (str(chunk_id), document_id, fts_document_text(chunk_content)),
            )
            indexed_chunks.append((chunk_id, chunk_content))
        for key, value in metadata.items():
            connection.execute(
                "INSERT INTO metadata(document_id, key, value_json, created_at) VALUES (?, ?, ?, ?)",
                (document_id, str(key), json.dumps(value, ensure_ascii=False), created_at),
            )
        if injection_scan is not None:
            connection.execute(
                "INSERT OR REPLACE INTO metadata"
                "(document_id, key, value_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    document_id,
                    INJECTION_SCAN_METADATA_KEY,
                    json.dumps(injection_scan, ensure_ascii=False),
                    created_at,
                ),
            )
        for tag_index, tag in enumerate(document_tags):
            connection.execute(
                "INSERT INTO document_tags(document_id, tag_index, value) "
                "VALUES (?, ?, ?)",
                (document_id, tag_index, tag),
            )
        return document_id, indexed_chunks

    def ingest(
        self,
        *,
        content: str,
        collection: str,
        domain: str | None,
        status: str,
        parent_id: str | None,
        supersedes: str | None,
        source: str | None,
        metadata: dict[str, Any],
        topic_key: str | None = None,
        title: str | None = None,
        content_type: str | None = None,
        language: str | None = None,
        tags: list[str] | None = None,
        chunking: dict[str, Any] | None = None,
        scan_injection: bool = True,
    ) -> tuple[str, int]:
        validated = IngestRequest(
            content=content,
            collection=collection,
            domain=domain,
            status=status,
            parent_id=parent_id,
            supersedes=supersedes,
            source=source,
            title=title,
            content_type=content_type,
            language=language,
            topic_key=topic_key,
            tags=[] if tags is None else tags,
            metadata=metadata,
            chunking=chunking,
            scan_injection=scan_injection,
        )
        values = validated.model_dump()
        content = values["content"]
        collection = values["collection"]
        domain = values["domain"]
        status = values["status"]
        parent_id = values["parent_id"]
        supersedes = values["supersedes"]
        source = values["source"]
        title = values["title"]
        content_type = values["content_type"]
        language = values["language"]
        topic_key = values["topic_key"]
        document_tags = values["tags"]
        metadata = values["metadata"]
        chunking = values["chunking"]
        scan_injection = values["scan_injection"]

        chunk_contents = [content]
        if chunking is not None and chunking.get("enabled", True):
            chunk_contents = chunk_text(
                content,
                target_size=chunking.get("target_size", 1_000),
                overlap=chunking.get("overlap", 0),
            )
        if not chunk_contents:
            raise ValueError("ingest must produce at least one nonempty chunk")

        injection_scan = scan_injection_text(content) if scan_injection else None
        content_hash = _content_hash(content)
        try:
            with self._connection() as connection:
                if topic_key is not None:
                    existing = connection.execute(
                        "SELECT id FROM documents WHERE topic_key = ? AND content_hash = ?",
                        (topic_key, content_hash),
                    ).fetchone()
                    if existing is not None:
                        count = connection.execute(
                            "SELECT count(*) FROM chunks WHERE document_id = ?",
                            (existing["id"],),
                        ).fetchone()[0]
                        return existing["id"], int(count)
                document_id, indexed_chunks = self._insert_document(
                    connection,
                    content=content,
                    collection=collection,
                    domain=domain,
                    status=status,
                    parent_id=parent_id,
                    supersedes=supersedes,
                    source=source,
                    metadata=metadata,
                    title=title,
                    content_type=content_type,
                    language=language,
                    topic_key=topic_key,
                    document_tags=document_tags,
                    chunk_contents=chunk_contents,
                    injection_scan=injection_scan,
                )
        except sqlite3.IntegrityError:
            if topic_key is None:
                raise
            with self._connection() as connection:
                existing = connection.execute(
                    "SELECT id FROM documents WHERE topic_key = ? AND content_hash = ?",
                    (topic_key, content_hash),
                ).fetchone()
                if existing is None:
                    raise
                count = connection.execute(
                    "SELECT count(*) FROM chunks WHERE document_id = ?",
                    (existing["id"],),
                ).fetchone()[0]
            return existing["id"], int(count)
        if self.embedder is not None and indexed_chunks:
            self._index_vectors(indexed_chunks, document_id)
        logger.info(
            "Ingested document %s as %s chunk(s) into collection %s",
            document_id,
            len(chunk_contents),
            collection,
        )
        return document_id, len(chunk_contents)

    def _candidate_public(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["candidate_id"] = result.pop("id")
        result["metadata"] = json.loads(result.pop("metadata_json"))
        result["tags"] = json.loads(result.pop("tags_json"))
        chunking_json = result.pop("chunking_json")
        result["chunking"] = None if chunking_json is None else json.loads(chunking_json)
        scan_json = result.pop("scan_injection_json")
        result["scan_injection"] = None if scan_json is None else json.loads(scan_json)
        return result

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        """Return one candidate, including its review payload."""
        if not candidate_id:
            raise ValueError("candidate_id must not be empty")
        with self._connection() as connection:
            row = connection.execute(
                """SELECT id, content, collection, domain, source, title, content_type,
                        language, topic_key, content_hash, metadata_json, tags_json, chunking_json,
                        scan_injection_json, status, committed_document_id,
                        created_at, updated_at, revision
                 FROM candidates WHERE id = ?""",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"candidate not found: {candidate_id}")
        return self._candidate_public(row)

    def create_candidate(
        self,
        *,
        content: str,
        collection: str = "default",
        domain: str | None = None,
        source: str | None = None,
        title: str | None = None,
        content_type: str | None = None,
        language: str | None = None,
        topic_key: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        chunking: dict[str, Any] | None = None,
        scan_injection: bool = True,
    ) -> dict[str, Any]:
        """Create an unsearchable candidate in the pending state."""
        validated = IngestRequest(
            content=content,
            collection=collection,
            domain=domain,
            status="pending",
            source=source,
            title=title,
            content_type=content_type,
            language=language,
            topic_key=topic_key,
            tags=[] if tags is None else tags,
            metadata={} if metadata is None else metadata,
            chunking=chunking,
            scan_injection=scan_injection,
        )
        values = validated.model_dump()
        candidate_id = str(uuid.uuid4())
        created_at = _utc_now()
        scan = scan_injection_text(values["content"]) if values["scan_injection"] else None
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            content_hash = _content_hash(values["content"])
            if values["topic_key"] is not None:
                existing = connection.execute(
                    """SELECT id, content, collection, domain, source, title, content_type,
                              language, topic_key, content_hash, metadata_json, tags_json,
                              chunking_json, scan_injection_json, status,
                              committed_document_id, created_at, updated_at, revision
                       FROM candidates WHERE topic_key = ? AND content_hash = ?""",
                    (values["topic_key"], content_hash),
                ).fetchone()
                if existing is not None:
                    return self._candidate_public(existing)
            connection.execute(
                """INSERT INTO candidates(
                    id, content, collection, domain, source, title, content_type,
                    language, topic_key, content_hash, metadata_json, tags_json,
                    chunking_json, scan_injection_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    candidate_id, values["content"], values["collection"],
                    values["domain"], values["source"], values["title"],
                    values["content_type"], values["language"], values["topic_key"],
                    content_hash,
                    json.dumps(values["metadata"], ensure_ascii=False),
                    json.dumps(values["tags"], ensure_ascii=False),
                    None if values["chunking"] is None else json.dumps(
                        values["chunking"], ensure_ascii=False
                    ),
                    None if scan is None else json.dumps(scan, ensure_ascii=False),
                    created_at, created_at,
                ),
            )
            self._record_audit(
                connection,
                entity_type="candidate",
                entity_id=candidate_id,
                event_type="candidate.created",
                from_status=None,
                to_status="pending",
                revision=0,
            )
        return self.get_candidate(candidate_id)

    def list_candidates(
        self,
        *,
        status: str | None = "pending",
        collection: str | None = None,
        topic_key: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List candidates for review, defaulting to pending items."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        conditions: list[str] = []
        parameters: list[Any] = []
        if status is not None:
            conditions.append("status = ?")
            parameters.append(status)
        if collection is not None:
            conditions.append("collection = ?")
            parameters.append(collection)
        if topic_key is not None:
            conditions.append("topic_key = ?")
            parameters.append(topic_key)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT id, content, collection, domain, source, title, content_type,
                       language, topic_key, content_hash, metadata_json, tags_json, chunking_json,
                       scan_injection_json, status, committed_document_id,
                       created_at, updated_at, revision
                FROM candidates {where}
                ORDER BY updated_at ASC, id ASC LIMIT ?""",
                [*parameters, limit],
            ).fetchall()
        return [self._candidate_public(row) for row in rows]

    def _record_audit(
        self,
        connection: sqlite3.Connection,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        revision: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO audit_events(
                entity_type, entity_id, event_type, from_status, to_status,
                revision, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entity_type, entity_id, event_type, from_status, to_status,
                revision, json.dumps(details or {}, ensure_ascii=False), _utc_now(),
            ),
        )

    def list_audit_events(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        candidate_id: str | None = None,
        document_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return audit events in durable insertion order."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("limit must be an integer between 1 and 1000")
        aliases = [value for value in (candidate_id, document_id) if value is not None]
        if len(aliases) > 1:
            raise ValueError("candidate_id and document_id are mutually exclusive")
        if aliases:
            entity_type = "candidate" if candidate_id is not None else "document"
            entity_id = aliases[0]
        conditions: list[str] = []
        parameters: list[Any] = []
        if entity_type is not None:
            conditions.append("entity_type = ?")
            parameters.append(entity_type)
        if entity_id is not None:
            conditions.append("entity_id = ?")
            parameters.append(entity_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT id, entity_type, entity_id, event_type, from_status,
                           to_status, revision, details_json, created_at
                    FROM audit_events {where} ORDER BY id ASC LIMIT ?""",
                [*parameters, limit],
            ).fetchall()
        events = [dict(row) for row in rows]
        for event in events:
            event["event_id"] = event.pop("id")
            event["details"] = json.loads(event.pop("details_json"))
        return events

    def _transition_candidate(
        self,
        candidate_id: str,
        target_status: str,
        *,
        expected_revision: int | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        if not candidate_id:
            raise ValueError("candidate_id must not be empty")
        expected_revision = _resolve_expected_revision(expected_revision, revision)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, revision FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"candidate not found: {candidate_id}")
            if expected_revision is not None and row["revision"] != expected_revision:
                raise RevisionConflict(
                    f"revision conflict for candidate {candidate_id}: "
                    f"expected {expected_revision}, current {row['revision']}"
                )
            if row["status"] != "pending":
                raise ValueError(
                    f"cannot transition candidate from {row['status']!r} to {target_status!r}"
                )
            revision = row["revision"] + 1
            connection.execute(
                "UPDATE candidates SET status = ?, revision = ?, updated_at = ? WHERE id = ?",
                (target_status, revision, _utc_now(), candidate_id),
            )
            self._record_audit(
                connection,
                entity_type="candidate",
                entity_id=candidate_id,
                event_type=f"candidate.{target_status}",
                from_status="pending",
                to_status=target_status,
                revision=revision,
            )
        return self.get_candidate(candidate_id)

    def approve_candidate(
        self,
        candidate_id: str,
        *,
        expected_revision: int | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Move a pending candidate to approved."""
        return self._transition_candidate(
            candidate_id, "approved", expected_revision=expected_revision,
            revision=revision,
        )

    def reject_candidate(
        self,
        candidate_id: str,
        *,
        expected_revision: int | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Move a pending candidate to rejected."""
        return self._transition_candidate(
            candidate_id, "rejected", expected_revision=expected_revision,
            revision=revision,
        )

    def commit_candidate(
        self,
        candidate_id: str,
        *,
        expected_revision: int | None = None,
        revision: int | None = None,
        supersedes_document_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically publish an approved candidate to the formal knowledge base."""
        if not candidate_id:
            raise ValueError("candidate_id must not be empty")
        expected_revision = _resolve_expected_revision(expected_revision, revision)
        indexed_chunks: list[tuple[int, str]] = []
        with self._connection() as connection:
            # Serialize the read-check-write sequence so concurrent commits
            # cannot both publish the same approved candidate.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"candidate not found: {candidate_id}")
            if expected_revision is not None and row["revision"] != expected_revision:
                raise RevisionConflict(
                    f"revision conflict for candidate {candidate_id}: "
                    f"expected {expected_revision}, current {row['revision']}"
                )
            if row["status"] == "committed":
                return {
                    "candidate_id": candidate_id,
                    "status": "committed",
                    "document_id": row["committed_document_id"],
                    "revision": row["revision"],
                }
            if row["status"] != "approved":
                raise ValueError(
                    f"only approved candidates can be committed, got {row['status']!r}"
                )
            if supersedes_document_id is not None:
                supersedes_row = connection.execute(
                    "SELECT id FROM documents WHERE id = ?",
                    (supersedes_document_id,),
                ).fetchone()
                if supersedes_row is None:
                    raise KeyError(f"document not found: {supersedes_document_id}")
            content_hash = row["content_hash"] or _content_hash(row["content"])
            existing_document = None
            if row["topic_key"] is not None:
                existing_document = connection.execute(
                    """SELECT id FROM documents
                       WHERE topic_key = ? AND content_hash = ?""",
                    (row["topic_key"], content_hash),
                ).fetchone()
            if existing_document is not None:
                new_revision = row["revision"] + 1
                connection.execute(
                    """UPDATE candidates
                       SET status = 'committed', committed_document_id = ?,
                           revision = ?, updated_at = ?
                       WHERE id = ? AND status = 'approved'""",
                    (existing_document["id"], new_revision, _utc_now(), candidate_id),
                )
                self._record_audit(
                    connection,
                    entity_type="candidate",
                    entity_id=candidate_id,
                    event_type="candidate.committed",
                    from_status="approved",
                    to_status="committed",
                    revision=new_revision,
                    details={
                        "document_id": existing_document["id"],
                        "topic_key": row["topic_key"],
                        "content_hash": content_hash,
                        "supersedes_document_id": supersedes_document_id,
                    },
                )
                return {
                    "candidate_id": candidate_id,
                    "status": "committed",
                    "document_id": existing_document["id"],
                    "revision": new_revision,
                }
            metadata = json.loads(row["metadata_json"])
            tags = json.loads(row["tags_json"])
            chunking = None if row["chunking_json"] is None else json.loads(row["chunking_json"])
            chunk_contents = [row["content"]]
            if chunking is not None and chunking.get("enabled", True):
                chunk_contents = chunk_text(
                    row["content"],
                    target_size=chunking.get("target_size", 1_000),
                    overlap=chunking.get("overlap", 0),
                )
            if not chunk_contents:
                raise ValueError("candidate must produce at least one nonempty chunk")
            injection_scan = (
                None if row["scan_injection_json"] is None
                else json.loads(row["scan_injection_json"])
            )
            document_id, indexed_chunks = self._insert_document(
                connection,
                content=row["content"], collection=row["collection"],
                domain=row["domain"], status="active", parent_id=None,
                supersedes=supersedes_document_id, source=row["source"], metadata=metadata,
                title=row["title"], content_type=row["content_type"],
                language=row["language"], topic_key=row["topic_key"], document_tags=tags,
                chunk_contents=chunk_contents, injection_scan=injection_scan,
            )
            document_revision = 0
            self._record_audit(
                connection,
                entity_type="document",
                entity_id=document_id,
                event_type="document.created",
                from_status=None,
                to_status="active",
                revision=document_revision,
                details={
                    "candidate_id": candidate_id,
                    "topic_key": row["topic_key"],
                    "content_hash": content_hash,
                    "supersedes_document_id": supersedes_document_id,
                },
            )
            connection.execute(
                """UPDATE candidates
                SET status = 'committed', committed_document_id = ?,
                    revision = ?, updated_at = ?
                WHERE id = ? AND status = 'approved'""",
                (document_id, row["revision"] + 1, _utc_now(), candidate_id),
            )
            self._record_audit(
                connection,
                entity_type="candidate",
                entity_id=candidate_id,
                event_type="candidate.committed",
                from_status="approved",
                to_status="committed",
                revision=row["revision"] + 1,
                details={
                    "document_id": document_id,
                    "topic_key": row["topic_key"],
                    "content_hash": content_hash,
                    "supersedes_document_id": supersedes_document_id,
                },
            )
        if self.embedder is not None and indexed_chunks:
            self._index_vectors(indexed_chunks, document_id)
        return {
            "candidate_id": candidate_id,
            "status": "committed",
            "document_id": document_id,
            "revision": row["revision"] + 1,
        }

    def list_documents(
        self,
        *,
        collection: str | None = None,
        domain: str | None = None,
        topic_key: str | None = None,
        status: str | None = "active",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List document summaries for management integrations."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        conditions: list[str] = []
        parameters: list[Any] = []
        if collection is not None:
            conditions.append("collection = ?")
            parameters.append(collection)
        if domain is not None:
            conditions.append("domain = ?")
            parameters.append(domain)
        if topic_key is not None:
            conditions.append("topic_key = ?")
            parameters.append(topic_key)
        if status is not None:
            conditions.append("status = ?")
            parameters.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT id, title, collection, domain, status, supersedes, source,
                       topic_key, content_hash, revision, created_at,
                       updated_at, length(content) AS content_length,
                       (SELECT count(*) FROM chunks WHERE document_id = documents.id)
                           AS chunk_count
                FROM documents
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                [*parameters, limit],
            ).fetchall()
        return [dict(row) for row in rows]

    def update_document(
        self,
        document_id: str,
        *,
        status: str | None = None,
        source: str | None = None,
        title: str | None = None,
        supersedes: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        expected_revision: int | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Update lifecycle and descriptive fields without rewriting content."""
        if not document_id:
            raise ValueError("document_id must not be empty")
        expected_revision = _resolve_expected_revision(expected_revision, revision)
        if status is None and source is None and title is None and supersedes is None and metadata is None and tags is None:
            raise ValueError("at least one update field is required")
        if tags is not None:
            if len(tags) > 100 or any(
                not isinstance(tag, str) or not 1 <= len(tag) <= 100 for tag in tags
            ):
                raise ValueError("tags must contain at most 100 strings of 1-100 characters")
        if metadata is not None:
            metadata = {
                key: value
                for key, value in metadata.items()
                if key != INJECTION_SCAN_METADATA_KEY
            }
            try:
                json.dumps(metadata, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
                raise ValueError("metadata must contain safe JSON values") from error
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id, revision FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if existing is None:
                raise KeyError(f"document not found: {document_id}")
            if expected_revision is not None and existing["revision"] != expected_revision:
                raise RevisionConflict(
                    f"revision conflict for document {document_id}: "
                    f"expected {expected_revision}, current {existing['revision']}"
                )
            assignments: list[str] = []
            parameters: list[Any] = []
            for column, value in (
                ("status", status),
                ("source", source),
                ("title", title),
                ("supersedes", supersedes),
            ):
                if value is not None:
                    assignments.append(f"{column} = ?")
                    parameters.append(value)
            assignments.extend(["revision = revision + 1", "updated_at = ?"])
            parameters.extend([_utc_now(), document_id])
            connection.execute(
                f"UPDATE documents SET {', '.join(assignments)} WHERE id = ?",
                parameters,
            )
            if status is not None:
                connection.execute(
                    "UPDATE chunks SET status = ? WHERE document_id = ?",
                    (status, document_id),
                )
            if metadata is not None:
                connection.execute("DELETE FROM metadata WHERE document_id = ?", (document_id,))
                created_at = _utc_now()
                for key, value in metadata.items():
                    connection.execute(
                        "INSERT INTO metadata(document_id, key, value_json, created_at) VALUES (?, ?, ?, ?)",
                        (document_id, str(key), json.dumps(value, ensure_ascii=False), created_at),
                    )
            if tags is not None:
                connection.execute("DELETE FROM document_tags WHERE document_id = ?", (document_id,))
                connection.executemany(
                    "INSERT INTO document_tags(document_id, tag_index, value) VALUES (?, ?, ?)",
                    [(document_id, index, tag) for index, tag in enumerate(tags)],
                )
            row = connection.execute(
                "SELECT id, title, collection, domain, status, source, supersedes, revision, updated_at FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
        return dict(row)

    def _index_vectors(
        self,
        chunks: list[tuple[int, str]],
        document_id: str,
    ) -> None:
        """Embed committed chunks without holding a database transaction."""
        if not _vec_supported():
            return
        try:
            vectors = self.embedder.embed_documents(
                [content for _chunk_id, content in chunks]
            )
            if len(vectors) != len(chunks):
                raise ValueError("embedder returned the wrong number of vectors")
            with self._connection() as connection:
                connection.executemany(
                    "INSERT INTO chunk_vec(rowid, embedding) VALUES (?, ?)",
                    [
                        (chunk_id, json.dumps(vector))
                        for (chunk_id, _content), vector in zip(
                            chunks, vectors, strict=True
                        )
                    ],
                )
        except Exception as error:  # network/model failure: keep FTS copy
            logger.warning(
                "vector indexing failed for document %s: %s",
                document_id, error,
            )

    def search(
        self,
        *,
        query: str,
        collection: str | None,
        domain: str | None,
        limit: int,
        topic_key: str | None = None,
        mode: str = "dense",
    ) -> list[dict[str, Any]]:
        """Retrieve chunks. mode: 'fts5' | 'dense' | 'hybrid'.

        dense/hybrid fall back to fts5 when no embedder is configured or the
        embedding backend is unreachable, so search never hard-fails.
        """
        if mode == "fts5":
            return self._search_fts(query, collection, domain, limit, topic_key)
        if mode in ("dense", "hybrid"):
            if self.embedder is None:
                logger.warning("no embedder configured; falling back to fts5")
                return self._search_fts(query, collection, domain, limit, topic_key)
            try:
                if mode == "dense":
                    rows = self._search_dense(query, collection, domain, limit, topic_key)
                    if rows:
                        return rows
                    logger.warning("dense index returned no rows; falling back to fts5")
                    return self._search_fts(query, collection, domain, limit, topic_key)
                return self._search_hybrid(query, collection, domain, limit, topic_key)
            except Exception as error:  # backend down / empty index
                logger.warning(
                    "%s search failed (%s); falling back to fts5", mode, error
                )
                return self._search_fts(query, collection, domain, limit, topic_key)
        raise ValueError(f"unknown search mode: {mode!r}")

    def _search_fts(
        self,
        query: str,
        collection: str | None,
        domain: str | None,
        limit: int,
        topic_key: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return self._search_fts_rows(
                connection, query, collection, domain, limit, topic_key
            )

    def _search_fts_rows(
        self,
        connection: sqlite3.Connection,
        query: str,
        collection: str | None,
        domain: str | None,
        limit: int,
        topic_key: str | None = None,
    ) -> list[dict[str, Any]]:
        match_query = safe_match_query(query)
        if match_query is None:
            return []
        conditions = ["chunks_fts MATCH ?", "c.status = ?"]
        parameters: list[Any] = [match_query, "active"]
        if collection is not None:
            conditions.append("c.collection = ?")
            parameters.append(collection)
        if domain is not None:
            conditions.append("c.domain = ?")
            parameters.append(domain)
        if topic_key is not None:
            conditions.append("d.topic_key = ?")
            parameters.append(topic_key)
        parameters.append(limit)
        sql = f"""
            SELECT c.document_id, c.id AS chunk_id, c.content, c.collection, c.domain,
                   bm25(chunks_fts) AS score
            FROM chunks_fts
            INNER JOIN chunks AS c ON c.id = CAST(chunks_fts.chunk_id AS INTEGER)
            INNER JOIN documents AS d ON d.id = c.document_id
            WHERE {' AND '.join(conditions)}
            ORDER BY score ASC, c.id ASC
            LIMIT ?
        """
        rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]

    def _vec_rows(
        self,
        connection: sqlite3.Connection,
        vector: list[float],
        limit: int,
    ) -> list[tuple[int, float]]:
        """Return (chunk_id, distance) nearest neighbours for an embedded query."""
        rows = connection.execute(
            "SELECT rowid, distance FROM chunk_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (json.dumps(vector), limit),
        ).fetchall()
        return [(row["rowid"], float(row["distance"])) for row in rows]

    def _filtered_neighbours(
        self,
        connection: sqlite3.Connection,
        vector: list[float],
        collection: str | None,
        domain: str | None,
        limit: int,
        topic_key: str | None = None,
    ) -> tuple[list[tuple[int, float]], list[dict[str, Any]]]:
        """KNN-search progressively until post-filters can fill ``limit`` rows."""
        conditions = ["c.status = ?"]
        parameters: list[Any] = ["active"]
        if collection is not None:
            conditions.append("c.collection = ?")
            parameters.append(collection)
        if domain is not None:
            conditions.append("c.domain = ?")
            parameters.append(domain)
        if topic_key is not None:
            conditions.append("d.topic_key = ?")
            parameters.append(topic_key)
        matching_ids = {
            int(row["id"])
            for row in connection.execute(
                f"""SELECT c.id FROM chunks AS c
                    INNER JOIN documents AS d ON d.id = c.document_id
                    WHERE {' AND '.join(conditions)}""", parameters
            )
        }
        if not matching_ids:
            return [], []

        total = int(connection.execute("SELECT count(*) FROM chunk_vec").fetchone()[0])
        if total == 0:
            return [], []

        candidate_limit = min(total, max(limit * 3, 1))
        while True:
            neighbours = self._vec_rows(connection, vector, candidate_limit)
            filtered = [item for item in neighbours if item[0] in matching_ids][:limit]
            if len(filtered) >= limit or candidate_limit >= total:
                rows = self._rows_for_chunks(
                    connection,
                    [chunk_id for chunk_id, _ in filtered],
                    collection,
                    domain,
                    topic_key,
                )
                return filtered, rows
            candidate_limit = min(total, candidate_limit * 2)

    def _rows_for_chunks(
        self,
        connection: sqlite3.Connection,
        chunk_ids: list[int],
        collection: str | None,
        domain: str | None,
        topic_key: str | None = None,
    ) -> list[dict[str, Any]]:
        if not chunk_ids:
            return []
        placeholders = ",".join("?" * len(chunk_ids))
        conditions = [f"c.id IN ({placeholders})", "c.status = ?"]
        parameters: list[Any] = [*chunk_ids, "active"]
        if collection is not None:
            conditions.append("c.collection = ?")
            parameters.append(collection)
        if domain is not None:
            conditions.append("c.domain = ?")
            parameters.append(domain)
        if topic_key is not None:
            conditions.append("d.topic_key = ?")
            parameters.append(topic_key)
        sql = f"""
            SELECT c.document_id, c.id AS chunk_id, c.content, c.collection, c.domain,
                   0.0 AS score
            FROM chunks AS c
            INNER JOIN documents AS d ON d.id = c.document_id
            WHERE {' AND '.join(conditions)}
        """
        rows = connection.execute(sql, parameters).fetchall()
        by_id = {row["chunk_id"]: dict(row) for row in rows}
        # Preserve rank order from the fused list; score filled below by caller.
        return [by_id[cid] for cid in chunk_ids if cid in by_id]

    def _search_dense(
        self,
        query: str,
        collection: str | None,
        domain: str | None,
        limit: int,
        topic_key: str | None = None,
    ) -> list[dict[str, Any]]:
        if self.embedder is None:
            raise RuntimeError("no embedder configured")
        vector = self.embedder.embed_query(query)
        with self._connection() as connection:
            neighbours, rows = self._filtered_neighbours(
                connection, vector, collection, domain, limit, topic_key
            )
        # Distance is a similarity distance: smaller = closer, expose as score.
        dist_by_id = {cid: dist for cid, dist in neighbours}
        for row in rows:
            row["score"] = -dist_by_id.get(row["chunk_id"], 0.0)
        return rows

    def _search_hybrid(
        self,
        query: str,
        collection: str | None,
        domain: str | None,
        limit: int,
        topic_key: str | None = None,
    ) -> list[dict[str, Any]]:
        if self.embedder is None:
            raise RuntimeError("no embedder configured")
        vector = self.embedder.embed_query(query)
        rank_limit = limit * 3
        with self._connection() as connection:
            neighbours, _dense_rows = self._filtered_neighbours(
                connection, vector, collection, domain, rank_limit, topic_key
            )
            fts_rows = self._search_fts_rows(
                connection, query, collection, domain, rank_limit, topic_key
            )
            fused = _rrf_fuse(
                [
                    [(row["chunk_id"], 0.0) for row in fts_rows],
                    neighbours,
                ]
            )[:limit]
            fused_ids = [chunk_id for chunk_id, _ in fused]
            rows = self._rows_for_chunks(
                connection, fused_ids, collection, domain, topic_key
            )
        score_by_id = dict(fused)
        for row in rows:
            row["score"] = score_by_id[row["chunk_id"]]
        return rows
