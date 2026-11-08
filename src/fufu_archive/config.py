"""Environment-backed application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings. Missing API tokens deliberately do not get a fallback."""

    db_path: Path
    host: str
    port: int
    api_token: str | None
    embed_url: str = "http://127.0.0.1:11434"
    embed_model: str = "qwen3-embedding:0.6b"
    embed_dim: int = 1024
    upload_max_bytes: int = 1_000_000
    semantic_provider: str | None = None
    semantic_model: str = "deepseek-v4-flash"

    @classmethod
    def from_env(cls) -> "Settings":
        raw_port = os.getenv("FUFU_ARCHIVE_PORT", "8000")
        try:
            port = int(raw_port)
        except ValueError as error:
            raise ValueError("FUFU_ARCHIVE_PORT must be an integer") from error
        if not 1 <= port <= 65535:
            raise ValueError("FUFU_ARCHIVE_PORT must be between 1 and 65535")

        raw_embed_dim = os.getenv("FUFU_ARCHIVE_EMBED_DIM", "1024")
        try:
            embed_dim = int(raw_embed_dim)
        except ValueError as error:
            raise ValueError("FUFU_ARCHIVE_EMBED_DIM must be an integer") from error
        if embed_dim < 1:
            raise ValueError("FUFU_ARCHIVE_EMBED_DIM must be positive")

        raw_upload_max_bytes = os.getenv("FUFU_ARCHIVE_UPLOAD_MAX_BYTES", "1000000")
        try:
            upload_max_bytes = int(raw_upload_max_bytes)
        except ValueError as error:
            raise ValueError(
                "FUFU_ARCHIVE_UPLOAD_MAX_BYTES must be an integer"
            ) from error
        if upload_max_bytes < 1:
            raise ValueError("FUFU_ARCHIVE_UPLOAD_MAX_BYTES must be positive")

        token = os.getenv("FUFU_ARCHIVE_API_TOKEN")
        return cls(
            db_path=Path(os.getenv("FUFU_ARCHIVE_DB_PATH", "data/fufu_archive.db")),
            host=os.getenv("FUFU_ARCHIVE_HOST", "127.0.0.1"),
            port=port,
            api_token=token if token else None,
            embed_url=os.getenv("FUFU_ARCHIVE_EMBED_URL", "http://127.0.0.1:11434"),
            embed_model=os.getenv("FUFU_ARCHIVE_EMBED_MODEL", "qwen3-embedding:0.6b"),
            embed_dim=embed_dim,
            upload_max_bytes=upload_max_bytes,
            semantic_provider=(os.getenv("FUFU_ARCHIVE_SEMANTIC_PROVIDER") or None),
            semantic_model=os.getenv("FUFU_ARCHIVE_SEMANTIC_MODEL", "deepseek-v4-flash"),
        )
