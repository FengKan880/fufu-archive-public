from __future__ import annotations
import json, time
from pathlib import Path
import pytest
from fufu_archive.archive_codex_provider import ArchiveCodexCredentialStore, ArchiveCodexResponsesProvider, CodexProviderError
from fufu_archive.semantic_provider import SemanticExtractionRequest

def request():
    return SemanticExtractionRequest("winefox", "s-1", "a" * 64, (1, 2), "仅用于合成测试的非敏感文本")

def test_archive_store_requires_explicit_own_credential_file(tmp_path: Path):
    path = tmp_path / "archive-secrets" / "codex.json"
    with pytest.raises(CodexProviderError) as raised:
        ArchiveCodexCredentialStore(path).load()
    assert raised.value.reason_code == "credential_missing"
    assert not path.exists()

def test_provider_uses_archive_credential_and_codex_responses_request(tmp_path: Path):
    path = tmp_path / "codex.json"
    ArchiveCodexCredentialStore(path).save({"access_token":"archive-token","refresh_token":"archive-refresh","expires_at":time.time()+3600})
    calls=[]
    def transport(endpoint, headers, payload, timeout):
        calls.append((endpoint, headers, payload, timeout))
        return {"output_text": json.dumps({"items":[{"title":"经验","content":"步骤","evidence":[1]}]})}
    result=ArchiveCodexResponsesProvider(ArchiveCodexCredentialStore(path), transport=transport, model="gpt-5.6-luna").extract(request())
    assert result["items"][0]["evidence"] == [1]
    endpoint,headers,payload,timeout=calls[0]
    assert endpoint.endswith("/codex/responses") and headers["Authorization"] == "Bearer archive-token"
    assert payload["model"] == "gpt-5.6-luna" and payload["store"] is False and timeout > 0

def test_provider_never_falls_back_to_hermes_auth_path(tmp_path: Path):
    hermes_like=tmp_path/"profiles"/"winefox"/"auth.json"; hermes_like.parent.mkdir(parents=True); hermes_like.write_text(json.dumps({"access_token":"must-not-read"}),encoding="utf-8")
    with pytest.raises(CodexProviderError) as raised:
        ArchiveCodexResponsesProvider(ArchiveCodexCredentialStore(tmp_path/"archive-codex.json"), transport=lambda *args: {}).extract(request())
    assert raised.value.reason_code == "credential_missing" and "must-not-read" not in str(raised.value)

def test_provider_redacts_transport_errors_and_rejects_bad_response(tmp_path: Path):
    path=tmp_path/"codex.json"; ArchiveCodexCredentialStore(path).save({"access_token":"archive-token","expires_at":time.time()+3600})
    def broken(*args): raise RuntimeError("api_key=secret-value upstream failure")
    with pytest.raises(CodexProviderError) as raised:
        ArchiveCodexResponsesProvider(ArchiveCodexCredentialStore(path), transport=broken).extract(request())
    assert raised.value.reason_code == "provider_http_error" and "secret-value" not in str(raised.value)
    with pytest.raises(CodexProviderError) as raised:
        ArchiveCodexResponsesProvider(ArchiveCodexCredentialStore(path), transport=lambda *args:{"output_text":"not-json"}).extract(request())
    assert raised.value.reason_code == "provider_invalid_response"
