from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eval.run_eval import build_kb, parse_args, retrieve_dense


class FailingEmbedder:
    dim = 1024

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding backend unavailable")

    def embed_query(self, query: str) -> list[float]:
        raise RuntimeError("embedding backend unavailable")


def test_custom_cutoffs_replace_defaults_and_deduplicate() -> None:
    assert parse_args(["--k", "5", "--k", "10", "--k", "5"]).k == [5, 10]


def test_cutoffs_must_be_positive() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--k", "0"])


def test_dense_eval_rejects_incomplete_index(tmp_path: Path) -> None:
    documents: list[dict[str, Any]] = [
        {
            "id": "doc-1",
            "collection": "test",
            "domain": "test",
            "title": "test document",
            "content": "vector indexing must succeed for a dense evaluation",
        }
    ]

    with pytest.raises(RuntimeError, match="dense index is incomplete: 0/1"):
        build_kb(tmp_path / "eval.db", documents, embedder=FailingEmbedder())


def test_dense_eval_bypasses_public_fts_fallback() -> None:
    expected = [{"chunk_id": 7}]

    class StubKnowledgeBase:
        def _search_dense(self, *args: object, **kwargs: object) -> list[dict[str, int]]:
            return expected

        def search(self, *args: object, **kwargs: object) -> list[dict[str, int]]:
            raise AssertionError("public fallback path must not be used by dense eval")

    assert retrieve_dense(StubKnowledgeBase(), "query", 10) == expected  # type: ignore[arg-type]
