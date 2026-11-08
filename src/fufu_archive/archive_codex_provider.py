"""Standalone Codex Responses provider boundary for Fufu Archive.

This module intentionally owns no Hermes profile state. Credentials are loaded
only from an explicit archive-owned path, and the HTTP transport is injectable
for tests. Browser OAuth acquisition is a separate, explicit operation.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .semantic_provider import SemanticExtractionRequest


class CodexProviderError(RuntimeError):
    """A safe, stable error at the standalone Codex provider boundary."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        self.detail = _redact(detail)[:240]
        super().__init__(f"{reason_code}: {self.detail or 'Codex provider operation failed'}")


def _redact(value: object) -> str:
    text = str(value)
    for key in ("api_key", "access_token", "refresh_token", "password", "secret", "token"):
        import re
        text = re.sub(rf"(?i)\b{key}\s*[=:]\s*[^\s,;]+", f"{key}=[REDACTED]", text)
    return text


class ArchiveCodexCredentialStore:
    """Explicit archive-owned credential store; never discovers Hermes files."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise CodexProviderError("credential_missing", "archive credential file is absent")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CodexProviderError("credential_invalid", "archive credential file is unreadable") from exc
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"].strip():
            raise CodexProviderError("credential_invalid", "access_token is missing")
        if "expires_at" in data and (not isinstance(data["expires_at"], (int, float)) or isinstance(data["expires_at"], bool)):
            raise CodexProviderError("credential_invalid", "expires_at is invalid")
        return data

    def save(self, data: Mapping[str, Any]) -> None:
        if not isinstance(data, Mapping) or not isinstance(data.get("access_token"), str) or not data["access_token"].strip():
            raise CodexProviderError("credential_invalid", "access_token is missing")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                json.dump(dict(data), stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        except OSError as exc:
            try: os.unlink(temp_name)
            except OSError: pass
            raise CodexProviderError("credential_invalid", "archive credential save failed") from exc


class ArchiveCredentialLock:
    """Small cross-process O_EXCL lock for archive-owned credential refresh."""

    def __init__(self, credential_path: Path, *, timeout_seconds: float = 10.0) -> None:
        self.path = Path(credential_path).with_name(Path(credential_path).name + ".refresh.lock")
        self.timeout_seconds = timeout_seconds
        self._fd: int | None = None

    def __enter__(self) -> "ArchiveCredentialLock":
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode("ascii", "replace"))
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise CodexProviderError("credential_refresh_locked", "archive credential refresh is busy")
                time.sleep(0.02)
            except OSError as exc:
                raise CodexProviderError("credential_invalid", "archive credential lock failed") from exc

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try: self.path.unlink()
        except FileNotFoundError: pass
        except OSError: pass


Transport = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Mapping[str, Any]]
RefreshCallback = Callable[[str], Mapping[str, Any]]


def _default_transport(endpoint: str, headers: Mapping[str, str], payload: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
    request = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text_parts: list[str] = []
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "response.output_text.delta" and isinstance(event.get("delta"), str):
                    text_parts.append(event["delta"])
            return {"output_text": "".join(text_parts)}
    except urllib.error.HTTPError as exc:
        code = "quota_exhausted" if exc.code in {402, 429} else "provider_http_error"
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("detail", "")
        except Exception:
            detail = ""
        raise CodexProviderError(code, f"upstream HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CodexProviderError("provider_timeout" if isinstance(exc, TimeoutError) else "provider_http_error", exc.__class__.__name__) from exc
    except UnicodeError as exc:
        raise CodexProviderError("provider_invalid_response", "upstream response is not UTF-8") from exc


class ArchiveCodexResponsesProvider:
    """Standalone, injectable Codex Responses provider for semantic extraction."""

    def __init__(self, credential_store: ArchiveCodexCredentialStore, *, transport: Transport | None = None, refresh: RefreshCallback | None = None, endpoint: str = "https://chatgpt.com/backend-api/codex/responses", model: str = "gpt-5.6-luna", originator: str | None = "codex_cli_rs", timeout_seconds: float = 60.0) -> None:
        self.credential_store = credential_store
        self.transport = transport or _default_transport
        self.refresh = refresh
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.originator = originator
        self.timeout_seconds = timeout_seconds

    def _credentials(self) -> dict[str, Any]:
        credentials = self.credential_store.load()
        expires_at = credentials.get("expires_at")
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool) and expires_at <= time.time():
            refresh_token = credentials.get("refresh_token")
            if not isinstance(refresh_token, str) or not refresh_token.strip() or self.refresh is None:
                raise CodexProviderError("credential_expired", "archive credential requires explicit refresh")
            with ArchiveCredentialLock(self.credential_store.path):
                credentials = self.credential_store.load()
                expires_at = credentials.get("expires_at")
                if isinstance(expires_at, (int, float)) and expires_at > time.time():
                    return credentials
                try:
                    credentials = dict(self.refresh(str(credentials["refresh_token"])))
                except CodexProviderError: raise
                except Exception as exc: raise CodexProviderError("credential_invalid", exc.__class__.__name__) from exc
                self.credential_store.save(credentials)
        return credentials

    def extract(self, request: SemanticExtractionRequest) -> Mapping[str, Any]:
        credentials = self._credentials()
        payload = {
            "model": self.model,
            "store": False,
            "stream": True,
            "input": [{"role": "user", "content": request.transcript}],
            "instructions": "Extract only traceable facts. Return JSON only with items, each containing title, content, and evidence as source message IDs.",
        }
        try:
            headers = {"Authorization": f"Bearer {credentials['access_token']}"}
            account_id = credentials.get("account_id")
            if isinstance(account_id, str) and account_id.strip():
                headers["chatgpt-account-id"] = account_id.strip()
            if self.originator:
                headers["originator"] = self.originator
            response = self.transport(self.endpoint, headers, payload, self.timeout_seconds)
        except CodexProviderError: raise
        except TimeoutError as exc: raise CodexProviderError("provider_timeout", "upstream request timed out") from exc
        except Exception as exc: raise CodexProviderError("provider_http_error", exc.__class__.__name__) from exc
        text = response.get("output_text") if isinstance(response, Mapping) else None
        if not isinstance(text, str):
            raise CodexProviderError("provider_invalid_response", "missing output_text")
        try: result = json.loads(text)
        except (json.JSONDecodeError, UnicodeError) as exc: raise CodexProviderError("provider_invalid_response", "output_text is not JSON") from exc
        if not isinstance(result, Mapping) or not isinstance(result.get("items"), list):
            raise CodexProviderError("provider_invalid_response", "JSON output has no items array")
        for item in result["items"]:
            if not isinstance(item, Mapping) or not isinstance(item.get("evidence"), list) or any(type(i) is not int or i not in request.source_message_ids for i in item["evidence"]):
                raise CodexProviderError("provider_invalid_response", "evidence is not traceable")
        return result


__all__ = ["ArchiveCodexCredentialStore", "ArchiveCodexResponsesProvider", "ArchiveCredentialLock", "CodexProviderError"]
