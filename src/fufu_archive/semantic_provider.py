"""Explicit semantic-extraction provider contract.

No provider implementation or network access belongs here. Callers inject a
provider implementing ``extract``; this module only defines the request shape
and the JSON-compatible response contract used by deterministic refinement.
"""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

@dataclass(frozen=True, slots=True)
class SemanticExtractionRequest:
    profile: str | None
    session_id: str
    content_sha256: str
    source_message_ids: tuple[int, ...]
    transcript: str

@runtime_checkable
class SemanticExtractionProvider(Protocol):
    """Replaceable, explicitly injected semantic extraction boundary."""
    def extract(self, request: SemanticExtractionRequest) -> Mapping[str, Any] | str: ...

__all__ = ["SemanticExtractionProvider", "SemanticExtractionRequest"]
