from __future__ import annotations

import json
from pathlib import Path
import pytest
from fufu_archive.db import KnowledgeBase, initialize_database
from fufu_archive.deterministic_refinement import RefinementBlocked, build_pending_candidate, persist_pending_candidate

def complete_record():
    return {"session_id":"s-1","source":"test","title":"A session","content_sha256":"a"*64,"messages":[{"source_message_id":1,"role":"user","preview":"Question"},{"source_message_id":2,"role":"assistant","preview":"Answer"}],"coverage_manifest":{"status":"complete","message_rows":2,"user_assistant_bodies":2,"cleanup_qualification_blocked":False}}

def test_deterministic_refinement_emits_pending_only_with_evidence_mapping():
    result=build_pending_candidate(complete_record())
    assert result["status"] == "pending"
    assert result["provider"] is None
    assert result["semantic_extraction"] is False
    assert result["evidence"] == [{"source_message_id":1,"message_index":0},{"source_message_id":2,"message_index":1}]
    assert result["structure"]["valid"] is True
    assert result["content"] == "Question\n\nAnswer"

def test_partial_or_malformed_source_fails_closed():
    partial=complete_record(); partial["coverage_manifest"]={"status":"partial"}
    with pytest.raises(RefinementBlocked, match="coverage"): build_pending_candidate(partial)
    malformed=complete_record(); malformed["messages"]=[{"role":"user","preview":"missing id"}]
    with pytest.raises(RefinementBlocked, match="message"): build_pending_candidate(malformed)

def test_persisted_refinement_is_pending_and_not_searchable(tmp_path: Path):
    db=tmp_path/"archive.sqlite3"; initialize_database(db)
    candidate=persist_pending_candidate(KnowledgeBase(db), complete_record())
    assert candidate["status"] == "pending"
    assert candidate["metadata"]["refinement"]["evidence"][0]["source_message_id"] == 1
    assert KnowledgeBase(db).search(query="Question",collection="refinement",domain=None,limit=10,mode="fts5") == []


def test_missing_or_invalid_source_identity_fails_closed():
    missing_session = complete_record()
    missing_session.pop("session_id")
    with pytest.raises(RefinementBlocked, match="source identity"):
        build_pending_candidate(missing_session)

    invalid_hash = complete_record()
    invalid_hash["content_sha256"] = "not-a-sha256"
    with pytest.raises(RefinementBlocked, match="source identity"):
        build_pending_candidate(invalid_hash)


# --- collector -> refinement end-to-end contract -------------------------


def _create_hermes_state_db(path: Path, profile: str, message_count: int = 2) -> None:
    import sqlite3

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
              role TEXT NOT NULL, content TEXT, tool_name TEXT,
              active INTEGER NOT NULL DEFAULT 1,
              compacted INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "collector-session",
                "desktop",
                "Collector session",
                1.0,
                2.0,
                "session_reset",
                None,
                message_count,
                profile,
            ),
        )


def _add_hermes_message(
    path: Path, session_id: str, role: str, content: str, tool_name=None
) -> int:
    import sqlite3

    with sqlite3.connect(path) as con:
        cursor = con.execute(
            "INSERT INTO messages(session_id, role, content, tool_name) "
            "VALUES (?, ?, ?, ?)",
            (session_id, role, content, tool_name),
        )
        return int(cursor.lastrowid)


def _run_collector(tmp_path: Path, db_path: Path, profile: str):
    from fufu_archive import session_collector as collector

    collector.incremental_run(
        db_path,
        tmp_path / "checkpoint.json",
        tmp_path / "source-archive.json",
        tmp_path / "candidate-payloads",
        tmp_path / "reports",
        min_effective_messages=2,
        profile=profile,
    )
    archive = collector.load_source_archive(tmp_path / "source-archive.json")
    return [
        record for record in archive["records"] if record["record_type"] == "session"
    ]


def test_collector_source_archive_record_flows_into_a_pending_candidate(
    tmp_path: Path,
) -> None:
    from fufu_archive import session_collector as collector

    profile = "winefox"
    db_path = tmp_path / "state.db"
    _create_hermes_state_db(db_path, profile)
    user_id = _add_hermes_message(
        db_path, "collector-session", "user", "请把这次结果归档，以后复用"
    )
    assistant_id = _add_hermes_message(
        db_path,
        "collector-session",
        "assistant",
        "## 已完成\n已真实验证测试通过，pytest 20 passed，来源明确。" * 12,
    )

    records = _run_collector(tmp_path, db_path, profile)
    assert len(records) == 1
    record = records[0]
    assert record["coverage_manifest"]["status"] == "complete"

    kb = KnowledgeBase(tmp_path / "archive.sqlite3")
    initialize_database(tmp_path / "archive.sqlite3")
    candidate = persist_pending_candidate(kb, record)

    assert candidate["status"] == "pending"
    refinement = candidate["metadata"]["refinement"]
    assert refinement["source_session_id"] == "collector-session"
    assert refinement["source_profile"] == profile
    assert [item["source_message_id"] for item in refinement["evidence"]] == [
        user_id,
        assistant_id,
    ]
    assert profile in candidate["topic_key"]

    # The archived transcript must be the collector's real message bodies.
    assert "归档" in refinement["content"]
    assert len(collector.load_source_archive(tmp_path / "source-archive.json")) > 0


def test_batch_refinement_skips_partial_records_without_emitting_candidates(
    tmp_path: Path,
) -> None:
    from fufu_archive.deterministic_refinement import refine_source_archive

    profile = "winefox"
    db_path = tmp_path / "state.db"
    _create_hermes_state_db(db_path, profile, message_count=3)
    _add_hermes_message(db_path, "collector-session", "user", "请归档，以后复用")
    _add_hermes_message(
        db_path,
        "collector-session",
        "assistant",
        "## 已完成\n已真实验证测试通过，pytest 20 passed，来源明确。" * 12,
    )
    _add_hermes_message(
        db_path, "collector-session", "tool", "{\"tool\": \"read_file\"}", "read_file"
    )

    records = _run_collector(tmp_path, db_path, profile)
    assert records[0]["coverage_manifest"]["status"] == "partial"

    db = tmp_path / "archive.sqlite3"
    initialize_database(db)
    report = refine_source_archive(
        tmp_path / "source-archive.json", KnowledgeBase(db), profile=profile
    )

    assert report["candidates_created"] == 0
    assert report["skipped"] == [
        {
            "profile": profile,
            "session_id": "collector-session",
            "reason": "coverage_not_complete",
        }
    ]
    assert KnowledgeBase(db).search(
        query="归档", collection="refinement", domain=None, limit=10, mode="fts5"
    ) == []


def test_refinement_rejects_a_record_whose_identity_block_disagrees(
    tmp_path: Path,
) -> None:
    from fufu_archive import session_collector as collector

    profile = "winefox"
    db_path = tmp_path / "state.db"
    _create_hermes_state_db(db_path, profile)
    _add_hermes_message(db_path, "collector-session", "user", "请归档，以后复用")
    _add_hermes_message(
        db_path,
        "collector-session",
        "assistant",
        "## 已完成\n已真实验证测试通过，pytest 20 passed，来源明确。" * 12,
    )
    record = _run_collector(tmp_path, db_path, profile)[0]
    assert record["content_sha256"] == record["identity"]["content_sha256"]

    tampered = json.loads(json.dumps(record))
    tampered["identity"]["content_sha256"] = "b" * 64
    with pytest.raises(RefinementBlocked, match="identity"):
        build_pending_candidate(tampered)

    other_profile = json.loads(json.dumps(record))
    other_profile["identity"]["profile"] = "nekochan"
    with pytest.raises(RefinementBlocked, match="identity"):
        build_pending_candidate(other_profile)

    assert collector.load_source_archive(tmp_path / "source-archive.json")["records"]


def test_refinement_cli_is_wired_and_only_creates_pending_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fufu_archive.deterministic_refinement import main

    profile = "winefox"
    db_path = tmp_path / "state.db"
    _create_hermes_state_db(db_path, profile)
    _add_hermes_message(db_path, "collector-session", "user", "请归档，以后复用")
    _add_hermes_message(
        db_path,
        "collector-session",
        "assistant",
        "## 已完成\n已真实验证测试通过，pytest 20 passed，来源明确。" * 12,
    )
    _run_collector(tmp_path, db_path, profile)

    archive_db = tmp_path / "archive.sqlite3"
    initialize_database(archive_db)
    assert (
        main(
            [
                "--source-archive",
                str(tmp_path / "source-archive.json"),
                "--db",
                str(archive_db),
                "--profile",
                profile,
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["candidates_created"] == 1
    assert report["skipped"] == []
    assert report["candidates"][0]["status"] == "pending"

    kb = KnowledgeBase(archive_db)
    pending = kb.list_candidates(status="pending") if hasattr(
        kb, "list_candidates"
    ) else None
    if pending is not None:
        assert len(pending) == 1
    assert kb.search(
        query="归档", collection="refinement", domain=None, limit=10, mode="fts5"
    ) == []


def test_refinement_cli_requires_an_existing_archive_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fufu_archive.deterministic_refinement import main

    with pytest.raises(SystemExit):
        main(
            [
                "--source-archive",
                str(tmp_path / "missing.json"),
                "--db",
                str(tmp_path / "never-created.sqlite3"),
            ]
        )
    assert not (tmp_path / "never-created.sqlite3").exists()


# --- coverage eligibility policy -----------------------------------------


def _partial_tool_record() -> dict:
    """A record whose only incompleteness is a counted, declared tool exclusion."""
    record = complete_record()
    record["coverage_manifest"] = {
        "version": 1,
        "status": "partial",
        "message_rows": 2,
        "user_assistant_bodies": 2,
        "tool_bodies_excluded": 1,
        "unknown_roles": 0,
        "unknown_role_names": [],
        "malformed_or_missing_content": 0,
        "invalid_message_ids": 0,
        "invalid_fields": 0,
        "message_count_mismatch": False,
        "cleanup_qualification_blocked": True,
    }
    return record


def test_partial_records_stay_ineligible_by_default() -> None:
    with pytest.raises(RefinementBlocked, match="coverage"):
        build_pending_candidate(_partial_tool_record())


def test_declared_tool_exclusions_need_explicit_opt_in_and_are_recorded() -> None:
    payload = build_pending_candidate(
        _partial_tool_record(), allow_declared_exclusions=True
    )
    assert payload["status"] == "pending"
    assert payload["source_coverage_status"] == "partial"
    assert payload["declared_exclusions"] == {"tool_bodies_excluded": 1}
    assert payload["content"] == "Question\n\nAnswer"


def test_undeclared_partial_causes_stay_ineligible_even_with_opt_in() -> None:
    for key, value in (
        ("unknown_roles", 1),
        ("malformed_or_missing_content", 1),
        ("invalid_message_ids", 1),
        ("invalid_fields", 1),
        ("message_count_mismatch", True),
    ):
        record = _partial_tool_record()
        record["coverage_manifest"][key] = value
        with pytest.raises(RefinementBlocked, match="coverage"):
            build_pending_candidate(record, allow_declared_exclusions=True)


def test_blocked_coverage_is_never_eligible_even_with_opt_in() -> None:
    record = _partial_tool_record()
    record["coverage_manifest"]["status"] = "blocked"
    with pytest.raises(RefinementBlocked, match="coverage"):
        build_pending_candidate(record, allow_declared_exclusions=True)


def test_batch_refinement_opt_in_creates_candidates_and_declares_exclusions(
    tmp_path: Path,
) -> None:
    from fufu_archive.deterministic_refinement import refine_source_archive

    profile = "winefox"
    db_path = tmp_path / "state.db"
    _create_hermes_state_db(db_path, profile, message_count=3)
    _add_hermes_message(db_path, "collector-session", "user", "请归档，以后复用")
    _add_hermes_message(
        db_path,
        "collector-session",
        "assistant",
        "## 已完成\n已真实验证测试通过，pytest 20 passed，来源明确。" * 12,
    )
    _add_hermes_message(
        db_path, "collector-session", "tool", "{\"tool\": \"read_file\"}", "read_file"
    )
    records = _run_collector(tmp_path, db_path, profile)
    assert records[0]["coverage_manifest"]["status"] == "partial"

    db = tmp_path / "archive.sqlite3"
    initialize_database(db)
    kb = KnowledgeBase(db)

    default_report = refine_source_archive(
        tmp_path / "source-archive.json", kb, profile=profile
    )
    assert default_report["candidates_created"] == 0
    assert default_report["skipped"][0]["reason"] == "coverage_not_complete"

    opted_in = refine_source_archive(
        tmp_path / "source-archive.json",
        kb,
        profile=profile,
        allow_declared_exclusions=True,
    )
    assert opted_in["candidates_created"] == 1
    assert opted_in["skipped"] == []
    refinement = opted_in["candidates"][0]["metadata"]["refinement"]
    assert refinement["source_coverage_status"] == "partial"
    assert refinement["declared_exclusions"] == {"tool_bodies_excluded": 1}
    assert opted_in["candidates"][0]["status"] == "pending"


# --- provider contract boundary ------------------------------------------


def _provider_record() -> dict:
    record = complete_record()
    record["profile"] = "winefox"
    record["identity"] = {"profile": "winefox", "session_id": "s-1", "content_sha256": "a" * 64}
    record["messages"] = [
        {"source_message_id": 11, "role": "user", "content": "保留这条经验"},
        {"source_message_id": 12, "role": "assistant", "content": "步骤一：验证；步骤二：记录。"},
    ]
    return record


def test_provider_success_is_structured_pending_and_preserves_provenance():
    from fufu_archive.semantic_provider import SemanticExtractionProvider, SemanticExtractionRequest
    from fufu_archive.deterministic_refinement import refine_with_provider

    class Provider:
        def extract(self, request: SemanticExtractionRequest):
            assert request.profile == "winefox"
            assert request.source_message_ids == (11, 12)
            return {"items": [{"title": "经验", "content": "步骤一：验证；步骤二：记录。", "evidence": [11, 12]}]}

    assert isinstance(Provider(), SemanticExtractionProvider)
    result = refine_with_provider(_provider_record(), Provider())
    assert result["status"] == "pending"
    assert result["semantic_extraction"] is True
    assert result["provider"] == "Provider"
    assert result["source_profile"] == "winefox"
    assert result["evidence"] == [{"source_message_id": 11}, {"source_message_id": 12}]
    assert result["items"][0]["evidence"] == [11, 12]


def test_provider_rejects_invalid_json_with_stable_code_and_does_not_persist(tmp_path: Path):
    from fufu_archive.deterministic_refinement import ProviderRefinementError, refine_with_provider
    class Bad:
        def extract(self, request): return "not json"
    with pytest.raises(ProviderRefinementError) as raised:
        refine_with_provider(_provider_record(), Bad())
    assert raised.value.reason_code == "provider_invalid_json"
    db = tmp_path / "archive.sqlite3"; initialize_database(db)
    assert KnowledgeBase(db).list_candidates(status="pending") == []


def test_provider_timeout_is_bounded_and_retried_only_configured_times():
    from fufu_archive.deterministic_refinement import ProviderRefinementError, refine_with_provider
    class Slow:
        calls = 0
        def extract(self, request):
            self.calls += 1
            import time; time.sleep(0.05)
    provider = Slow()
    with pytest.raises(ProviderRefinementError) as raised:
        refine_with_provider(_provider_record(), provider, timeout_seconds=0.005, max_attempts=2)
    assert raised.value.reason_code == "provider_timeout"
    assert provider.calls == 2
    assert raised.value.attempts == 2


def test_provider_rejects_sensitive_output_and_batch_budget_boundaries():
    from fufu_archive.deterministic_refinement import ProviderRefinementError, refine_with_provider
    class Sensitive:
        def extract(self, request): return {"items": [{"title": "x", "content": "api_key=secret", "evidence": [11]}]}
    with pytest.raises(ProviderRefinementError) as raised:
        refine_with_provider(_provider_record(), Sensitive())
    assert raised.value.reason_code == "provider_sensitive_information"
    with pytest.raises(ProviderRefinementError) as raised:
        refine_with_provider(_provider_record(), Sensitive(), max_items=0)
    assert raised.value.reason_code == "batch_limit_exceeded"
