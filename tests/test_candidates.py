from __future__ import annotations

from collections import Counter
import threading
from pathlib import Path

import pytest

from fufu_archive.db import KnowledgeBase, RevisionConflict, initialize_database


def test_p41_candidate_operations_acceptance(tmp_path: Path) -> None:
    db_path = tmp_path / "p41-candidate-operations.sqlite3"
    initialize_database(db_path)
    kb = KnowledgeBase(db_path)
    collection = "p41-sanitized"

    def add_candidate(name: str) -> dict[str, object]:
        return kb.create_candidate(
            content=f"sanitized p41 candidate {name}",
            collection=collection,
            topic_key=f"p41.{name}",
            metadata={"fixture": "p41-acceptance"},
        )

    candidates = {
        name: add_candidate(name)
        for name in (
            "anchor",
            "replacement",
            "pending-a",
            "rejected-a",
            "stale-approved",
            "pending-b",
            "rejected-b",
            "committed-b",
        )
    }

    pending = candidates["pending-a"]
    for name in ("pending-a", "pending-b"):
        assert kb.search(
            query=name, collection=collection, domain=None, limit=10, mode="fts5"
        ) == []

    anchor = kb.approve_candidate(
        str(candidates["anchor"]["candidate_id"]),
        expected_revision=int(candidates["anchor"]["revision"]),
    )
    anchor_commit = kb.commit_candidate(
        str(anchor["candidate_id"]), expected_revision=int(anchor["revision"])
    )
    assert kb.search(
        query="anchor", collection=collection, domain=None, limit=10, mode="fts5"
    )[0]["document_id"] == anchor_commit["document_id"]
    assert anchor_commit["document_id"] in {
        row["id"] for row in kb.list_documents(collection=collection)
    }
    assert kb.commit_candidate(str(anchor["candidate_id"])) == anchor_commit

    replacement = kb.approve_candidate(str(candidates["replacement"]["candidate_id"]))
    replacement_commit = kb.commit_candidate(
        str(replacement["candidate_id"]),
        expected_revision=int(replacement["revision"]),
        supersedes_document_id=str(anchor_commit["document_id"]),
    )
    documents = {
        row["id"]: row
        for row in kb.list_documents(collection=collection, status=None)
    }
    assert documents[replacement_commit["document_id"]]["supersedes"] == anchor_commit[
        "document_id"
    ]
    assert documents[anchor_commit["document_id"]]["status"] == "active"

    rejected = kb.reject_candidate(str(candidates["rejected-a"]["candidate_id"]))
    with pytest.raises(ValueError, match="only approved"):
        kb.commit_candidate(str(rejected["candidate_id"]))
    for name in ("rejected-a", "rejected-b"):
        assert kb.search(
            query=name, collection=collection, domain=None, limit=10, mode="fts5"
        ) == []

    stale = kb.approve_candidate(str(candidates["stale-approved"]["candidate_id"]))
    with pytest.raises(RevisionConflict, match="revision conflict"):
        kb.reject_candidate(str(stale["candidate_id"]), expected_revision=0)
    assert kb.get_candidate(str(stale["candidate_id"]))["status"] == "approved"

    kb.reject_candidate(str(candidates["rejected-b"]["candidate_id"]))
    committed_b = kb.approve_candidate(str(candidates["committed-b"]["candidate_id"]))
    kb.commit_candidate(
        str(committed_b["candidate_id"]), expected_revision=int(committed_b["revision"])
    )

    status_counts = Counter(
        candidate["status"]
        for candidate in kb.list_candidates(status=None, collection=collection, limit=20)
    )
    assert status_counts == {
        "pending": 2,
        "approved": 1,
        "rejected": 2,
        "committed": 3,
    }
    assert {
        candidate["candidate_id"] for candidate in kb.list_candidates(collection=collection)
    } == {pending["candidate_id"], candidates["pending-b"]["candidate_id"]}

    events = kb.list_audit_events(candidate_id=str(anchor["candidate_id"]))
    assert [event["event_type"] for event in events] == [
        "candidate.created",
        "candidate.approved",
        "candidate.committed",
    ]
    assert [event["revision"] for event in events] == [0, 1, 2]
    assert [event["event_id"] for event in events] == sorted(
        event["event_id"] for event in events
    )


def test_candidate_audit_events_are_ordered_and_revisioned(tmp_path: Path) -> None:
    db_path = tmp_path / "audit.db"
    initialize_database(db_path)
    kb = KnowledgeBase(db_path)

    candidate = kb.create_candidate(content="审计候选", collection="review")
    approved = kb.approve_candidate(
        candidate["candidate_id"], expected_revision=candidate["revision"]
    )
    committed = kb.commit_candidate(
        candidate["candidate_id"], expected_revision=approved["revision"]
    )

    events = kb.list_audit_events(candidate_id=candidate["candidate_id"])
    assert [event["event_type"] for event in events] == [
        "candidate.created",
        "candidate.approved",
        "candidate.committed",
    ]
    assert [event["revision"] for event in events] == [0, 1, 2]
    assert events[-1]["to_status"] == "committed"
    assert committed["revision"] == 2

    rejected = kb.create_candidate(content="审计拒绝", collection="review")
    kb.reject_candidate(
        rejected["candidate_id"], expected_revision=rejected["revision"]
    )
    assert [event["event_type"] for event in kb.list_audit_events(
        candidate_id=rejected["candidate_id"]
    )] == ["candidate.created", "candidate.rejected"]


def test_stale_candidate_and_document_revisions_are_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "revision.db"
    initialize_database(db_path)
    kb = KnowledgeBase(db_path)

    candidate = kb.create_candidate(content="revision candidate")
    approved = kb.approve_candidate(
        candidate["candidate_id"], expected_revision=candidate["revision"]
    )
    with pytest.raises(RevisionConflict, match="revision conflict"):
        kb.reject_candidate(candidate["candidate_id"], expected_revision=0)
    assert kb.get_candidate(candidate["candidate_id"])["revision"] == approved["revision"]

    document_id, _ = kb.ingest(
        content="revision document", collection="default", domain=None,
        status="active", parent_id=None, supersedes=None, source=None,
        metadata={},
    )
    updated = kb.update_document(document_id, status="expired", expected_revision=0)
    with pytest.raises(RevisionConflict, match="revision conflict"):
        kb.update_document(document_id, status="active", expected_revision=0)
    assert updated["revision"] == 1


def test_commit_audit_failure_rolls_back_candidate_and_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "audit-rollback.db"
    initialize_database(db_path)
    kb = KnowledgeBase(db_path)
    candidate = kb.create_candidate(content="审计事务回滚", collection="review")
    approved = kb.approve_candidate(candidate["candidate_id"])
    original_record = kb._record_audit

    def fail_commit_audit(*args: object, **kwargs: object) -> None:
        if kwargs.get("event_type") == "candidate.committed":
            raise RuntimeError("simulated audit failure")
        original_record(*args, **kwargs)

    monkeypatch.setattr(kb, "_record_audit", fail_commit_audit)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        kb.commit_candidate(candidate["candidate_id"], expected_revision=approved["revision"])

    assert kb.get_candidate(candidate["candidate_id"])["status"] == "approved"
    assert kb.list_documents(collection="review") == []
    assert [event["event_type"] for event in kb.list_audit_events(
        candidate_id=candidate["candidate_id"]
    )] == ["candidate.created", "candidate.approved"]


def test_new_candidate_is_pending_and_not_searchable(tmp_path: Path) -> None:
    db_path = tmp_path / "candidates.db"
    initialize_database(db_path)
    kb = KnowledgeBase(db_path)

    candidate = kb.create_candidate(
        content="候选知识只应在提交后进入正式知识库。",
        collection="review",
    )

    assert candidate["status"] == "pending"
    assert kb.search(
        query="候选知识",
        collection="review",
        domain=None,
        limit=10,
        mode="fts5",
    ) == []


def test_approved_candidate_commits_and_commit_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "candidates.db"
    initialize_database(db_path)
    kb = KnowledgeBase(db_path)
    candidate = kb.create_candidate(
        content="已审核知识应该进入正式检索。",
        collection="review",
        metadata={"source_kind": "test"},
    )

    assert kb.approve_candidate(candidate["candidate_id"])["status"] == "approved"
    assert kb.search(
        query="已审核知识", collection="review", domain=None, limit=10, mode="fts5"
    ) == []

    committed = kb.commit_candidate(candidate["candidate_id"])
    repeated = kb.commit_candidate(candidate["candidate_id"])

    assert committed["status"] == "committed"
    assert repeated == committed
    assert kb.get_candidate(candidate["candidate_id"])["status"] == "committed"
    assert len(kb.list_documents(collection="review")) == 1
    assert kb.search(
        query="已审核知识", collection="review", domain=None, limit=10, mode="fts5"
    )[0]["document_id"] == committed["document_id"]


def test_rejected_candidate_cannot_be_committed_or_seen(tmp_path: Path) -> None:
    db_path = tmp_path / "candidates.db"
    initialize_database(db_path)
    kb = KnowledgeBase(db_path)
    candidate = kb.create_candidate(
        content="这条知识会被拒绝，不应进入正式库。", collection="review"
    )

    assert kb.reject_candidate(candidate["candidate_id"])["status"] == "rejected"
    with pytest.raises(ValueError, match="only approved"):
        kb.commit_candidate(candidate["candidate_id"])
    assert kb.search(
        query="拒绝", collection="review", domain=None, limit=10, mode="fts5"
    ) == []
    assert kb.list_documents(collection="review") == []


def test_candidate_rejects_illegal_state_transitions(tmp_path: Path) -> None:
    db_path = tmp_path / "candidates.db"
    initialize_database(db_path)
    kb = KnowledgeBase(db_path)
    pending = kb.create_candidate(content="pending", collection="review")
    rejected = kb.create_candidate(content="rejected", collection="review")
    approved = kb.create_candidate(content="approved", collection="review")

    with pytest.raises(ValueError, match="only approved"):
        kb.commit_candidate(pending["candidate_id"])
    kb.reject_candidate(rejected["candidate_id"])
    with pytest.raises(ValueError, match="rejected"):
        kb.approve_candidate(rejected["candidate_id"])
    kb.approve_candidate(approved["candidate_id"])
    with pytest.raises(ValueError, match="approved"):
        kb.approve_candidate(approved["candidate_id"])
    with pytest.raises(ValueError, match="approved"):
        kb.reject_candidate(approved["candidate_id"])


def test_candidate_commit_rolls_back_formal_write_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "candidates.db"
    initialize_database(db_path)
    kb = KnowledgeBase(db_path)
    candidate = kb.create_candidate(content="事务边界测试知识", collection="review")
    kb.approve_candidate(candidate["candidate_id"])

    original_insert = kb._insert_document

    def insert_then_fail(*args: object, **kwargs: object) -> object:
        original_insert(*args, **kwargs)
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(kb, "_insert_document", insert_then_fail)
    with pytest.raises(RuntimeError, match="simulated"):
        kb.commit_candidate(candidate["candidate_id"])

    assert kb.get_candidate(candidate["candidate_id"])["status"] == "approved"
    assert kb.list_documents(collection="review") == []


def test_concurrent_candidate_commit_publishes_once(tmp_path: Path) -> None:
    db_path = tmp_path / "candidates.db"
    initialize_database(db_path)
    writer = KnowledgeBase(db_path)
    candidate = writer.create_candidate(content="并发提交只应生成一份", collection="review")
    writer.approve_candidate(candidate["candidate_id"])
    writers = [KnowledgeBase(db_path), KnowledgeBase(db_path)]
    results: list[dict[str, str]] = []

    def commit(kb: KnowledgeBase) -> None:
        results.append(kb.commit_candidate(candidate["candidate_id"]))

    threads = [threading.Thread(target=commit, args=(kb,)) for kb in writers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert results[0] == results[1]
    assert len(writer.list_documents(collection="review")) == 1
