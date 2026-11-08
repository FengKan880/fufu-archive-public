from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from fufu_archive import session_collector as collector


def _create_state_db(path: Path) -> None:
    with sqlite3.connect(path) as con:
        con.executescript(
            """
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT,
              started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
              parent_session_id TEXT, message_count INTEGER DEFAULT 0
            );
            CREATE TABLE messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
              role TEXT NOT NULL, content TEXT, tool_name TEXT
            );
            """
        )


def _add_session(
    path: Path,
    session_id: str,
    *,
    source: str = "desktop",
    ended_at: float | None = 2.0,
    parent_session_id: str | None = None,
) -> None:
    with sqlite3.connect(path) as con:
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                source,
                "Test session",
                1.0,
                ended_at,
                "session_reset",
                parent_session_id,
                6,
            ),
        )


def _add_message(
    path: Path,
    session_id: str,
    role: str,
    content: str = "",
    tool_name: str | None = None,
) -> None:
    with sqlite3.connect(path) as con:
        con.execute(
            "INSERT INTO messages(session_id, role, content, tool_name) VALUES (?, ?, ?, ?)",
            (session_id, role, content, tool_name),
        )


def _add_good_messages(path: Path, session_id: str) -> None:
    _add_message(path, session_id, "user", "请把这次结果归档，以后复用")
    _add_message(
        path,
        session_id,
        "assistant",
        "## 已完成\n已验证测试通过，pytest 20 passed，来源明确。" * 10,
    )
    _add_message(path, session_id, "user", "确认完成")
    _add_message(
        path,
        session_id,
        "assistant",
        "总结：实际验收通过，git diff --check 通过。" * 8,
    )
    _add_message(path, session_id, "tool", "SECRET TOOL BODY MUST NOT APPEAR", "pytest")
    _add_message(path, session_id, "tool", "", "git")


def test_repository_collector_preserves_candidate_first_and_redaction(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_state_db(db_path)
    _add_session(db_path, "good")
    _add_good_messages(db_path, "good")

    candidates, skipped, child_logs, seen = collector.collect(
        db_path, {"processed": {}}
    )
    report = collector.render_json(candidates, skipped, child_logs, db_path)

    assert [candidate.session_id for candidate in candidates] == ["good"]
    assert seen["good"]["decision"] == "review"
    assert report["dry_run"] is True
    assert report["archive_writes"] == 0
    assert report["coverage_manifest"]["status_counts"]["complete"] == 0
    assert report["coverage_manifest"]["status_counts"]["partial"] == 1
    assert report["coverage_manifest"]["status_counts"]["blocked"] == 0
    assert "SECRET TOOL BODY MUST NOT APPEAR" not in json.dumps(
        report, ensure_ascii=False
    )


def test_source_schema_report_is_deterministic_and_allows_optional_tool_name(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    _create_state_db(db_path)

    report = collector.source_schema_report(db_path)

    assert report["valid"] is True
    assert report["missing_required"] == {}
    assert report["tables"]["messages"]["optional_present"] == ["tool_name"]
    assert collector.validate_source_schema(db_path) == report


def test_source_schema_report_rejects_missing_required_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "incomplete-state.db"
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        con.execute("CREATE TABLE messages (id INTEGER, session_id TEXT, content TEXT)")

    report = collector.source_schema_report(db_path)

    assert report["valid"] is False
    assert report["missing_required"] == {
        "messages": ["role"],
        "sessions": [
            "end_reason", "ended_at", "message_count", "parent_session_id",
            "source", "started_at", "title",
        ],
    }
    with pytest.raises(RuntimeError, match="missing required fields"):
        collector.collect(db_path, {"processed": {}})


def test_source_archive_rejects_message_body_tampering(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    archive_path = tmp_path / "source-archive.json"
    candidate_dir = tmp_path / "candidates"
    _create_state_db(db_path)
    _add_session(db_path, "tamper-check")
    _add_good_messages(db_path, "tamper-check")
    candidates, skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, min_effective_messages=0, min_score=0
    )
    collector.archive_collected_sessions(
        db_path, candidates, skipped, archive_path, candidate_dir
    )
    payload = json.loads(archive_path.read_text(encoding="utf-8"))
    payload["records"][0]["messages"][0]["content"] = "tampered body"
    archive_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid source archive"):
        collector.load_source_archive(archive_path)


def test_source_archive_rejects_missing_message_fingerprints(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    archive_path = tmp_path / "source-archive.json"
    candidate_dir = tmp_path / "candidates"
    _create_state_db(db_path)
    _add_session(db_path, "missing-fingerprints")
    _add_good_messages(db_path, "missing-fingerprints")
    candidates, skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, min_effective_messages=0, min_score=0
    )
    collector.archive_collected_sessions(
        db_path, candidates, skipped, archive_path, candidate_dir
    )
    payload = json.loads(archive_path.read_text(encoding="utf-8"))
    del payload["records"][0]["message_fingerprints"]
    archive_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid source archive"):
        collector.load_source_archive(archive_path)


def test_source_archive_rejects_message_without_a_fingerprint(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    archive_path = tmp_path / "source-archive.json"
    candidate_dir = tmp_path / "candidates"
    _create_state_db(db_path)
    _add_session(db_path, "unbound-message")
    _add_good_messages(db_path, "unbound-message")
    candidates, skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, min_effective_messages=0, min_score=0
    )
    collector.archive_collected_sessions(
        db_path, candidates, skipped, archive_path, candidate_dir
    )
    payload = json.loads(archive_path.read_text(encoding="utf-8"))
    payload["records"][0]["messages"].append(
        {"source_message_id": 999, "role": "assistant", "content": "unbound body"}
    )
    archive_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid source archive"):
        collector.load_source_archive(archive_path)


def test_source_archive_rejects_reusing_one_fingerprint_for_two_messages(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    archive_path = tmp_path / "source-archive.json"
    candidate_dir = tmp_path / "candidates"
    _create_state_db(db_path)
    _add_session(db_path, "reused-fingerprint")
    _add_good_messages(db_path, "reused-fingerprint")
    candidates, skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, min_effective_messages=0, min_score=0
    )
    collector.archive_collected_sessions(
        db_path, candidates, skipped, archive_path, candidate_dir
    )
    payload = json.loads(archive_path.read_text(encoding="utf-8"))
    payload["records"][0]["messages"].append(
        dict(payload["records"][0]["messages"][0])
    )
    archive_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid source archive"):
        collector.load_source_archive(archive_path)


def test_candidate_payload_rejects_message_body_tampering(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    archive_path = tmp_path / "source-archive.json"
    candidate_dir = tmp_path / "candidates"
    _create_state_db(db_path)
    _add_session(db_path, "candidate-tamper")
    _add_good_messages(db_path, "candidate-tamper")
    candidates, skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, min_effective_messages=0, min_score=0
    )
    collector.archive_collected_sessions(
        db_path, candidates, skipped, archive_path, candidate_dir
    )
    candidate_path = next(candidate_dir.glob("*.json"))
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    payload["messages"][0]["content"] = "tampered candidate body"
    candidate_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid candidate payload"):
        collector.load_candidate_payload(candidate_path)


def test_incremental_run_fails_closed_before_outputs_on_invalid_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "incomplete-state.db"
    checkpoint_path = tmp_path / "checkpoint.json"
    archive_path = tmp_path / "archive.json"
    candidate_dir = tmp_path / "candidates"
    report_dir = tmp_path / "reports"
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        con.execute("CREATE TABLE messages (id INTEGER, session_id TEXT, content TEXT)")

    with pytest.raises(ValueError, match="schema is missing required fields"):
        collector.incremental_run(
            db_path,
            checkpoint_path,
            archive_path,
            candidate_dir,
            report_dir,
        )

    assert not checkpoint_path.exists()
    assert not archive_path.exists()
    assert not candidate_dir.exists()
    assert not report_dir.exists()


def test_active_snapshot_fails_closed_before_outputs_on_invalid_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "incomplete-state.db"
    archive_path = tmp_path / "active.json"
    report_dir = tmp_path / "reports"
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        con.execute("CREATE TABLE messages (id INTEGER, session_id TEXT, content TEXT)")

    with pytest.raises(ValueError, match="schema is missing required fields"):
        collector.active_snapshot_run(
            db_path,
            archive_path,
            report_dir,
            profile="winefox",
            quiet_seconds=60,
            now=100.0,
        )

    assert not archive_path.exists()
    assert not report_dir.exists()


def test_ended_timestamp_ordering_and_invalid_rows_are_explicit(tmp_path: Path) -> None:
    db_path = tmp_path / "timestamps.db"
    _create_state_db(db_path)
    _add_session(db_path, "z", ended_at=2)
    _add_good_messages(db_path, "z")
    _add_session(db_path, "a", ended_at=2.0)
    _add_good_messages(db_path, "a")
    _add_session(db_path, "float", ended_at=2.5)
    _add_good_messages(db_path, "float")
    _add_session(db_path, "active", ended_at=None)
    _add_good_messages(db_path, "active")
    _add_session(db_path, "invalid", ended_at="not-a-timestamp")  # type: ignore[arg-type]
    _add_good_messages(db_path, "invalid")

    candidates, skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, min_score=0
    )

    assert [item.session_id for item in candidates] == ["a", "z", "float"]
    assert all(item.session_id != "active" for item in candidates + skipped)
    invalid = next(item for item in skipped if item.session_id == "invalid")
    assert invalid.reason == "invalid_timestamp_fields"
    assert invalid.coverage_manifest["status"] == "blocked"
    assert invalid.coverage_manifest["invalid_fields"] == 1


def test_message_fingerprint_changes_for_order_and_excluded_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "fingerprints.db"
    _create_state_db(db_path)
    _add_session(db_path, "fingerprint")
    _add_good_messages(db_path, "fingerprint")

    first = collector.collect(db_path, {"processed": {}}, min_score=0)[0][0]
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO messages(session_id, role, content, tool_name) VALUES (?, ?, ?, ?)",
            ("fingerprint", "system", "unknown row", None),
        )
    second = collector.collect(db_path, {"processed": {}}, min_score=0)[0][0]
    with sqlite3.connect(db_path) as con:
        con.execute("DELETE FROM messages WHERE role = 'system'")
        con.execute(
            "INSERT INTO messages(session_id, role, content, tool_name) VALUES (?, ?, ?, ?)",
            ("fingerprint", "tool", "different excluded body", "other-tool"),
        )
    third = collector.collect(db_path, {"processed": {}}, min_score=0)[0][0]

    assert len({first.content_sha256, second.content_sha256, third.content_sha256}) == 3
    assert second.coverage_manifest["unknown_roles"] == 1
    assert third.coverage_manifest["tool_bodies_excluded"] == 3


def test_source_archive_reports_partial_coverage_for_unknown_and_missing_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    archive_path = tmp_path / "source.json"
    candidate_dir = tmp_path / "candidates"
    _create_state_db(db_path)
    _add_session(db_path, "coverage")
    _add_good_messages(db_path, "coverage")
    _add_message(db_path, "coverage", "system", "unmodeled system event")
    _add_message(db_path, "coverage", "assistant", None)  # type: ignore[arg-type]

    candidates, skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, profile="winefox"
    )
    collector.archive_collected_sessions(
        db_path, candidates, skipped, archive_path, candidate_dir, "winefox"
    )
    record = collector.load_source_archive(archive_path)["records"][0]
    coverage = record["coverage"]
    manifest = record["coverage_manifest"]
    assert coverage["status"] == "partial"
    assert coverage["tool_bodies_excluded"] is True
    assert coverage["unknown_roles"] == ["system"]
    assert coverage["malformed_or_missing_content"] >= 1
    assert coverage["cleanup_eligible"] is False
    assert manifest["tool_bodies_excluded"] == 2
    assert manifest["unknown_roles"] == 1
    assert manifest["unknown_role_names"] == ["system"]
    assert manifest["cleanup_qualification_blocked"] is True
    assert record["message_fingerprints"]
    assert "unmodeled system event" not in archive_path.read_text(encoding="utf-8")


def test_ended_collector_accepts_legacy_messages_schema_without_tool_name(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-state.db"
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT,
              started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
              parent_session_id TEXT, message_count INTEGER DEFAULT 0
            );
            CREATE TABLE messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL, role TEXT, content TEXT
            );
            INSERT INTO sessions VALUES
              ('legacy', 'desktop', 'Legacy schema', 1.0, 2.0,
               'session_reset', NULL, 4);
            INSERT INTO messages(session_id, role, content) VALUES
              ('legacy', 'user', '请归档这次结果'),
              ('legacy', 'assistant', '已验证，结果可复用。' || printf('%0*d', 700, 1)),
              ('legacy', 'user', '确认完成'),
              ('legacy', 'assistant', '总结：测试通过。' || printf('%0*d', 700, 1));
            """
        )

    candidates, skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, profile="legacy"
    )
    assert [candidate.session_id for candidate in candidates] == ["legacy"]
    assert skipped == []
    assert candidates[0].profile == "legacy"


def test_invalid_message_id_is_reported_without_aborting_other_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "malformed.db"
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY, source TEXT, title TEXT,
              started_at REAL, ended_at REAL, end_reason TEXT,
              parent_session_id TEXT, message_count INTEGER
            );
            CREATE TABLE messages (
              id PRIMARY KEY, session_id TEXT, role TEXT, content, tool_name TEXT
            );
            INSERT INTO sessions VALUES
              ('malformed-id', 'desktop', 'Test session', 1, 2,
               'session_reset', NULL, 5);
            INSERT INTO messages VALUES
              (1, 'malformed-id', 'user', '璇锋妸杩欐缁撴灉褰掓。', NULL),
              (2, 'malformed-id', 'assistant', '## 宸插畬鎴愶紝pytest passed', NULL),
              (3, 'malformed-id', 'user', '纭瀹屾垚', NULL),
              (4, 'malformed-id', 'assistant', '鎬荤粨锛氭湁鏁堢粨鏋�', NULL),
              ('bad-id', 'malformed-id', 'assistant', 'unretained malformed row', NULL);
            """
        )
    candidates, skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, min_score=0
    )
    assert [item.session_id for item in candidates] == ["malformed-id"]
    manifest = candidates[0].coverage_manifest
    assert manifest["invalid_message_ids"] == 1
    assert manifest["invalid_fields"] >= 1
    assert manifest["status"] == "partial"
    assert all(
        "unretained malformed row" not in json.dumps(
            item.__dict__, ensure_ascii=False
        )
        for item in candidates
    )


def test_repository_collector_keeps_active_and_child_sessions_separate(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_state_db(db_path)
    _add_session(db_path, "active", ended_at=None)
    _add_good_messages(db_path, "active")
    _add_session(db_path, "child", parent_session_id="parent")
    _add_good_messages(db_path, "child")
    _add_session(db_path, "cron", source="cron")
    _add_good_messages(db_path, "cron")

    candidates, skipped, child_logs, _ = collector.collect(
        db_path, {"processed": {}}
    )

    assert candidates == []
    assert [log.session_id for log in child_logs] == ["child"]
    assert {item.session_id: item.reason for item in skipped}["cron"] == (
        "excluded_source"
    )
    assert all(item.session_id != "active" for item in skipped)


def test_repository_collector_blocks_sensitive_content_without_preview(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    _create_state_db(db_path)
    _add_session(db_path, "secret")
    _add_good_messages(db_path, "secret")
    _add_message(db_path, "secret", "user", "API_KEY=sk-abcdefghijklmnop")

    candidates, skipped, child_logs, _ = collector.collect(
        db_path, {"processed": {}}
    )
    report_text = json.dumps(
        collector.render_json(candidates, skipped, child_logs, db_path),
        ensure_ascii=False,
    )

    item = next(item for item in skipped if item.session_id == "secret")
    assert item.reason == "sensitive_content"
    assert "api_key" in item.sensitive_kinds
    assert "abcdefghijklmnop" not in report_text


def test_repository_collector_opens_state_db_read_only(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_state_db(db_path)

    with collector._readonly_connect(db_path) as con:
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1
        try:
            con.execute("CREATE TABLE should_not_exist (id INTEGER)")
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("read-only collector connection allowed a write")


def test_checkpoint_watermark_replay_and_equal_timestamp_ordering(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    checkpoint_path = tmp_path / "collector-checkpoint.json"
    _create_state_db(db_path)
    _add_session(db_path, "a", ended_at=2.0)
    _add_good_messages(db_path, "a")

    candidates, skipped, child_logs, seen = collector.collect(
        db_path, collector.load_checkpoint(checkpoint_path)
    )
    assert [candidate.session_id for candidate in candidates] == ["a"]
    checkpoint = collector.merge_checkpoint(
        collector.load_checkpoint(checkpoint_path), seen
    )
    collector.save_checkpoint(checkpoint_path, checkpoint)

    saved = collector.load_checkpoint(checkpoint_path)
    assert saved["version"] == collector.CHECKPOINT_VERSION
    assert saved["watermark"] == {"ended_at": 2.0, "session_id": "a"}
    identity = collector.processed_identity("a", candidates[0].content_sha256)
    assert identity in saved["processed"]

    replay = collector.collect(db_path, saved)
    assert replay[0:3] == ([], [], [])

    _add_message(db_path, "a", "assistant", "补充的已验证结果，应该被重新采集。" * 20)
    changed = collector.collect(db_path, saved)
    assert [candidate.session_id for candidate in changed[0]] == ["a"]
    assert changed[0][0].content_sha256 != candidates[0].content_sha256

    _add_session(db_path, "z", ended_at=2.0)
    _add_good_messages(db_path, "z")
    same_timestamp = collector.collect(db_path, saved)
    assert [candidate.session_id for candidate in same_timestamp[0]] == ["a", "z"]

    _add_session(db_path, "new", ended_at=3.0)
    _add_good_messages(db_path, "new")
    after_watermark = collector.collect(
        db_path, collector.merge_checkpoint(saved, same_timestamp[3])
    )
    assert [candidate.session_id for candidate in after_watermark[0]] == ["new"]


def test_processed_identity_keeps_legacy_call_shape_and_accepts_explicit_profile() -> None:
    assert collector.processed_identity("session", "digest") == (
        "unknown:session:digest"
    )
    assert collector.processed_identity("session", "digest", "winefox") == (
        "winefox:session:digest"
    )
    assert collector.processed_identity(
        "session", "digest", profile="otter"
    ) == "otter:session:digest"


def test_ended_identity_isolated_by_profile_for_checkpoint_and_archive(
    tmp_path: Path,
) -> None:
    first_db = tmp_path / "first" / "state.db"
    second_db = tmp_path / "second" / "state.db"
    checkpoint_path = tmp_path / "collector-checkpoint.json"
    archive_path = tmp_path / "source-archive.json"
    candidate_dir = tmp_path / "candidate-payloads"
    for db_path in (first_db, second_db):
        db_path.parent.mkdir()
        _create_state_db(db_path)
        _add_session(db_path, "shared-session")
        _add_good_messages(db_path, "shared-session")

    first = collector.collect(
        first_db, collector.load_checkpoint(checkpoint_path), profile="winefox"
    )
    first_checkpoint = collector.merge_checkpoint(
        collector.load_checkpoint(checkpoint_path), first[3]
    )
    collector.save_checkpoint(checkpoint_path, first_checkpoint)
    second = collector.collect(
        second_db, collector.load_checkpoint(checkpoint_path), profile="otter"
    )

    assert [candidate.profile for candidate in first[0]] == ["winefox"]
    assert [candidate.profile for candidate in second[0]] == ["otter"]
    second_checkpoint = collector.merge_checkpoint(first_checkpoint, second[3])
    assert collector.processed_identity(
        "shared-session", first[0][0].content_sha256, "winefox"
    ) in second_checkpoint["processed"]
    assert collector.processed_identity(
        "shared-session", first[0][0].content_sha256, "otter"
    ) in second_checkpoint["processed"]

    collector.archive_collected_sessions(
        first_db, first[0], first[1], archive_path, candidate_dir, "winefox"
    )
    collector.archive_collected_sessions(
        second_db, second[0], second[1], archive_path, candidate_dir, "otter"
    )
    records = collector.load_source_archive(archive_path)["records"]
    assert {record["profile"] for record in records} == {"winefox", "otter"}
    assert {record["identity"]["profile"] for record in records} == {
        "winefox",
        "otter",
    }
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in candidate_dir.glob("*.json")
    ]
    assert {payload["traceability"]["profile"] for payload in payloads} == {
        "winefox",
        "otter",
    }
    assert len(payloads) == 2


def test_old_checkpoint_and_archive_are_compatible_without_cross_profile_loss(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    checkpoint_path = tmp_path / "old-checkpoint.json"
    archive_path = tmp_path / "old-archive.json"
    candidate_dir = tmp_path / "candidate-payloads"
    _create_state_db(db_path)
    _add_session(db_path, "legacy")
    _add_good_messages(db_path, "legacy")

    baseline = collector.collect(db_path, {"processed": {}})
    with collector._readonly_connect(db_path) as con:
        baseline_record = collector._archive_record(
            collector._session_snapshot(con, "legacy"),
            db_path,
        )
    baseline_digest = baseline[0][0].content_sha256
    baseline_record.pop("profile")
    baseline_record["identity"].pop("profile")
    baseline_record["traceability"].pop("profile")
    baseline_record.pop("coverage_manifest")
    baseline_record.pop("coverage")
    archive_path.write_text(
        json.dumps(
            {
                "format": collector.SOURCE_ARCHIVE_FORMAT,
                "version": collector.SOURCE_ARCHIVE_VERSION,
                "archive_type": "source",
                "records": [baseline_record],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    legacy_loaded = collector.load_source_archive(archive_path)["records"][0]
    assert legacy_loaded["coverage_manifest"]["legacy"] is True
    assert legacy_loaded["coverage_manifest"]["status"] == "partial"
    checkpoint_path.write_text(
        json.dumps(
            {
                "version": 2,
                "watermark": {"ended_at": 2.0, "session_id": "legacy"},
                "processed": {
                    f"legacy:{baseline_digest}": {
                        "session_id": "legacy",
                        "content_sha256": baseline_digest,
                        "ended_at": 2.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    old_default = collector.collect(
        db_path, collector.load_checkpoint(checkpoint_path)
    )
    explicit_profile = collector.collect(
        db_path, collector.load_checkpoint(checkpoint_path), profile="winefox"
    )
    assert old_default[0] == []
    assert [candidate.session_id for candidate in explicit_profile[0]] == ["legacy"]

    collector.archive_collected_sessions(
        db_path,
        explicit_profile[0],
        explicit_profile[1],
        archive_path,
        candidate_dir,
        "winefox",
    )
    records = collector.load_source_archive(archive_path)["records"]
    assert len(records) == 2
    assert {record.get("profile", collector.DEFAULT_PROFILE) for record in records} == {
        collector.DEFAULT_PROFILE,
        "winefox",
    }


def test_malformed_coverage_manifest_is_rejected_before_archive_overwrite(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    archive_path = tmp_path / "archive.json"
    candidate_dir = tmp_path / "candidates"
    _create_state_db(db_path)
    _add_session(db_path, "archive-validation")
    _add_good_messages(db_path, "archive-validation")
    candidates, skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, min_score=0
    )
    collector.archive_collected_sessions(
        db_path, candidates, skipped, archive_path, candidate_dir
    )
    archive_path.write_text(
        archive_path.read_text(encoding="utf-8").replace('"status": "partial"', '"status": "corrupt"'),
        encoding="utf-8",
    )
    before = archive_path.read_bytes()

    with pytest.raises(RuntimeError, match="invalid source archive"):
        collector.archive_collected_sessions(
            db_path, candidates, skipped, archive_path, candidate_dir
        )

    assert archive_path.read_bytes() == before


def test_legacy_candidate_payload_loads_as_unknown_partial(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    candidate_dir = tmp_path / "candidates"
    archive_path = tmp_path / "archive.json"
    _create_state_db(db_path)
    _add_session(db_path, "candidate-legacy")
    _add_good_messages(db_path, "candidate-legacy")
    candidates, skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, min_score=0
    )
    collector.archive_collected_sessions(
        db_path, candidates, skipped, archive_path, candidate_dir
    )
    payload_path = next(candidate_dir.glob("*.json"))
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload.pop("profile")
    payload["identity"].pop("profile")
    payload["traceability"].pop("profile")
    payload["candidate"].pop("profile")
    payload.pop("coverage_manifest")
    payload.pop("coverage")
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = collector.load_candidate_payload(payload_path)

    assert loaded["profile"] == collector.DEFAULT_PROFILE
    assert loaded["coverage_manifest"]["legacy"] is True
    assert loaded["coverage_manifest"]["status"] == "partial"


def test_checkpoint_does_not_advance_for_active_or_failed_collection(tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "collector-checkpoint.json"
    empty = collector.load_checkpoint(checkpoint_path)
    db_path = tmp_path / "state.db"
    _create_state_db(db_path)
    _add_session(db_path, "active", ended_at=None)
    _add_good_messages(db_path, "active")

    result = collector.collect(db_path, empty)
    assert result[0:3] == ([], [], [])
    assert collector.merge_checkpoint(empty, result[3]) == empty

    collector.save_checkpoint(checkpoint_path, empty)
    before = checkpoint_path.read_text(encoding="utf-8")
    invalid_db = tmp_path / "invalid.db"
    invalid_db.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(sqlite3.DatabaseError):
        collector.main(
            [
                "--db",
                str(invalid_db),
                "--state",
                str(checkpoint_path),
                "--output-dir",
                str(tmp_path / "reports"),
                "--commit-state",
            ]
        )
    assert checkpoint_path.read_text(encoding="utf-8") == before


def test_default_cli_does_not_mutate_checkpoint(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state.db"
    checkpoint_path = tmp_path / "collector-checkpoint.json"
    output_dir = tmp_path / "reports"
    _create_state_db(db_path)
    _add_session(db_path, "good")
    _add_good_messages(db_path, "good")
    initial = collector.load_checkpoint(checkpoint_path)
    collector.save_checkpoint(checkpoint_path, initial)
    before = checkpoint_path.read_text(encoding="utf-8")

    assert collector.main(
        [
            "--db",
            str(db_path),
            "--state",
            str(checkpoint_path),
            "--output-dir",
            str(output_dir),
        ]
    ) == 0
    capsys.readouterr()
    assert checkpoint_path.read_text(encoding="utf-8") == before


def test_opt_in_archive_recovers_raw_user_assistant_messages_without_tool_bodies(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    archive_path = tmp_path / "source-archive.json"
    candidate_dir = tmp_path / "candidate-payloads"
    _create_state_db(db_path)
    _add_session(db_path, "recoverable")
    _add_good_messages(db_path, "recoverable")

    candidates, skipped, _child_logs, _seen = collector.collect(
        db_path, collector.load_checkpoint(tmp_path / "state.json")
    )
    result = collector.archive_collected_sessions(
        db_path, candidates, skipped, archive_path, candidate_dir
    )

    archive = collector.load_source_archive(archive_path)
    record = archive["records"][0]
    assert result.source_records_added == 1
    assert record["session_id"] == "recoverable"
    assert record["content_sha256"] == candidates[0].content_sha256
    assert [message["role"] for message in record["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert "请把这次结果归档，以后复用" in record["messages"][0]["content"]
    assert "SECRET TOOL BODY MUST NOT APPEAR" not in archive_path.read_text(
        encoding="utf-8"
    )
    assert record["tool_calls"] == {"count": 2, "names": ["git", "pytest"]}

    payloads = list(candidate_dir.glob("*.json"))
    assert len(payloads) == 1
    payload = json.loads(payloads[0].read_text(encoding="utf-8"))
    assert payload["review_status"] == "pending"
    assert payload["formal_archive"]["committed"] is False
    assert "SECRET TOOL BODY MUST NOT APPEAR" not in payloads[0].read_text(
        encoding="utf-8"
    )


def test_opt_in_archive_quarantines_sensitive_session_without_raw_content(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    archive_path = tmp_path / "source-archive.json"
    candidate_dir = tmp_path / "candidate-payloads"
    _create_state_db(db_path)
    _add_session(db_path, "sensitive")
    _add_good_messages(db_path, "sensitive")
    _add_message(db_path, "sensitive", "user", "API_KEY=sk-abcdefghijklmnop")
    _add_message(db_path, "sensitive", "tool", "Bearer tool-secret-abcdefghijkl", "http")

    candidates, skipped, _child_logs, _seen = collector.collect(
        db_path, collector.load_checkpoint(tmp_path / "state.json")
    )
    assert candidates == []
    collector.archive_collected_sessions(
        db_path, candidates, skipped, archive_path, candidate_dir
    )

    archive_text = archive_path.read_text(encoding="utf-8")
    record = collector.load_source_archive(archive_path)["records"][0]
    assert record["record_type"] == "blocked"
    assert record["coverage_manifest"]["status"] == "blocked"
    assert record["coverage_manifest"]["cleanup_qualification_blocked"] is True
    assert record["blocked"]["raw_content_written"] is False
    assert "abcdefghijklmnop" not in archive_text
    assert not list(candidate_dir.glob("*.json"))


def test_opt_in_archive_replay_is_idempotent_and_preserves_existing_records(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    archive_path = tmp_path / "source-archive.json"
    candidate_dir = tmp_path / "candidate-payloads"
    _create_state_db(db_path)
    _add_session(db_path, "replay")
    _add_good_messages(db_path, "replay")
    candidates, skipped, _child_logs, _seen = collector.collect(db_path, {"processed": {}})

    collector.archive_collected_sessions(
        db_path, candidates, skipped, archive_path, candidate_dir
    )
    original = collector.load_source_archive(archive_path)
    original["records"][0]["reviewer_note"] = "preserve me"
    collector._atomic_write(
        archive_path, json.dumps(original, ensure_ascii=False, indent=2) + "\n"
    )
    payload_path = next(candidate_dir.glob("*.json"))
    payload_before = payload_path.read_text(encoding="utf-8")

    result = collector.archive_collected_sessions(
        db_path, candidates, skipped, archive_path, candidate_dir
    )
    assert result.source_records_added == 0
    assert result.candidate_payloads_written == 0
    assert len(collector.load_source_archive(archive_path)["records"]) == 1
    assert collector.load_source_archive(archive_path)["records"][0]["reviewer_note"] == (
        "preserve me"
    )
    assert payload_path.read_text(encoding="utf-8") == payload_before


def test_opt_in_archive_rejects_corrupt_output_before_writing_candidates(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    archive_path = tmp_path / "source-archive.json"
    candidate_dir = tmp_path / "candidate-payloads"
    _create_state_db(db_path)
    _add_session(db_path, "corrupt")
    _add_good_messages(db_path, "corrupt")
    candidates, skipped, _child_logs, _seen = collector.collect(db_path, {"processed": {}})
    archive_path.write_text('{"format":', encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid source archive"):
        collector.archive_collected_sessions(
            db_path, candidates, skipped, archive_path, candidate_dir
        )
    assert archive_path.read_text(encoding="utf-8") == '{"format":'
    assert not candidate_dir.exists()


def test_default_cli_does_not_create_source_archive_or_candidate_payloads(
    tmp_path: Path, capsys
) -> None:
    db_path = tmp_path / "state.db"
    checkpoint_path = tmp_path / "collector-checkpoint.json"
    output_dir = tmp_path / "reports"
    archive_path = tmp_path / "source-archive.json"
    candidate_dir = tmp_path / "candidate-payloads"
    _create_state_db(db_path)
    _add_session(db_path, "dry-run")
    _add_good_messages(db_path, "dry-run")

    assert collector.main(
        [
            "--db",
            str(db_path),
            "--state",
            str(checkpoint_path),
            "--output-dir",
            str(output_dir),
        ]
    ) == 0
    capsys.readouterr()
    assert not archive_path.exists()
    assert not candidate_dir.exists()


def test_jsonl_archive_and_destination_collision_are_explicit_and_replayable(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    archive_path = tmp_path / "source-archive.jsonl"
    candidate_dir = tmp_path / "candidate-payloads"
    _create_state_db(db_path)
    _add_session(db_path, "jsonl")
    _add_good_messages(db_path, "jsonl")
    candidates, skipped, _child_logs, _seen = collector.collect(db_path, {"processed": {}})

    collector.archive_collected_sessions(
        db_path, candidates, skipped, archive_path, candidate_dir
    )
    lines = archive_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["version"] == collector.SOURCE_ARCHIVE_VERSION
    assert len(collector.load_source_archive(archive_path)["records"]) == 1
    with pytest.raises(collector.ArchiveCollisionError):
        collector.archive_collected_sessions(
            db_path, candidates, skipped, db_path, candidate_dir
        )


def test_incremental_jsonl_run_stages_each_output_beside_destination(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "state.db"
    checkpoint_path = tmp_path / "checkpoint.json"
    archive_path = tmp_path / "source-archive.jsonl"
    candidate_dir = tmp_path / "candidate-payloads"
    report_output = tmp_path / "reports"
    _create_state_db(db_path)
    _add_session(db_path, "jsonl-incremental")
    _add_good_messages(db_path, "jsonl-incremental")

    staged_paths: list[Path] = []
    original_atomic_write = collector._atomic_write

    def record_staging(path: Path, text: str) -> None:
        staged_paths.append(Path(path))
        original_atomic_write(path, text)

    monkeypatch.setattr(collector, "_atomic_write", record_staging)
    report = collector.incremental_run(
        db_path,
        checkpoint_path,
        archive_path,
        candidate_dir,
        report_output,
    )

    assert report["captured_count"] == 1
    assert report["source_schema"]["valid"] is True
    assert report["source_schema"]["optional_fallbacks"]["messages.tool_name"] == "native column"
    markdown = (report_output / "incremental-run.md").read_text(encoding="utf-8")
    assert "## Source schema" in markdown
    assert "Valid: `True`" in markdown
    assert "SECRET TOOL BODY MUST NOT APPEAR" not in markdown
    assert len(archive_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(collector.load_source_archive(archive_path)["records"]) == 1
    allowed_parents = {
        tmp_path.resolve(),
        candidate_dir.resolve(),
        report_output.resolve(),
    }
    assert staged_paths
    assert all(path.parent.parent.resolve() in allowed_parents for path in staged_paths)
    assert not list(tmp_path.rglob("*.staging-*"))


def test_atomic_write_leaves_no_partial_destination_or_temp_file_on_publish_error(
    tmp_path: Path, monkeypatch
) -> None:
    destination = tmp_path / "atomic.json"

    def fail_replace(_source, _destination):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(collector.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated publish failure"):
        collector._atomic_write(destination, "partial must not publish")
    assert not destination.exists()
    assert list(tmp_path.glob("atomic.json.*.tmp")) == []


def test_active_snapshot_jsonl_staging_is_cleaned_after_publish(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "state.db"
    archive_path = tmp_path / "active-snapshots.jsonl"
    report_output = tmp_path / "active-reports"
    _create_state_db(db_path)
    _add_session(db_path, "active-jsonl", ended_at=None)
    _add_good_messages(db_path, "active-jsonl")

    staged_paths: list[Path] = []
    original_atomic_write = collector._atomic_write

    def record_staging(path: Path, text: str) -> None:
        staged_paths.append(Path(path))
        original_atomic_write(path, text)

    monkeypatch.setattr(collector, "_atomic_write", record_staging)
    report = collector.active_snapshot_run(
        db_path,
        archive_path,
        report_output,
        profile="winefox",
        quiet_seconds=60,
        last_message_at={"active-jsonl": 0.0},
        now=100.0,
    )

    assert report["snapshot_records_added"] == 1
    assert report["source_schema"]["valid"] is True
    assert len(collector.load_active_snapshot_archive(archive_path)["records"]) == 1
    assert staged_paths
    assert all(
        path.parent.parent.resolve() in {
            tmp_path.resolve(),
            report_output.resolve(),
        }
        for path in staged_paths
    )
    assert not list(tmp_path.rglob("*.staging-*"))


def test_incremental_run_is_idempotent_incremental_and_failure_atomic(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "state.db"
    checkpoint_path = tmp_path / "incremental-checkpoint.json"
    archive_path = tmp_path / "source-archive.json"
    candidate_dir = tmp_path / "candidate-payloads"
    report_output = tmp_path / "reports"
    _create_state_db(db_path)
    _add_session(db_path, "active", ended_at=None)
    _add_good_messages(db_path, "active")
    _add_session(db_path, "first", ended_at=2.0)
    _add_good_messages(db_path, "first")

    first = collector.incremental_run(
        db_path=db_path,
        checkpoint_path=checkpoint_path,
        source_archive_path=archive_path,
        candidate_payload_dir=candidate_dir,
        report_output=report_output,
    )
    assert first["captured_count"] == 1
    assert first["active_sessions"]["status"] == "deferred_unsupported"
    assert first["active_sessions"]["count"] == 1
    first_checkpoint = checkpoint_path.read_text(encoding="utf-8")
    first_archive = archive_path.read_text(encoding="utf-8")
    assert len(collector.load_source_archive(archive_path)["records"]) == 1
    assert len(list(candidate_dir.glob("*.json"))) == 1

    replay = collector.incremental_run(
        db_path=db_path,
        checkpoint_path=checkpoint_path,
        source_archive_path=archive_path,
        candidate_payload_dir=candidate_dir,
        report_output=report_output,
    )
    assert replay["captured_count"] == 0
    assert checkpoint_path.read_text(encoding="utf-8") == first_checkpoint
    assert archive_path.read_text(encoding="utf-8") == first_archive
    assert len(list(candidate_dir.glob("*.json"))) == 1
    assert len(list(report_output.glob("*.json"))) == 1

    _add_session(db_path, "later", ended_at=3.0)
    _add_good_messages(db_path, "later")
    later = collector.incremental_run(
        db_path=db_path,
        checkpoint_path=checkpoint_path,
        source_archive_path=archive_path,
        candidate_payload_dir=candidate_dir,
        report_output=report_output,
    )
    assert later["captured_count"] == 1
    assert later["captured_session_ids"] == ["later"]
    assert len(collector.load_source_archive(archive_path)["records"]) == 2
    assert len(list(candidate_dir.glob("*.json"))) == 2

    _add_message(db_path, "first", "assistant", "旧会话补充的第二版已验证结果。" * 20)
    changed = collector.incremental_run(
        db_path=db_path,
        checkpoint_path=checkpoint_path,
        source_archive_path=archive_path,
        candidate_payload_dir=candidate_dir,
        report_output=report_output,
    )
    assert changed["captured_count"] == 1
    assert changed["captured_session_ids"] == ["first"]
    assert len(collector.load_source_archive(archive_path)["records"]) == 3
    assert len(list(candidate_dir.glob("*.json"))) == 3

    before_failure_checkpoint = checkpoint_path.read_text(encoding="utf-8")
    before_failure_archive = archive_path.read_text(encoding="utf-8")
    _add_session(db_path, "failed", ended_at=4.0)
    _add_good_messages(db_path, "failed")
    original_atomic_write = collector._atomic_write

    def fail_staging(_path: Path, _text: str) -> None:
        raise OSError("simulated incremental output failure")

    monkeypatch.setattr(collector, "_atomic_write", fail_staging)
    with pytest.raises(OSError, match="simulated incremental output failure"):
        collector.incremental_run(
            db_path=db_path,
            checkpoint_path=checkpoint_path,
            source_archive_path=archive_path,
            candidate_payload_dir=candidate_dir,
            report_output=report_output,
        )
    assert checkpoint_path.read_text(encoding="utf-8") == before_failure_checkpoint
    assert archive_path.read_text(encoding="utf-8") == before_failure_archive
    assert len(list(candidate_dir.glob("*.json"))) == 3
    assert not list(tmp_path.rglob("*.staging-*"))

    monkeypatch.setattr(collector, "_atomic_write", original_atomic_write)


def test_incremental_run_persists_explicit_profile_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    checkpoint_path = tmp_path / "checkpoint.json"
    archive_path = tmp_path / "source-archive.json"
    candidate_dir = tmp_path / "candidate-payloads"
    report_output = tmp_path / "reports"
    _create_state_db(db_path)
    _add_session(db_path, "profiled")
    _add_good_messages(db_path, "profiled")

    report = collector.incremental_run(
        db_path,
        checkpoint_path,
        archive_path,
        candidate_dir,
        report_output,
        profile="winefox",
    )

    digest = report["candidates"][0]["content_sha256"]
    assert report["profile"] == "winefox"
    assert "winefox:profiled:" + digest in collector.load_checkpoint(
        checkpoint_path
    )["processed"]
    record = collector.load_source_archive(archive_path)["records"][0]
    assert record["profile"] == "winefox"
    payload_path = next(candidate_dir.glob("*.json"))
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert "winefox" in payload_path.name
    assert payload["profile"] == payload["identity"]["profile"] == (
        payload["traceability"]["profile"]
    ) == "winefox"


def test_cli_profile_is_written_to_default_report(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state.db"
    checkpoint_path = tmp_path / "collector-checkpoint.json"
    output_dir = tmp_path / "reports"
    _create_state_db(db_path)
    _add_session(db_path, "cli-profile")
    _add_good_messages(db_path, "cli-profile")

    assert collector.main([
        "--db", str(db_path),
        "--state", str(checkpoint_path),
        "--output-dir", str(output_dir),
        "--profile", "otter",
    ]) == 0
    capsys.readouterr()
    report = json.loads(next(output_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert report["profile"] == "otter"
    assert report["candidates"][0]["profile"] == "otter"


def _active_run_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "archive": tmp_path / "active-snapshots.json",
        "report": tmp_path / "active-reports",
        "checkpoint": tmp_path / "ended-checkpoint.json",
    }


def _run_active_snapshot(
    db_path: Path,
    paths: dict[str, Path],
    *,
    last_message_at: dict[str, float] | None = None,
    now: float = 100.0,
    quiet_seconds: float = 60.0,
) -> dict:
    return collector.active_snapshot_run(
        db_path=db_path,
        snapshot_archive_path=paths["archive"],
        report_output=paths["report"],
        profile="winefox",
        quiet_seconds=quiet_seconds,
        last_message_at=last_message_at,
        now=now,
        ended_checkpoint_path=paths["checkpoint"],
    )


def test_active_snapshot_below_quiet_window_is_deferred_without_checkpoint_change(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    paths = _active_run_paths(tmp_path)
    _create_state_db(db_path)
    _add_session(db_path, "active", ended_at=None)
    _add_good_messages(db_path, "active")
    paths["checkpoint"].write_text('{"watermark":"preserve"}\n', encoding="utf-8")

    report = _run_active_snapshot(
        db_path, paths, last_message_at={"active": 50.0}
    )

    assert report["active_sessions"]["status"] == "deferred_quiet_window"
    assert report["active_sessions"]["deferred_count"] == 1
    assert report["snapshot_records_added"] == 0
    assert not paths["archive"].exists()
    assert paths["checkpoint"].read_text(encoding="utf-8") == '{"watermark":"preserve"}\n'


def test_quiet_active_snapshot_is_captured_with_explicit_fallback_limitation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    paths = _active_run_paths(tmp_path)
    _create_state_db(db_path)
    _add_session(db_path, "active", ended_at=None)
    _add_good_messages(db_path, "active")

    report = _run_active_snapshot(
        db_path, paths, last_message_at={"active": 0.0}
    )

    assert report["active_sessions"]["status"] == "captured"
    assert report["snapshot_records_added"] == 1
    assert report["timestamp_limitation"] == "message_timestamps_unavailable"
    records = collector.load_active_snapshot_archive(paths["archive"])["records"]
    assert records[0]["record_type"] == "active_snapshot"
    assert records[0]["snapshot_hash"] == records[0]["identity"]["snapshot_hash"]
    assert records[0]["identity"]["profile"] == "winefox"


def test_active_snapshot_uses_newest_message_timestamp_not_last_message_id(tmp_path: Path) -> None:
    db_path = tmp_path / "timestamp-order.db"
    paths = _active_run_paths(tmp_path)
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT,
              started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
              parent_session_id TEXT, message_count INTEGER DEFAULT 0
            );
            CREATE TABLE messages (
              id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
              content TEXT, tool_name TEXT, created_at REAL
            );
            INSERT INTO sessions VALUES ('active', 'desktop', 'test', 0, NULL, NULL, NULL, 2);
            INSERT INTO messages VALUES (1, 'active', 'user', 'newer', NULL, 200);
            INSERT INTO messages VALUES (2, 'active', 'assistant', 'older', NULL, 100);
            """
        )

    report = _run_active_snapshot(db_path, paths, now=250.0, quiet_seconds=150.0)

    assert report["active_sessions"]["status"] == "deferred_quiet_window"
    assert not paths["archive"].exists()


def test_active_snapshot_marks_unknown_roles_and_invalid_ids_partial(tmp_path: Path) -> None:
    db_path = tmp_path / "invalid-active.db"
    paths = _active_run_paths(tmp_path)
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT,
              started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
              parent_session_id TEXT, message_count INTEGER DEFAULT 0
            );
            CREATE TABLE messages (
              id, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, tool_name TEXT
            );
            INSERT INTO sessions VALUES ('active', 'desktop', 'test', 0, NULL, NULL, NULL, 2);
            INSERT INTO messages VALUES (1, 'active', 'system', 'event', NULL);
            INSERT INTO messages VALUES ('bad-id', 'active', 'user', 'body', NULL);
            """
        )

    report = _run_active_snapshot(db_path, paths, last_message_at={"active": 0.0})
    record = collector.load_active_snapshot_archive(paths["archive"])["records"][0]

    assert report["active_sessions"]["status"] == "captured"
    assert record["coverage_manifest"]["status"] == "partial"
    assert record["coverage_manifest"]["unknown_roles"] == 1
    assert record["coverage_manifest"]["invalid_message_ids"] == 1


def test_appending_active_messages_adds_immutable_snapshot_version(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    paths = _active_run_paths(tmp_path)
    _create_state_db(db_path)
    _add_session(db_path, "active", ended_at=None)
    _add_good_messages(db_path, "active")

    _run_active_snapshot(db_path, paths, last_message_at={"active": 0.0})
    before = collector.load_active_snapshot_archive(paths["archive"])["records"][0]
    _add_message(db_path, "active", "assistant", "An appended visible message")

    report = _run_active_snapshot(db_path, paths, last_message_at={"active": 0.0})
    records = collector.load_active_snapshot_archive(paths["archive"])["records"]
    assert report["snapshot_records_added"] == 1
    assert len(records) == 2
    assert records[0]["snapshot_hash"] == before["snapshot_hash"]
    assert records[1]["snapshot_hash"] != before["snapshot_hash"]
    assert len(records[0]["messages"]) < len(records[1]["messages"])


def test_same_active_snapshot_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    paths = _active_run_paths(tmp_path)
    _create_state_db(db_path)
    _add_session(db_path, "active", ended_at=None)
    _add_good_messages(db_path, "active")

    _run_active_snapshot(db_path, paths, last_message_at={"active": 0.0})
    before = paths["archive"].read_bytes()
    replay = _run_active_snapshot(db_path, paths, last_message_at={"active": 0.0})

    assert replay["snapshot_records_added"] == 0
    assert paths["archive"].read_bytes() == before
    assert len(collector.load_active_snapshot_archive(paths["archive"])["records"]) == 1


def test_active_snapshot_excludes_tool_bodies(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    paths = _active_run_paths(tmp_path)
    _create_state_db(db_path)
    _add_session(db_path, "active", ended_at=None)
    _add_good_messages(db_path, "active")

    _run_active_snapshot(db_path, paths, last_message_at={"active": 0.0})
    text = paths["archive"].read_text(encoding="utf-8")
    record = collector.load_active_snapshot_archive(paths["archive"])["records"][0]

    assert "SECRET TOOL BODY MUST NOT APPEAR" not in text
    assert all(message["role"] != "tool" for message in record["messages"])
    assert record["tool_calls"] == {"count": 2, "names": ["git", "pytest"]}


def test_active_snapshot_sensitive_content_is_quarantined(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    paths = _active_run_paths(tmp_path)
    _create_state_db(db_path)
    _add_session(db_path, "active", ended_at=None)
    _add_good_messages(db_path, "active")
    _add_message(db_path, "active", "tool", "Bearer active-secret-abcdefghijkl", "http")
    _add_message(db_path, "active", "user", "API_KEY=sk-abcdefghijklmnop")

    report = _run_active_snapshot(db_path, paths, last_message_at={"active": 0.0})
    text = paths["archive"].read_text(encoding="utf-8")
    record = collector.load_active_snapshot_archive(paths["archive"])["records"][0]

    assert report["quarantined_count"] == 1
    assert record["record_type"] == "blocked"
    assert record["blocked"]["raw_content_written"] is False
    assert "abcdefghijklmnop" not in text
    assert "active-secret" not in text
    assert "messages" not in record


def test_active_snapshot_source_connection_is_read_only(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_state_db(db_path)

    with collector._readonly_connect(db_path) as con:
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            con.execute("CREATE TABLE active_write_must_fail (id INTEGER)")


def test_active_snapshot_failure_preserves_outputs_and_ended_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "state.db"
    paths = _active_run_paths(tmp_path)
    _create_state_db(db_path)
    _add_session(db_path, "active", ended_at=None)
    _add_good_messages(db_path, "active")
    _run_active_snapshot(db_path, paths, last_message_at={"active": 0.0})
    archive_before = paths["archive"].read_bytes()
    report_before = (paths["report"] / "active-snapshot.json").read_bytes()
    paths["checkpoint"].write_text('{"watermark":"preserve"}\n', encoding="utf-8")
    checkpoint_before = paths["checkpoint"].read_bytes()
    _add_message(db_path, "active", "assistant", "new version")

    def fail_staging(_path: Path, _text: str) -> None:
        raise OSError("simulated active snapshot output failure")

    monkeypatch.setattr(collector, "_atomic_write", fail_staging)
    with pytest.raises(OSError, match="simulated active snapshot output failure"):
        _run_active_snapshot(db_path, paths, last_message_at={"active": 0.0})

    assert paths["archive"].read_bytes() == archive_before
    assert (paths["report"] / "active-snapshot.json").read_bytes() == report_before
    assert paths["checkpoint"].read_bytes() == checkpoint_before
    assert not list(tmp_path.rglob("*.staging-*"))


def test_active_snapshot_cli_is_explicit_and_source_only(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state.db"
    paths = _active_run_paths(tmp_path)
    _create_state_db(db_path)
    _add_session(db_path, "active", ended_at=None)
    _add_good_messages(db_path, "active")

    assert collector.main([
        "--active-snapshot",
        "--db", str(db_path),
        "--state", str(paths["checkpoint"]),
        "--profile", "winefox",
        "--snapshot-archive", str(paths["archive"]),
        "--output-dir", str(paths["report"]),
        "--quiet-seconds", "60",
        "--last-message-at", "0",
        "--now", "100",
    ]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["run_mode"] == "active-snapshot"
    assert summary["formal_kb_writes"] == 0
    assert summary["ended_checkpoint_advanced"] is False
    assert not paths["checkpoint"].exists()
    assert not list(tmp_path.glob("candidate-*"))


def _create_hermes_visibility_state_db(path: Path) -> None:
    with sqlite3.connect(path) as con:
        con.executescript(
            """
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT,
              started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
              parent_session_id TEXT, message_count INTEGER DEFAULT 0,
              profile_name TEXT
            );
            CREATE TABLE messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
              role TEXT NOT NULL, content TEXT, tool_name TEXT, timestamp REAL NOT NULL,
              active INTEGER NOT NULL DEFAULT 1, compacted INTEGER NOT NULL DEFAULT 0,
              display_identity BLOB, display_order INTEGER
            );
            """
        )


def _add_visible_message(
    path: Path, session_id: str, role: str, content: str, *, timestamp: float,
    active: int = 1, compacted: int = 0,
) -> None:
    with sqlite3.connect(path) as con:
        con.execute(
            """INSERT INTO messages(session_id, role, content, tool_name, timestamp, active, compacted)
               VALUES (?, ?, ?, NULL, ?, ?, ?)""",
            (session_id, role, content, timestamp, active, compacted),
        )


def test_ended_collector_uses_hermes_live_rows_and_profile_fence(tmp_path: Path) -> None:
    db_path = tmp_path / "hermes-state.db"
    _create_hermes_visibility_state_db(db_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """INSERT INTO sessions VALUES
            ('winefox-session', 'desktop', 'visible', 1, 2, 'session_reset', NULL, 2, 'winefox'),
            ('other-profile', 'desktop', 'other', 1, 2, 'session_reset', NULL, 2, 'nekochan')"""
        )
    _add_visible_message(db_path, "winefox-session", "user", "请保存当前可见结果", timestamp=10)
    _add_visible_message(db_path, "winefox-session", "assistant", "当前可见答案已经验证" * 50, timestamp=11)
    _add_visible_message(db_path, "winefox-session", "assistant", "Bearer hidden-value-should-not-affect-collection", timestamp=12, active=0)
    _add_visible_message(db_path, "winefox-session", "assistant", "compacted historical generation must remain invisible", timestamp=13, active=0, compacted=1)
    _add_visible_message(db_path, "other-profile", "user", "other profile request", timestamp=10)
    _add_visible_message(db_path, "other-profile", "assistant", "other profile answer" * 50, timestamp=11)

    candidates, skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, min_effective_messages=0, min_score=0, profile="winefox"
    )

    assert [candidate.session_id for candidate in candidates] == ["winefox-session"]
    assert skipped == []
    assert candidates[0].coverage_manifest["status"] == "complete"
    assert candidates[0].coverage_manifest["message_rows"] == 2
    assert "hidden-value" not in candidates[0].final_preview


def test_active_snapshot_uses_only_live_hermes_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "hermes-active.db"
    paths = _active_run_paths(tmp_path)
    _create_hermes_visibility_state_db(db_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """INSERT INTO sessions VALUES
            ('active', 'desktop', 'visible active', 1, NULL, NULL, NULL, 2, 'winefox')"""
        )
    _add_visible_message(db_path, "active", "user", "current user", timestamp=10)
    _add_visible_message(db_path, "active", "assistant", "current assistant", timestamp=11)
    _add_visible_message(db_path, "active", "assistant", "Bearer inactive-secret-value", timestamp=999, active=0)

    report = collector.active_snapshot_run(
        db_path, paths["archive"], paths["report"], profile="winefox", quiet_seconds=60, now=100,
    )

    record = collector.load_active_snapshot_archive(paths["archive"])["records"][0]
    assert report["active_sessions"]["status"] == "captured"
    assert record["coverage_manifest"]["status"] == "complete"
    assert [message["content"] for message in record["messages"]] == ["current user", "current assistant"]


def test_source_schema_reports_hermes_visibility_and_profile_capabilities(tmp_path: Path) -> None:
    db_path = tmp_path / "hermes-profile.db"
    _create_hermes_visibility_state_db(db_path)

    report = collector.source_schema_report(db_path)

    assert report["source_capabilities"]["session_profile_name"] is True
    assert report["source_capabilities"]["message_visibility"] == "active_only"


def test_structured_hermes_content_is_partial_and_not_archived_as_serialized_text(tmp_path: Path) -> None:
    db_path = tmp_path / "hermes-structured.db"
    archive_path = tmp_path / "source.json"
    candidate_dir = tmp_path / "candidates"
    _create_hermes_visibility_state_db(db_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """INSERT INTO sessions VALUES
            ('structured', 'desktop', 'structured', 1, 2, 'session_reset', NULL, 3, 'winefox')"""
        )
    _add_visible_message(db_path, "structured", "user", "plain user request", timestamp=10)
    _add_visible_message(
        db_path,
        "structured",
        "assistant",
        "\x00json:[{\"type\":\"image_url\",\"image_url\":{\"url\":\"private-attachment-ref\"}}]",
        timestamp=11,
    )
    _add_visible_message(db_path, "structured", "assistant", "plain visible response" * 50, timestamp=12)

    candidates, skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, min_effective_messages=0, min_score=0, profile="winefox"
    )
    collector.archive_collected_sessions(
        db_path, candidates, skipped, archive_path, candidate_dir, "winefox"
    )

    record = collector.load_source_archive(archive_path)["records"][0]
    assert record["coverage_manifest"]["status"] == "partial"
    assert "private-attachment-ref" not in archive_path.read_text(encoding="utf-8")
    assert all("\x00json:" not in message["content"] for message in record["messages"])


def test_incremental_run_archives_the_same_ended_snapshot_used_for_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "consistent-ended.db"
    checkpoint_path = tmp_path / "checkpoint.json"
    archive_path = tmp_path / "source.json"
    candidate_dir = tmp_path / "candidates"
    report_output = tmp_path / "reports"
    _create_state_db(db_path)
    _add_session(db_path, "stable")
    _add_good_messages(db_path, "stable")
    original_prepare = collector._prepare_archive_plan

    def append_after_selection(*args, **kwargs):
        _add_message(db_path, "stable", "assistant", "late mutation must not enter this run")
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(collector, "_prepare_archive_plan", append_after_selection)
    collector.incremental_run(
        db_path, checkpoint_path, archive_path, candidate_dir, report_output
    )

    record = collector.load_source_archive(archive_path)["records"][0]
    archived_text = "\n".join(message["content"] for message in record["messages"])
    assert "late mutation must not enter this run" not in archived_text


def test_legacy_direct_archive_fails_closed_when_selected_candidate_changed(tmp_path: Path) -> None:
    db_path = tmp_path / "direct-race.db"
    archive_path = tmp_path / "source.json"
    candidate_dir = tmp_path / "candidates"
    _create_state_db(db_path)
    _add_session(db_path, "changed")
    _add_good_messages(db_path, "changed")
    candidates, skipped, _logs, _seen = collector.collect(db_path, {"processed": {}})
    _add_message(db_path, "changed", "assistant", "changed after candidate selection")

    with pytest.raises(RuntimeError, match="changed after candidate selection"):
        collector.archive_collected_sessions(
            db_path, candidates, skipped, archive_path, candidate_dir
        )

    assert not archive_path.exists()
    assert not candidate_dir.exists()


def test_publish_recovers_hard_interrupt_before_next_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transaction_key = tmp_path / "checkpoint.json"
    archive = tmp_path / "archive.json"
    report = tmp_path / "report.json"
    archive.write_text("old archive", encoding="utf-8")
    report.write_text("old report", encoding="utf-8")
    first_archive = tmp_path / "first-archive.stage"
    first_report = tmp_path / "first-report.stage"
    first_archive.write_text("interrupted archive", encoding="utf-8")
    first_report.write_text("interrupted report", encoding="utf-8")
    real_replace = collector.os.replace
    replacements = 0

    def interrupt_after_first_destination(source, destination):
        nonlocal replacements
        if Path(destination) in {archive, report}:
            replacements += 1
            if replacements == 2:
                raise KeyboardInterrupt("simulated hard interruption")
        return real_replace(source, destination)

    monkeypatch.setattr(collector.os, "replace", interrupt_after_first_destination)
    with pytest.raises(KeyboardInterrupt, match="hard interruption"):
        collector._publish_staged_outputs(
            [(first_archive, archive), (first_report, report)], recovery_key=transaction_key
        )

    monkeypatch.setattr(collector.os, "replace", real_replace)
    second_archive = tmp_path / "second-archive.stage"
    second_report = tmp_path / "second-report.stage"
    second_archive.write_text("final archive", encoding="utf-8")
    second_report.write_text("final report", encoding="utf-8")
    collector._recover_staged_publish(transaction_key)

    assert archive.read_text(encoding="utf-8") == "old archive"
    assert report.read_text(encoding="utf-8") == "old report"

    collector._publish_staged_outputs(
        [(second_archive, archive), (second_report, report)], recovery_key=transaction_key
    )

    assert archive.read_text(encoding="utf-8") == "final archive"
    assert report.read_text(encoding="utf-8") == "final report"
    assert not collector._publish_manifest_path(transaction_key).exists()


def test_incremental_run_recovers_interrupted_archive_batch_before_replanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    checkpoint_path = tmp_path / "checkpoint.json"
    archive_path = tmp_path / "source.json"
    candidate_dir = tmp_path / "candidates"
    report_dir = tmp_path / "reports"
    _create_state_db(db_path)
    _add_session(db_path, "recover-incremental")
    _add_good_messages(db_path, "recover-incremental")
    real_replace = collector.os.replace

    def interrupt_checkpoint_replace(source, destination):
        if Path(destination) == checkpoint_path:
            raise KeyboardInterrupt("interrupt before checkpoint publish")
        return real_replace(source, destination)

    monkeypatch.setattr(collector.os, "replace", interrupt_checkpoint_replace)
    with pytest.raises(KeyboardInterrupt, match="before checkpoint"):
        collector.incremental_run(
            db_path, checkpoint_path, archive_path, candidate_dir, report_dir
        )

    assert collector._publish_manifest_path(checkpoint_path).exists()
    assert collector.load_source_archive(archive_path)["records"]

    monkeypatch.setattr(collector.os, "replace", real_replace)
    recovered = collector.incremental_run(
        db_path, checkpoint_path, archive_path, candidate_dir, report_dir
    )

    assert recovered["captured_count"] == 1
    assert len(collector.load_source_archive(archive_path)["records"]) == 1
    assert checkpoint_path.exists()
    assert not collector._publish_manifest_path(checkpoint_path).exists()


def test_incremental_run_recovers_a_real_process_kill_during_publish(
    tmp_path: Path,
) -> None:
    """Acceptance evidence: a genuine process death, not an unwound exception.

    ``os._exit`` skips every ``finally`` block and exception handler, which is
    what a power loss or SIGKILL looks like to the filesystem. The child dies
    immediately after the source archive lands and before the checkpoint does,
    so the parent must observe a真 half-published state and then recover it.
    """
    import subprocess
    import sys

    db_path = tmp_path / "state.db"
    checkpoint_path = tmp_path / "checkpoint.json"
    archive_path = tmp_path / "source-archive.json"
    candidate_dir = tmp_path / "candidate-payloads"
    report_dir = tmp_path / "reports"
    _create_state_db(db_path)
    _add_session(db_path, "hard-kill")
    _add_good_messages(db_path, "hard-kill")

    child = tmp_path / "hard_kill_child.py"
    child.write_text(
        "\n".join(
            [
                "import os",
                "import sys",
                "from pathlib import Path",
                "",
                "from fufu_archive import session_collector as collector",
                "",
                "db, checkpoint, archive, candidates, reports = (",
                "    Path(value) for value in sys.argv[1:6]",
                ")",
                "real_replace = os.replace",
                "",
                "def killing_replace(source, destination):",
                "    result = real_replace(source, destination)",
                "    if Path(destination) == archive:",
                "        sys.stdout.write('published-archive')",
                "        sys.stdout.flush()",
                "        os._exit(137)",
                "    return result",
                "",
                "os.replace = killing_replace",
                "collector.incremental_run(db, checkpoint, archive, candidates, reports)",
                "",
            ]
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(child),
            str(db_path),
            str(checkpoint_path),
            str(archive_path),
            str(candidate_dir),
            str(report_dir),
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=180,
    )

    # Proof the child really died inside the publish batch.
    assert completed.returncode == 137, (completed.stdout, completed.stderr)
    assert "published-archive" in completed.stdout
    assert collector.load_source_archive(archive_path)["records"]
    assert collector._publish_manifest_path(checkpoint_path).exists()
    assert not checkpoint_path.exists()

    # The next run recovers the durable journal before consuming any state.
    recovered = collector.incremental_run(
        db_path, checkpoint_path, archive_path, candidate_dir, report_dir
    )

    assert recovered["captured_count"] == 1
    assert len(collector.load_source_archive(archive_path)["records"]) == 1
    assert checkpoint_path.exists()
    assert not collector._publish_manifest_path(checkpoint_path).exists()
    assert not list(tmp_path.glob(".*.bak"))



def _create_modern_classification_db(path: Path, *, include_tool_calls: bool = True) -> None:
    columns = ", tool_calls TEXT" if include_tool_calls else ""
    with sqlite3.connect(path) as con:
        con.executescript(
            f"""
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT,
              started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
              parent_session_id TEXT, message_count INTEGER DEFAULT 0,
              profile_name TEXT
            );
            CREATE TABLE messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
              role TEXT NOT NULL, content TEXT, tool_name TEXT{columns},
              active INTEGER NOT NULL DEFAULT 1,
              compacted INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("classification", "desktop", "Classification", 1.0, 2.0,
             "session_reset", None, 4, "winefox"),
        )


def test_coverage_classifies_tool_only_assistant_without_double_counting(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "classification.db"
    _create_modern_classification_db(db_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO messages(session_id, role, content, tool_name, tool_calls) "
            "VALUES (?, ?, ?, ?, ?)",
            ("classification", "user", "请归档这次结果，以后复用", None, None),
        )
        con.execute(
            "INSERT INTO messages(session_id, role, content, tool_name, tool_calls) "
            "VALUES (?, ?, ?, ?, ?)",
            ("classification", "assistant", "", None, '[{"id":"tool-1"}]'),
        )
        con.execute(
            "INSERT INTO messages(session_id, role, content, tool_name, tool_calls) "
            "VALUES (?, ?, ?, ?, ?)",
            ("classification", "tool", "", "read_file", None),
        )
        con.execute(
            "INSERT INTO messages(session_id, role, content, tool_name, tool_calls) "
            "VALUES (?, ?, ?, ?, ?)",
            ("classification", "assistant", "## 已完成\n已真实验证 pytest 通过。" * 20, None, None),
        )

    candidates, skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, min_effective_messages=2, min_score=0,
        profile="winefox",
    )
    assert len(candidates) == 1
    coverage = candidates[0].coverage_manifest
    assert coverage["status"] == "partial"
    assert coverage["tool_call_only_turns"] == 1
    assert coverage["tool_bodies_excluded"] == 1
    assert coverage["malformed_or_missing_content"] == 0
    assert coverage["cleanup_qualification_blocked"] is True
    assert skipped == []


def test_coverage_keeps_empty_assistant_without_tool_calls_as_malformed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "empty-assistant.db"
    _create_modern_classification_db(db_path)
    with sqlite3.connect(db_path) as con:
        for role, content, calls in (
            ("user", "请归档这次结果，以后复用", None),
            ("assistant", "", None),
            ("assistant", "## 已完成\n已真实验证 pytest 通过。" * 20, None),
            ("tool", "tool output", None),
        ):
            con.execute(
                "INSERT INTO messages(session_id, role, content, tool_name, tool_calls) "
                "VALUES (?, ?, ?, ?, ?)",
                ("classification", role, content, None, calls),
            )
    candidates, _skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, min_effective_messages=2, min_score=0,
        profile="winefox",
    )
    assert candidates[0].coverage_manifest["tool_call_only_turns"] == 0
    assert candidates[0].coverage_manifest["malformed_or_missing_content"] == 1


def test_old_schema_without_tool_calls_does_not_guess_tool_only_turn(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "old-schema.db"
    _create_modern_classification_db(db_path, include_tool_calls=False)
    with sqlite3.connect(db_path) as con:
        for role, content in (
            ("user", "请归档这次结果，以后复用"),
            ("assistant", ""),
            ("assistant", "## 已完成\n已真实验证 pytest 通过。" * 20),
            ("tool", "tool output"),
        ):
            con.execute(
                "INSERT INTO messages(session_id, role, content, tool_name) VALUES (?, ?, ?, ?)",
                ("classification", role, content, None),
            )
    candidates, _skipped, _logs, _seen = collector.collect(
        db_path, {"processed": {}}, min_effective_messages=2, min_score=0,
        profile="winefox",
    )
    coverage = candidates[0].coverage_manifest
    assert coverage["tool_call_only_turns"] == 0
    assert coverage["malformed_or_missing_content"] >= 1
    assert coverage["status"] == "partial"


def test_source_archive_normalizes_historical_v1_coverage_without_tool_call_only_turns(
    tmp_path: Path,
) -> None:
    """A new v1 counter must not make prior v1 source archives unreadable."""
    db_path = tmp_path / "state.db"
    archive_path = tmp_path / "source.json"
    candidate_dir = tmp_path / "candidates"
    _create_state_db(db_path)
    _add_session(db_path, "historical-v1")
    _add_good_messages(db_path, "historical-v1")
    candidates, skipped, _logs, _seen = collector.collect(db_path, {"processed": {}})
    collector.archive_collected_sessions(
        db_path, candidates, skipped, archive_path, candidate_dir, "winefox"
    )

    payload = json.loads(archive_path.read_text(encoding="utf-8"))
    del payload["records"][0]["coverage_manifest"]["tool_call_only_turns"]
    archive_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = collector.load_source_archive(archive_path)["records"][0]
    assert loaded["coverage_manifest"]["tool_call_only_turns"] == 0


def test_sensitive_detection_covers_common_credential_families() -> None:
    families = {
        "github_token": "ghp_" + "A" * 36,
        "github_token_pat": "github_pat_" + "B" * 30,
        "slack_token": "xoxb-" + "1" * 12 + "-" + "2" * 12 + "-" + "C" * 24,
        "aws_access_key": "AKIA" + "D" * 16,
        "google_api_key": "AIza" + "E" * 35,
        "gitlab_token": "glpat-" + "F" * 20,
        "stripe_key": "sk_live_" + "G" * 24,
        "huggingface_token": "hf_" + "H" * 34,
        "jwt": ".".join(
            (
                "eyJhbGciOiJIUzI1NiJ9",
                "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
                "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV",
            )
        ),
        "connection_string": "postgres://svcuser:sup3rs3cret@db.internal:5432/app",
    }
    for family, sample in families.items():
        assert collector._sensitive_kinds(sample), f"{family} was not detected"


def test_sensitive_detection_ignores_ordinary_prose_and_plain_urls() -> None:
    for benign in (
        "普通的会议记录：今天讨论了归档流程与检索质量。",
        "https://example.com/docs/archive?page=2",
        "git 提交 a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        "版本 1.2.3 已发布，共 45 项通过测试。",
    ):
        assert collector._sensitive_kinds(benign) == [], benign
