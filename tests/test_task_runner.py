from __future__ import annotations

import hashlib
import json
import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from fufu_archive.task_queue import TaskQueue
from fufu_archive.task_runner import RunnerConfig, main, run_once
from fufu_archive.collector_tasks import (
    COLLECT_INCREMENTAL_TASK,
    collector_handlers,
    enqueue_incremental_collection,
    handle_incremental_collection,
)


def _process_once_handler(payload: object) -> dict[str, object]:
    """Picklable handler used only by the Windows spawn-process acceptance test."""
    return {"processed": payload}


def _run_once_in_spawned_process(path: str, worker_id: str, result_pipe) -> None:
    queue = TaskQueue(path)
    report = run_once(
        queue,
        {"work": _process_once_handler},
        RunnerConfig(worker_id, max_tasks=1, lease_seconds=30),
    )
    result_pipe.send(report.as_dict())
    result_pipe.close()


class MutableClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_run_once_processes_bounded_batch_and_stores_result(tmp_path: Path) -> None:
    queue = TaskQueue(tmp_path / "queue.db")
    queue.enqueue("add", "one", {"left": 2, "right": 3})
    queue.enqueue("add", "two", {"left": 4, "right": 5})
    seen: list[dict[str, int]] = []

    report = run_once(
        queue,
        {"add": lambda payload: seen.append(payload) or {"sum": sum(payload.values())}},
        RunnerConfig("runner", max_tasks=1),
    )

    assert report.as_dict() == {
        "claimed": 1,
        "succeeded": 1,
        "retried_or_failed": 0,
        "idle": False,
    }
    assert seen == [{"left": 2, "right": 3}]
    assert queue.list_tasks(status="succeeded")[0].result_metadata == {"sum": 5}
    assert len(queue.list_tasks(status="queued")) == 1


def test_run_once_retries_handler_failure_and_continues_batch(tmp_path: Path) -> None:
    queue = TaskQueue(tmp_path / "queue.db", base_backoff=1)
    queue.enqueue("broken", "one", {})
    queue.enqueue("working", "two", {})

    def broken(_: object) -> None:
        raise RuntimeError("boom")

    report = run_once(
        queue,
        {"broken": broken, "working": lambda _: {"ok": True}},
        RunnerConfig("runner", max_tasks=2),
    )

    assert report.claimed == 2
    assert report.succeeded == 1
    assert report.retried_or_failed == 1
    assert queue.list_tasks(status="queued")[0].last_error == "RuntimeError: boom"
    assert queue.list_tasks(status="succeeded")[0].task_type == "working"


def test_run_once_redacts_common_credentials_from_failure_metadata(tmp_path: Path) -> None:
    queue = TaskQueue(tmp_path / "queue.db", base_backoff=1)
    queue.enqueue("leaky", "one", {})

    def leaky(_: object) -> None:
        raise RuntimeError(
            "request failed token=abc123 password:super-secret api_key=key-456 "
            "Authorization: Bearer synthetic-auth-value"
        )

    run_once(queue, {"leaky": leaky}, RunnerConfig("runner"))

    error = queue.list_tasks(status="queued")[0].last_error
    assert error is not None
    assert "abc123" not in error
    assert "super-secret" not in error
    assert "key-456" not in error
    assert "synthetic-auth-value" not in error
    assert error.count("[REDACTED]") >= 4


def test_run_once_unknown_task_is_retryable_and_reports_idle(tmp_path: Path) -> None:
    queue = TaskQueue(tmp_path / "queue.db", base_backoff=1)
    queue.enqueue("unknown", "one", {})

    report = run_once(queue, {}, RunnerConfig("runner", max_tasks=2))

    assert report.claimed == 1
    assert report.succeeded == 0
    assert report.retried_or_failed == 1
    assert report.idle is True
    assert queue.list_tasks(status="queued")[0].last_error.startswith("KeyError:")


def test_run_once_does_not_swallow_base_exception(tmp_path: Path) -> None:
    queue = TaskQueue(tmp_path / "queue.db")
    queue.enqueue("interruptible", "one", {})

    def interrupt(_: object) -> None:
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run_once(queue, {"interruptible": interrupt}, RunnerConfig("runner"))

    task = queue.list_tasks(status="running")[0]
    assert task.last_error is None
    assert task.lease_owner == "runner"


@pytest.mark.parametrize("max_tasks", [0, -1, True, "1"])
def test_run_once_rejects_invalid_batch_size(tmp_path: Path, max_tasks: object) -> None:
    with pytest.raises(ValueError):
        run_once(
            TaskQueue(tmp_path / "queue.db"),
            {},
            RunnerConfig("runner", max_tasks=max_tasks),
        )


def test_runner_config_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_RUNNER_WORKER_ID", "worker-x")
    monkeypatch.setenv("TEST_RUNNER_MAX_TASKS", "3")
    monkeypatch.setenv("TEST_RUNNER_LEASE_SECONDS", "12.5")
    monkeypatch.setenv("TEST_RUNNER_ENABLED", "no")

    config = RunnerConfig.from_env("TEST_RUNNER_")

    assert config == RunnerConfig("worker-x", max_tasks=3, lease_seconds=12.5, enabled=False)


def test_disabled_runner_does_not_claim_tasks(tmp_path: Path) -> None:
    queue = TaskQueue(tmp_path / "queue.db")
    queue.enqueue("work", "one", {})

    report = run_once(queue, {"work": lambda _: {"done": True}}, RunnerConfig("runner", enabled=False))

    assert report == report.__class__(0, 0, 0, True)
    assert queue.list_tasks(status="queued")


def test_runner_reclaims_task_when_acknowledgement_lease_has_expired(tmp_path: Path) -> None:
    now = [0.0]
    queue = TaskQueue(tmp_path / "queue.db", clock=lambda: now[0])
    queue.enqueue("work", "one", {})

    def complete_after_lease(_: object) -> dict[str, bool]:
        now[0] = 2.0
        return {"completed": True}

    report = run_once(
        queue,
        {"work": complete_after_lease},
        RunnerConfig("runner", lease_seconds=1.0),
    )
    task = queue.list_tasks(status="queued")[0]

    assert report == report.__class__(claimed=1, succeeded=0, retried_or_failed=1, idle=False)
    assert task.lease_owner is None
    assert task.lease_token is None


def test_persisted_pause_blocks_runner_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "queue.db"
    queue = TaskQueue(path)
    queue.enqueue("work", "one", {})
    assert queue.set_paused(True) is True

    report = run_once(queue, {"work": lambda _: {"done": True}}, RunnerConfig("runner"))
    restarted = TaskQueue(path)

    assert report == report.__class__(0, 0, 0, True)
    assert restarted.is_paused() is True
    assert restarted.list_tasks(status="queued")
    assert restarted.set_paused(False) is False
    assert restarted.is_paused() is False


def test_runner_cli_pause_and_control_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FUFU_ARCHIVE_TRUSTED_LOCAL_OPERATOR", "1")
    path = tmp_path / "queue.db"
    assert main(["--queue-db", str(path), "--pause"]) == 0
    assert json.loads(capsys.readouterr().out) == {"paused": True}
    assert main(["--queue-db", str(path), "--control-status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"paused": True}


def test_failed_task_can_be_manually_requeued_without_resetting_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FUFU_ARCHIVE_TRUSTED_LOCAL_OPERATOR", "1")
    path = tmp_path / "queue.db"
    queue = TaskQueue(path, max_attempts=1)
    queue.enqueue("work", "one", {})
    run_once(queue, {"work": lambda _: (_ for _ in ()).throw(RuntimeError("bad"))}, RunnerConfig("runner"))
    failed = queue.list_tasks(status="failed")[0]

    assert (
        main(
            [
                "--queue-db",
                str(path),
                "--retry-failed",
                failed.id,
                "--reason",
                "operator reviewed the failure after fixing the upstream path",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    requeued = TaskQueue(path).get(failed.id)
    assert output["status"] == "queued"
    assert requeued is not None and requeued.attempts == 1
    assert requeued.last_error is None


def test_collector_handler_requires_all_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing required paths"):
        handle_incremental_collection({"db_path": str(tmp_path / "state.db")})


def test_enqueue_collector_task_rejects_destination_collisions_without_execution(
    tmp_path: Path,
) -> None:
    queue = TaskQueue(tmp_path / "queue.db")
    payload = {
        "db_path": str(tmp_path / "state.db"),
        "checkpoint_path": str(tmp_path / "checkpoint.json"),
        "source_archive_path": str(tmp_path / "state.db"),
        "candidate_payload_dir": str(tmp_path / "candidates"),
        "report_output": str(tmp_path / "reports"),
    }

    with pytest.raises(ValueError, match="different paths"):
        enqueue_incremental_collection(queue, "collision", payload)

    assert queue.status_counts() == {
        "queued": 0, "running": 0, "succeeded": 0, "failed": 0
    }


def test_collector_handler_is_registered_without_default_paths() -> None:
    handlers = collector_handlers()
    assert set(handlers) == {COLLECT_INCREMENTAL_TASK}
    assert handlers[COLLECT_INCREMENTAL_TASK] is handle_incremental_collection


def test_enqueue_collector_task_is_idempotent_and_does_not_execute(
    tmp_path: Path,
) -> None:
    queue = TaskQueue(tmp_path / "queue.db")
    payload = {
        "db_path": str(tmp_path / "state.db"),
        "checkpoint_path": str(tmp_path / "checkpoint.json"),
        "source_archive_path": str(tmp_path / "archive.json"),
        "candidate_payload_dir": str(tmp_path / "candidates"),
        "report_output": str(tmp_path / "reports"),
    }

    first = enqueue_incremental_collection(queue, "batch-1", payload)
    duplicate = enqueue_incremental_collection(queue, "batch-1", payload)

    assert first == duplicate
    assert first.task_type == COLLECT_INCREMENTAL_TASK
    assert first.status == "queued"
    assert not (tmp_path / "checkpoint.json").exists()


def test_runner_restart_does_not_repeat_succeeded_task(tmp_path: Path) -> None:
    path = tmp_path / "queue.db"
    queue = TaskQueue(path)
    queue.enqueue("work", "one", {"value": 1})
    calls: list[dict[str, int]] = []

    first_report = run_once(
        queue,
        {"work": lambda payload: calls.append(payload) or {"done": True}},
        RunnerConfig("first-runner"),
    )
    second_calls: list[dict[str, int]] = []
    second_report = run_once(
        TaskQueue(path),
        {"work": lambda payload: second_calls.append(payload) or {"done": True}},
        RunnerConfig("restarted-runner"),
    )

    assert first_report.succeeded == 1
    assert second_report == second_report.__class__(0, 0, 0, True)
    assert calls == [{"value": 1}]
    assert second_calls == []
    assert TaskQueue(path).list_tasks(status="succeeded")[0].result_metadata == {
        "done": True
    }


def test_runner_restart_retries_failed_task_after_backoff(tmp_path: Path) -> None:
    path = tmp_path / "queue.db"
    clock = MutableClock()
    queue = TaskQueue(path, clock=clock, base_backoff=5)
    queue.enqueue("work", "one", {})
    attempts = 0

    def flaky(_: object) -> dict[str, bool]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary")
        return {"recovered": True}

    first = run_once(queue, {"work": flaky}, RunnerConfig("runner"))
    assert first.retried_or_failed == 1
    assert queue.list_tasks(status="queued")[0].attempts == 1

    clock.advance(5)
    second = run_once(
        TaskQueue(path, clock=clock),
        {"work": flaky},
        RunnerConfig("restarted-runner"),
    )
    assert second.succeeded == 1
    assert attempts == 2
    assert TaskQueue(path).list_tasks(status="succeeded")[0].attempts == 2


def test_runner_cli_inspect_lists_tasks_without_claiming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "queue.db"
    queue = TaskQueue(path)
    created = queue.enqueue("work", "one", {"value": 1})

    assert main(["--queue-db", str(path), "--inspect", "--status", "queued"]) == 0
    output = capsys.readouterr().out
    assert created.id in output
    assert '"status": "queued"' in output
    assert TaskQueue(path).get(created.id).status == "queued"


def test_runner_cli_summary_reports_counts_and_pause_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "queue.db"
    queue = TaskQueue(path)
    queue.enqueue("work", "one", {})
    queue.set_paused(True)

    assert main(["--queue-db", str(path), "--summary"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "paused": True,
        "counts": {"queued": 1, "running": 0, "succeeded": 0, "failed": 0},
        "lease_visibility": {
            "active": 0,
            "expired": 0,
            "missing_expiry": 0,
            "owners": [],
        },
    }


def test_runner_cli_health_is_read_only_and_reports_integrity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "queue.db"
    queue = TaskQueue(path)
    queue.enqueue("work", "one", {})

    assert main(["--queue-db", str(path), "--health"]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["ok"] is True
    assert output["integrity"] == "ok"
    assert output["counts"]["queued"] == 1
    assert queue.list_tasks(status="queued")[0].status == "queued"


def test_runner_cli_health_rejects_mutating_management_combination(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--queue-db", str(tmp_path / "queue.db"), "--health", "--pause"])


@pytest.mark.parametrize("control", ["--pause", "--resume", "--control-status"])
def test_runner_cli_inspect_rejects_other_management_actions(
    tmp_path: Path, control: str
) -> None:
    with pytest.raises(SystemExit):
        main(["--queue-db", str(tmp_path / "queue.db"), "--inspect", control])


def test_runner_cli_health_does_not_initialize_missing_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "missing" / "queue.db"

    assert main(["--queue-db", str(path), "--health"]) == 1
    output = json.loads(capsys.readouterr().out)

    assert output["ok"] is False
    assert output["error"] == "queue database does not exist"
    assert not path.exists()


def test_runner_cli_health_returns_failure_for_corrupt_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "broken.db"
    path.write_bytes(b"not sqlite")

    assert main(["--queue-db", str(path), "--health"]) == 1
    output = json.loads(capsys.readouterr().out)

    assert output["ok"] is False
    assert "not readable" in output["error"]


def test_runner_cli_rejects_status_without_inspect(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--queue-db", str(tmp_path / "queue.db"), "--status", "queued"])


def test_runner_cli_has_no_self_asserted_role_switch(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--queue-db", str(tmp_path / "queue.db"), "--role", "admin", "--pause"])


def test_runner_cli_control_actions_fail_closed_without_local_operator_interlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FUFU_ARCHIVE_TRUSTED_LOCAL_OPERATOR", raising=False)
    with pytest.raises(SystemExit):
        main(["--queue-db", str(tmp_path / "queue.db"), "--pause"])


def test_runner_cli_control_actions_require_explicit_local_operator_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FUFU_ARCHIVE_TRUSTED_LOCAL_OPERATOR", "1")
    path = tmp_path / "queue.db"
    assert main(["--queue-db", str(path), "--pause"]) == 0
    assert json.loads(capsys.readouterr().out) == {"paused": True}


@pytest.mark.parametrize("action", ["--inspect", "--summary", "--control-status"])
def test_runner_cli_read_only_actions_refuse_missing_queue_without_creating_it(
    tmp_path: Path, action: str
) -> None:
    path = tmp_path / "missing" / "queue.db"

    with pytest.raises(SystemExit):
        main(["--queue-db", str(path), action])

    assert not path.exists()
    assert not path.parent.exists()


def test_runner_cli_read_only_actions_need_no_interlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("FUFU_ARCHIVE_TRUSTED_LOCAL_OPERATOR", raising=False)
    path = tmp_path / "queue.db"
    TaskQueue(path)
    assert main(["--queue-db", str(path), "--summary"]) == 0
    assert json.loads(capsys.readouterr().out)["paused"] is False


def test_runner_role_allows_worker_to_process_tasks(tmp_path: Path) -> None:
    queue = TaskQueue(tmp_path / "queue.db")
    queue.enqueue("add", "one", {"value": 1})
    report = run_once(
        queue, {"add": lambda payload: payload}, RunnerConfig("worker-1", role="worker")
    )
    assert report.succeeded == 1


def test_runner_renews_lease_while_a_slow_handler_is_running(tmp_path: Path) -> None:
    import threading
    import time

    queue_path = tmp_path / "queue.db"
    queue = TaskQueue(queue_path)
    queue.enqueue("slow", "slow-lease", {"value": 1})
    started = threading.Event()
    release = threading.Event()
    reports: list[object] = []

    def slow_handler(_: object) -> dict[str, bool]:
        started.set()
        assert release.wait(timeout=2)
        return {"done": True}

    thread = threading.Thread(
        target=lambda: reports.append(
            run_once(
                queue,
                {"slow": slow_handler},
                RunnerConfig("runner-a", lease_seconds=0.06),
            )
        ),
        daemon=True,
    )
    thread.start()
    assert started.wait(timeout=1)
    time.sleep(0.14)

    assert TaskQueue(queue_path).claim("runner-b", lease_seconds=0.06) is None

    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert reports and reports[0].succeeded == 1


def test_spawned_runners_execute_one_queue_task_at_most_once(tmp_path: Path) -> None:
    """Two OS processes running run_once must not execute one task twice."""
    queue_path = tmp_path / "queue.db"
    queue = TaskQueue(queue_path)
    task = queue.enqueue("work", "spawned-runner", {"value": 1})

    context = multiprocessing.get_context("spawn")
    pipes = [context.Pipe(duplex=False) for _ in range(2)]
    processes = [
        context.Process(
            target=_run_once_in_spawned_process,
            args=(str(queue_path), f"runner-{index}", send),
        )
        for index, (_receive, send) in enumerate(pipes)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    reports = []
    for receive, _send in pipes:
        assert receive.poll(1), "spawned runner exited without a report"
        reports.append(receive.recv())

    assert sum(report["claimed"] for report in reports) == 1
    assert sum(report["succeeded"] for report in reports) == 1
    assert sum(bool(report["idle"]) for report in reports) == 1
    final = TaskQueue(queue_path).get(task.id)
    assert final is not None and final.status == "succeeded"


def _create_legacy_queue_schema(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, task_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL, available_at REAL NOT NULL,
                lease_owner TEXT, lease_expires_at REAL, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, last_error TEXT, result_metadata_json TEXT
            );
            CREATE TABLE queue_control (id INTEGER PRIMARY KEY, paused INTEGER NOT NULL);
            INSERT INTO queue_control VALUES (1, 0);
            CREATE TABLE queue_settings (
                id INTEGER PRIMARY KEY, max_attempts INTEGER NOT NULL,
                base_backoff REAL NOT NULL, max_backoff REAL NOT NULL
            );
            INSERT INTO queue_settings VALUES (1, 3, 1.0, 300.0);
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, event TEXT NOT NULL,
                reason TEXT, created_at REAL NOT NULL
            );
            """
        )


def _file_identity(path: Path) -> tuple[int, int, str]:
    return (
        path.stat().st_size,
        path.stat().st_mtime_ns,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


@pytest.mark.parametrize("action", ["--inspect", "--summary", "--control-status"])
def test_runner_read_only_actions_refuse_legacy_queue_without_migrating_it(
    tmp_path: Path, action: str
) -> None:
    path = tmp_path / "legacy.db"
    _create_legacy_queue_schema(path)
    before = _file_identity(path)
    tables_before = sqlite3.connect(path).execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()

    with pytest.raises(SystemExit):
        main(["--queue-db", str(path), action])

    assert _file_identity(path) == before
    assert not path.with_name(path.name + "-wal").exists()
    assert sqlite3.connect(path).execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall() == tables_before


def test_runner_read_only_actions_do_not_mutate_a_current_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "queue.db"
    TaskQueue(path).enqueue("work", "one", {"value": 1})
    before = _file_identity(path)

    for action in ("--inspect", "--summary", "--control-status"):
        assert main(["--queue-db", str(path), action]) == 0
        capsys.readouterr()
        assert _file_identity(path) == before
