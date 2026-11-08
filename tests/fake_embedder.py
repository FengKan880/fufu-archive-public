"""Deterministic fake embedder shared by tests (no Ollama dependency)."""

from __future__ import annotations

import hashlib
import math

from fufu_archive.embedding import Embedder

_DIM = 1024


class FakeEmbedder(Embedder):
    """Digest-based embedder: identical text maps to an identical unit vector."""

    dim = _DIM

    def _vector(self, text: str) -> list[float]:
        values: list[float] = []
        block = 0
        while len(values) < _DIM:
            digest = hashlib.sha256(
                block.to_bytes(4, "big") + text.encode("utf-8")
            ).digest()
            values.extend(b / 255.0 for b in digest)
            block += 1
        values = values[:_DIM]
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, query: str) -> list[float]:
        return self._vector(query)
