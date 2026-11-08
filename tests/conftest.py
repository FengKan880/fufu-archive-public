from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fufu_archive.config import Settings
from fufu_archive.main import create_app

from fake_embedder import FakeEmbedder


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        host="127.0.0.1",
        port=8000,
        api_token="test-token-not-a-default",
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings, embedder=FakeEmbedder())) as test_client:
        yield test_client
