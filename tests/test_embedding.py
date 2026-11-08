from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from fufu_archive.embedding import OllamaEmbedder


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_embedder_batches_and_validates_matching_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_sizes: list[int] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
        inputs = json.loads(request.data.decode("utf-8"))["input"]
        batch_sizes.append(len(inputs))
        assert timeout == 12.0
        return FakeResponse({"embeddings": [[1, 0] for _ in inputs]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    embedder = OllamaEmbedder(dim=2, timeout=12.0, batch_size=2)

    assert embedder.embed_documents(["a", "b", "c"]) == [
        [1.0, 0.0],
        [1.0, 0.0],
        [1.0, 0.0],
    ]
    assert batch_sizes == [2, 1]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "0 embeddings for 1 inputs"),
        ({"embeddings": []}, "0 embeddings for 1 inputs"),
        ({"embeddings": [[1.0]]}, "dimension 1; expected 2"),
        ({"embeddings": [[1.0, float("nan")]]}, "non-finite values"),
    ],
)
def test_embedder_rejects_malformed_embeddings(
    monkeypatch: pytest.MonkeyPatch, payload: object, message: str
) -> None:
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout: FakeResponse(payload)
    )
    embedder = OllamaEmbedder(dim=2)

    with pytest.raises(RuntimeError, match=message):
        embedder.embed_documents(["text"])


def test_embedder_reports_non_200_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(request: urllib.request.Request, timeout: float) -> FakeResponse:
        raise urllib.error.HTTPError(
            request.full_url,
            500,
            "server error",
            hdrs=None,
            fp=io.BytesIO(b'model unavailable'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    embedder = OllamaEmbedder(dim=2)

    with pytest.raises(RuntimeError, match="HTTP 500: model unavailable"):
        embedder.embed_documents(["text"])


def test_embedder_empty_batch_does_not_call_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*args: object, **kwargs: object) -> FakeResponse:
        raise AssertionError("backend should not be called")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected)
    assert OllamaEmbedder(dim=2).embed_documents([]) == []
