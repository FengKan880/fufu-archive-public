from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from fufu_archive.db import KnowledgeBase, initialize_database


def _kb(tmp_path: Path) -> KnowledgeBase:
    db_path = tmp_path / "topic-key.db"
    initialize_database(db_path)
    return KnowledgeBase(db_path)


def test_topic_key_is_saved_and_available_for_candidate_document_query_and_list(
    tmp_path: Path,
) -> None:
    kb = _kb(tmp_path)

    candidate = kb.create_candidate(
        content="topic candidate",
        collection="review",
        topic_key="ops.hermes",
    )
    assert candidate["topic_key"] == "ops.hermes"
    assert kb.list_candidates(topic_key="ops.hermes")[0]["candidate_id"] == candidate[
        "candidate_id"
    ]

    document_id, _ = kb.ingest(
        content="topic document",
        collection="review",
        domain=None,
        status="active",
        parent_id=None,
        supersedes=None,
        source=None,
        metadata={},
        topic_key="ops.hermes",
    )
    assert kb.list_documents(collection="review", topic_key="ops.hermes")[0]["id"] == document_id
    assert kb.search(
        query="topic document",
        collection="review",
        domain=None,
        topic_key="ops.hermes",
        limit=10,
        mode="fts5",
    )[0]["document_id"] == document_id


def test_same_topic_and_content_hash_is_idempotent_and_commit_returns_existing_document(
    tmp_path: Path,
) -> None:
    kb = _kb(tmp_path)
    first = kb.create_candidate(
        content="same topic content", collection="review", topic_key="topic.same"
    )
    duplicate = kb.create_candidate(
        content="same topic content", collection="review", topic_key="topic.same"
    )
    assert duplicate["candidate_id"] == first["candidate_id"]

    approved = kb.approve_candidate(first["candidate_id"])
    committed = kb.commit_candidate(first["candidate_id"], expected_revision=approved["revision"])
    repeated = kb.commit_candidate(duplicate["candidate_id"])
    assert repeated == committed
    assert len(kb.list_documents(topic_key="topic.same")) == 1


def test_same_topic_with_different_content_keeps_candidates_and_versions_separate(
    tmp_path: Path,
) -> None:
    kb = _kb(tmp_path)
    first = kb.create_candidate(
        content="version one", collection="review", topic_key="topic.version"
    )
    second = kb.create_candidate(
        content="version two", collection="review", topic_key="topic.version"
    )
    assert first["candidate_id"] != second["candidate_id"]
    assert {
        candidate["content"]
        for candidate in kb.list_candidates(topic_key="topic.version")
    } == {"version one", "version two"}


def test_approved_candidate_can_explicitly_supersede_old_document_in_one_traceable_transaction(
    tmp_path: Path,
) -> None:
    kb = _kb(tmp_path)
    old_document_id, _ = kb.ingest(
        content="old version",
        collection="review",
        domain=None,
        status="active",
        parent_id=None,
        supersedes=None,
        source=None,
        metadata={},
        topic_key="topic.version",
    )
    candidate = kb.create_candidate(
        content="new version", collection="review", topic_key="topic.version"
    )
    approved = kb.approve_candidate(candidate["candidate_id"])
    committed = kb.commit_candidate(
        candidate["candidate_id"],
        expected_revision=approved["revision"],
        supersedes_document_id=old_document_id,
    )

    listed = kb.list_documents(topic_key="topic.version", status=None)
    new_document = next(row for row in listed if row["id"] == committed["document_id"])
    assert new_document["supersedes"] == old_document_id
    event = kb.list_audit_events(candidate_id=candidate["candidate_id"])[-1]
    assert event["details"]["supersedes_document_id"] == old_document_id


def test_invalid_topic_key_is_rejected(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    with pytest.raises(ValueError):
        kb.create_candidate(content="bad topic", topic_key="not a topic")
    with pytest.raises(ValueError):
        kb.ingest(
            content="bad topic document",
            collection="default",
            domain=None,
            status="active",
            parent_id=None,
            supersedes=None,
            source=None,
            metadata={},
            topic_key="-starts-invalid",
        )


def test_pending_and_rejected_topic_candidates_never_enter_topic_search(
    tmp_path: Path,
) -> None:
    kb = _kb(tmp_path)
    pending = kb.create_candidate(
        content="pending topic secret", collection="review", topic_key="topic.hidden"
    )
    rejected = kb.create_candidate(
        content="rejected topic secret", collection="review", topic_key="topic.hidden"
    )
    kb.reject_candidate(rejected["candidate_id"])
    assert kb.search(
        query="topic secret",
        collection="review",
        domain=None,
        topic_key="topic.hidden",
        limit=10,
        mode="fts5",
    ) == []
    assert kb.get_candidate(pending["candidate_id"])["status"] == "pending"


def test_concurrent_same_topic_candidate_and_commit_are_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "topic-concurrent.db"
    initialize_database(db_path)
    writers = [KnowledgeBase(db_path) for _ in range(2)]
    candidates: list[dict[str, object]] = []

    def create(kb: KnowledgeBase) -> None:
        candidates.append(
            kb.create_candidate(
                content="concurrent topic content",
                collection="review",
                topic_key="topic.concurrent",
            )
        )

    threads = [threading.Thread(target=create, args=(kb,)) for kb in writers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({candidate["candidate_id"] for candidate in candidates}) == 1

    candidate_id = str(candidates[0]["candidate_id"])
    writers[0].approve_candidate(candidate_id)
    results: list[dict[str, object]] = []

    def commit(kb: KnowledgeBase) -> None:
        results.append(kb.commit_candidate(candidate_id))

    threads = [threading.Thread(target=commit, args=(kb,)) for kb in writers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results[0] == results[1]
    assert len(writers[0].list_documents(topic_key="topic.concurrent")) == 1


def test_invalid_supersedes_document_rolls_back_candidate_commit(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    candidate = kb.create_candidate(content="rollback version", topic_key="topic.rollback")
    approved = kb.approve_candidate(candidate["candidate_id"])
    with pytest.raises(KeyError):
        kb.commit_candidate(
            candidate["candidate_id"],
            expected_revision=approved["revision"],
            supersedes_document_id="missing-document",
        )
    assert kb.get_candidate(candidate["candidate_id"])["status"] == "approved"
    assert kb.list_documents(topic_key="topic.rollback") == []
