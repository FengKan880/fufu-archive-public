"""Public API request and response models."""

from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator, model_validator

from fufu_archive.injection_scan import INJECTION_SCAN_METADATA_KEY


Tag = Annotated[str, Field(strict=True, min_length=1, max_length=100)]
TopicKey = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
    ),
]
MIN_CHUNK_TARGET_SIZE = 16


class ChunkingOptions(BaseModel):
    """Opt-in ingest chunking measured in Unicode characters."""

    enabled: bool = Field(default=True, strict=True)
    target_size: int = Field(
        default=1_000,
        strict=True,
        ge=1,
        le=1_000_000,
    )
    overlap: int = Field(default=0, strict=True, ge=0, le=999_999)

    @model_validator(mode="after")
    def sizes_must_be_safe(self) -> "ChunkingOptions":
        if self.enabled and self.target_size < MIN_CHUNK_TARGET_SIZE:
            raise ValueError(
                "target_size must be at least "
                f"{MIN_CHUNK_TARGET_SIZE} when chunking is enabled"
            )
        if self.overlap >= self.target_size:
            raise ValueError("overlap must be smaller than target_size")
        return self


class IngestRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1_000_000)
    collection: str = Field(default="default", min_length=1, max_length=100)
    domain: str | None = Field(default=None, max_length=100)
    status: str = Field(default="active", min_length=1, max_length=30)
    parent_id: str | None = Field(default=None, max_length=64)
    supersedes: str | None = Field(default=None, max_length=64)
    source: str | None = Field(default=None, max_length=2_000)
    title: str | None = Field(default=None, max_length=500)
    content_type: str | None = Field(default=None, max_length=255)
    language: str | None = Field(default=None, max_length=35)
    topic_key: TopicKey | None = None
    tags: list[Tag] = Field(default_factory=list, strict=True, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict, strict=True)
    chunking: ChunkingOptions | None = None
    scan_injection: bool = Field(
        default=True,
        description="store deterministic prompt-injection scan metadata",
    )

    @field_validator("content")
    @classmethod
    def content_must_contain_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must contain non-whitespace text")
        return value

    @field_validator("tags", mode="before")
    @classmethod
    def tags_must_contain_only_strings(cls, value: object) -> object:
        if isinstance(value, list) and any(not isinstance(tag, str) for tag in value):
            raise ValueError("tags must contain only strings")
        return value

    @field_validator("metadata")
    @classmethod
    def metadata_must_be_safe_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        caller_metadata = {
            key: item
            for key, item in value.items()
            if key != INJECTION_SCAN_METADATA_KEY
        }
        try:
            json.dumps(
                caller_metadata,
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
            raise ValueError("metadata must contain safe JSON values") from error
        return caller_metadata


class IngestResponse(BaseModel):
    document_id: str
    chunk_count: int
    parent_chunk_count: int
    status: str


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1_000)
    collection: str | None = Field(default=None, min_length=1, max_length=100)
    domain: str | None = Field(default=None, min_length=1, max_length=100)
    topic_key: TopicKey | None = None
    limit: int = Field(default=10, ge=1, le=50)
    mode: str = Field(
        default="dense",
        pattern="^(fts5|dense|hybrid)$",
        description="retrieval strategy: fts5 | dense (default) | hybrid",
    )
    rewrite: bool = Field(
        default=False,
        description="opt in to deterministic conversational query variants",
    )

    @field_validator("query")
    @classmethod
    def query_must_contain_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must contain non-whitespace text")
        return value


class SearchResult(BaseModel):
    document_id: str
    chunk_id: int
    content: str
    collection: str
    domain: str | None
    score: float


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    rewritten_queries: list[str] | None = None


class HealthResponse(BaseModel):
    status: str
    service: str
    database: str


class ErrorResponse(BaseModel):
    detail: str
