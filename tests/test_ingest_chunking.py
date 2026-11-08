from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fufu_archive.db import KnowledgeBase, initialize_database

from fake_embedder import FakeEmbedder


def _make_kb(tmp_path: Path, embedder=None) -> tuple[KnowledgeBase, Path]:
    db_path = tmp_path / "chunked.db"
    initialize_database(db_path)
    return KnowledgeBase(db_path, embedder=embedder), db_path


def _ingest(kb: KnowledgeBase, content: str, **overrides):
    values = {
        "content": content,
        "collection": "notes",
        "domain": "memory",
        "status": "active",
        "parent_id": "lifecycle-parent",
        "supersedes": "older-document",
        "source": "unit-test",
        "metadata": {"arbitrary": {"nested": True}},
    }
    values.update(overrides)
    return kb.ingest(**values)


def test_legacy_ingest_still_creates_one_chunk_without_parent_context(
    tmp_path: Path,
) -> None:
    kb, db_path = _make_kb(tmp_path)

    document_id, chunk_count = _ingest(kb, "legacy content remains one chunk")

    with sqlite3.connect(db_path) as connection:
        document = connection.execute(
            "SELECT content FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        chunks = connection.execute(
            "SELECT chunk_index, content, parent_context_id FROM chunks "
            "WHERE document_id = ?",
            (document_id,),
        ).fetchall()
        parent_count = connection.execute(
            "SELECT count(*) FROM parent_contexts WHERE document_id = ?",
            (document_id,),
        ).fetchone()[0]

    assert chunk_count == 1
    assert document == ("legacy content remains one chunk",)
    assert chunks == [(0, "legacy content remains one chunk", None)]
    assert parent_count == 0


def test_enabled_chunking_persists_children_parent_link_and_document_metadata(
    tmp_path: Path,
) -> None:
    kb, db_path = _make_kb(tmp_path)
    content = (
        "第一段包含一些中文内容，用来验证文档切块。\n\n"
        "Second paragraph contains enough English text for another child chunk."
    )

    document_id, chunk_count = _ingest(
        kb,
        content,
        title="中英测试文档",
        content_type="text/plain",
        language="zh-CN",
        tags=["memory", "中英"],
        chunking={"enabled": True, "target_size": 28, "overlap": 4},
    )

    with sqlite3.connect(db_path) as connection:
        document = connection.execute(
            "SELECT content, title, content_type, language FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        children = connection.execute(
            "SELECT chunk_index, content, collection, domain, status, parent_id, "
            "supersedes, parent_context_id FROM chunks WHERE document_id = ? "
            "ORDER BY chunk_index",
            (document_id,),
        ).fetchall()
        parent_rows = connection.execute(
            "SELECT pc.id, d.content FROM parent_contexts pc "
            "JOIN documents d ON d.id = pc.document_id WHERE pc.document_id = ?",
            (document_id,),
        ).fetchall()
        tags = connection.execute(
            "SELECT value FROM document_tags WHERE document_id = ? ORDER BY tag_index",
            (document_id,),
        ).fetchall()
        arbitrary_metadata = connection.execute(
            "SELECT value_json FROM metadata WHERE document_id = ? AND key = 'arbitrary'",
            (document_id,),
        ).fetchone()[0]

    assert chunk_count == len(children) > 1
    assert document == (content, "中英测试文档", "text/plain", "zh-CN")
    assert [row[0] for row in children] == list(range(chunk_count))
    assert all(row[1].strip() for row in children)
    assert all(row[2:7] == ("notes", "memory", "active", "lifecycle-parent", "older-document") for row in children)
    assert len(parent_rows) == 1
    assert parent_rows[0][1] == content
    assert {row[7] for row in children} == {parent_rows[0][0]}
    assert tags == [("memory",), ("中英",)]
    assert arbitrary_metadata == '{"nested": true}'


def test_fts_finds_content_in_a_child_chunk(tmp_path: Path) -> None:
    kb, _ = _make_kb(tmp_path)
    content = "ordinary prefix text. " * 4 + "uniquechildneedle appears here. " + "tail text. " * 4
    document_id, chunk_count = _ingest(
        kb,
        content,
        chunking={"enabled": True, "target_size": 45, "overlap": 5},
    )

    results = kb.search(
        query="uniquechildneedle",
        collection="notes",
        domain="memory",
        limit=5,
        mode="fts5",
    )

    assert chunk_count > 1
    assert results
    assert results[0]["document_id"] == document_id
    assert "uniquechildneedle" in results[0]["content"]


class RecordingFakeEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        self.document_batches: list[list[str]] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_batches.append(list(texts))
        return super().embed_documents(texts)


def test_every_child_is_embedded_and_vector_indexed(tmp_path: Path) -> None:
    embedder = RecordingFakeEmbedder()
    kb, db_path = _make_kb(tmp_path, embedder=embedder)

    document_id, chunk_count = _ingest(
        kb,
        "vector child content without punctuation " * 8,
        chunking={"enabled": True, "target_size": 48, "overlap": 6},
    )

    with kb._connection() as connection:
        child_texts = [
            row[0]
            for row in connection.execute(
                "SELECT content FROM chunks WHERE document_id = ? ORDER BY chunk_index",
                (document_id,),
            )
        ]
        vector_count = connection.execute(
            "SELECT count(*) FROM chunk_vec v JOIN chunks c ON c.id = v.rowid "
            "WHERE c.document_id = ?",
            (document_id,),
        ).fetchone()[0]

    assert chunk_count > 1
    assert embedder.document_batches == [child_texts]
    assert vector_count == chunk_count


def test_ingest_rejects_non_string_tag_values(tmp_path: Path) -> None:
    kb, _ = _make_kb(tmp_path)

    with pytest.raises(ValueError, match="tags must contain only strings"):
        _ingest(kb, "content", tags=["valid", 3])


@pytest.mark.parametrize(
    "overrides",
    [
        {"content": " \n\t "},
        {"collection": ""},
        {"title": "x" * 501},
        {"tags": [str(index) for index in range(101)]},
        {"tags": ["x" * 101]},
        {"metadata": []},
        {"metadata": {"not_finite": float("nan")}},
        {"chunking": {"enabled": "yes"}},
        {"chunking": {"target_size": "40", "overlap": 5}},
        {"chunking": {"target_size": 1_000_001, "overlap": 0}},
        {"chunking": {"target_size": 40, "overlap": -1}},
        {"chunking": {"target_size": 40, "overlap": 40}},
        {"chunking": {"target_size": 1, "overlap": 0}},
    ],
)
def test_direct_ingest_enforces_shared_invariants_without_writing(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    kb, db_path = _make_kb(tmp_path)
    values = dict(overrides)
    content = str(values.pop("content", "valid content"))

    with pytest.raises(ValueError):
        _ingest(kb, content, **values)

    with sqlite3.connect(db_path) as connection:
        document_count = connection.execute("SELECT count(*) FROM documents").fetchone()[0]
        chunk_count = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
    assert (document_count, chunk_count) == (0, 0)
