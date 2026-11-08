from __future__ import annotations

import sqlite3
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from fufu_archive.task_queue import TaskLeaseError, TaskQueue, TaskValidationError


class MutableClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _claim_in_process(path: str, worker: str, result_pipe) -> None:
    task = TaskQueue(path).claim(worker, lease_seconds=60)
    result_pipe.send(None if task is None else task.id)
    result_pipe.close()


def test_enqueue_is_idempotent_and_preserves_original_task(tmp_path: Path) -> None:
    queue = TaskQueue(tmp_path / "queue.db")

    first = queue.enqueue(
        "collect.session",
        "session-1",
        {"source": "hermes", "limit": 10},
    )
    duplicate = queue.enqueue(
        "collect.session",
        "session-1",
        {"source": "hermes", "limit": 10},
    )

    assert duplicate == first
    with pytest.raises(ValueError, match="idempotency key already exists"):
        queue.enqueue("different.task", "session-1", {"source": "other"})


def test_concurrent_workers_claim_a_task_at_most_once(tmp_path: Path) -> None:
    path = tmp_path / "queue.db"
    TaskQueue(path).enqueue("example", "one", {"value": 1})

    def claim(worker: str):
        return TaskQueue(path).claim(worker, lease_seconds=60)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(claim, ["worker-a", "worker-b"]))

    successful = [task for task in claimed if task is not None]
    assert len(successful) == 1
    assert successful[0].lease_owner in {"worker-a", "worker-b"}


def test_separate_processes_claim_a_task_at_most_once(tmp_path: Path) -> None:
    path = tmp_path / "queue.db"
    TaskQueue(path).enqueue("example", "one", {})
    context = multiprocessing.get_context("spawn")
    pipes = [context.Pipe(duplex=False) for _ in range(2)]
    processes = [
        context.Process(
            target=_claim_in_process,
            args=(str(path), f"process-{index}", pipe[1]),
        )
        for index, pipe in enumerate(pipes)
    ]
    for process in processes:
        process.start()
    results = [pipe[0].recv() for pipe in pipes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert sum(result is not None for result in results) == 1


def test_expired_lease_can_be_reclaimed_by_another_worker(tmp_path: Path) -> None:
    clock = MutableClock()
    path = tmp_path / "queue.db"
    queue = TaskQueue(path, clock=clock, max_attempts=3)
    queue.enqueue("example", "one", {})

    first = queue.claim("worker-a", lease_seconds=10)
    assert first is not None
    clock.advance(11)

    assert TaskQueue(path, clock=clock).reclaim_expired_leases() == 1
    # Reclaimed work waits out one bounded backoff before it can be re-claimed.
    clock.advance(1)
    recovered = TaskQueue(path, clock=clock).claim("worker-b", lease_seconds=10)
    assert recovered is not None
    assert recovered.id == first.id
    assert recovered.attempts == 2
    assert recovered.lease_owner == "worker-b"


def test_retry_uses_injected_clock_and_bounded_exponential_backoff(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    queue = TaskQueue(
        tmp_path / "queue.db",
        clock=clock,
        max_attempts=4,
        base_backoff=5,
        max_backoff=12,
    )
    queue.enqueue("example", "one", {})
    claimed = queue.claim("worker", lease_seconds=30)
    assert claimed is not None

    retried = queue.retry(
        claimed.id, "worker", "temporary failure", lease_token=claimed.lease_token
    )
    assert retried.status == "queued"
    assert retried.available_at == 1_005
    assert queue.claim("other", lease_seconds=30) is None

    clock.advance(5)
    claimed_again = queue.claim("other", lease_seconds=30)
    assert claimed_again is not None
    assert claimed_again.attempts == 2
    retried_again = queue.retry(
        claimed_again.id,
        "other",
        "still failing",
        lease_token=claimed_again.lease_token,
    )
    assert retried_again.available_at == 1_015

    clock.advance(10)
    claimed_third = queue.claim("other", lease_seconds=30)
    assert claimed_third is not None
    retried_third = queue.retry(
        claimed_third.id,
        "other",
        "bounded failure",
        lease_token=claimed_third.lease_token,
    )
    assert retried_third.available_at == 1_027


def test_retry_marks_task_failed_after_max_attempts(tmp_path: Path) -> None:
    clock = MutableClock()
    queue = TaskQueue(tmp_path / "queue.db", clock=clock, max_attempts=2)
    queue.enqueue("example", "one", {})

    first = queue.claim("worker", lease_seconds=30)
    assert first is not None
    queue.retry(first.id, "worker", "failure 1", lease_token=first.lease_token)
    clock.advance(1)
    second = queue.claim("worker", lease_seconds=30)
    assert second is not None

    failed = queue.retry(
        second.id, "worker", "failure 2", lease_token=second.lease_token
    )
    assert failed.status == "failed"
    assert failed.attempts == 2
    assert failed.last_error == "failure 2"
    assert queue.claim("other", lease_seconds=30) is None


def test_success_acknowledgement_and_inspection_are_durable(tmp_path: Path) -> None:
    path = tmp_path / "queue.db"
    queue = TaskQueue(path)
    queue.enqueue("example", "one", {"value": 1})
    claimed = queue.claim("worker", lease_seconds=30)
    assert claimed is not None

    completed = queue.acknowledge(
        claimed.id,
        "worker",
        {"document_id": "doc-1", "count": 2},
        lease_token=claimed.lease_token,
    )
    assert completed.status == "succeeded"
    assert completed.result_metadata == {"document_id": "doc-1", "count": 2}
    assert queue.get(claimed.id) == completed
    assert [task.status for task in queue.list_tasks()] == ["succeeded"]


def test_summary_reports_counts_pause_and_lease_visibility_without_reclaiming(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    queue = TaskQueue(tmp_path / "queue.db", clock=clock)
    queue.enqueue("example", "one", {})
    claimed = queue.claim("worker-a", lease_seconds=10)
    assert claimed is not None
    queue.set_paused(True)

    summary = queue.summary()

    assert summary == {
        "paused": True,
        "counts": {"queued": 0, "running": 1, "succeeded": 0, "failed": 0},
        "lease_visibility": {
            "active": 1,
            "expired": 0,
            "missing_expiry": 0,
            "owners": ["worker-a"],
        },
    }
    assert queue.get(claimed.id).status == "running"


def test_health_is_read_only_and_reports_integrity_and_counts(tmp_path: Path) -> None:
    path = tmp_path / "queue.db"
    queue = TaskQueue(path)
    queue.enqueue("example", "one", {})

    health = queue.health()

    assert health["ok"] is True
    assert health["integrity"] == "ok"
    assert health["missing_tables"] == []
    assert health["counts"] == {
        "queued": 1,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
    }
    assert queue.list_tasks(status="queued")[0].status == "queued"


def test_health_does_not_create_missing_database(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "queue.db"
    queue = TaskQueue.__new__(TaskQueue)
    queue.db_path = path

    health = queue.health()

    assert health["ok"] is False
    assert health["error"] == "queue database does not exist"
    assert not path.exists()


def test_health_reports_missing_schema_without_initializing_it(tmp_path: Path) -> None:
    path = tmp_path / "partial.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")

    health = TaskQueue.read_only_health(path)

    assert health["ok"] is False
    assert health["missing_tables"] == [
        "queue_control",
        "queue_settings",
        "task_events",
    ]


def test_health_rejects_tables_with_missing_required_columns(tmp_path: Path) -> None:
    path = tmp_path / "wrong-columns.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks(status TEXT);
            CREATE TABLE queue_control(paused INTEGER);
            CREATE TABLE queue_settings(
                max_attempts INTEGER, base_backoff REAL, max_backoff REAL
            );
            INSERT INTO tasks VALUES ('queued');
            """
        )

    health = TaskQueue.read_only_health(path)

    assert health["ok"] is False
    assert health["missing_columns"]["tasks"]
    assert health["missing_columns"]["queue_control"] == ["id"]


def test_health_reports_unreadable_sqlite_file(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    path.write_bytes(b"not a sqlite database")

    health = TaskQueue.read_only_health(path)

    assert health["ok"] is False
    assert "not readable" in health["error"]


def test_health_does_not_reclaim_expired_leases(tmp_path: Path) -> None:
    clock = MutableClock()
    queue = TaskQueue(tmp_path / "queue.db", clock=clock)
    created = queue.enqueue("work", "one", {})
    claimed = queue.claim("worker", lease_seconds=5)
    assert claimed is not None
    clock.advance(10)

    health = queue.health()

    assert health["ok"] is True
    assert health["counts"]["running"] == 1
    assert queue.get(created.id).status == "running"


def test_restart_recovers_queued_task(tmp_path: Path) -> None:
    path = tmp_path / "queue.db"
    first_queue = TaskQueue(path)
    created = first_queue.enqueue("example", "one", {"value": 1})

    restarted = TaskQueue(path)
    assert restarted.get(created.id) == created
    claimed = restarted.claim("worker", lease_seconds=30)
    assert claimed is not None
    assert claimed.id == created.id


def test_restart_requires_the_persisted_retry_configuration(tmp_path: Path) -> None:
    path = tmp_path / "queue.db"
    TaskQueue(path, max_attempts=4, base_backoff=2, max_backoff=20)

    reopened = TaskQueue(path)
    assert reopened.max_attempts == 4
    assert reopened.base_backoff == 2
    assert reopened.max_backoff == 20
    assert reopened.status_counts() == {
        "queued": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
    }
    with pytest.raises(TaskValidationError, match="retry settings conflict"):
        TaskQueue(path, max_attempts=3, base_backoff=2, max_backoff=20)


def test_stale_lease_token_cannot_ack_after_same_worker_reclaims_task(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    path = tmp_path / "queue.db"
    queue = TaskQueue(path, clock=clock)
    queue.enqueue("example", "one", {})
    first = queue.claim("same-worker", lease_seconds=5)
    assert first is not None and first.lease_token is not None

    clock.advance(6)
    assert TaskQueue(path, clock=clock).reclaim_expired_leases() == 1
    clock.advance(1)
    second = TaskQueue(path, clock=clock).claim("same-worker", lease_seconds=5)
    assert second is not None and second.lease_token is not None
    assert second.lease_token != first.lease_token

    with pytest.raises(TaskLeaseError):
        TaskQueue(path, clock=clock).acknowledge(
            first.id, "same-worker", lease_token=first.lease_token
        )
    current = TaskQueue(path, clock=clock).get(second.id)
    assert current is not None and current.status == "running"


@pytest.mark.parametrize(
    "task_type, idempotency_key, payload",
    [
        ("", "key", {}),
        ("example", "", {}),
        ("example", "key", object()),
        ("example", "key", {"value": float("nan")}),
        ("example", "key", {"value": "\ud800"}),
    ],
)
def test_enqueue_rejects_invalid_values(
    tmp_path: Path, task_type: object, idempotency_key: object, payload: object
) -> None:
    queue = TaskQueue(tmp_path / "queue.db")

    with pytest.raises(ValueError):
        queue.enqueue(task_type, idempotency_key, payload)


def test_queue_path_is_explicit_and_isolated_from_formal_archive_db(
    tmp_path: Path,
) -> None:
    formal_archive = tmp_path / "formal-archive.db"
    with sqlite3.connect(formal_archive) as connection:
        connection.execute("CREATE TABLE archive_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO archive_marker VALUES ('untouched')")

    queue_path = tmp_path / "queues" / "tasks.db"
    queue = TaskQueue(queue_path)
    queue.enqueue("example", "one", {})

    assert queue_path.exists()
    with sqlite3.connect(formal_archive) as connection:
        assert connection.execute(
            "SELECT value FROM archive_marker"
        ).fetchone() == ("untouched",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone() is None


def test_paused_queue_rejects_direct_claim_even_without_runner(tmp_path: Path) -> None:
    queue = TaskQueue(tmp_path / "queue.db")
    queue.enqueue("work", "paused-direct", {"value": 1})
    queue.set_paused(True)

    assert queue.claim("worker", lease_seconds=30) is None


def _drive_to_failed(
    queue: TaskQueue, clock: MutableClock, task_id: str, cycles: int = 1
) -> None:
    for _ in range(cycles):
        claimed = queue.claim("worker", lease_seconds=30)
        assert claimed is not None and claimed.id == task_id
        queue.retry(
            claimed.id, "worker", "boom", lease_token=claimed.lease_token
        )
        clock.advance(queue.max_backoff)


def test_manual_requeue_is_bounded_and_durably_audited(tmp_path: Path) -> None:
    clock = MutableClock()
    queue = TaskQueue(tmp_path / "queue.db", clock=clock, max_attempts=3)
    task = queue.enqueue("work", "manual-bound", {})
    _drive_to_failed(queue, clock, task.id, cycles=3)
    assert queue.get(task.id).status == "failed"

    for expected in (1, 2, 3):
        requeued = queue.requeue_failed(
            task.id, reason=f"operator retry {expected}"
        )
        assert requeued.status == "queued"
        assert requeued.manual_retries == expected
        _drive_to_failed(queue, clock, task.id)

    with pytest.raises(TaskValidationError, match="manual retr"):
        queue.requeue_failed(task.id, reason="one too many")

    assert [event["reason"] for event in queue.list_events(task.id)] == [
        "operator retry 1",
        "operator retry 2",
        "operator retry 3",
    ]


def test_inspection_redacts_payload_lease_token_and_result_metadata(
    tmp_path: Path,
) -> None:
    queue = TaskQueue(tmp_path / "queue.db")
    queue.enqueue("work", "redact", {"api_key": "sk-live-DEADBEEF"})
    claimed = queue.claim("worker", lease_seconds=30)
    assert claimed is not None

    public = claimed.as_dict(redact=True)
    assert public["payload"] == "[REDACTED]"
    assert public["lease_token"] == "[REDACTED]"
    assert public["result_metadata"] is None
    assert public["lease_owner"] == "worker"
    assert public["status"] == "running"

    queue.enqueue("work", "redact-result", {})
    finished = queue.claim("worker", lease_seconds=30)
    queue.acknowledge(
        finished.id,
        "worker",
        {"token": "result-secret"},
        lease_token=finished.lease_token,
    )
    done = queue.get(finished.id).as_dict(redact=True)
    assert done["result_metadata"] == "[REDACTED]"

    listed = queue.list_tasks(status="running")[0].as_dict(redact=True)
    assert listed["payload"] == "[REDACTED]"

    raw = claimed.as_dict()
    assert raw["payload"] == {"api_key": "sk-live-DEADBEEF"}


def test_expired_lease_reclaim_honours_backoff_before_reclaiming(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    path = tmp_path / "queue.db"
    queue = TaskQueue(path, clock=clock, max_attempts=5)
    queue.enqueue("work", "reclaim-backoff", {})
    claimed = queue.claim("worker-a", lease_seconds=10)
    assert claimed is not None
    clock.advance(11)

    assert queue.reclaim_expired_leases() == 1
    recovered = queue.get(claimed.id)
    assert recovered.status == "queued"
    assert recovered.available_at > clock.value
    assert queue.claim("worker-b", lease_seconds=10) is None

    clock.advance(queue.base_backoff)
    again = queue.claim("worker-b", lease_seconds=10)
    assert again is not None and again.id == claimed.id


def test_queue_refuses_to_initialize_inside_the_formal_archive_database(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "fufu-archive.sqlite3"
    with sqlite3.connect(archive) as connection:
        connection.execute("CREATE TABLE documents (id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE candidates (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO documents(id) VALUES ('untouched')")

    with pytest.raises(TaskValidationError, match="archive database"):
        TaskQueue(archive)

    with sqlite3.connect(archive) as connection:
        assert connection.execute("SELECT id FROM documents").fetchone() == (
            "untouched",
        )
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone() is None


def test_reclaim_uses_bounded_exponential_backoff_like_explicit_retry(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    path = tmp_path / "queue.db"
    queue = TaskQueue(
        path, clock=clock, max_attempts=5, base_backoff=5, max_backoff=100
    )
    queue.enqueue("example", "reclaim-exponential", {})

    first = queue.claim("worker-a", lease_seconds=1)
    assert first is not None
    clock.value = 1_001
    assert queue.reclaim_expired_leases() == 1
    # attempts=1, so the exponential term is base * 2 ** 0 = 5.
    clock.value = 1_006
    second = queue.claim("worker-b", lease_seconds=1)
    assert second is not None
    assert second.attempts == 2

    clock.value = 1_007
    assert queue.reclaim_expired_leases() == 1
    # attempts=2 must wait base * 2 ** 1 = 10s, exactly like TaskQueue.retry().
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT attempts, available_at FROM tasks"
        ).fetchone()
    assert row[0] == 2
    assert row[1] == 1_017


def test_reclaim_backoff_is_clamped_to_max_backoff(tmp_path: Path) -> None:
    clock = MutableClock()
    path = tmp_path / "queue.db"
    queue = TaskQueue(
        path, clock=clock, max_attempts=9, base_backoff=5, max_backoff=12
    )
    queue.enqueue("example", "reclaim-clamped", {})

    available = {1: 1_006, 2: 1_017, 3: 1_030, 4: 1_043}
    for attempt, expected_available_at in available.items():
        claimed = queue.claim("worker", lease_seconds=1)
        assert claimed is not None
        assert claimed.attempts == attempt
        clock.value = claimed.lease_expires_at
        assert queue.reclaim_expired_leases() == 1
        with sqlite3.connect(path) as connection:
            row = connection.execute("SELECT available_at FROM tasks").fetchone()
        assert row[0] == expected_available_at
        clock.value = expected_available_at


def test_unrepresentable_backoff_option_is_rejected_as_a_validation_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(TaskValidationError):
        TaskQueue(tmp_path / "queue.db", base_backoff=10**1000)
