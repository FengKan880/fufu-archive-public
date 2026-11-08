"""Read-only Hermes session collector for Fufu Archive.

The default invocation never writes to the archive. It reads ended sessions
from Hermes state.db, applies deterministic safety/quality filters, and emits
review-only JSON/Markdown reports. State mutation is opt-in via --commit-state
and affects only the collector's own JSON checkpoint; the separate explicit
--incremental-run operation also persists caller-selected source and pending
candidate outputs.

The separate --active-snapshot operation captures quiet active sessions into
an append-only source-only snapshot archive. It never advances the ended
session checkpoint or writes the formal Fufu Archive database.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, Sequence

ARCHIVE_TERMS = (
    "保存", "归档", "记住", "写入档案馆", "存到档案馆", "以后复用",
    "save this", "archive this", "remember this",
)
EVIDENCE_TERMS = (
    "实际验收", "验收通过", "测试通过", "已验证", "真实验证", "根因",
    "回滚", "来源", "compileall", "git diff --check", "smoke", "pytest",
    "passed", "http 冒烟", "mcp 协议",
)
STRUCTURE_TERMS = ("## ", "### ", "总结", "结论", "已完成", "结果")
TRANSIENT_TERMS = ("还在运行", "尚未完成", "暂时不能", "等待", "稍后", "下次继续")
EXCLUDED_SOURCES = {"cron", "delegate", "delegation", "background", "auxiliary"}
CHECKPOINT_VERSION = 2
LEGACY_CHECKPOINT_VERSIONS = {1, 2}
DEFAULT_PROFILE = "unknown"
SOURCE_ARCHIVE_FORMAT = "fufu-archive.hermes-source"
SOURCE_ARCHIVE_VERSION = 1
CANDIDATE_PAYLOAD_FORMAT = "fufu-archive.hermes-candidate"
CANDIDATE_PAYLOAD_VERSION = 1
ACTIVE_SNAPSHOT_FORMAT = "fufu-archive.hermes-active-snapshot"
ACTIVE_SNAPSHOT_VERSION = 1

SOURCE_SCHEMA_REQUIRED = {
    "sessions": frozenset({
        "id", "source", "title", "started_at", "ended_at", "end_reason",
        "parent_session_id", "message_count",
    }),
    "messages": frozenset({"id", "session_id", "role", "content"}),
}
SOURCE_SCHEMA_OPTIONAL = {
    "sessions": frozenset({"profile_name"}),
    "messages": frozenset({
        "tool_name", "tool_calls", "active", "compacted", "display_identity", "display_order",
    }),
}

HERMES_STRUCTURED_CONTENT_PREFIX = "\x00json:"

SENSITIVE_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I)),
    ("api_key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}", re.I)),
    ("credential_assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\b\s*[:=]\s*['\"]?[A-Za-z0-9._~+/-]{8,}"
    )),
    ("auth_file", re.compile(r"(?i)(?:^|[\\/])(?:auth\.json|\.env)(?:$|\s|[\\/])")),
    # Named provider token families. These are deliberately anchored on the
    # issuer prefix and a minimum body length: a bare hex blob, an ordinary URL
    # or prose must not be quarantined, while a real credential must not be
    # archived just because it is not an "sk-" key.
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}\b")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")),
    ("connection_string", re.compile(r"\b[a-z][a-z0-9+.\-]{1,20}://[^\s/:@]{1,64}:[^\s/@]{1,128}@")),
)


@dataclass
class Candidate:
    session_id: str
    source: str
    title: str | None
    started_at: float
    ended_at: float
    end_reason: str | None
    user_messages: int
    assistant_messages: int
    tool_calls: int
    tool_names: list[str]
    content_sha256: str
    score: int
    signals: list[str]
    user_preview: str
    final_preview: str
    profile: str = DEFAULT_PROFILE
    coverage_manifest: dict[str, object] = field(default_factory=dict)


@dataclass
class Skip:
    session_id: str
    source: str
    title: str | None
    reason: str
    sensitive_kinds: list[str]
    profile: str = DEFAULT_PROFILE
    coverage_manifest: dict[str, object] = field(default_factory=dict)


@dataclass
class ChildAgentLog:
    session_id: str
    parent_session_id: str
    source: str
    title: str | None
    started_at: float
    ended_at: float
    end_reason: str | None
    user_messages: int
    assistant_messages: int
    tool_calls: int
    tool_names: list[str]
    content_sha256: str
    sensitive_kinds: list[str]
    final_preview: str
    profile: str = DEFAULT_PROFILE
    coverage_manifest: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ArchiveResult:
    source_records_added: int
    blocked_records_added: int
    candidate_payloads_written: int
    source_archive_path: Path
    candidate_payload_paths: tuple[Path, ...]

    @property
    def archive_writes(self) -> int:
        return self.source_records_added + self.candidate_payloads_written


class ArchiveCollisionError(ValueError):
    """A requested archive destination aliases an input or another output."""


@dataclass(frozen=True)
class _ArchivePlan:
    result: ArchiveResult
    archive_text: str | None
    candidate_writes: tuple[tuple[Path, str], ...]


def _readonly_connect(path: Path) -> sqlite3.Connection:
    resolved = path.resolve().as_posix()
    con = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _source_schema_report_for_connection(con: sqlite3.Connection) -> dict[str, object]:
    """Return a deterministic, read-only compatibility report for state.db."""
    tables: dict[str, dict[str, object]] = {}
    missing: dict[str, list[str]] = {}
    for table in sorted(SOURCE_SCHEMA_REQUIRED):
        columns = sorted(_sqlite_columns(con, table))
        required = sorted(SOURCE_SCHEMA_REQUIRED[table])
        optional = sorted(SOURCE_SCHEMA_OPTIONAL.get(table, ()))
        table_exists = bool(columns)
        absent = sorted(set(required) - set(columns))
        tables[table] = {
            "exists": table_exists,
            "columns": columns,
            "required": required,
            "optional": optional,
            "optional_present": sorted(set(optional) & set(columns)),
        }
        if absent:
            missing[table] = absent
    message_columns = set(tables["messages"]["columns"])
    session_columns = set(tables["sessions"]["columns"])
    return {
        "version": 1,
        "valid": not missing,
        "tables": tables,
        "missing_required": missing,
        "optional_fallbacks": {
            "messages.tool_name": "NULL tool_name; tool names unavailable"
            if "tool_name" not in message_columns else "native column"
        },
        "source_capabilities": {
            "session_profile_name": "profile_name" in session_columns,
            "message_visibility": "active_only" if "active" in message_columns else "legacy_all_rows",
            "compacted_history": "compacted" in message_columns,
            "logical_display_identity": "display_identity" in message_columns
            and "display_order" in message_columns,
        },
    }


def source_schema_report(db_path: Path) -> dict[str, object]:
    """Inspect an explicitly supplied state database without mutating it.

    The report is stable JSON data: required fields are sorted and the only
    tolerated schema omission is the optional ``messages.tool_name`` column.
    """
    try:
        con = _readonly_connect(Path(db_path))
    except (OSError, sqlite3.Error) as exc:
        return {
            "version": 1,
            "valid": False,
            "tables": {},
            "missing_required": {},
            "optional_fallbacks": {},
            "error": "source database is not readable",
        }
    try:
        return _source_schema_report_for_connection(con)
    finally:
        con.close()


validate_source_schema = source_schema_report
audit_source_schema = source_schema_report


def _empty_checkpoint() -> dict:
    return {
        "version": CHECKPOINT_VERSION,
        "watermark": {"ended_at": None, "session_id": None},
        "processed": {},
    }


def _validate_profile(profile: str) -> str:
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError("profile must be a non-empty string")
    return profile.strip()


def processed_identity(
    session_id: str,
    content_sha256: str,
    profile: str = DEFAULT_PROFILE,
) -> str:
    """Return the durable profile/session/content identity.

    The first two arguments retain the pre-profile call shape.  The optional
    third argument is the explicit source profile, which defaults to the
    conservative ``unknown`` profile.
    """
    return f"{_validate_profile(profile)}:{session_id}:{content_sha256}"


def _legacy_processed_identity(session_id: str, content_sha256: str) -> str:
    return f"{session_id}:{content_sha256}"


def _normalize_checkpoint(state: dict) -> dict:
    if not isinstance(state, dict):
        raise RuntimeError("invalid collector checkpoint shape")
    if "version" not in state and "watermark" not in state:
        # Keep direct callers of the pre-checkpoint collect() API compatible.
        processed = state.get("processed", {})
        if not isinstance(processed, dict):
            raise RuntimeError("invalid collector checkpoint processed records")
        normalized = _empty_checkpoint()
        normalized["processed"] = dict(processed)
        return normalized
    if state.get("version") in LEGACY_CHECKPOINT_VERSIONS and (
        state.get("version") == 1 or "watermark" not in state
    ):
        # Legacy checkpoints had session-id keyed records and/or no watermark.
        # Read them so an existing opt-in checkpoint remains usable.
        processed = state.get("processed", {})
        if not isinstance(processed, dict):
            raise RuntimeError("invalid collector checkpoint processed records")
        normalized = _empty_checkpoint()
        normalized["processed"] = dict(processed)
        return normalized
    if state.get("version") not in LEGACY_CHECKPOINT_VERSIONS | {CHECKPOINT_VERSION}:
        raise RuntimeError(
            f"unsupported collector checkpoint version: {state.get('version')}"
        )
    watermark = state.get("watermark")
    if not isinstance(watermark, dict) or set(watermark) != {"ended_at", "session_id"}:
        raise RuntimeError("invalid collector checkpoint watermark")
    ended_at = watermark["ended_at"]
    session_id = watermark["session_id"]
    if ended_at is None or session_id is None:
        if ended_at is not None or session_id is not None:
            raise RuntimeError("invalid collector checkpoint watermark")
    elif not isinstance(ended_at, (int, float)) or isinstance(ended_at, bool) or not isinstance(
        session_id, str
    ):
        raise RuntimeError("invalid collector checkpoint watermark")
    processed = state.get("processed")
    if not isinstance(processed, dict):
        raise RuntimeError("invalid collector checkpoint processed records")
    normalized = {
        "version": CHECKPOINT_VERSION,
        "watermark": {
            "ended_at": ended_at,
            "session_id": session_id,
        },
        "processed": dict(processed),
    }
    for key in ("last_run_at", "last_source_db"):
        if key in state:
            normalized[key] = state[key]
    return normalized


def load_checkpoint(path: Path) -> dict:
    """Load a collector checkpoint, or return an empty checkpoint."""
    path = Path(path)
    if not path.exists():
        return _empty_checkpoint()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RuntimeError(f"invalid collector checkpoint: {path}")
    try:
        return _normalize_checkpoint(data)
    except RuntimeError as exc:
        raise RuntimeError(f"invalid collector checkpoint: {path}") from exc


def _atomic_write_bytes(path: Path, contents: bytes) -> None:
    """Write bytes through a same-directory fsynced temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(contents)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _atomic_write(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def save_checkpoint(path: Path, state: dict) -> None:
    """Atomically save a validated collector checkpoint."""
    checkpoint = _normalize_checkpoint(state)
    _atomic_write(
        Path(path), json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n"
    )


def merge_checkpoint(state: dict, seen: dict[str, dict]) -> dict:
    """Return checkpoint state after a fully successful collection."""
    checkpoint = _normalize_checkpoint(state)
    processed = checkpoint["processed"]
    watermark = checkpoint["watermark"]
    next_watermark = (
        None
        if watermark["ended_at"] is None
        else (float(watermark["ended_at"]), str(watermark["session_id"]))
    )
    for session_id, record in seen.items():
        content_sha256 = record.get("content_sha256")
        ended_at = record.get("ended_at")
        if not isinstance(content_sha256, str) or not isinstance(ended_at, (int, float)):
            raise RuntimeError("invalid collection result for checkpoint")
        profile = _validate_profile(record.get("profile", DEFAULT_PROFILE))
        identity = processed_identity(session_id, content_sha256, profile)
        processed[identity] = dict(record) | {
            "profile": profile,
            "session_id": session_id,
            "content_sha256": content_sha256,
            "ended_at": float(ended_at),
        }
        ordering = (float(ended_at), session_id)
        if next_watermark is None or ordering > next_watermark:
            next_watermark = ordering
    if next_watermark is not None:
        checkpoint["watermark"] = {
            "ended_at": next_watermark[0],
            "session_id": next_watermark[1],
        }
    return checkpoint


def _load_state(path: Path) -> dict:
    """Compatibility alias for the repository's collector state API."""
    return load_checkpoint(path)


def _normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _preview(text: str, limit: int = 280) -> str:
    text = _normalize(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _sensitive_kinds(text: str) -> list[str]:
    return [name for name, pattern in SENSITIVE_PATTERNS if pattern.search(text)]


def _safe_content_hash(content: object) -> str:
    """Hash content without retaining or serializing its body."""
    if isinstance(content, str):
        payload = b"str\0" + content.encode("utf-8")
    elif content is None:
        payload = b"null"
    elif isinstance(content, bytes):
        payload = b"bytes\0" + content
    else:
        payload = (
            f"{type(content).__name__}\0{repr(content)}"
        ).encode("utf-8", errors="backslashreplace")
    return hashlib.sha256(payload).hexdigest()


def _archivable_text_content(raw_content: object) -> tuple[str, bool]:
    """Return plain text safe for a text-only archive and whether it is complete.

    Hermes persists list/dict multimodal content as a sentinel-prefixed JSON
    scalar.  This collector intentionally does not reconstruct attachments or
    structured parts: retaining the encoded scalar would misrepresent it as
    recoverable text.  Mark it incomplete and leave its body out of outputs.
    """
    if not isinstance(raw_content, str):
        return "", False
    if raw_content.startswith(HERMES_STRUCTURED_CONTENT_PREFIX):
        return "", False
    return raw_content, True


def _coverage_manifest(
    *,
    message_rows: int,
    user_assistant_bodies: int,
    tool_bodies_excluded: int,
    tool_call_only_turns: int,
    unknown_roles: int,
    malformed_or_missing_content: int,
    invalid_message_ids: int,
    invalid_fields: int,
    message_count_mismatch: bool,
    unknown_role_names: Sequence[str],
    blocked: bool = False,
) -> dict[str, object]:
    incomplete = any((
        tool_bodies_excluded,
        tool_call_only_turns,
        unknown_roles,
        malformed_or_missing_content,
        invalid_message_ids,
        invalid_fields,
        message_count_mismatch,
    ))
    status = "blocked" if blocked else "partial" if incomplete else "complete"
    return {
        "version": 1,
        "status": status,
        "message_rows": message_rows,
        "user_assistant_bodies": user_assistant_bodies,
        "tool_bodies_excluded": tool_bodies_excluded,
        "tool_call_only_turns": tool_call_only_turns,
        "unknown_roles": unknown_roles,
        "unknown_role_names": sorted(set(unknown_role_names)),
        "malformed_or_missing_content": malformed_or_missing_content,
        "invalid_message_ids": invalid_message_ids,
        "invalid_fields": invalid_fields,
        "message_count_mismatch": bool(message_count_mismatch),
        "cleanup_qualification_blocked": status != "complete",
    }


def _legacy_coverage_manifest(record: dict) -> dict[str, object]:
    """Describe pre-P4.2 records without claiming that they were lossless."""
    messages = record.get("messages")
    message_count = len(messages) if isinstance(messages, list) else 0
    tool_calls = record.get("tool_calls")
    tool_count = (
        int(tool_calls.get("count", 0))
        if isinstance(tool_calls, dict)
        and isinstance(tool_calls.get("count", 0), int)
        and not isinstance(tool_calls.get("count", 0), bool)
        else 0
    )
    return _coverage_manifest(
        message_rows=message_count + tool_count,
        user_assistant_bodies=message_count,
        tool_bodies_excluded=tool_count,
        tool_call_only_turns=0,
        unknown_roles=0,
        malformed_or_missing_content=0,
        invalid_message_ids=0,
        invalid_fields=0,
        message_count_mismatch=False,
        unknown_role_names=[],
    ) | {
        "status": "partial",
        "legacy": True,
        "cleanup_qualification_blocked": True,
    }


def _coverage_compatibility_view(manifest: dict[str, object]) -> dict[str, object]:
    """Retain the short pre-manifest shape for existing report consumers."""
    return {
        "status": manifest.get("status", "partial"),
        "user_assistant_bodies": "included",
        "tool_bodies_excluded": bool(manifest.get("tool_bodies_excluded", 0)),
        "tool_call_only_turns": int(manifest.get("tool_call_only_turns", 0)),
        "unknown_roles": list(manifest.get("unknown_role_names", [])),
        "malformed_or_missing_content": int(
            manifest.get("malformed_or_missing_content", 0)
        ),
        "cleanup_eligible": not bool(
            manifest.get("cleanup_qualification_blocked", True)
        ),
    }


def _validate_coverage_manifest(value: object, path: Path, kind: str) -> None:
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid {kind} coverage manifest: {path}")
    if value.get("version") != 1 or value.get("status") not in {
        "complete", "partial", "blocked"
    }:
        raise RuntimeError(f"invalid {kind} coverage manifest: {path}")
    # ``tool_call_only_turns`` was added after coverage manifest v1 shipped.
    # Keep old v1 archives readable, but do not soften validation of any
    # pre-existing counter or status field.
    if "tool_call_only_turns" not in value:
        value["tool_call_only_turns"] = 0
    for key in (
        "message_rows", "user_assistant_bodies", "tool_bodies_excluded",
        "tool_call_only_turns", "unknown_roles", "malformed_or_missing_content", "invalid_message_ids",
        "invalid_fields",
    ):
        if not isinstance(value.get(key), int) or isinstance(value.get(key), bool) or value[key] < 0:
            raise RuntimeError(f"invalid {kind} coverage manifest: {path}")
    if (
        not isinstance(value.get("unknown_role_names"), list)
        or any(not isinstance(name, str) for name in value["unknown_role_names"])
        or not isinstance(value.get("message_count_mismatch"), bool)
        or not isinstance(value.get("cleanup_qualification_blocked"), bool)
    ):
        raise RuntimeError(f"invalid {kind} coverage manifest: {path}")
    incomplete = any(
        value[key] > 0 for key in (
            "tool_bodies_excluded", "tool_call_only_turns", "unknown_roles", "malformed_or_missing_content",
            "invalid_message_ids", "invalid_fields",
        )
    ) or value["message_count_mismatch"]
    expected_status = "blocked" if value["status"] == "blocked" else (
        "partial" if incomplete else "complete"
    )
    if value["status"] == "complete" and incomplete:
        raise RuntimeError(f"invalid {kind} coverage manifest: {path}")
    if value["status"] != "complete" and not value["cleanup_qualification_blocked"]:
        raise RuntimeError(f"invalid {kind} coverage manifest: {path}")
    if expected_status == "complete" and value["cleanup_qualification_blocked"]:
        raise RuntimeError(f"invalid {kind} coverage manifest: {path}")


def _tool_sensitive_kinds(con: sqlite3.Connection, session_id: str) -> list[str]:
    """Scan tool bodies for quarantine decisions without returning their text."""
    kinds: list[str] = []
    for row in con.execute(
        "SELECT content FROM messages WHERE session_id=? AND role='tool'",
        (session_id,),
    ):
        content = row["content"] if isinstance(row["content"], str) else ""
        kinds.extend(_sensitive_kinds(content))
    return sorted(set(kinds))


def _session_content(
    con: sqlite3.Connection, session_id: str
) -> tuple[list[str], list[str], list[str]]:
    snapshot = _session_snapshot(con, session_id)
    users = [
        _normalize(item["content"])
        for item in snapshot["messages"]
        if item["role"] == "user" and isinstance(item["content"], str)
    ]
    assistants = [
        _normalize(item["content"])
        for item in snapshot["messages"]
        if item["role"] == "assistant" and isinstance(item["content"], str)
    ]
    return users, assistants, list(snapshot["tool_names"])


def _message_visibility_clause(con: sqlite3.Connection, table_alias: str = "") -> str:
    """Return the Hermes live-message filter, or the legacy fallback.

    Hermes stores rewind and compaction-history rows alongside the current
    conversation.  When the modern ``active`` column is available, collector
    inputs must match Hermes default reads and exclude inactive rows.  Older
    state databases lack this distinction, so their historical all-row behavior
    remains an explicitly reported compatibility fallback.
    """
    columns = _sqlite_columns(con, "messages")
    if "active" not in columns:
        return ""
    prefix = f'{table_alias}.' if table_alias else ""
    return f' AND {prefix}"active" = 1'


def _session_profile_clause(
    con: sqlite3.Connection, profile: str, table_alias: str = ""
) -> tuple[str, tuple[str, ...]]:
    """Constrain modern profile-aware state stores to the requested profile."""
    if "profile_name" not in _sqlite_columns(con, "sessions"):
        return "", ()
    prefix = f'{table_alias}.' if table_alias else ""
    return f' AND {prefix}"profile_name" = ?', (profile,)


def _positive_tool_calls_evidence(value: object) -> bool:
    """Recognize only explicit non-empty Hermes tool-call structures.

    Missing/invalid optional-column data is deliberately not guessed to be a
    tool-only turn. Hermes currently stores JSON text here, while fixture and
    future adapters may expose a decoded list/dict directly.
    """
    if isinstance(value, (list, dict)):
        return bool(value)
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(decoded, (list, dict)) and bool(decoded)


def _ended_message_rows(con: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    """Read current ended-session messages when optional Hermes columns are absent."""
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(messages)")}
    tool_name = "tool_name" if "tool_name" in columns else "NULL AS tool_name"
    tool_calls = "tool_calls" if "tool_calls" in columns else "NULL AS tool_calls"
    visibility = _message_visibility_clause(con)
    return con.execute(
        f"SELECT id, role, content, {tool_name}, {tool_calls} FROM messages "
        f"WHERE session_id=?{visibility} ORDER BY id",
        (session_id,),
    ).fetchall()


def _session_snapshot(
    con: sqlite3.Connection, session_id: str, profile: str = DEFAULT_PROFILE
) -> dict:
    """Read a recovery-oriented snapshot without retaining tool bodies."""
    session = con.execute(
        """
        SELECT id, source, title, started_at, ended_at, end_reason,
               parent_session_id, message_count
        FROM sessions WHERE id=?
        """,
        (session_id,),
    ).fetchone()
    if session is None:
        raise RuntimeError(f"session disappeared during archival: {session_id}")

    try:
        started_at = _source_timestamp(session["started_at"], "started_at")
        ended_at = (
            _source_timestamp(session["ended_at"], "ended_at")
            if session["ended_at"] is not None else None
        )
        message_count = int(session["message_count"] or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(f"invalid session timestamp fields: {session_id}") from exc

    users: list[str] = []
    assistants: list[str] = []
    tool_names: list[str] = []
    tool_call_count = 0
    tool_call_only_turns = 0
    tool_sensitive_kinds: list[str] = []
    unknown_role_names: list[str] = []
    malformed_or_missing_content = 0
    invalid_message_ids = 0
    invalid_fields = 0
    message_fingerprints: list[dict[str, object]] = []
    messages: list[dict[str, object]] = []
    observed_rows = 0
    for row in _ended_message_rows(con, session_id):
        observed_rows += 1
        raw_id = row["id"]
        valid_id = isinstance(raw_id, int) and not isinstance(raw_id, bool)
        source_message_id = int(raw_id) if valid_id else None
        if not valid_id:
            invalid_message_ids += 1
            invalid_fields += 1
        raw_role = row["role"]
        role = raw_role if isinstance(raw_role, str) else None
        if not isinstance(raw_role, str):
            invalid_fields += 1
        is_unknown_role = role not in {"user", "assistant", "tool"}
        if is_unknown_role:
            unknown_role_names.append(role if role is not None else "<missing>")
        raw_content = row["content"]
        content, content_is_archivable = _archivable_text_content(raw_content)
        raw_tool_calls = row["tool_calls"]
        tool_calls_present = _positive_tool_calls_evidence(raw_tool_calls)
        content_blank = isinstance(raw_content, str) and not _normalize(raw_content)
        tool_call_only_turn = (
            role == "assistant"
            and tool_calls_present
            and content_blank
        )
        if role == "tool":
            # Tool output is intentionally excluded; an empty tool body is not
            # a second malformed-content failure.
            pass
        elif tool_call_only_turn:
            tool_call_only_turns += 1
        elif not content_is_archivable or not _normalize(content):
            malformed_or_missing_content += 1
        raw_tool_name = row["tool_name"]
        if raw_tool_name is not None and not isinstance(raw_tool_name, str):
            invalid_fields += 1
        message_fingerprints.append({
            "ordinal": observed_rows,
            "id": source_message_id,
            "role": role,
            "content_sha256": _safe_content_hash(raw_content),
            "content_present": content_is_archivable and bool(_normalize(content)),
            "tool_name": raw_tool_name if isinstance(raw_tool_name, str) else None,
            "tool_calls_present": tool_calls_present,
        })
        if is_unknown_role:
            if isinstance(raw_content, str):
                tool_sensitive_kinds.extend(_sensitive_kinds(content))
            continue
        if role == "user":
            normalized = _normalize(content)
            if normalized:
                users.append(normalized)
        elif role == "assistant":
            normalized = _normalize(content)
            if normalized:
                assistants.append(normalized)
        elif role == "tool":
            tool_call_count += 1
            if isinstance(raw_tool_name, str) and raw_tool_name:
                tool_names.append(raw_tool_name)
            tool_sensitive_kinds.extend(_sensitive_kinds(content))
            continue
        if source_message_id is None or not content_is_archivable:
            continue
        messages.append({
            "source_message_id": source_message_id,
            "role": role,
            "content": content,
        })

    coverage = _coverage_manifest(
        message_rows=observed_rows,
        user_assistant_bodies=len(messages),
        tool_bodies_excluded=tool_call_count,
        tool_call_only_turns=tool_call_only_turns,
        unknown_roles=len(unknown_role_names),
        malformed_or_missing_content=malformed_or_missing_content,
        invalid_message_ids=invalid_message_ids,
        invalid_fields=invalid_fields,
        message_count_mismatch=message_count != observed_rows,
        unknown_role_names=unknown_role_names,
    )
    content_sha256 = hashlib.sha256(
        json.dumps(
            message_fingerprints,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    combined = "\n".join(users + assistants)
    return {
        "profile": _validate_profile(profile),
        "session_id": str(session["id"]),
        "source": str(session["source"] or ""),
        "title": session["title"],
        "started_at": started_at,
        "ended_at": ended_at,
        "end_reason": session["end_reason"],
        "parent_session_id": session["parent_session_id"],
        "message_count": message_count,
        "messages": messages,
        "message_fingerprints": message_fingerprints,
        "user_messages": len(users),
        "assistant_messages": len(assistants),
        "tool_names": sorted(set(tool_names)),
        "tool_call_count": tool_call_count,
        "tool_call_only_turns": tool_call_only_turns,
        "coverage_manifest": coverage,
        "content_sha256": content_sha256,
        "sensitive_kinds": sorted(
            set(
                _sensitive_kinds(combined)
                + _sensitive_kinds(str(session["title"] or ""))
                + tool_sensitive_kinds
            )
        ),
    }


def _source_timestamp(value: object, field: str) -> float:
    """Parse the numeric Unix timestamps used by ended-session state rows."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {field} timestamp")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"invalid {field} timestamp")
    return result


def _ended_session_sort_key(row: sqlite3.Row) -> tuple[bool, float, str]:
    try:
        timestamp = _source_timestamp(row["ended_at"], "ended_at")
    except (TypeError, ValueError, OverflowError):
        return True, 0.0, str(row["id"])
    return False, timestamp, str(row["id"])

def _traceability(snapshot: dict, source_db: Path, *, redacted: bool = False) -> dict:
    return {
        "source_db": str(source_db),
        "profile": snapshot["profile"],
        "session_id": snapshot["session_id"],
        "source": snapshot["source"],
        "title": None if redacted else snapshot["title"],
        "started_at": snapshot["started_at"],
        "ended_at": snapshot["ended_at"],
        "end_reason": snapshot["end_reason"],
        "parent_session_id": snapshot["parent_session_id"],
        "message_count": snapshot["message_count"],
    }


def _archive_record(snapshot: dict, source_db: Path) -> dict:
    digest = snapshot["content_sha256"]
    return {
        "format": SOURCE_ARCHIVE_FORMAT,
        "version": SOURCE_ARCHIVE_VERSION,
        "record_type": "session",
        "profile": snapshot["profile"],
        "session_id": snapshot["session_id"],
        "content_sha256": digest,
        "identity": {
            "profile": snapshot["profile"],
            "session_id": snapshot["session_id"],
            "content_sha256": digest,
        },
        "traceability": _traceability(snapshot, source_db),
        "message_fingerprints": snapshot["message_fingerprints"],
        "coverage_manifest": snapshot["coverage_manifest"],
        # Keep the short key for callers of the earlier development API.
        "coverage": _coverage_compatibility_view(snapshot["coverage_manifest"]),
        "messages": snapshot["messages"],
        "tool_calls": {
            "count": snapshot["tool_call_count"],
            "names": snapshot["tool_names"],
        },
        "content_recovery": "user_assistant_messages_only",
    }


def _blocked_record(snapshot: dict, source_db: Path, kinds: Sequence[str]) -> dict:
    digest = snapshot["content_sha256"]
    return {
        "format": SOURCE_ARCHIVE_FORMAT,
        "version": SOURCE_ARCHIVE_VERSION,
        "record_type": "blocked",
        "profile": snapshot["profile"],
        "session_id": snapshot["session_id"],
        "content_sha256": digest,
        "identity": {
            "profile": snapshot["profile"],
            "session_id": snapshot["session_id"],
            "content_sha256": digest,
        },
        "traceability": _traceability(snapshot, source_db, redacted=True),
        "message_fingerprints": snapshot["message_fingerprints"],
        "coverage_manifest": dict(snapshot["coverage_manifest"]) | {
            "status": "blocked", "cleanup_qualification_blocked": True
        },
        "coverage": _coverage_compatibility_view(dict(snapshot["coverage_manifest"]) | {
            "status": "blocked", "cleanup_qualification_blocked": True
        }),
        "blocked": {
            "reason": "sensitive_content",
            "sensitive_kinds": sorted(set(kinds)),
            "raw_content_written": False,
            "message_bodies": "excluded",
        },
    }


def _validate_message_evidence(
    messages: list[dict],
    fingerprints: object,
    digest: str,
    path: Path,
    label: str,
) -> None:
    """Verify a source/candidate body has a complete, unambiguous fingerprint map."""
    if not isinstance(fingerprints, list) or any(
        not isinstance(fingerprint, dict)
        or not isinstance(fingerprint.get("ordinal"), int)
        or isinstance(fingerprint.get("ordinal"), bool)
        or not isinstance(fingerprint.get("content_sha256"), str)
        for fingerprint in fingerprints
    ):
        raise RuntimeError(f"invalid {label}: {path}")
    calculated_digest = hashlib.sha256(
        json.dumps(
            fingerprints,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if calculated_digest != digest:
        raise RuntimeError(f"invalid {label}: {path}")

    fingerprints_by_message: dict[tuple[int, str], list[dict]] = {}
    present_fingerprint_keys: set[tuple[int, str]] = set()
    for fingerprint in fingerprints:
        message_id = fingerprint.get("id")
        role = fingerprint.get("role")
        if (
            isinstance(message_id, int)
            and not isinstance(message_id, bool)
            and role in {"user", "assistant"}
        ):
            key = (message_id, role)
            fingerprints_by_message.setdefault(key, []).append(fingerprint)
            if fingerprint.get("content_present", True):
                present_fingerprint_keys.add(key)

    message_keys = [
        (message["source_message_id"], message["role"])
        for message in messages
    ]
    message_key_set = set(message_keys)
    if (
        len(message_keys) != len(message_key_set)
        or not message_key_set <= set(fingerprints_by_message)
        or not present_fingerprint_keys <= message_key_set
    ):
        raise RuntimeError(f"invalid {label}: {path}")
    for message, key in zip(messages, message_keys):
        matching = fingerprints_by_message[key]
        if len(matching) != 1 or (
            _safe_content_hash(message["content"]) != matching[0]["content_sha256"]
        ):
            raise RuntimeError(f"invalid {label}: {path}")


def _validate_archive_record(record: object, path: Path) -> dict:
    if not isinstance(record, dict):
        raise RuntimeError(f"invalid source archive: {path}")
    if record.get("format") != SOURCE_ARCHIVE_FORMAT or record.get("version") != SOURCE_ARCHIVE_VERSION:
        raise RuntimeError(f"invalid source archive: {path}")
    if record.get("record_type") not in {"session", "blocked"}:
        raise RuntimeError(f"invalid source archive: {path}")
    session_id = record.get("session_id")
    digest = record.get("content_sha256")
    identity = record.get("identity")
    if not isinstance(session_id, str) or not isinstance(digest, str):
        raise RuntimeError(f"invalid source archive: {path}")
    profile = record.get("profile", DEFAULT_PROFILE)
    if not isinstance(profile, str) or not profile.strip():
        raise RuntimeError(f"invalid source archive: {path}")
    if (
        not isinstance(identity, dict)
        or identity.get("session_id") != session_id
        or identity.get("content_sha256") != digest
        or identity.get("profile", DEFAULT_PROFILE) != profile
    ):
        raise RuntimeError(f"invalid source archive: {path}")
    traceability = record.get("traceability")
    if isinstance(traceability, dict) and traceability.get("profile", profile) != profile:
        raise RuntimeError(f"invalid source archive: {path}")
    if record["record_type"] == "session":
        messages = record.get("messages")
        if not isinstance(messages, list) or any(
            not isinstance(message, dict)
            or message.get("role") not in {"user", "assistant"}
            or not isinstance(message.get("source_message_id"), int)
            or not isinstance(message.get("content"), str)
            for message in messages
        ):
            raise RuntimeError(f"invalid source archive: {path}")
        _validate_message_evidence(
            messages,
            record.get("message_fingerprints"),
            digest,
            path,
            "source archive",
        )
        if "coverage_manifest" in record:
            _validate_coverage_manifest(record["coverage_manifest"], path, "source archive")
        else:
            record["coverage_manifest"] = _legacy_coverage_manifest(record)
        record.setdefault("coverage", _coverage_compatibility_view(record["coverage_manifest"]))
    elif "messages" in record or "content" in record:
        raise RuntimeError(f"invalid source archive: {path}")
    elif "coverage_manifest" in record:
        _validate_coverage_manifest(record["coverage_manifest"], path, "source archive")
    if record["record_type"] == "blocked" and "coverage_manifest" not in record:
        record["coverage_manifest"] = _legacy_coverage_manifest(record) | {
            "status": "blocked",
            "cleanup_qualification_blocked": True,
        }
    if record["record_type"] == "blocked":
        record.setdefault("coverage", _coverage_compatibility_view(record["coverage_manifest"]))
    return record


def load_source_archive(path: Path) -> dict:
    """Load and validate a source archive, returning its records as a list."""
    path = Path(path)
    if not path.is_file():
        if path.exists():
            raise RuntimeError(f"invalid source archive: {path}")
        return {
            "format": SOURCE_ARCHIVE_FORMAT,
            "version": SOURCE_ARCHIVE_VERSION,
            "archive_type": "source",
            "records": [],
        }
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError("empty archive")
        if path.suffix.lower() == ".jsonl":
            records = [
                _validate_archive_record(json.loads(line), path)
                for line in text.splitlines()
                if line.strip()
            ]
            if not records:
                raise ValueError("empty archive")
            envelope = {
                "format": SOURCE_ARCHIVE_FORMAT,
                "version": SOURCE_ARCHIVE_VERSION,
                "archive_type": "source",
                "records": records,
            }
        else:
            envelope = json.loads(text)
            if not isinstance(envelope, dict) or envelope.get("format") != SOURCE_ARCHIVE_FORMAT or envelope.get("version") != SOURCE_ARCHIVE_VERSION or envelope.get("archive_type") != "source" or not isinstance(envelope.get("records"), list):
                raise ValueError("invalid envelope")
            records = [_validate_archive_record(record, path) for record in envelope["records"]]
            envelope = dict(envelope)
            envelope["records"] = records
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid source archive: {path}") from exc

    identities = [
        (
            record.get("profile", DEFAULT_PROFILE),
            record["session_id"],
            record["content_sha256"],
        )
        for record in records
    ]
    if len(identities) != len(set(identities)):
        raise RuntimeError(f"invalid source archive: duplicate identity in {path}")
    return envelope


def _candidate_payload_path(
    candidate_dir: Path,
    session_id: str,
    digest: str,
    profile: str = DEFAULT_PROFILE,
) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id).strip("._") or "session"
    profile = _validate_profile(profile)
    if profile == DEFAULT_PROFILE:
        # Preserve the pre-profile filename for compatibility with old
        # pending payloads while explicit profiles receive isolated paths.
        identity_hash = hashlib.sha256(
            f"{session_id}\0{digest}".encode("utf-8")
        ).hexdigest()
        return candidate_dir / f"candidate-{safe_id}-{digest}-{identity_hash}.json"
    safe_profile = re.sub(r"[^A-Za-z0-9_.-]+", "_", profile).strip("._") or "profile"
    identity_hash = hashlib.sha256(
        f"{profile}\0{session_id}\0{digest}".encode("utf-8")
    ).hexdigest()
    return candidate_dir / f"candidate-{safe_profile}-{safe_id}-{digest}-{identity_hash}.json"


def _candidate_payload(snapshot: dict, candidate: Candidate, source_db: Path) -> dict:
    digest = snapshot["content_sha256"]
    return {
        "format": CANDIDATE_PAYLOAD_FORMAT,
        "version": CANDIDATE_PAYLOAD_VERSION,
        "record_type": "candidate_payload",
        "review_status": "pending",
        "profile": snapshot["profile"],
        "session_id": snapshot["session_id"],
        "content_sha256": digest,
        "identity": {
            "profile": snapshot["profile"],
            "session_id": snapshot["session_id"],
            "content_sha256": digest,
        },
        "traceability": _traceability(snapshot, source_db),
        "coverage_manifest": snapshot["coverage_manifest"],
        "coverage": _coverage_compatibility_view(snapshot["coverage_manifest"]),
        "candidate": asdict(candidate) | {"content_sha256": digest},
        "messages": snapshot["messages"],
        "message_fingerprints": snapshot["message_fingerprints"],
        "tool_calls": {
            "count": snapshot["tool_call_count"],
            "names": snapshot["tool_names"],
        },
        "formal_archive": {
            "kb_add_called": False,
            "committed": False,
        },
    }


def _validate_candidate_payload(
    path: Path,
    expected_session_id: str,
    expected_digest: str,
    expected_profile: str = DEFAULT_PROFILE,
) -> None:
    expected_profile = _validate_profile(expected_profile)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid candidate payload: {path}") from exc
    identity = data.get("identity") if isinstance(data, dict) else None
    messages = data.get("messages") if isinstance(data, dict) else None
    payload_profile = data.get("profile", DEFAULT_PROFILE) if isinstance(data, dict) else None
    identity_profile = identity.get("profile", DEFAULT_PROFILE) if isinstance(identity, dict) else None
    traceability = data.get("traceability") if isinstance(data, dict) else None
    traceability_profile = (
        traceability.get("profile", DEFAULT_PROFILE)
        if isinstance(traceability, dict)
        else None
    )
    candidate = data.get("candidate") if isinstance(data, dict) else None
    candidate_profile = (
        candidate.get("profile", DEFAULT_PROFILE)
        if isinstance(candidate, dict)
        else expected_profile
    )
    if isinstance(data, dict) and "coverage_manifest" in data:
        _validate_coverage_manifest(data["coverage_manifest"], path, "candidate payload")
    if isinstance(data, dict) and "coverage" in data:
        coverage = data["coverage"]
        if not isinstance(coverage, dict):
            raise RuntimeError(f"invalid candidate payload coverage: {path}")
        manifest = data.get("coverage_manifest")
        if isinstance(manifest, dict) and coverage != _coverage_compatibility_view(manifest):
            raise RuntimeError(f"invalid candidate payload coverage: {path}")
    if isinstance(messages, list) and isinstance(data, dict):
        _validate_message_evidence(
            messages,
            data.get("message_fingerprints"),
            expected_digest,
            path,
            "candidate payload",
        )
    if (
        not isinstance(data, dict)
        or data.get("format") != CANDIDATE_PAYLOAD_FORMAT
        or data.get("version") != CANDIDATE_PAYLOAD_VERSION
        or data.get("record_type") != "candidate_payload"
        or data.get("review_status") != "pending"
        or data.get("session_id") != expected_session_id
        or data.get("content_sha256") != expected_digest
        or payload_profile != expected_profile
        or not isinstance(identity, dict)
        or identity.get("session_id") != expected_session_id
        or identity.get("content_sha256") != expected_digest
        or identity_profile != expected_profile
        or candidate_profile != expected_profile
        or (
            isinstance(traceability, dict)
            and traceability_profile != expected_profile
        )
        or not isinstance(messages, list)
        or any(
            not isinstance(message, dict)
            or message.get("role") not in {"user", "assistant"}
            or not isinstance(message.get("content"), str)
            for message in messages
        )
    ):
        raise RuntimeError(f"invalid candidate payload: {path}")


def load_candidate_payload(path: Path) -> dict:
    """Load a pending candidate, normalizing old unprofiled payloads in memory."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid candidate payload: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"invalid candidate payload: {path}")
    session_id = data.get("session_id")
    digest = data.get("content_sha256")
    profile = data.get("profile", DEFAULT_PROFILE)
    if not isinstance(session_id, str) or not isinstance(digest, str):
        raise RuntimeError(f"invalid candidate payload: {path}")
    _validate_candidate_payload(path, session_id, digest, profile)
    normalized = dict(data)
    normalized["profile"] = profile
    if isinstance(normalized.get("identity"), dict):
        normalized["identity"] = dict(normalized["identity"]) | {"profile": profile}
    if isinstance(normalized.get("traceability"), dict):
        normalized["traceability"] = dict(normalized["traceability"]) | {"profile": profile}
    if isinstance(normalized.get("candidate"), dict):
        normalized["candidate"] = dict(normalized["candidate"]) | {"profile": profile}
    if "coverage_manifest" not in normalized:
        manifest = _legacy_coverage_manifest(normalized)
        normalized["coverage_manifest"] = manifest
        normalized.setdefault("coverage", _coverage_compatibility_view(manifest))
    return normalized


def _reject_archive_collisions(source_db: Path, source_archive: Path, candidate_dir: Path | None) -> None:
    source = Path(source_db).resolve()
    archive = Path(source_archive).resolve()
    if source == archive:
        raise ArchiveCollisionError("source DB and source archive must be different paths")
    if candidate_dir is None:
        return
    candidates = Path(candidate_dir).resolve()
    if candidates == source or candidates == archive:
        raise ArchiveCollisionError("source and archive destinations must be different paths")
    if candidates.exists() and not candidates.is_dir():
        raise ArchiveCollisionError(f"candidate destination is not a directory: {candidate_dir}")


def _prepare_archive_plan(
    source_db: Path,
    candidates: Sequence[Candidate],
    skipped: Sequence[Skip],
    source_archive: Path,
    candidate_dir: Path | None = None,
    profile: str = DEFAULT_PROFILE,
    snapshots: Mapping[tuple[str, str], dict] | None = None,
) -> _ArchivePlan:
    """Build and validate archival outputs without publishing any output."""

    source_db = Path(source_db)
    source_archive = Path(source_archive)
    candidate_dir = Path(candidate_dir) if candidate_dir is not None else None
    profile = _validate_profile(profile)
    _reject_archive_collisions(source_db, source_archive, candidate_dir)
    existing = load_source_archive(source_archive)
    existing_records = list(existing["records"])
    existing_by_identity = {
        (
            record.get("profile", DEFAULT_PROFILE),
            record["session_id"],
            record["content_sha256"],
        ): record
        for record in existing_records
    }

    requested: list[tuple[str, str, Candidate | None, list[str]]] = []
    requested_ids: set[tuple[str, str]] = set()
    for candidate in candidates:
        item_profile = _validate_profile(getattr(candidate, "profile", profile))
        if item_profile == DEFAULT_PROFILE and profile != DEFAULT_PROFILE:
            item_profile = profile
        key = (item_profile, candidate.session_id)
        if key not in requested_ids:
            requested.append((item_profile, candidate.session_id, candidate, []))
            requested_ids.add(key)
    for item in skipped:
        item_profile = _validate_profile(getattr(item, "profile", profile))
        if item_profile == DEFAULT_PROFILE and profile != DEFAULT_PROFILE:
            item_profile = profile
        key = (item_profile, item.session_id)
        if item.reason == "sensitive_content" and key not in requested_ids:
            requested.append((item_profile, item.session_id, None, list(item.sensitive_kinds)))
            requested_ids.add(key)

    planned_snapshots: list[tuple[dict, Candidate | None, list[str]]] = []
    if snapshots is None:
        con = _readonly_connect(source_db)
        try:
            con.execute("BEGIN")
            for item_profile, session_id, candidate, skipped_kinds in requested:
                snapshot = _session_snapshot(con, session_id, item_profile)
                kinds = sorted(set(snapshot["sensitive_kinds"] + skipped_kinds))
                planned_snapshots.append((snapshot, candidate, kinds))
        finally:
            con.close()
    else:
        for item_profile, session_id, candidate, skipped_kinds in requested:
            snapshot = snapshots.get((item_profile, session_id))
            if snapshot is None:
                raise RuntimeError(
                    f"missing selected-session snapshot for archive plan: {item_profile}:{session_id}"
                )
            kinds = sorted(set(snapshot["sensitive_kinds"] + skipped_kinds))
            planned_snapshots.append((snapshot, candidate, kinds))

    new_records: list[dict] = []
    candidate_writes: list[tuple[Path, str]] = []
    for snapshot, candidate, kinds in planned_snapshots:
        digest = snapshot["content_sha256"]
        if (
            snapshots is None
            and candidate is not None
            and candidate.content_sha256 != digest
        ):
            raise RuntimeError(
                "source session changed after candidate selection; rerun collection before archival"
            )
        identity = (snapshot["profile"], snapshot["session_id"], digest)
        record = _blocked_record(snapshot, source_db, kinds) if candidate is None or kinds else _archive_record(snapshot, source_db)
        if identity not in existing_by_identity:
            new_records.append(record)
        if candidate is not None and not kinds:
            existing_record = existing_by_identity.get(identity)
            if existing_record is not None and existing_record["record_type"] == "blocked":
                continue
            if candidate_dir is None:
                raise ValueError("candidate_dir is required when candidate payloads exist")
            path = _candidate_payload_path(
                candidate_dir,
                snapshot["session_id"],
                digest,
                snapshot["profile"],
            )
            if path.exists():
                if path.is_dir():
                    raise RuntimeError(f"invalid candidate payload: {path}")
                _validate_candidate_payload(
                    path,
                    snapshot["session_id"],
                    digest,
                    snapshot["profile"],
                )
            else:
                candidate_writes.append((
                    path,
                    json.dumps(
                        _candidate_payload(snapshot, candidate, source_db),
                        ensure_ascii=False,
                        indent=2,
                    ) + "\n",
                ))

    # Preflight all writes before changing any destination. A corrupt archive
    # or payload therefore cannot leave a newly-created sibling output behind.
    archive_text: str | None = None
    if new_records:
        merged_records = existing_records + new_records
        if source_archive.suffix.lower() == ".jsonl":
            archive_text = "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                for record in merged_records
            )
        else:
            envelope = dict(existing)
            envelope.update({
                "format": SOURCE_ARCHIVE_FORMAT,
                "version": SOURCE_ARCHIVE_VERSION,
                "archive_type": "source",
                "records": merged_records,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            archive_text = json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"
        for path, _payload in candidate_writes:
            if path.resolve() == source_archive.resolve() or path.resolve() == source_db.resolve():
                raise ArchiveCollisionError(f"candidate payload collides with another path: {path}")
    elif candidate_writes:
        raise RuntimeError("candidate payloads have no source archive record")

    result = ArchiveResult(
        source_records_added=len(new_records),
        blocked_records_added=sum(record["record_type"] == "blocked" for record in new_records),
        candidate_payloads_written=len(candidate_writes),
        source_archive_path=source_archive,
        candidate_payload_paths=tuple(path for path, _payload in candidate_writes),
    )
    return _ArchivePlan(result, archive_text, tuple(candidate_writes))


def archive_collected_sessions(
    source_db: Path,
    candidates: Sequence[Candidate],
    skipped: Sequence[Skip],
    source_archive: Path,
    candidate_dir: Path | None = None,
    profile: str = DEFAULT_PROFILE,
    snapshots: Mapping[tuple[str, str], dict] | None = None,
) -> ArchiveResult:
    """Persist selected safe sessions and blocked manifests, without KB writes.

    This compatibility entry point retains the prior direct archival behavior.
    The incremental-run entry point uses the same plan but publishes it only
    after the complete run has been staged and validated.
    """
    source_archive = Path(source_archive)
    _recover_staged_publish(source_archive)
    plan = _prepare_archive_plan(
        source_db, candidates, skipped, source_archive, candidate_dir, profile, snapshots
    )
    with ExitStack() as staging_stack:
        staged_outputs: list[tuple[Path, Path]] = []
        for index, (path, payload_text) in enumerate(plan.candidate_writes):
            staged_outputs.append((
                _destination_staging_file(staging_stack, path, f"candidate-{index}.json", payload_text),
                path,
            ))
        if plan.archive_text is not None:
            staged_outputs.append((
                _destination_staging_file(
                    staging_stack, plan.result.source_archive_path, "source-archive.json", plan.archive_text
                ),
                plan.result.source_archive_path,
            ))
        _publish_staged_outputs(staged_outputs, recovery_key=source_archive)
    return plan.result


# Descriptive aliases for callers that use the operation name rather than the
# collection phase name.
archive_sessions = archive_collected_sessions
read_source_archive = load_source_archive


def _score(
    users: list[str], assistants: list[str], tools: list[str]
) -> tuple[int, list[str]]:
    user_text = "\n".join(users).lower()
    assistant_text = "\n".join(assistants).lower()
    score = 0
    signals: list[str] = []
    if any(term in user_text for term in ARCHIVE_TERMS):
        score += 4
        signals.append("explicit_archive_request")
    evidence_hits = sorted({term for term in EVIDENCE_TERMS if term in assistant_text})
    if evidence_hits:
        score += min(4, 1 + len(evidence_hits) // 2)
        signals.append("verified_evidence")
    if any(term.lower() in assistant_text for term in STRUCTURE_TERMS):
        score += 1
        signals.append("structured_conclusion")
    if len(assistant_text) >= 600:
        score += 1
        signals.append("substantive_final_content")
    if len(set(tools)) >= 2:
        score += 1
        signals.append("multi_tool_work")
    if any(term in assistant_text for term in TRANSIENT_TERMS) and not evidence_hits:
        score -= 3
        signals.append("transient_or_unfinished")
    return score, signals


def _unreadable_coverage_manifest(*, invalid_fields: int = 1) -> dict[str, object]:
    return {
        "version": 1,
        "status": "blocked",
        "message_rows": 0,
        "user_assistant_bodies": 0,
        "tool_bodies_excluded": 0,
        "tool_call_only_turns": 0,
        "unknown_roles": 0,
        "unknown_role_names": [],
        "malformed_or_missing_content": 0,
        "invalid_message_ids": 0,
        "invalid_fields": invalid_fields,
        "message_count_mismatch": False,
        "cleanup_qualification_blocked": True,
    }


def collect(
    db_path: Path,
    state: dict,
    min_effective_messages: int = 4,
    min_score: int = 3,
    profile: str = DEFAULT_PROFILE,
    snapshots: MutableMapping[tuple[str, str], dict] | None = None,
) -> tuple[list[Candidate], list[Skip], list[ChildAgentLog], dict]:
    candidates: list[Candidate] = []
    skipped: list[Skip] = []
    child_agent_logs: list[ChildAgentLog] = []
    profile = _validate_profile(profile)
    checkpoint = _normalize_checkpoint(state)
    processed = checkpoint["processed"]
    watermark = checkpoint["watermark"]
    seen: dict[str, dict] = {}
    con = _readonly_connect(db_path)
    try:
        # Keep selection, message reads and all cached source records on one
        # SQLite snapshot while Hermes may append concurrently in WAL mode.
        con.execute("BEGIN")
        schema = _source_schema_report_for_connection(con)
        if not schema["valid"]:
            missing = "; ".join(
                f"{table}: {', '.join(fields)}"
                for table, fields in sorted(schema["missing_required"].items())
            )
            raise RuntimeError(f"invalid source schema; missing required fields: {missing}")
        profile_clause, profile_params = _session_profile_clause(con, profile)
        sessions = con.execute(
            """
            SELECT id, source, title, started_at, ended_at, end_reason,
                   parent_session_id, message_count
            FROM sessions
            WHERE ended_at IS NOT NULL
            """ + profile_clause,
            profile_params,
        ).fetchall()
        # SQLite may return mixed numeric/text values from a weakly typed
        # source. Sort valid timestamps numerically and invalid rows last so
        # equal timestamps still have deterministic ID ordering.
        sessions.sort(key=_ended_session_sort_key)
        for row in sessions:
            sid = str(row["id"])
            source = str(row["source"] or "")
            title = row["title"]
            try:
                snapshot = _session_snapshot(con, sid, profile)
            except RuntimeError as error:
                # The row cannot safely participate in identity/checkpointing,
                # but one malformed session must not abort ordinary collection.
                reason = (
                    "invalid_timestamp_fields"
                    if "timestamp" in str(error)
                    else "malformed_session_fields"
                )
                skipped.append(Skip(
                    sid, source, title, reason, [], profile,
                    _unreadable_coverage_manifest(),
                ))
                continue

            if snapshots is not None:
                snapshots[(profile, sid)] = snapshot

            users = [
                item["content"] for item in snapshot["messages"]
                if item["role"] == "user" and isinstance(item["content"], str)
            ]
            assistants = [
                item["content"] for item in snapshot["messages"]
                if item["role"] == "assistant" and isinstance(item["content"], str)
            ]
            tools = snapshot["tool_names"]
            digest = snapshot["content_sha256"]
            identity = processed_identity(sid, digest, profile)
            legacy_record = processed.get(_legacy_processed_identity(sid, digest))
            if legacy_record is None:
                legacy_record = processed.get(sid)
            if identity in processed or (
                profile == DEFAULT_PROFILE
                and isinstance(legacy_record, dict)
                and legacy_record.get("content_sha256") in (None, digest)
            ) or (
                isinstance(legacy_record, dict)
                and legacy_record.get("session_id") == sid
                and legacy_record.get("profile") == profile
                and legacy_record.get("content_sha256") in (None, digest)
            ):
                continue

            coverage = snapshot["coverage_manifest"]
            sensitive = snapshot["sensitive_kinds"]
            blocked_coverage = dict(coverage) | {
                "status": "blocked",
                "cleanup_qualification_blocked": True,
            }
            if snapshot["parent_session_id"] is not None:
                child_agent_logs.append(ChildAgentLog(
                    session_id=sid,
                    parent_session_id=str(snapshot["parent_session_id"]),
                    source=source,
                    title=title,
                    started_at=snapshot["started_at"],
                    ended_at=snapshot["ended_at"],
                    end_reason=snapshot["end_reason"],
                    user_messages=len(users),
                    assistant_messages=len(assistants),
                    tool_calls=snapshot["tool_call_count"],
                    tool_names=tools,
                    content_sha256=digest,
                    sensitive_kinds=sensitive,
                    final_preview="" if sensitive else _preview(assistants[-1] if assistants else ""),
                    profile=profile,
                    coverage_manifest=blocked_coverage if sensitive else coverage,
                ))
                seen[sid] = {
                    "decision": "child_agent_log",
                    "profile": profile,
                    "content_sha256": digest,
                    "ended_at": snapshot["ended_at"],
                    "sensitive": bool(sensitive),
                    "coverage_manifest": coverage,
                }
                continue
            if source.lower() in EXCLUDED_SOURCES or any(
                x in source.lower() for x in ("cron", "delegate", "aux")
            ):
                skipped.append(Skip(sid, source, title, "excluded_source", [], profile, coverage))
                seen[sid] = {
                    "decision": "skip", "profile": profile, "content_sha256": digest,
                    "ended_at": snapshot["ended_at"], "reason": "excluded_source",
                    "coverage_manifest": coverage,
                }
                continue
            if sensitive:
                skipped.append(Skip(
                    sid, source, title, "sensitive_content", sensitive, profile,
                    blocked_coverage,
                ))
                seen[sid] = {
                    "decision": "blocked", "profile": profile, "content_sha256": digest,
                    "ended_at": snapshot["ended_at"], "reason": "sensitive_content",
                    "coverage_manifest": blocked_coverage,
                }
                continue
            if len(users) + len(assistants) < min_effective_messages:
                skipped.append(Skip(sid, source, title, "too_few_effective_messages", [], profile, coverage))
                seen[sid] = {
                    "decision": "skip", "profile": profile, "content_sha256": digest,
                    "ended_at": snapshot["ended_at"], "reason": "too_few_effective_messages",
                    "coverage_manifest": coverage,
                }
                continue
            score, signals = _score(users, assistants, tools)
            if score < min_score:
                skipped.append(Skip(sid, source, title, "low_signal", [], profile, coverage))
                seen[sid] = {
                    "decision": "skip", "profile": profile, "content_sha256": digest,
                    "ended_at": snapshot["ended_at"], "reason": "low_signal", "score": score,
                    "coverage_manifest": coverage,
                }
                continue
            candidate = Candidate(
                session_id=sid,
                source=source,
                title=title,
                started_at=snapshot["started_at"],
                ended_at=snapshot["ended_at"],
                end_reason=snapshot["end_reason"],
                user_messages=len(users),
                assistant_messages=len(assistants),
                tool_calls=snapshot["tool_call_count"],
                tool_names=tools,
                content_sha256=digest,
                score=score,
                signals=signals,
                user_preview=_preview(users[-1] if users else ""),
                final_preview=_preview(assistants[-1] if assistants else ""),
                profile=profile,
                coverage_manifest=coverage,
            )
            candidates.append(candidate)
            seen[sid] = {
                "decision": "review", "profile": profile, "content_sha256": digest,
                "ended_at": snapshot["ended_at"], "score": score,
                "coverage_manifest": coverage,
            }
    finally:
        con.close()
    return candidates, skipped, child_agent_logs, seen


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _aggregate_coverage(
    candidates: Sequence[Candidate],
    skipped: Sequence[Skip],
    child_agent_logs: Sequence[ChildAgentLog],
) -> dict[str, object]:
    manifests = [
        item.coverage_manifest
        for item in (*candidates, *skipped, *child_agent_logs)
        if item.coverage_manifest
    ]
    status = "complete"
    if any(item.get("status") == "blocked" for item in manifests):
        status = "blocked"
    elif any(item.get("status") == "partial" for item in manifests):
        status = "partial"
    keys = (
        "message_rows", "user_assistant_bodies", "tool_bodies_excluded",
        "tool_call_only_turns", "unknown_roles", "malformed_or_missing_content", "invalid_message_ids",
        "invalid_fields",
    )
    status_counts = {name: 0 for name in ("complete", "partial", "blocked")}
    for item in manifests:
        item_status = item.get("status")
        if item_status in status_counts:
            status_counts[item_status] += 1
    return {
        "version": 1,
        "status": status,
        "sessions": len(manifests),
        "status_counts": status_counts,
        **{key: sum(int(item.get(key, 0)) for item in manifests) for key in keys},
        "unknown_role_names": sorted({
            name
            for item in manifests
            for name in item.get("unknown_role_names", [])
            if isinstance(name, str)
        }),
        "cleanup_qualification_blocked": status != "complete",
    }


def render_json(
    candidates: list[Candidate],
    skipped: list[Skip],
    child_agent_logs: list[ChildAgentLog],
    db_path: Path,
    *,
    archival_mode: bool = False,
    source_records_added: int = 0,
    candidate_payloads_written: int = 0,
    generated_at: str | None = None,
    profile: str = DEFAULT_PROFILE,
) -> dict:
    return {
        "version": 1,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "source_db": str(db_path),
        "profile": _validate_profile(profile),
        "dry_run": not archival_mode,
        "archival_mode": archival_mode,
        "archive_writes": source_records_added + candidate_payloads_written,
        "source_records_added": source_records_added,
        "candidate_payloads_written": candidate_payloads_written,
        "candidate_count": len(candidates),
        "skipped_count": len(skipped),
        "child_agent_log_count": len(child_agent_logs),
        "coverage_manifest": _aggregate_coverage(
            candidates, skipped, child_agent_logs
        ),
        "candidates": [
            asdict(x) | {"started_at_iso": _iso(x.started_at), "ended_at_iso": _iso(x.ended_at)}
            for x in candidates
        ],
        "skipped": [asdict(x) for x in skipped],
        "child_agent_logs": [
            asdict(x) | {"started_at_iso": _iso(x.started_at), "ended_at_iso": _iso(x.ended_at)}
            for x in child_agent_logs
        ],
    }


def render_markdown(report: dict) -> str:
    lines = [
        "# 🦊 Fufu Archive 会话候选 Dry-Run",
        "",
        f"> 生成：{report['generated_at']}",
        f"> 候选：{report['candidate_count']}｜子 agent 优化日志：{report['child_agent_log_count']}｜跳过/阻断：{report['skipped_count']}｜档案馆写入：0",
        "",
        "## 待审候选",
        "",
    ]
    if not report["candidates"]:
        lines.append("- 本轮没有候选。")
    for i, item in enumerate(report["candidates"], 1):
        lines += [
            f"### {i}. {item.get('title') or item['session_id']}",
            f"- Session：`{item['session_id']}`",
            f"- 来源：`{item['source']}`｜分数：{item['score']}｜信号：{', '.join(item['signals'])}",
            f"- 工具：{', '.join(item['tool_names']) or '无'}",
            f"- 用户末条预览：{item['user_preview'] or '（空）'}",
            f"- 最终回复预览：{item['final_preview'] or '（空）'}",
            "",
        ]
    coverage = report.get("coverage_manifest", {})
    schema = report.get("source_schema")
    lines += [
        "## Source schema",
        "",
        (
            f"- Valid: `{schema.get('valid')}`; missing required: "
            f"`{schema.get('missing_required', {})}`; optional fallback: "
            f"`{schema.get('optional_fallbacks', {})}`"
            if isinstance(schema, dict)
            else "- Schema audit: `not included (legacy report)`"
        ),
        "",
        "## Coverage",
        "",
        f"- Status: `{coverage.get('status', 'legacy')}`; "
        f"user/assistant bodies: `{coverage.get('user_assistant_bodies', 0)}`; "
        f"tool bodies excluded: `{coverage.get('tool_bodies_excluded', 0)}`; "
        f"unknown roles: `{coverage.get('unknown_roles', 0)}`; "
        f"malformed/missing content: `{coverage.get('malformed_or_missing_content', 0)}`",
        f"- Session status counts: `{coverage.get('status_counts', {})}`",
        "",
        "## 子 Agent 优化日志", ""
    ]
    if not report["child_agent_logs"]:
        lines.append("- 本轮没有已结束的子 agent 会话。")
    for item in report["child_agent_logs"]:
        preview = "（敏感内容已阻断）" if item["sensitive_kinds"] else (item["final_preview"] or "（无最终回复）")
        lines += [
            f"### {item.get('title') or item['session_id']}",
            f"- Session：`{item['session_id']}`｜Parent：`{item['parent_session_id']}`",
            f"- 结束：`{item.get('end_reason') or 'unknown'}`｜工具调用：{item['tool_calls']}｜工具：{', '.join(item['tool_names']) or '无'}",
            f"- 最终回复预览：{preview}",
            "",
        ]
    counts: dict[str, int] = {}
    for item in report["skipped"]:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
    lines += ["## 跳过/阻断统计", ""]
    if counts:
        lines.extend(f"- `{reason}`：{count}" for reason, count in sorted(counts.items()))
    else:
        lines.append("- 无。")
    lines += [
        "",
        "## 安全说明",
        "",
        "本报告不包含 tool 输出正文；敏感会话只记录阻断类型，不记录正文预览；本次不会调用 `kb_add`。",
        "",
    ]
    return "\n".join(lines)


def _active_snapshot_report_paths(report_output: Path) -> tuple[Path, Path]:
    """Resolve the stable JSON/Markdown destinations for active snapshots."""
    report_output = Path(report_output)
    if report_output.exists() and report_output.is_file():
        if report_output.suffix.lower() not in {".json", ".md"}:
            raise ValueError("active snapshot report output must end in .json or .md")
        if report_output.suffix.lower() == ".json":
            return report_output, report_output.with_suffix(".md")
        return report_output.with_suffix(".json"), report_output
    if report_output.suffix.lower() in {".json", ".md"}:
        if report_output.suffix.lower() == ".json":
            return report_output, report_output.with_suffix(".md")
        return report_output.with_suffix(".json"), report_output
    return report_output / "active-snapshot.json", report_output / "active-snapshot.md"


def _sqlite_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')}


def _message_timestamp_column(con: sqlite3.Connection) -> str | None:
    columns = _sqlite_columns(con, "messages")
    for candidate in (
        "created_at", "timestamp", "created_ts", "sent_at", "message_at",
        "created", "ts", "time",
    ):
        if candidate in columns:
            return candidate
    return None


def _coerce_timestamp(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            try:
                normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
                parsed = datetime.fromisoformat(normalized)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except ValueError:
                return None
    return None


def _caller_last_message_at(
    supplied: float | Mapping[str, float] | Callable[[str], float] | None,
    session_id: str,
) -> float | None:
    if supplied is None:
        return None
    if isinstance(supplied, Mapping):
        value = supplied.get(session_id)
    elif callable(supplied):
        value = supplied(session_id)
    else:
        value = supplied
    return _coerce_timestamp(value)


def _clock_value(now: float | Callable[[], float] | None) -> float:
    value = datetime.now(timezone.utc).timestamp() if now is None else (
        now() if callable(now) else now
    )
    timestamp = _coerce_timestamp(value)
    if timestamp is None:
        raise ValueError("now must be a numeric timestamp or callable returning one")
    return timestamp


def _active_snapshot_hash(
    profile: str,
    source: str,
    session_id: str,
    message_fingerprints: list[dict[str, object]],
) -> str:
    identity = {
        "profile": profile,
        "source": source,
        "session_id": session_id,
        "message_fingerprints": message_fingerprints,
    }
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_active_snapshots(
    con: sqlite3.Connection,
    profile: str,
) -> tuple[list[dict], list[dict], str | None]:
    """Read active sessions and all messages under one consistent read transaction."""
    timestamp_column = _message_timestamp_column(con)
    timestamp_select = (
        f', "{timestamp_column}" AS __message_timestamp'
        if timestamp_column is not None else ""
    )
    profile_clause, profile_params = _session_profile_clause(con, profile)
    sessions = con.execute(
        """
        SELECT id, source, title, started_at, ended_at, end_reason,
               parent_session_id, message_count
        FROM sessions
        WHERE ended_at IS NULL
        """ + profile_clause + " ORDER BY id",
        profile_params,
    ).fetchall()
    snapshots: list[dict] = []
    child_sessions: list[dict] = []
    for session in sessions:
        session_id = str(session["id"])
        source = str(session["source"] or "")
        messages: list[dict[str, object]] = []
        message_fingerprints: list[dict[str, object]] = []
        users: list[str] = []
        assistants: list[str] = []
        tool_names: list[str] = []
        tool_call_count = 0
        sensitive_kinds: list[str] = []
        last_message_at: float | None = None
        last_message_id: int | None = None
        unknown_roles: list[str] = []
        malformed_or_missing_content = 0
        invalid_message_ids = 0
        invalid_fields = 0
        visibility = _message_visibility_clause(con)
        rows = con.execute(
            f'SELECT id, role, content, tool_name{timestamp_select} '
            f"FROM messages WHERE session_id=?{visibility} ORDER BY id",
            (session_id,),
        ).fetchall()
        for row in rows:
            raw_message_id = row["id"]
            raw_content = row["content"]
            role = row["role"]
            if timestamp_column is not None:
                parsed_timestamp = _coerce_timestamp(row["__message_timestamp"])
                if parsed_timestamp is not None:
                    last_message_at = max(last_message_at or parsed_timestamp, parsed_timestamp)
            if not isinstance(raw_message_id, int) or isinstance(raw_message_id, bool):
                invalid_message_ids += 1
                continue
            message_id = raw_message_id
            content, content_is_archivable = _archivable_text_content(raw_content)
            if not content_is_archivable:
                malformed_or_missing_content += 1
            content_hash = _safe_content_hash(raw_content)
            message_fingerprints.append({
                "id": message_id,
                "content_sha256": content_hash,
            })
            last_message_id = message_id
            if not isinstance(role, str):
                invalid_fields += 1
                unknown_roles.append(type(role).__name__)
                continue
            if role == "tool":
                tool_call_count += 1
                if row["tool_name"]:
                    tool_names.append(str(row["tool_name"]))
                sensitive_kinds.extend(_sensitive_kinds(content))
                continue
            if role not in {"user", "assistant"}:
                unknown_roles.append(role)
                continue
            normalized = _normalize(content)
            if normalized:
                (users if role == "user" else assistants).append(normalized)
            # Only user/assistant bodies are eligible for output; tool rows above
            # remain represented only by their ID/content hash and metadata.
            if content_is_archivable:
                messages.append({
                    "source_message_id": message_id,
                    "role": role,
                    "content": content,
                })
        snapshot = {
            "profile": profile,
            "session_id": session_id,
            "source": source,
            "title": session["title"],
            "started_at": float(session["started_at"]),
            "ended_at": None,
            "end_reason": session["end_reason"],
            "parent_session_id": session["parent_session_id"],
            "message_count": int(session["message_count"] or 0),
            "messages": messages,
            "message_fingerprints": message_fingerprints,
            "last_message_id": last_message_id,
            "last_message_at": last_message_at,
            "user_messages": len(users),
            "assistant_messages": len(assistants),
            "tool_names": sorted(set(tool_names)),
            "tool_call_count": tool_call_count,
            "coverage_manifest": _coverage_manifest(
                message_rows=len(rows),
                user_assistant_bodies=len(messages),
                tool_bodies_excluded=tool_call_count,
                tool_call_only_turns=0,
                unknown_roles=len(unknown_roles),
                malformed_or_missing_content=malformed_or_missing_content,
                invalid_message_ids=invalid_message_ids,
                invalid_fields=invalid_fields,
                message_count_mismatch=int(session["message_count"] or 0) != len(rows),
                unknown_role_names=unknown_roles,
            ),
            "content_sha256": hashlib.sha256("\n".join(users + assistants).encode("utf-8")).hexdigest(),
            "sensitive_kinds": sorted(set(
                sensitive_kinds
                + _sensitive_kinds("\n".join(users + assistants))
                + _sensitive_kinds(str(session["title"] or ""))
            )),
        }
        snapshot["snapshot_hash"] = _active_snapshot_hash(
            profile, source, session_id, message_fingerprints
        )
        if session["parent_session_id"] is not None:
            child_sessions.append({
                "session_id": session_id,
                "parent_session_id": str(session["parent_session_id"]),
                "source": source,
                "snapshot_hash": snapshot["snapshot_hash"],
                "status": "separate_child_agent_session",
            })
        else:
            snapshots.append(snapshot)
    return snapshots, child_sessions, timestamp_column


def _active_snapshot_record(snapshot: dict, source_db: Path, *, blocked: bool = False) -> dict:
    identity = {
        "profile": snapshot["profile"],
        "source": snapshot["source"],
        "session_id": snapshot["session_id"],
        "message_fingerprints": snapshot["message_fingerprints"],
        "snapshot_hash": snapshot["snapshot_hash"],
    }
    record = {
        "format": ACTIVE_SNAPSHOT_FORMAT,
        "version": ACTIVE_SNAPSHOT_VERSION,
        "snapshot_version": 1,
        "record_type": "blocked" if blocked else "active_snapshot",
        "lifecycle": "active_snapshot",
        "profile": snapshot["profile"],
        "source": snapshot["source"],
        "session_id": snapshot["session_id"],
        "snapshot_hash": snapshot["snapshot_hash"],
        "identity": identity,
        "content_sha256": snapshot["content_sha256"],
        "traceability": _traceability(snapshot, source_db),
        "last_message_id": snapshot["last_message_id"],
        "quiet_policy": "explicit_quiet_window",
        "coverage_manifest": snapshot["coverage_manifest"],
    }
    if blocked:
        record["blocked"] = {
            "reason": "sensitive_content",
            "sensitive_kinds": snapshot["sensitive_kinds"],
            "raw_content_written": False,
            "message_bodies": "excluded",
        }
    else:
        record.update({
            "messages": snapshot["messages"],
            "tool_calls": {
                "count": snapshot["tool_call_count"],
                "names": snapshot["tool_names"],
            },
            "content_recovery": "user_assistant_messages_only",
            "formal_kb": {"searchable": False, "candidate": False},
        })
    return record


def _validate_active_snapshot_record(record: object, path: Path) -> dict:
    if not isinstance(record, dict):
        raise RuntimeError(f"invalid active snapshot archive: {path}")
    if record.get("format") != ACTIVE_SNAPSHOT_FORMAT or record.get("version") != ACTIVE_SNAPSHOT_VERSION:
        raise RuntimeError(f"invalid active snapshot archive: {path}")
    if record.get("record_type") not in {"active_snapshot", "blocked"}:
        raise RuntimeError(f"invalid active snapshot archive: {path}")
    identity = record.get("identity")
    required = ("profile", "source", "session_id", "message_fingerprints")
    if (
        not isinstance(identity, dict)
        or any(key not in identity for key in required)
        or record.get("profile") != identity.get("profile")
        or record.get("source") != identity.get("source")
        or record.get("session_id") != identity.get("session_id")
        or identity.get("snapshot_hash") != record.get("snapshot_hash")
        or not isinstance(record.get("snapshot_hash"), str)
        or not isinstance(identity["message_fingerprints"], list)
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("id"), int)
            or not isinstance(item.get("content_sha256"), str)
            for item in identity["message_fingerprints"]
        )
        or _active_snapshot_hash(
            str(identity["profile"]), str(identity["source"]),
            str(identity["session_id"]), identity["message_fingerprints"]
        ) != record["snapshot_hash"]
    ):
        raise RuntimeError(f"invalid active snapshot archive: {path}")
    if record["record_type"] == "active_snapshot":
        messages = record.get("messages")
        if not isinstance(messages, list) or any(
            not isinstance(message, dict)
            or message.get("role") not in {"user", "assistant"}
            or not isinstance(message.get("content"), str)
            for message in messages
        ):
            raise RuntimeError(f"invalid active snapshot archive: {path}")
    elif "messages" in record:
        raise RuntimeError(f"invalid active snapshot archive: {path}")
    if "coverage_manifest" in record:
        _validate_coverage_manifest(record["coverage_manifest"], path, "active snapshot archive")
    return record


def load_active_snapshot_archive(path: Path) -> dict:
    """Load and validate the append-only active snapshot source archive."""
    path = Path(path)
    if not path.is_file():
        if path.exists():
            raise RuntimeError(f"invalid active snapshot archive: {path}")
        return {
            "format": ACTIVE_SNAPSHOT_FORMAT,
            "version": ACTIVE_SNAPSHOT_VERSION,
            "archive_type": "active_snapshots",
            "records": [],
        }
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".jsonl":
            records = [
                _validate_active_snapshot_record(json.loads(line), path)
                for line in text.splitlines() if line.strip()
            ]
            envelope = {
                "format": ACTIVE_SNAPSHOT_FORMAT,
                "version": ACTIVE_SNAPSHOT_VERSION,
                "archive_type": "active_snapshots",
                "records": records,
            }
        else:
            data = json.loads(text)
            if (
                not isinstance(data, dict)
                or data.get("format") != ACTIVE_SNAPSHOT_FORMAT
                or data.get("version") != ACTIVE_SNAPSHOT_VERSION
                or data.get("archive_type") != "active_snapshots"
                or not isinstance(data.get("records"), list)
            ):
                raise ValueError("invalid envelope")
            envelope = dict(data)
            envelope["records"] = [
                _validate_active_snapshot_record(record, path)
                for record in data["records"]
            ]
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid active snapshot archive: {path}") from exc
    hashes = [record["snapshot_hash"] for record in envelope["records"]]
    if len(hashes) != len(set(hashes)):
        raise RuntimeError(f"invalid active snapshot archive: duplicate snapshot hash in {path}")
    return envelope


def _active_snapshot_archive_text(
    path: Path, existing: dict, new_records: list[dict]
) -> str:
    records = list(existing["records"]) + new_records
    if path.suffix.lower() == ".jsonl":
        return "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)
    envelope = dict(existing)
    envelope.update({
        "format": ACTIVE_SNAPSHOT_FORMAT,
        "version": ACTIVE_SNAPSHOT_VERSION,
        "archive_type": "active_snapshots",
        "records": records,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    return json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"


def _reject_active_snapshot_collisions(
    db_path: Path,
    snapshot_archive_path: Path,
    report_paths: tuple[Path, Path],
    ended_checkpoint_path: Path | None,
) -> None:
    paths = {
        "state DB": Path(db_path).resolve(),
        "snapshot archive": Path(snapshot_archive_path).resolve(),
        "snapshot JSON report": Path(report_paths[0]).resolve(),
        "snapshot Markdown report": Path(report_paths[1]).resolve(),
    }
    if ended_checkpoint_path is not None:
        paths["ended checkpoint"] = Path(ended_checkpoint_path).resolve()
    values = list(paths.values())
    if len(values) != len(set(values)):
        raise ArchiveCollisionError("active snapshot input and output paths must be distinct")
    report_parent = Path(report_paths[0]).parent.resolve()
    if report_parent in {paths["state DB"], paths["snapshot archive"]}:
        raise ArchiveCollisionError("active snapshot report directory must be distinct from collector paths")


def _active_quiet_status(
    snapshot: dict,
    *,
    last_message_at: float | Mapping[str, float] | Callable[[str], float] | None,
    now: float,
    quiet_seconds: float,
    timestamp_column: str | None,
) -> tuple[bool, dict]:
    actual_last = snapshot["last_message_at"]
    supplied_last = _caller_last_message_at(last_message_at, snapshot["session_id"])
    quiet_basis = "message_timestamp_column"
    last = actual_last
    limitation = None
    if last is None:
        last = supplied_last
        quiet_basis = "caller_last_message_at_message_id_fallback"
        limitation = "message_timestamps_unavailable" if timestamp_column is None else "message_timestamp_value_unavailable"
    if last is None:
        return False, {
            "session_id": snapshot["session_id"],
            "snapshot_hash": snapshot["snapshot_hash"],
            "status": "deferred_quiet_window",
            "reason": "explicit_last_message_at_required",
            "last_message_id": snapshot["last_message_id"],
            "quiet_seconds": quiet_seconds,
            "quiet_basis": quiet_basis,
            "timestamp_limitation": limitation,
        }
    elapsed = now - last
    details = {
        "session_id": snapshot["session_id"],
        "snapshot_hash": snapshot["snapshot_hash"],
        "last_message_id": snapshot["last_message_id"],
        "last_message_at": last,
        "quiet_seconds": quiet_seconds,
        "quiet_elapsed_seconds": elapsed,
        "quiet_basis": quiet_basis,
        "timestamp_limitation": limitation,
    }
    if elapsed < quiet_seconds:
        details.update({"status": "deferred_quiet_window", "reason": "quiet_window_not_elapsed"})
        return False, details
    details["status"] = "quiet"
    return True, details


def active_snapshot_run(
    db_path: Path,
    snapshot_archive_path: Path,
    report_output: Path,
    *,
    profile: str = "unknown",
    quiet_seconds: float,
    last_message_at: float | Mapping[str, float] | Callable[[str], float] | None = None,
    now: float | Callable[[], float] | None = None,
    ended_checkpoint_path: Path | None = None,
    checkpoint_path: Path | None = None,
) -> dict:
    """Capture one explicit, quiet-window-gated active-session snapshot run.

    This mode never calls the ended-session collector, never reads the
    checkpoint contents, and never advances a checkpoint. ``state.db`` is
    read in one SQLite read transaction. With no message timestamp column,
    callers must provide ``last_message_at``; message IDs remain the
    conservative snapshot boundary and the limitation is reported.
    """
    if checkpoint_path is not None:
        if ended_checkpoint_path is not None and Path(checkpoint_path) != Path(ended_checkpoint_path):
            raise ValueError("checkpoint_path and ended_checkpoint_path disagree")
        ended_checkpoint_path = Path(checkpoint_path)
    if not isinstance(profile, str) or not profile:
        raise ValueError("profile must be a non-empty string")
    if not isinstance(quiet_seconds, (int, float)) or isinstance(quiet_seconds, bool) or quiet_seconds < 0:
        raise ValueError("quiet_seconds must be a non-negative number")
    db_path = Path(db_path)
    snapshot_archive_path = Path(snapshot_archive_path)
    report_paths = _active_snapshot_report_paths(Path(report_output))
    if not db_path.is_file():
        raise FileNotFoundError(f"state DB not found: {db_path}")
    _reject_active_snapshot_collisions(
        db_path, snapshot_archive_path, report_paths, ended_checkpoint_path
    )
    _recover_staged_publish(snapshot_archive_path)
    schema_report = source_schema_report(db_path)
    if not schema_report.get("valid", False):
        missing = schema_report.get("missing_required", {})
        if missing:
            raise ValueError(f"source database schema is missing required fields: {missing}")
        raise ValueError("source database schema is not readable")
    clock = _clock_value(now)
    existing = load_active_snapshot_archive(snapshot_archive_path)
    existing_by_hash = {record["snapshot_hash"]: record for record in existing["records"]}

    # BEGIN is intentionally explicit: every session and message read below
    # observes one SQLite snapshot, even if Hermes appends concurrently.
    con = _readonly_connect(db_path)
    try:
        con.execute("BEGIN")
        snapshots, child_sessions, timestamp_column = _read_active_snapshots(con, profile)
    finally:
        con.close()

    statuses: list[dict] = []
    new_records: list[dict] = []
    for snapshot in snapshots:
        if snapshot["source"].lower() in EXCLUDED_SOURCES or any(
            token in snapshot["source"].lower() for token in ("cron", "delegate", "aux")
        ):
            statuses.append({
                "session_id": snapshot["session_id"],
                "snapshot_hash": snapshot["snapshot_hash"],
                "status": "excluded_source",
                "reason": "excluded_source",
            })
            continue
        quiet, status = _active_quiet_status(
            snapshot,
            last_message_at=last_message_at,
            now=clock,
            quiet_seconds=float(quiet_seconds),
            timestamp_column=timestamp_column,
        )
        if not quiet:
            statuses.append(status)
            continue
        if snapshot["sensitive_kinds"]:
            record = _active_snapshot_record(snapshot, db_path, blocked=True)
            status["status"] = "quarantined"
            status["sensitive_kinds"] = snapshot["sensitive_kinds"]
        else:
            record = _active_snapshot_record(snapshot, db_path)
            status["status"] = "captured"
        statuses.append(status)
        if snapshot["snapshot_hash"] not in existing_by_hash:
            new_records.append(record)

    archive_text = (
        _active_snapshot_archive_text(snapshot_archive_path, existing, new_records)
        if new_records else None
    )
    deferred_count = sum(item["status"] == "deferred_quiet_window" for item in statuses)
    captured_count = sum(item["status"] == "captured" for item in statuses)
    quarantined_count = sum(item["status"] == "quarantined" for item in statuses)
    report = {
        "version": 1,
        "run_mode": "active-snapshot",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_db": str(db_path),
        "source_schema": schema_report,
        "profile": profile,
        "quiet_policy": {
            "quiet_seconds": float(quiet_seconds),
            "clock": clock,
            "input": "last_message_at_or_message_timestamp_column",
        },
        "timestamp_column": timestamp_column,
        "timestamp_limitation": (
            "message_timestamps_unavailable" if timestamp_column is None else None
        ),
        "active_sessions": {
            "status": (
                "deferred_quiet_window" if deferred_count and not (captured_count or quarantined_count)
                else "captured" if (captured_count or quarantined_count) and not deferred_count
                else "no_active_sessions" if not snapshots else "mixed"
            ),
            "count": len(snapshots),
            "deferred_count": deferred_count,
            "captured_count": captured_count,
            "quarantined_count": quarantined_count,
        },
        "sessions": statuses,
        "child_agent_sessions": child_sessions,
        "snapshot_records_added": len(new_records),
        "snapshot_records_total": len(existing["records"]) + len(new_records),
        "quarantined_count": quarantined_count,
        "formal_kb_writes": 0,
        "candidate_payloads_written": 0,
        "ended_checkpoint_advanced": False,
    }
    report_json_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    report_markdown_text = "\n".join([
        "# Fufu Archive active-session snapshots",
        "",
        f"- Run mode: `active-snapshot`",
        f"- Profile: `{profile}`",
        f"- Quiet window: `{float(quiet_seconds)}` seconds",
        f"- Active sessions: `{len(snapshots)}`; new snapshots: `{len(new_records)}`",
        f"- Deferred: `{deferred_count}`; quarantined: `{quarantined_count}`",
        "- Formal KB writes: `0`; ended checkpoint advanced: `false`",
        "",
        "Active snapshots are source-only and excluded from formal KB search and candidate commit.",
        "",
    ])

    with ExitStack() as staging_stack:
        staged_outputs: list[tuple[Path, Path]] = []

        def stage(name: str, destination: Path, contents: str) -> None:
            staged_path = _destination_staging_file(
                staging_stack, destination, name, contents
            )
            staged_outputs.append((staged_path, destination))

        if archive_text is not None:
            stage(
                f"snapshot-archive{snapshot_archive_path.suffix}",
                snapshot_archive_path,
                archive_text,
            )
            load_active_snapshot_archive(staged_outputs[-1][0])
        stage("report-json", report_paths[0], report_json_text)
        json.loads(staged_outputs[-1][0].read_text(encoding="utf-8"))
        stage("report-markdown", report_paths[1], report_markdown_text)
        _publish_staged_outputs(staged_outputs, recovery_key=snapshot_archive_path)
    return report


capture_active_snapshots = active_snapshot_run
active_snapshot = active_snapshot_run


def _active_session_count(db_path: Path, profile: str = DEFAULT_PROFILE) -> int:
    con = _readonly_connect(db_path)
    try:
        profile_clause, profile_params = _session_profile_clause(con, _validate_profile(profile))
        return int(con.execute(
            "SELECT count(*) FROM sessions WHERE ended_at IS NULL" + profile_clause,
            profile_params,
        ).fetchone()[0])
    finally:
        con.close()


def _incremental_report_paths(report_output: Path) -> tuple[Path, Path]:
    """Resolve one explicit report output into stable JSON and Markdown paths."""
    report_output = Path(report_output)
    if report_output.exists() and report_output.is_file():
        if report_output.suffix.lower() not in {".json", ".md"}:
            raise ValueError("report output file must end in .json or .md")
        if report_output.suffix.lower() == ".json":
            return report_output, report_output.with_suffix(".md")
        return report_output.with_suffix(".json"), report_output
    if report_output.suffix.lower() in {".json", ".md"}:
        if report_output.suffix.lower() == ".json":
            return report_output, report_output.with_suffix(".md")
        return report_output.with_suffix(".json"), report_output
    return report_output / "incremental-run.json", report_output / "incremental-run.md"


def _reject_incremental_collisions(
    db_path: Path,
    checkpoint_path: Path,
    source_archive_path: Path,
    candidate_payload_dir: Path,
    report_paths: tuple[Path, Path],
) -> None:
    _reject_archive_collisions(db_path, source_archive_path, candidate_payload_dir)
    report_root = Path(report_paths[0]).parent.resolve()
    if report_root in {
        Path(db_path).resolve(),
        Path(checkpoint_path).resolve(),
        Path(source_archive_path).resolve(),
        Path(candidate_payload_dir).resolve(),
    }:
        raise ArchiveCollisionError("report output directory must be distinct from collector paths")
    paths = {
        "state DB": Path(db_path).resolve(),
        "checkpoint": Path(checkpoint_path).resolve(),
        "source archive": Path(source_archive_path).resolve(),
        "candidate JSON": report_paths[0].resolve(),
        "candidate Markdown": report_paths[1].resolve(),
    }
    values = list(paths.values())
    if len(values) != len(set(values)):
        raise ArchiveCollisionError("incremental input and output paths must be distinct")
    candidate_dir = Path(candidate_payload_dir).resolve()
    if any(path.parent == candidate_dir for path in report_paths):
        raise ArchiveCollisionError("report output must not be inside candidate payload directory")
    if Path(checkpoint_path).exists() and Path(checkpoint_path).is_dir():
        raise ValueError(f"checkpoint path is a directory: {checkpoint_path}")


PUBLISH_MANIFEST_VERSION = 1


def _publish_manifest_path(recovery_key: Path) -> Path:
    """Return the deterministic durable journal for one publishing operation."""
    key = Path(recovery_key).resolve()
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:16]
    return key.parent / f".{key.name}.fufu-publish-{digest}.json"


def _load_publish_manifest(manifest_path: Path) -> dict:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid pending publish manifest: {manifest_path}") from exc
    if not isinstance(payload, dict) or payload.get("version") != PUBLISH_MANIFEST_VERSION:
        raise RuntimeError(f"invalid pending publish manifest: {manifest_path}")
    outputs = payload.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise RuntimeError(f"invalid pending publish manifest: {manifest_path}")
    for item in outputs:
        if not isinstance(item, dict) or not isinstance(item.get("destination"), str):
            raise RuntimeError(f"invalid pending publish manifest: {manifest_path}")
        if not isinstance(item.get("existed"), bool):
            raise RuntimeError(f"invalid pending publish manifest: {manifest_path}")
        if item["existed"] and not isinstance(item.get("backup"), str):
            raise RuntimeError(f"invalid pending publish manifest: {manifest_path}")
    return payload


def _recover_staged_publish(recovery_key: Path) -> bool:
    """Restore the pre-publish state left by a prior interrupted batch.

    The journal is committed before the first destination replacement. Recovery
    deliberately rolls back rather than guessing whether a partially applied
    new batch was complete; the caller can then recompute from the old state.
    """
    manifest_path = _publish_manifest_path(recovery_key)
    if not manifest_path.exists():
        return False
    manifest = _load_publish_manifest(manifest_path)
    for item in reversed(manifest["outputs"]):
        destination = Path(item["destination"])
        if item["existed"]:
            backup = Path(item["backup"])
            if not backup.is_file():
                raise RuntimeError(
                    f"pending publish recovery backup is missing: {backup}"
                )
            _atomic_write_bytes(destination, backup.read_bytes())
        else:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
    for item in manifest["outputs"]:
        if item["existed"]:
            try:
                Path(item["backup"]).unlink()
            except FileNotFoundError:
                pass
    manifest_path.unlink()
    return True


def _publish_staged_outputs(
    outputs: Sequence[tuple[Path, Path]], *, recovery_key: Path
) -> None:
    """Publish staged files with a durable rollback journal.

    ``KeyboardInterrupt``/process termination can bypass normal exception
    handlers. The next invocation with the same recovery key restores old
    outputs before any source/archive state is consumed again.
    """
    if not outputs:
        return
    recovery_key = Path(recovery_key)
    _recover_staged_publish(recovery_key)
    manifest_path = _publish_manifest_path(recovery_key)
    manifest_outputs: list[dict] = []
    for index, (_staged, raw_destination) in enumerate(outputs):
        destination = Path(raw_destination).resolve()
        if destination.exists() and destination.is_dir():
            raise OSError(f"output path is a directory: {destination}")
        existed = destination.exists()
        backup_path = destination.parent / (
            f".{destination.name}.fufu-publish-{manifest_path.stem}-{index}.bak"
        )
        if existed:
            _atomic_write_bytes(backup_path, destination.read_bytes())
        manifest_outputs.append({
            "destination": str(destination),
            "existed": existed,
            "backup": str(backup_path) if existed else None,
        })
    manifest = {
        "version": PUBLISH_MANIFEST_VERSION,
        "recovery_key": str(recovery_key.resolve()),
        "outputs": manifest_outputs,
    }
    _atomic_write_bytes(
        manifest_path, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    try:
        for staged, raw_destination in outputs:
            destination = Path(raw_destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, destination)
    except Exception:
        _recover_staged_publish(recovery_key)
        raise
    for item in manifest_outputs:
        if item["existed"]:
            try:
                Path(item["backup"]).unlink()
            except FileNotFoundError:
                pass
    manifest_path.unlink()


def _destination_staging_file(
    staging_stack: ExitStack,
    destination: Path,
    name: str,
    contents: str,
) -> Path:
    """Create one staged file beside its destination.

    ``os.replace`` requires source and destination to share a filesystem on
    Windows.  A single run may publish files to several caller-selected
    locations, so each output gets a temporary directory under its own
    destination parent.  ``ExitStack`` removes those directories on both
    successful publication and every staging/publishing failure.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_name = staging_stack.enter_context(
        tempfile.TemporaryDirectory(
            prefix=f".{destination.name}.staging-",
            dir=str(destination.parent),
        )
    )
    staged_path = Path(staging_name) / name
    _atomic_write(staged_path, contents)
    return staged_path


def incremental_run(
    db_path: Path,
    checkpoint_path: Path,
    source_archive_path: Path,
    candidate_payload_dir: Path,
    report_output: Path,
    min_effective_messages: int = 4,
    min_score: int = 3,
    profile: str = DEFAULT_PROFILE,
) -> dict:
    """Run one explicit ended-session collection and publish it atomically.

    Hermes ``state.db`` is opened read-only. Active sessions are counted for
    an explicit deferred outcome but are never captured. The checkpoint is
    published last, after all staged source, candidate, and report outputs
    validate successfully.
    """
    db_path = Path(db_path)
    checkpoint_path = Path(checkpoint_path)
    source_archive_path = Path(source_archive_path)
    candidate_payload_dir = Path(candidate_payload_dir)
    profile = _validate_profile(profile)
    report_paths = _incremental_report_paths(Path(report_output))
    if not db_path.is_file():
        raise FileNotFoundError(f"state DB not found: {db_path}")
    if candidate_payload_dir.exists() and not candidate_payload_dir.is_dir():
        raise ValueError(f"candidate payload path is not a directory: {candidate_payload_dir}")
    _reject_incremental_collisions(
        db_path, checkpoint_path, source_archive_path, candidate_payload_dir, report_paths
    )
    _recover_staged_publish(checkpoint_path)

    # Audit the explicitly supplied source before reading any session rows.
    # ``tool_name`` is intentionally optional for older Hermes schemas, while
    # missing required fields must fail closed instead of producing a partial
    # archive that looks complete.
    schema_report = source_schema_report(db_path)
    if not schema_report.get("valid", False):
        missing = schema_report.get("missing_required", {})
        if missing:
            raise ValueError(f"source database schema is missing required fields: {missing}")
        raise ValueError("source database schema is not readable")

    state = load_checkpoint(checkpoint_path)
    active_count = _active_session_count(db_path, profile)
    selected_snapshots: dict[tuple[str, str], dict] = {}
    candidates, skipped, child_agent_logs, seen = collect(
        db_path, state, min_effective_messages, min_score, profile, selected_snapshots
    )
    archive_plan = _prepare_archive_plan(
        db_path,
        candidates,
        skipped,
        source_archive_path,
        candidate_payload_dir,
        profile,
        selected_snapshots,
    )
    next_checkpoint = merge_checkpoint(state, seen)
    watermark = next_checkpoint["watermark"]
    stable_generated_at = (
        _iso(float(watermark["ended_at"]))
        if watermark["ended_at"] is not None
        else "1970-01-01T00:00:00+00:00"
    )
    report = render_json(
        candidates,
        skipped,
        child_agent_logs,
        db_path,
        archival_mode=True,
        source_records_added=archive_plan.result.source_records_added,
        candidate_payloads_written=archive_plan.result.candidate_payloads_written,
        generated_at=stable_generated_at,
        profile=profile,
    )
    captured_session_ids = [candidate.session_id for candidate in candidates]
    captured_session_ids.extend(
        item.session_id for item in skipped if item.reason == "sensitive_content"
    )
    report.update({
        "run_mode": "incremental-run",
        "source_schema": schema_report,
        "captured_count": archive_plan.result.source_records_added,
        "captured_session_ids": captured_session_ids,
        "active_sessions": {
            "status": "deferred_unsupported",
            "count": active_count,
            "reason": "active-session capture is not implemented in P4.2-4",
        },
        "formal_kb_writes": 0,
        "checkpoint_advanced": next_checkpoint != state,
    })
    report_json_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    report_markdown_text = render_markdown(report)
    checkpoint_text = json.dumps(
        _normalize_checkpoint(next_checkpoint), ensure_ascii=False, indent=2
    ) + "\n"

    with ExitStack() as staging_stack:
        staged_outputs: list[tuple[Path, Path]] = []

        def stage(name: str, destination: Path, contents: str) -> None:
            staged_path = _destination_staging_file(
                staging_stack, destination, name, contents
            )
            staged_outputs.append((staged_path, destination))

        if archive_plan.archive_text is not None:
            stage(
                f"source-archive{source_archive_path.suffix}",
                source_archive_path,
                archive_plan.archive_text,
            )
            load_source_archive(staged_outputs[-1][0])
        for index, (destination, contents) in enumerate(archive_plan.candidate_writes):
            staged_path = _destination_staging_file(
                staging_stack, destination, f"candidate-{index}.json", contents
            )
            staged_payload = json.loads(
                staged_path.read_text(encoding="utf-8")
            )
            _validate_candidate_payload(
                staged_path,
                staged_payload["session_id"],
                staged_payload["content_sha256"],
                staged_payload.get("profile", DEFAULT_PROFILE),
            )
            staged_outputs.append((staged_path, destination))
        stage("report-json", report_paths[0], report_json_text)
        parsed_report = json.loads(
            staged_outputs[-1][0].read_text(encoding="utf-8")
        )
        if not isinstance(parsed_report, dict) or parsed_report.get("run_mode") != "incremental-run":
            raise RuntimeError("invalid incremental JSON report")
        stage("report-markdown", report_paths[1], report_markdown_text)
        if not report_markdown_text.strip():
            raise RuntimeError("invalid incremental Markdown report")
        if not checkpoint_path.exists() or next_checkpoint != state:
            stage("checkpoint", checkpoint_path, checkpoint_text)
            _normalize_checkpoint(
                json.loads(staged_outputs[-1][0].read_text(encoding="utf-8"))
            )

        _publish_staged_outputs(staged_outputs, recovery_key=checkpoint_path)

    return report


run_incremental = incremental_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--state", "--state-checkpoint", dest="state", type=Path)
    parser.add_argument(
        "--output-dir", "--report-output", dest="output_dir", type=Path,
        help="report directory for the default collector, or explicit report output for incremental-run",
    )
    parser.add_argument(
        "--incremental-run",
        action="store_true",
        help="run one staged, checkpointed ended-session incremental collection",
    )
    parser.add_argument(
        "--active-snapshot", "--active-snapshot-run", dest="active_snapshot",
        action="store_true",
        help="run one explicit quiet-window-gated active-session snapshot capture",
    )
    parser.add_argument(
        "--profile", default="unknown",
        help="explicit Hermes profile for ended-session and active-snapshot identity (default: unknown)",
    )
    parser.add_argument(
        "--snapshot-archive", "--active-snapshot-archive",
        dest="snapshot_archive", type=Path,
        help="explicit append-only active snapshot source archive destination",
    )
    parser.add_argument(
        "--quiet-seconds", type=float,
        help="required active snapshot quiet-window duration",
    )
    parser.add_argument(
        "--last-message-at", type=float,
        help="explicit fallback last-message Unix timestamp for timestamp-poor state.db schemas",
    )
    parser.add_argument(
        "--now", type=float,
        help="testable Unix clock value for active quiet-window evaluation",
    )
    parser.add_argument("--commit-state", action="store_true")
    parser.add_argument(
        "--archive",
        action="store_true",
        help="explicitly persist safe source records and pending candidate payloads",
    )
    parser.add_argument(
        "--source-archive",
        "--archive-source",
        "--archive-path",
        dest="source_archive",
        type=Path,
        help="explicit JSON/JSONL source archive destination (enables archival)",
    )
    parser.add_argument(
        "--candidate-dir",
        "--candidate-output-dir",
        dest="candidate_dir",
        type=Path,
        help="explicit directory for review-only candidate payload files",
    )
    parser.add_argument("--min-effective-messages", type=int, default=4)
    parser.add_argument("--min-score", type=int, default=3)
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error(f"state DB not found: {args.db}")
    if not args.active_snapshot and args.state is None:
        parser.error("the ended-session collector requires --state")
    if args.active_snapshot:
        if args.incremental_run:
            parser.error("--active-snapshot cannot be combined with --incremental-run")
        if args.snapshot_archive is None:
            parser.error("--active-snapshot requires --snapshot-archive")
        if args.output_dir is None:
            parser.error("--active-snapshot requires --report-output or --output-dir")
        if args.quiet_seconds is None:
            parser.error("--active-snapshot requires --quiet-seconds")
        if args.source_archive is not None:
            parser.error("--active-snapshot uses --snapshot-archive, not --source-archive")
        if args.candidate_dir is not None or args.archive or args.commit_state:
            parser.error("--active-snapshot is source-only and cannot write candidates or checkpoint state")
        try:
            report = active_snapshot_run(
                db_path=args.db,
                snapshot_archive_path=args.snapshot_archive,
                report_output=args.output_dir,
                profile=args.profile,
                quiet_seconds=args.quiet_seconds,
                last_message_at=args.last_message_at,
                now=args.now,
                ended_checkpoint_path=args.state,
            )
        except (ArchiveCollisionError, FileNotFoundError, RuntimeError, ValueError, OSError) as exc:
            parser.error(str(exc))
        report_json_path, report_markdown_path = _active_snapshot_report_paths(args.output_dir)
        print(json.dumps({
            "run_mode": "active-snapshot",
            "active_sessions": report["active_sessions"],
            "snapshot_records_added": report["snapshot_records_added"],
            "quarantined_count": report["quarantined_count"],
            "formal_kb_writes": report["formal_kb_writes"],
            "ended_checkpoint_advanced": report["ended_checkpoint_advanced"],
            "json_report": str(report_json_path),
            "markdown_report": str(report_markdown_path),
        }, ensure_ascii=False))
        return 0
    if args.incremental_run:
        if args.source_archive is None:
            parser.error("--incremental-run requires --source-archive")
        if args.candidate_dir is None:
            parser.error("--incremental-run requires --candidate-dir")
        if args.output_dir is None:
            parser.error("--incremental-run requires --report-output or --output-dir")
        try:
            report = incremental_run(
                db_path=args.db,
                checkpoint_path=args.state,
                source_archive_path=args.source_archive,
                candidate_payload_dir=args.candidate_dir,
                report_output=args.output_dir,
                min_effective_messages=args.min_effective_messages,
                min_score=args.min_score,
                profile=args.profile,
            )
        except (ArchiveCollisionError, FileNotFoundError, RuntimeError, ValueError, OSError) as exc:
            parser.error(str(exc))
        report_json_path, report_markdown_path = _incremental_report_paths(args.output_dir)
        print(json.dumps({
            "run_mode": "incremental-run",
            "captured_count": report["captured_count"],
            "child_agent_log_count": report["child_agent_log_count"],
            "skipped_count": report["skipped_count"],
            "active_sessions": report["active_sessions"],
            "archive_writes": report["archive_writes"],
            "formal_kb_writes": report["formal_kb_writes"],
            "checkpoint_advanced": report["checkpoint_advanced"],
            "json_report": str(report_json_path),
            "markdown_report": str(report_markdown_path),
        }, ensure_ascii=False))
        return 0
    if args.output_dir is None:
        parser.error("the default collector requires --output-dir")
    state = load_checkpoint(args.state)
    archival_requested = bool(args.archive or args.source_archive or args.candidate_dir)
    selected_snapshots: dict[tuple[str, str], dict] | None = {} if archival_requested else None
    candidates, skipped, child_agent_logs, seen = collect(
        args.db, state, args.min_effective_messages, args.min_score, args.profile, selected_snapshots
    )
    if archival_requested and args.source_archive is None:
        parser.error("archival operation requires --source-archive")
    if archival_requested:
        try:
            _reject_archive_collisions(args.db, args.source_archive, args.candidate_dir)
            if args.source_archive.resolve() == args.state.resolve():
                raise ArchiveCollisionError("source archive and collector state must be different paths")
            if args.source_archive.resolve() == args.output_dir.resolve():
                raise ArchiveCollisionError("source archive and report directory must be different paths")
            if args.candidate_dir is not None and args.candidate_dir.resolve() == args.state.resolve():
                raise ArchiveCollisionError("candidate destination and collector state must be different paths")
            if args.candidate_dir is not None and args.candidate_dir.resolve() == args.output_dir.resolve():
                raise ArchiveCollisionError("candidate and report directories must be different paths")
            archive_result = archive_collected_sessions(
                args.db,
                candidates,
                skipped,
                args.source_archive,
                args.candidate_dir,
                args.profile,
                selected_snapshots,
            )
        except (ArchiveCollisionError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
    else:
        archive_result = ArchiveResult(0, 0, 0, Path(""), ())
    report = render_json(
        candidates,
        skipped,
        child_agent_logs,
        args.db,
        archival_mode=archival_requested,
        source_records_added=archive_result.source_records_added,
        candidate_payloads_written=archive_result.candidate_payloads_written,
        profile=args.profile,
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"session-candidates-{stamp}.json"
    md_path = args.output_dir / f"session-candidates-{stamp}.md"
    _atomic_write(json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(md_path, render_markdown(report))
    if args.commit_state:
        checkpoint = merge_checkpoint(state, seen)
        checkpoint["last_run_at"] = report["generated_at"]
        checkpoint["last_source_db"] = str(args.db)
        save_checkpoint(args.state, checkpoint)
    print(json.dumps({
        "candidate_count": len(candidates),
        "child_agent_log_count": len(child_agent_logs),
        "skipped_count": len(skipped),
        "archive_writes": archive_result.archive_writes,
        "source_records_added": archive_result.source_records_added,
        "candidate_payloads_written": archive_result.candidate_payloads_written,
        "state_committed": bool(args.commit_state),
        "json_report": str(json_path),
        "markdown_report": str(md_path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
