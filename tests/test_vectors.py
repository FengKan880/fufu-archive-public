"""P1 vector retrieval tests (dense / hybrid) with a deterministic fake embedder.

The fake maps each text to a stable pseudo-vector keyed by a digest of the
text so dense KNN behaves predictably without an Ollama dependency.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fufu_archive.db import KnowledgeBase, initialize_database
from fufu_archive.embedding import Embedder

from fake_embedder import FakeEmbedder


class OrderedEmbedder:
    """Place 'near' documents before 'target' documents for filter tests."""

    dim = 1024

    @staticmethod
    def _axis(index: int) -> list[float]:
        vector = [0.0] * 1024
        vector[index] = 1.0
        return vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._axis(0 if "near" in text else 1) for text in texts]

    def embed_query(self, query: str) -> list[float]:
        return self._axis(0)


class QueryOnlyEmbedder(OrderedEmbedder):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding backend unavailable during ingest")


def _make_kb(tmp_path: Path, embedder: Embedder | None = FakeEmbedder()) -> KnowledgeBase:
    db_path = tmp_path / "vec.db"
    initialize_database(db_path)
    return KnowledgeBase(db_path, embedder=embedder)


def _ingest(kb: KnowledgeBase, content: str, collection: str = "default") -> str:
    doc_id, _ = kb.ingest(
        content=content,
        collection=collection,
        domain=None,
        status="active",
        parent_id=None,
        supersedes=None,
        source="test",
        metadata={},
    )
    return doc_id


def _ingest_with_domain(
    kb: KnowledgeBase, content: str, collection: str, domain: str
) -> str:
    doc_id, _ = kb.ingest(
        content=content,
        collection=collection,
        domain=domain,
        status="active",
        parent_id=None,
        supersedes=None,
        source="test",
        metadata={},
    )
    return doc_id


def test_dense_finds_exact_text(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path)
    _ingest(kb, "VRChat 中途切代理节点会断开 Photon 连接。")
    _ingest(kb, "混音时先找频段冲突再调 EQ。")
    kb._search_fts = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("dense test unexpectedly degraded to FTS5")
    )
    results = kb.search(
        query="VRChat 中途切代理节点会断开 Photon 连接。",
        collection=None,
        domain=None,
        limit=5,
        mode="dense",
    )
    assert results, "dense search should return at least one row"
    assert "Photon" in results[0]["content"]


def test_dense_ranks_closest_first(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path)
    doc_a = _ingest(kb, "汽包水位是锅炉的生命线。")
    _ingest(kb, "除氧器加热给水并除去溶解氧。")
    results = kb.search(
        query="汽包水位是锅炉的生命线。",
        collection=None,
        domain=None,
        limit=5,
        mode="dense",
    )
    assert results[0]["document_id"] == doc_a


def test_dense_respects_collection_filter(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path)
    _ingest(kb, "统一内容 A", collection="alpha")
    _ingest(kb, "统一内容 B", collection="beta")
    results = kb.search(
        query="统一内容 A",
        collection="beta",
        domain=None,
        limit=5,
        mode="dense",
    )
    assert all(row["collection"] == "beta" for row in results)


def test_dense_overfetches_until_collection_and_domain_filter_is_full(
    tmp_path: Path,
) -> None:
    kb = _make_kb(tmp_path, embedder=OrderedEmbedder())
    for index in range(5):
        kb.ingest(
            content=f"near alpha {index}", collection="alpha", domain="other",
            status="active", parent_id=None, supersedes=None, source="test", metadata={},
        )
    kb.ingest(
        content="near beta wrong domain", collection="beta", domain="other",
        status="active", parent_id=None, supersedes=None, source="test", metadata={},
    )
    expected = {
        _ingest_with_domain(kb, "target beta one", "beta", "wanted"),
        _ingest_with_domain(kb, "target beta two", "beta", "wanted"),
    }
    kb._search_fts = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("filtered dense search unexpectedly degraded to FTS5")
    )

    results = kb.search(
        query="anything", collection="beta", domain="wanted", limit=2, mode="dense"
    )

    assert len(results) == 2
    assert {row["document_id"] for row in results} == expected


def test_hybrid_returns_union_of_both_paths(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path)
    _ingest(kb, "电厂巡检要检查汽轮机振动和轴承温度。")
    _ingest(kb, "VRChat 角色模型常用 Blender 制作。")
    results = kb.search(
        query="电厂巡检要检查汽轮机振动和轴承温度。",
        collection=None,
        domain=None,
        limit=10,
        mode="hybrid",
    )
    assert results, "hybrid should return rows"
    assert "汽轮机" in results[0]["content"]
    assert all(row["score"] > 0 for row in results)
    assert [row["score"] for row in results] == sorted(
        (row["score"] for row in results), reverse=True
    )


def test_embedding_failure_keeps_fts_and_dense_hybrid_fall_back(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path, embedder=QueryOnlyEmbedder())
    _ingest(kb, "向量服务故障时仍然保留关键词副本。")

    for mode in ("fts5", "dense", "hybrid"):
        results = kb.search(
            query="关键词副本", collection=None, domain=None, limit=5, mode=mode
        )
        assert results, f"mode={mode} lost the committed FTS copy"


def test_empty_database_search_is_safe(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path)
    for mode in ("fts5", "dense", "hybrid"):
        assert kb.search(
            query="不存在", collection=None, domain=None, limit=5, mode=mode
        ) == []


def test_dense_without_embedder_falls_back_to_fts(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path, embedder=None)
    _ingest(kb, "Fufu Archive 提供中文知识检索。")
    # No embedder configured: dense should degrade to FTS5, not raise.
    results = kb.search(
        query="中文知识检索",
        collection=None,
        domain=None,
        limit=5,
        mode="dense",
    )
    assert results
    assert "中文知识检索" in results[0]["content"]


def test_fts_mode_still_works(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path)
    _ingest(kb, "记忆条数满了怎么办。")
    results = kb.search(
        query="记忆条数",
        collection=None,
        domain=None,
        limit=5,
        mode="fts5",
    )
    assert results


def test_invalid_mode_rejected(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path)
    with pytest.raises(ValueError):
        kb.search(
            query="x",
            collection=None,
            domain=None,
            limit=5,
            mode="bogus",
        )


def test_vec_table_present_after_init(tmp_path: Path) -> None:
    db_path = tmp_path / "schema.db"
    initialize_database(db_path)
    with sqlite3.connect(db_path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "chunk_vec" in names
