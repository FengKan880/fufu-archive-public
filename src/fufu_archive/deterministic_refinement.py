"""Model-free, fail-closed refinement boundary for collected session records.

This is deliberately not semantic summarization: it preserves selected message
text and maps every output fragment to source message ids. It can only create
pending candidates; approval and commit remain explicit KnowledgeBase actions.

Message body field
------------------
The collector's source archive stores message bodies under ``content``. The
earlier development shape used ``preview``; it stays accepted so existing
callers keep working, but the field actually consumed is recorded in the
payload as ``source_message_field`` rather than silently conflated.

Identity
--------
Records carry an ``identity`` block (profile, session_id, content_sha256).
When present it must agree with the top-level fields; a mismatch means the
record was merged, tampered with, or produced by an incompatible collector.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .db import KnowledgeBase

#: Message body keys accepted from a source record, in priority order.
MESSAGE_BODY_FIELDS = ("content", "preview")

#: Coverage counters that mean the transcript itself is unreliable. A partial
#: record may only be refined when every one of these is zero.
UNDECLARED_INCOMPLETE_KEYS = (
    "unknown_roles",
    "malformed_or_missing_content",
    "invalid_message_ids",
    "invalid_fields",
    "message_count_mismatch",
)

#: Coverage counters that describe content the collector deliberately and
#: countably dropped. Excluding tool bodies leaves the user/assistant
#: transcript intact, so it is a declared exclusion rather than corruption.
DECLARED_EXCLUSION_KEYS = (
    "tool_bodies_excluded",
    "tool_call_only_turns",
)

_SKIP_REASONS = {
    "coverage": "coverage_not_complete",
    "source identity": "source_identity_invalid",
    "message": "message_evidence_invalid",
}


class RefinementBlocked(ValueError):
    """The source cannot safely enter deterministic refinement."""


def _message_body(message: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return ``(field_name, body)`` for a supported message shape."""
    for field_name in MESSAGE_BODY_FIELDS:
        value = message.get(field_name)
        if isinstance(value, str) and value.strip():
            return field_name, value
    return None


def coverage_decision(
    coverage: object, *, allow_declared_exclusions: bool = False
) -> tuple[bool, str, dict[str, int]]:
    """Decide whether a coverage manifest may enter deterministic refinement.

    Returns ``(eligible, reason, declared_exclusions)``. The default refuses
    every non-complete record, which is the behavior refinement always had: a
    real Hermes session nearly always excludes tool bodies, so an operator has
    to opt in explicitly and the accepted exclusions are recorded in the
    candidate payload rather than being silently tolerated.
    """
    if not isinstance(coverage, Mapping):
        return False, "coverage_not_complete", {}
    status = coverage.get("status")
    if status == "complete" and not coverage.get("cleanup_qualification_blocked"):
        return True, "complete", {}
    if status != "partial" or not allow_declared_exclusions:
        return False, "coverage_not_complete", {}
    undeclared = {
        key: coverage.get(key, 0)
        for key in UNDECLARED_INCOMPLETE_KEYS
        if coverage.get(key, 0)
    }
    if undeclared:
        return False, "coverage_not_complete", {}
    declared = {
        key: int(coverage.get(key) or 0)
        for key in DECLARED_EXCLUSION_KEYS
        if int(coverage.get(key) or 0) > 0
    }
    if not declared:
        return False, "coverage_not_complete", {}
    return True, "partial_with_declared_exclusions", declared


def _validate(
    record: dict[str, Any], *, allow_declared_exclusions: bool = False
) -> tuple[list[dict[str, Any]], str, str, dict[str, int]]:
    if not isinstance(record, dict):
        raise RefinementBlocked("record structure is invalid")
    raw_coverage = record.get("coverage_manifest")
    eligible, coverage_status, declared = coverage_decision(
        raw_coverage,
        allow_declared_exclusions=allow_declared_exclusions,
    )
    if not eligible:
        raise RefinementBlocked("coverage is not complete")
    source_coverage_status = (
        raw_coverage.get("status")
        if isinstance(raw_coverage, Mapping)
        else "unknown"
    )
    session_id = record.get("session_id")
    content_sha256 = record.get("content_sha256")
    if (
        not isinstance(session_id, str)
        or not session_id.strip()
        or not isinstance(content_sha256, str)
        or len(content_sha256) != 64
        or any(char not in "0123456789abcdef" for char in content_sha256.lower())
    ):
        raise RefinementBlocked("source identity is malformed")
    profile = record.get("profile")
    if profile is not None and (
        not isinstance(profile, str) or not profile.strip()
    ):
        raise RefinementBlocked("source identity is malformed")
    identity = record.get("identity")
    if identity is not None:
        if not isinstance(identity, Mapping):
            raise RefinementBlocked("source identity is malformed")
        expected = {
            "session_id": session_id,
            "content_sha256": content_sha256.lower(),
        }
        for field, value in expected.items():
            actual = identity.get(field)
            if actual is None or not isinstance(actual, str) or actual.lower() != value:
                raise RefinementBlocked(
                    "source identity disagrees with the record identity block"
                )
        identity_profile = identity.get("profile")
        if identity_profile is not None and identity_profile != (profile or identity_profile):
            raise RefinementBlocked(
                "source identity disagrees with the record identity block"
            )
    messages = record.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RefinementBlocked("message evidence is missing")
    checked: list[dict[str, Any]] = []
    body_field: str | None = None
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("source_message_id"), int) or isinstance(message.get("source_message_id"), bool):
            raise RefinementBlocked("message evidence is malformed")
        if message.get("role") not in {"user", "assistant"}:
            raise RefinementBlocked("message evidence is malformed")
        body = _message_body(message)
        if body is None:
            raise RefinementBlocked("message evidence is malformed")
        if body_field is None:
            body_field = body[0]
        elif body_field != body[0]:
            raise RefinementBlocked(
                "message evidence mixes content and preview fields"
            )
        checked.append(message)
    assert body_field is not None  # non-empty list of validated messages
    return checked, body_field, source_coverage_status, coverage_status, declared


def build_pending_candidate(
    record: dict[str, Any], *, allow_declared_exclusions: bool = False
) -> dict[str, Any]:
    messages, body_field, source_coverage_status, coverage_status, declared = _validate(
        record, allow_declared_exclusions=allow_declared_exclusions
    )
    evidence = [{"source_message_id": m["source_message_id"], "message_index": i} for i,m in enumerate(messages)]
    content = "\n\n".join(m[body_field].strip() for m in messages)
    profile = record.get("profile")
    return {
        "status": "pending",
        "provider": None,
        "semantic_extraction": False,
        "content": content,
        "evidence": evidence,
        "structure": {"valid": True, "message_count": len(messages), "kind": "ordered-transcript"},
        "source_profile": profile,
        "source_session_id": record.get("session_id"),
        "source_content_sha256": record.get("content_sha256"),
        "source_message_field": body_field,
        # Provenance: never let a candidate imply a complete transcript when
        # the collector declared excluded content.
        "source_coverage_status": source_coverage_status,
        "coverage_eligibility": coverage_status,
        "declared_exclusions": declared,
    }


def persist_pending_candidate(
    kb: KnowledgeBase,
    record: dict[str, Any],
    *,
    allow_declared_exclusions: bool = False,
) -> dict[str, Any]:
    payload = build_pending_candidate(
        record, allow_declared_exclusions=allow_declared_exclusions
    )
    profile = payload.get("source_profile")
    return kb.create_candidate(
        content=payload["content"], collection="refinement",
        source=f"session:{payload.get('source_session_id')}",
        title=record.get("title"),
        topic_key=refinement_topic_key(
            profile, payload.get("source_session_id"), payload.get("source_content_sha256")
        ),
        metadata={"refinement": payload},
    )


def refinement_topic_key(
    profile: str | None, session_id: Any, content_sha256: Any
) -> str:
    """Isolate refinement topics per profile.

    Session ids are only unique inside one Hermes profile, so omitting the
    profile would let two profiles collapse into a single candidate.
    """
    return f"{profile or 'unknown'}:session:{session_id}:{content_sha256}"


def _skip_reason(error: RefinementBlocked) -> str:
    message = str(error)
    for marker, reason in _SKIP_REASONS.items():
        if marker in message:
            return reason
    return "refinement_blocked"


def refine_source_archive(
    archive_path: Path,
    kb: KnowledgeBase,
    *,
    profile: str | None = None,
    limit: int | None = None,
    allow_declared_exclusions: bool = False,
) -> dict[str, Any]:
    """Turn safe session records from one source archive into pending candidates.

    Only ``record_type == "session"`` records are considered; blocked and
    malformed records are reported as skipped instead of producing output.
    The function never approves or commits: every produced candidate stays
    pending for explicit review.
    """
    from .session_collector import load_source_archive

    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
    ):
        raise ValueError("limit must be a positive integer")
    archive = load_source_archive(Path(archive_path))
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for record in archive.get("records", []):
        if record.get("record_type") != "session":
            continue
        record_profile = record.get("profile")
        if not isinstance(record_profile, str) or not record_profile:
            record_profile = "unknown"
        if profile is not None and record_profile != profile:
            continue
        if limit is not None and len(candidates) >= limit:
            break
        try:
            candidate = persist_pending_candidate(
                kb, record, allow_declared_exclusions=allow_declared_exclusions
            )
        except RefinementBlocked as error:
            skipped.append({
                "profile": record_profile,
                "session_id": str(record.get("session_id")),
                "reason": _skip_reason(error),
            })
            continue
        candidates.append(candidate)
    return {
        "candidates_created": len(candidates),
        "candidates": candidates,
        "skipped": skipped,
    }


def main(argv: list[str] | None = None) -> int:
    """Create pending refinement candidates from one explicit source archive.

    This entry point is the only supported wiring for the module. It refuses a
    missing archive database so a mistyped path cannot silently create a new
    one, writes nothing but pending candidates, and never approves or commits.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-archive", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--allow-declared-exclusions",
        action="store_true",
        help=(
            "also refine partial records whose only incompleteness is a "
            "counted, declared exclusion such as excluded tool bodies"
        ),
    )
    args = parser.parse_args(argv)

    if not args.db.is_file():
        parser.error(f"archive database does not exist: {args.db}")
    if not args.source_archive.is_file():
        parser.error(f"source archive does not exist: {args.source_archive}")

    from .db import KnowledgeBase as _KnowledgeBase

    report = refine_source_archive(
        args.source_archive,
        _KnowledgeBase(args.db),
        profile=args.profile,
        limit=args.limit,
        allow_declared_exclusions=args.allow_declared_exclusions,
    )
    json.dump(report, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


class ProviderRefinementError(RefinementBlocked):
    """A provider boundary failure safe to report and persist as diagnostics."""

    def __init__(self, reason_code: str, detail: str = "", *, attempts: int = 0) -> None:
        self.reason_code = reason_code
        self.attempts = attempts
        self.detail = re.sub(r"(?i)(api[_-]?key|password|secret|token)\s*[=:]\s*\S+", r"\1=[REDACTED]", detail)[:240]
        super().__init__(f"{reason_code}: {self.detail or 'provider refinement blocked'}")


_SECRET_PATTERN = re.compile(r"(?i)\b(?:api[_-]?key|password|passwd|secret|access[_-]?token|bearer)\s*[=:]\s*[^\s,;]+")


def _provider_result(value: object, source_ids: set[int], *, max_items: int, max_output_chars: int) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as error:
            raise ProviderRefinementError("provider_invalid_json", "response is not valid JSON") from error
    if not isinstance(value, Mapping) or not isinstance(value.get("items"), list):
        raise ProviderRefinementError("provider_invalid_structure", "response must contain an items array")
    items = value["items"]
    if not items or len(items) > max_items:
        raise ProviderRefinementError("batch_limit_exceeded", "item count exceeds configured limit")
    total = 0
    checked: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("title"), str) or not isinstance(item.get("content"), str) or not isinstance(item.get("evidence"), list):
            raise ProviderRefinementError("provider_invalid_structure", "each item needs title, content, and evidence")
        title, content, evidence = item["title"].strip(), item["content"].strip(), item["evidence"]
        if not title or not content or not evidence or any(type(x) is not int or x not in source_ids for x in evidence):
            raise ProviderRefinementError("provider_invalid_structure", "item evidence must reference source messages")
        if _SECRET_PATTERN.search(title) or _SECRET_PATTERN.search(content):
            raise ProviderRefinementError("provider_sensitive_information", "sensitive values are not accepted")
        total += len(title) + len(content)
        if total > max_output_chars:
            raise ProviderRefinementError("batch_limit_exceeded", "output budget exceeded")
        checked.append({"title": title, "content": content, "evidence": list(evidence)})
    return checked


def refine_with_provider(
    record: dict[str, Any], provider: Any, *, timeout_seconds: float = 30.0,
    max_attempts: int = 3, max_items: int = 20, max_output_chars: int = 20_000,
) -> dict[str, Any]:
    """Run an injected provider under strict, bounded, fail-closed validation.

    This function only returns an in-memory pending payload. It never approves,
    commits, or writes the formal knowledge database.
    """
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 5:
        raise ValueError("max_attempts must be between 1 and 5")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
        raise ProviderRefinementError("batch_limit_exceeded", "max_items must be positive")
    if isinstance(max_output_chars, bool) or not isinstance(max_output_chars, int) or max_output_chars < 1:
        raise ProviderRefinementError("batch_limit_exceeded", "max_output_chars must be positive")
    if not callable(getattr(provider, "extract", None)):
        raise ProviderRefinementError("provider_invalid_interface", "provider must implement extract")
    messages, body_field, _source_status, _eligibility, _declared = _validate(record)
    from .semantic_provider import SemanticExtractionRequest
    source_ids = tuple(m["source_message_id"] for m in messages)
    transcript = "\n\n".join(m[body_field].strip() for m in messages)
    if _SECRET_PATTERN.search(transcript):
        raise ProviderRefinementError("provider_sensitive_information", "source contains sensitive values")
    request = SemanticExtractionRequest(record.get("profile"), record["session_id"], record["content_sha256"], source_ids, transcript)
    last: ProviderRefinementError | None = None
    for attempt in range(1, max_attempts + 1):
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(provider.extract, request)
        try:
            result = future.result(timeout=float(timeout_seconds))
            items = _provider_result(result, set(source_ids), max_items=max_items, max_output_chars=max_output_chars)
            evidence = [{"source_message_id": source_id} for source_id in source_ids]
            return {"status": "pending", "provider": provider.__class__.__name__, "semantic_extraction": True, "items": items, "evidence": evidence, "structure": {"valid": True, "kind": "semantic-extraction", "schema_version": 1}, "source_profile": record.get("profile"), "source_session_id": record["session_id"], "source_content_sha256": record["content_sha256"]}
        except concurrent.futures.TimeoutError as error:
            last = ProviderRefinementError("provider_timeout", "provider exceeded timeout", attempts=attempt)
        except ProviderRefinementError as error:
            last = ProviderRefinementError(error.reason_code, error.detail, attempts=attempt)
            if error.reason_code in {"provider_sensitive_information", "provider_invalid_structure", "provider_invalid_json", "batch_limit_exceeded"}:
                break
        except Exception as error:
            last = ProviderRefinementError("provider_failed", error.__class__.__name__, attempts=attempt)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    assert last is not None
    raise last


def persist_provider_candidate(kb: KnowledgeBase, record: dict[str, Any], provider: Any, **kwargs: Any) -> dict[str, Any]:
    """Persist a validated provider result only as an unsearchable pending candidate."""
    payload = refine_with_provider(record, provider, **kwargs)
    return kb.create_candidate(content="\n\n".join(item["content"] for item in payload["items"]), collection="refinement", source=f"session:{record['session_id']}", title=record.get("title"), topic_key=refinement_topic_key(record.get("profile"), record["session_id"], record["content_sha256"]), metadata={"refinement": payload})

def refine_with_chunked_provider(
    record: dict[str, Any], provider: Any, *, max_chars: int = 30_000,
    max_calls: int = 64, max_chunk_attempts: int = 2,
) -> dict[str, Any]:
    """Create an in-memory semantic payload using fail-closed chunk synthesis.

    Source validation happens before any provider invocation.  A blocked chunk
    run is returned as diagnostics only and must not be persisted by callers.
    """
    messages, body_field, source_status, eligibility, declared = _validate(record)
    normalized = [
        {"source_message_id": message["source_message_id"], "role": message["role"],
         "content": message[body_field].strip()}
        for message in messages
    ]
    if _SECRET_PATTERN.search("\n".join(message["content"] for message in normalized)):
        return {"status": "blocked", "semantic_extraction": True,
                "provider": provider.__class__.__name__, "reason": "provider_sensitive_information",
                "source_profile": record.get("profile"), "source_session_id": record["session_id"],
                "source_content_sha256": record["content_sha256"]}
    from .chunked_refinement import summarize_session_in_chunks
    outcome = summarize_session_in_chunks(
        normalized, provider, profile=record.get("profile"),
        session_id=record["session_id"], content_sha256=record["content_sha256"],
        max_chars=max_chars, max_calls=max_calls, max_chunk_attempts=max_chunk_attempts,
    )
    if outcome.get("status") != "pending":
        return {"status": "blocked", "semantic_extraction": True,
                "provider": provider.__class__.__name__, "outcome": outcome,
                "source_profile": record.get("profile"), "source_session_id": record["session_id"],
                "source_content_sha256": record["content_sha256"]}
    return {
        "status": "pending", "provider": provider.__class__.__name__,
        "semantic_extraction": True, "items": outcome["items"],
        "evidence_scope": "session_input_range", "structure": {"valid": True, "kind": "chunked-semantic-extraction", "schema_version": 2},
        "source_profile": record.get("profile"), "source_session_id": record["session_id"],
        "source_content_sha256": record["content_sha256"], "source_message_field": body_field,
        "source_coverage_status": source_status, "coverage_eligibility": eligibility,
        "declared_exclusions": declared, "chunk_diagnostics": outcome["chunks"],
        "synthesis": outcome["synthesis"],
    }


def persist_chunked_provider_candidate(kb: KnowledgeBase, record: dict[str, Any], provider: Any, **kwargs: Any) -> dict[str, Any] | None:
    """Persist only a completed chunked result as a pending candidate.

    ``None`` means the provider run was blocked. No candidate, approval,
    document, FTS entry, or formal knowledge write is created in that case.
    """
    payload = refine_with_chunked_provider(record, provider, **kwargs)
    if payload["status"] != "pending":
        return None
    return kb.create_candidate(
        content="\n\n".join(item["content"] for item in payload["items"]),
        collection="refinement", source=f"session:{record['session_id']}",
        title=record.get("title"),
        topic_key=refinement_topic_key(record.get("profile"), record["session_id"], record["content_sha256"]),
        metadata={"refinement": payload},
    )


def refine_source_archive_with_chunked_provider(
    archive_path: Path, kb: KnowledgeBase, provider: Any, *, profile: str | None = None,
    limit: int | None = None, max_chars: int = 30_000, max_calls: int = 64,
    max_chunk_attempts: int = 2,
) -> dict[str, Any]:
    """One-shot, pending-only semantic refinement of an explicit source archive.

    The report intentionally contains no transcript, candidate content, titles,
    or model output.  This function does not schedule work or approve/commit.
    """
    from .session_collector import load_source_archive
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive integer")
    archive = load_source_archive(Path(archive_path))
    created: list[str] = []; blocked: list[dict[str, str]] = []; considered = 0
    for record in archive.get("records", []):
        if not isinstance(record, dict) or record.get("record_type") != "session": continue
        record_profile = record.get("profile") if isinstance(record.get("profile"), str) else "unknown"
        if profile is not None and record_profile != profile: continue
        if limit is not None and considered >= limit: break
        considered += 1
        try:
            payload = refine_with_chunked_provider(record, provider, max_chars=max_chars, max_calls=max_calls, max_chunk_attempts=max_chunk_attempts)
            if payload["status"] != "pending":
                outcome = payload.get("outcome", {})
                blocked.append({"profile": record_profile, "session_id": str(record.get("session_id")), "reason": str(payload.get("reason") or outcome.get("reason") or "refinement_blocked")})
                continue
            topic_key = refinement_topic_key(record.get("profile"), record["session_id"], record["content_sha256"])
            existing_ids = {row["candidate_id"] for row in kb.list_candidates(status=None, topic_key=topic_key, limit=100)}
            candidate = kb.create_candidate(
                content="\n\n".join(item["content"] for item in payload["items"]), collection="refinement",
                source=f"session:{record['session_id']}", title=record.get("title"), topic_key=topic_key,
                metadata={"refinement": payload},
            )
            if candidate["candidate_id"] not in existing_ids: created.append(candidate["candidate_id"])
        except RefinementBlocked as error:
            blocked.append({"profile":record_profile,"session_id":str(record.get("session_id")),"reason":_skip_reason(error)})
    return {"records_considered": considered, "candidates_created": len(created), "candidate_ids": created, "blocked": blocked}

__all__ = [
    "DECLARED_EXCLUSION_KEYS",
    "MESSAGE_BODY_FIELDS",
    "UNDECLARED_INCOMPLETE_KEYS",
    "coverage_decision",
    "RefinementBlocked",
    "ProviderRefinementError",
    "refine_with_provider",
    "persist_provider_candidate",
    "refine_with_chunked_provider",
    "persist_chunked_provider_candidate",
    "refine_source_archive_with_chunked_provider",
    "build_pending_candidate",
    "persist_pending_candidate",
    "main",
    "refine_source_archive",
    "refinement_topic_key",
]


if __name__ == "__main__":  # pragma: no cover - exercised through CLI regression
    raise SystemExit(main())
