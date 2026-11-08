"""Pluggable embedding backends for Fufu Archive (P1).

The KnowledgeBase stores dense vectors produced by an ``Embedder``.  Backends
are swappable behind a small protocol so the local Ollama deployment can be
replaced by a cloud API without touching storage or retrieval code.

The baseline choice (2026-08-06 eval) is:

    Ollama + qwen3-embedding:0.6b  (1024 dims, 32K ctx)
    - documents: no prefix
    - queries:   Chinese instruct prefix (proven +2.3% recall@1 in eval)
"""

from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.request
from numbers import Real
from typing import Protocol

logger = logging.getLogger(__name__)

QUERY_PREFIX_CN = "Instruct: 为这个句子生成表示以用于检索相关文章\nQuery: "

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3-embedding:0.6b"
DEFAULT_DIM = 1024


class Embedder(Protocol):
    """Minimal embedding interface used by KnowledgeBase."""

    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed document-side text (no query prefix)."""

    def embed_query(self, query: str) -> list[float]:
        """Embed a single query (query-side prefix applied by backend)."""


class OllamaEmbedder:
    """Embedding backend backed by a local Ollama /api/embed endpoint."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        query_prefix: str | None = QUERY_PREFIX_CN,
        dim: int = DEFAULT_DIM,
        timeout: float = 60.0,
        batch_size: int = 32,
    ) -> None:
        if dim < 1:
            raise ValueError("embedding dimension must be positive")
        if timeout <= 0:
            raise ValueError("embedding timeout must be positive")
        if batch_size < 1:
            raise ValueError("embedding batch_size must be positive")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.query_prefix = query_prefix
        self.dim = dim
        self.timeout = timeout
        self.batch_size = batch_size

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, query: str) -> list[float]:
        (vector,) = self._embed([(self.query_prefix or "") + query])
        return vector

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        embeddings: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            embeddings.extend(self._embed_batch(batch))
        return embeddings

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/embed",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                raw_payload = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read(200).decode("utf-8", errors="replace").strip()
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(
                f"Ollama embedding request failed with HTTP {error.code}{suffix}"
            ) from error
        if status != 200:
            raise RuntimeError(f"Ollama embedding request failed with HTTP {status}")
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("Ollama returned an invalid JSON response") from error
        if not isinstance(payload, dict):
            raise RuntimeError("Ollama embedding response must be a JSON object")
        raw_embeddings = payload.get("embeddings")
        if not isinstance(raw_embeddings, list) or len(raw_embeddings) != len(texts):
            raise RuntimeError(
                f"Ollama returned {len(raw_embeddings) if isinstance(raw_embeddings, list) else 0} "
                f"embeddings for {len(texts)} inputs"
            )

        embeddings: list[list[float]] = []
        for index, raw_vector in enumerate(raw_embeddings):
            if not isinstance(raw_vector, list) or len(raw_vector) != self.dim:
                actual_dim = len(raw_vector) if isinstance(raw_vector, list) else 0
                raise RuntimeError(
                    f"Ollama embedding {index} has dimension {actual_dim}; expected {self.dim}"
                )
            if any(
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not math.isfinite(float(value))
                for value in raw_vector
            ):
                raise RuntimeError(f"Ollama embedding {index} contains non-finite values")
            embeddings.append([float(value) for value in raw_vector])
        return embeddings


def build_embedder(
    *,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    dim: int = DEFAULT_DIM,
) -> Embedder:
    """Factory for the default backend; swap here when adding cloud providers."""
    return OllamaEmbedder(base_url=base_url, model=model, dim=dim)
