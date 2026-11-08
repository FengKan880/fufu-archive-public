"""Explicit one-shot task runner for the durable queue.

This module intentionally provides no scheduler, daemon, cron integration, or
implicit database path. Callers supply an initialized :class:`TaskQueue`, a
worker id configuration, and handlers for the task types they want to execute.

Authorization status: there is no authenticated principal and no authorization
boundary. ``RunnerConfig.role`` is an in-process capability label for trusted
embedding code, not a credential. The CLI exposes no role switch, because a
caller-supplied role string is not an identity; state-changing queue control
actions instead fail closed behind an explicit local-operator interlock
(``FUFU_ARCHIVE_TRUSTED_LOCAL_OPERATOR=1``) that only acknowledges local trust
and does not authenticate anybody.
"""

from __future__ import annotations

import os
import argparse
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from pathlib import Path

from .task_queue import Task, TaskLeaseError, TaskQueue, TaskValidationError
from .security import require_capability


TaskHandler = Callable[[Any], Any | None]

_LOCAL_OPERATOR_ENV = "FUFU_ARCHIVE_TRUSTED_LOCAL_OPERATOR"
_LOCAL_OPERATOR_REQUIRED = (
    "queue control actions are disabled: this project has no authenticated "
    "authorization boundary, so a caller-supplied role cannot grant privileges. "
    f"Set {_LOCAL_OPERATOR_ENV}=1 only to acknowledge that this CLI is an "
    "explicitly trusted local operator interface."
)


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    """Validated settings for one explicit runner invocation."""

    worker_id: str
    max_tasks: int = 1
    lease_seconds: float = 60.0
    enabled: bool = True
    role: str = "worker"

    def __post_init__(self) -> None:
        if not isinstance(self.worker_id, str) or not self.worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        if len(self.worker_id) > 128:
            raise ValueError("worker_id must be at most 128 characters")
        if (
            isinstance(self.max_tasks, bool)
            or not isinstance(self.max_tasks, int)
            or self.max_tasks < 1
        ):
            raise ValueError("max_tasks must be a positive integer")
        if self.role not in {"reader", "worker", "admin"}:
            raise ValueError("role must be reader, worker, or admin")
        if (
            isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, (int, float))
            or self.lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive number")

    @classmethod
    def from_env(cls, prefix: str = "FUFU_ARCHIVE_RUNNER_") -> "RunnerConfig":
        """Read and validate runner settings without starting a runner."""
        worker_id = os.getenv(f"{prefix}WORKER_ID", "fufu-runner")
        raw_max_tasks = os.getenv(f"{prefix}MAX_TASKS", "1")
        raw_lease_seconds = os.getenv(f"{prefix}LEASE_SECONDS", "60")
        raw_enabled = os.getenv(f"{prefix}ENABLED", "true").strip().lower()
        try:
            max_tasks = int(raw_max_tasks)
        except ValueError as error:
            raise ValueError(f"{prefix}MAX_TASKS must be an integer") from error
        try:
            lease_seconds = float(raw_lease_seconds)
        except ValueError as error:
            raise ValueError(f"{prefix}LEASE_SECONDS must be a number") from error
        if raw_enabled not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError(f"{prefix}ENABLED must be a boolean")
        return cls(
            worker_id=worker_id,
            max_tasks=max_tasks,
            lease_seconds=lease_seconds,
            enabled=raw_enabled in {"true", "1", "yes"},
        )


@dataclass(frozen=True)
class RunReport:
    """Counters from one bounded runner invocation."""

    claimed: int
    succeeded: int
    retried_or_failed: int
    idle: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "retried_or_failed": self.retried_or_failed,
            "idle": self.idle,
        }


_SENSITIVE_ERROR_VALUE = re.compile(
    r"(?i)(\b(?:api[_-]?key|password|passwd|secret|token)\b\s*[=:]\s*)([^\s,;\]\}\)]+)|"
    r"(\bauthorization\b\s*[=:]\s*(?:bearer\s+)?)([^\s,;\]\}\)]+)|"
    r"(\bbearer\s*[:=]?\s*)([^\s,;\]\}\)]+)"
)


def _error_text(error: BaseException) -> str:
    """Return bounded diagnostic text without persisting common credentials."""
    message = str(error).strip() or error.__class__.__name__
    def redact(match: re.Match[str]) -> str:
        prefix = match.group(1) or match.group(3) or match.group(5) or ""
        return f"{prefix}[REDACTED]"

    safe = _SENSITIVE_ERROR_VALUE.sub(redact, message)
    return f"{error.__class__.__name__}: {safe}"[:4000]


def _lease_heartbeat(
    queue: TaskQueue, task: Task, config: RunnerConfig, stop: threading.Event
) -> None:
    """Keep one in-process handler lease live; exits with its bounded batch."""
    interval = max(0.01, min(config.lease_seconds / 3.0, 5.0))
    while not stop.wait(interval):
        try:
            queue.renew_lease(
                task.id,
                config.worker_id,
                lease_token=_lease_token(task),
                lease_seconds=config.lease_seconds,
            )
        except TaskLeaseError:
            return


def run_once(
    queue: TaskQueue,
    handlers: Mapping[str, TaskHandler],
    config: RunnerConfig,
) -> RunReport:
    """Claim and process at most the configured number of runnable tasks.

    The function returns after the bounded batch is exhausted or the queue is
    idle. It never starts a background loop. A disabled configuration performs
    no queue operation and returns an idle report.
    """
    if not isinstance(config, RunnerConfig):
        raise TypeError("config must be a RunnerConfig")
    if not isinstance(handlers, Mapping):
        raise TypeError("handlers must be a mapping")
    require_capability(config.role, "execute")
    if not config.enabled or queue.is_paused():
        return RunReport(claimed=0, succeeded=0, retried_or_failed=0, idle=True)

    claimed_count = 0
    succeeded_count = 0
    failed_count = 0
    for _ in range(config.max_tasks):
        task = queue.claim(config.worker_id, lease_seconds=config.lease_seconds)
        if task is None:
            break
        claimed_count += 1
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=_lease_heartbeat,
            args=(queue, task, config, heartbeat_stop),
            name=f"fufu-lease-{task.id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            try:
                handler = handlers[task.task_type]
                result = handler(task.payload)
            except Exception as error:
                try:
                    queue.retry(
                        task.id,
                        config.worker_id,
                        _error_text(error),
                        lease_token=_lease_token(task),
                    )
                except TaskLeaseError:
                    # A completed/failed handler may outlive its lease. Reclaiming
                    # leaves at-least-once delivery explicit instead of masking the
                    # loss with a second lease exception.
                    queue.reclaim_expired_leases()
                failed_count += 1
                continue
            try:
                queue.acknowledge(
                    task.id,
                    config.worker_id,
                    result,
                    lease_token=_lease_token(task),
                )
                succeeded_count += 1
            except TaskLeaseError:
                queue.reclaim_expired_leases()
                failed_count += 1
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=max(1.0, config.lease_seconds))

    return RunReport(
        claimed=claimed_count,
        succeeded=succeeded_count,
        retried_or_failed=failed_count,
        idle=claimed_count < config.max_tasks,
    )


def _lease_token(task: Task) -> str:
    if task.lease_token is None:  # pragma: no cover - claim always sets it
        raise RuntimeError(f"claimed task {task.id!r} has no lease token")
    return task.lease_token


def main(argv: list[str] | None = None) -> int:
    """Run one explicit queue batch using the built-in collector handlers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-db", required=True, type=Path)
    parser.add_argument("--worker-id")
    parser.add_argument("--max-tasks", type=int)
    parser.add_argument("--lease-seconds", type=float)
    parser.add_argument("--disabled", action="store_true")
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="list queue tasks as JSON without claiming or executing them",
    )
    parser.add_argument(
        "--summary", action="store_true", help="print queue counts and pause state"
    )
    parser.add_argument(
        "--health", action="store_true", help="run a read-only queue integrity check"
    )
    parser.add_argument("--status", choices=("queued", "running", "succeeded", "failed"))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--reclaim-expired",
        action="store_true",
        help="explicitly reclaim expired leases and exit",
    )
    parser.add_argument("--retry-failed", metavar="TASK_ID")
    parser.add_argument(
        "--reason",
        metavar="TEXT",
        help="operator reason recorded in the durable queue audit trail",
    )
    control = parser.add_mutually_exclusive_group()
    control.add_argument("--pause", action="store_true")
    control.add_argument("--resume", action="store_true")
    control.add_argument(
        "--control-status", action="store_true", help="print persisted pause state"
    )
    args = parser.parse_args(argv)
    if any((args.pause, args.resume, args.reclaim_expired, args.retry_failed)):
        if os.getenv(_LOCAL_OPERATOR_ENV, "").strip() != "1":
            parser.error(_LOCAL_OPERATOR_REQUIRED)
    management_action_count = sum(
        bool(value)
        for value in (
            args.inspect,
            args.summary,
            args.health,
            args.reclaim_expired,
            args.retry_failed,
            args.pause,
            args.resume,
            args.control_status,
        )
    )
    if management_action_count > 1:
        parser.error("only one management action may be requested at a time")
    if args.inspect and args.reclaim_expired:
        parser.error("--inspect cannot be combined with --reclaim-expired")
    if args.retry_failed and (args.inspect or args.reclaim_expired):
        parser.error("--retry-failed cannot be combined with inspection or reclaim")
    if args.retry_failed and not args.reason:
        parser.error("--retry-failed requires --reason so the requeue is audited")
    if args.reason and not args.retry_failed:
        parser.error("--reason is only valid with --retry-failed")
    if args.status is not None and not args.inspect:
        parser.error("--status requires --inspect")
    if args.summary and (args.inspect or args.reclaim_expired or args.retry_failed):
        parser.error("--summary cannot be combined with another management action")
    if args.health and any(
        (args.inspect, args.summary, args.reclaim_expired, args.retry_failed,
         args.pause, args.resume, args.control_status)
    ):
        parser.error("--health cannot be combined with another management action")
    if args.health:
        health = TaskQueue.read_only_health(args.queue_db)
        print(json.dumps(health, ensure_ascii=False))
        return 0 if health.get("ok") else 1
    if args.inspect or args.summary or args.control_status:
        try:
            if args.inspect:
                tasks = TaskQueue.read_only_list_tasks(
                    args.queue_db, status=args.status, limit=args.limit
                )
                print(json.dumps([task.as_dict(redact=True) for task in tasks], ensure_ascii=False))
            elif args.summary:
                print(json.dumps(TaskQueue.read_only_summary(args.queue_db), ensure_ascii=False))
            else:
                print(json.dumps({"paused": TaskQueue.read_only_control_status(args.queue_db)}, ensure_ascii=False))
        except TaskValidationError as error:
            parser.error(str(error))
        return 0
    queue = TaskQueue(args.queue_db)
    if args.reclaim_expired:
        reclaimed = queue.reclaim_expired_leases()
        print(json.dumps({"reclaimed": reclaimed}, ensure_ascii=False))
        return 0
    if args.retry_failed:
        task = queue.requeue_failed(args.retry_failed, reason=args.reason)
        print(json.dumps(task.as_dict(redact=True), ensure_ascii=False))
        return 0
    if args.pause or args.resume:
        paused = queue.set_paused(args.pause)
        print(json.dumps({"paused": paused}, ensure_ascii=False))
        return 0
    env_config = RunnerConfig.from_env()
    config = RunnerConfig(
        worker_id=args.worker_id or env_config.worker_id,
        max_tasks=args.max_tasks if args.max_tasks is not None else env_config.max_tasks,
        lease_seconds=(
            args.lease_seconds
            if args.lease_seconds is not None
            else env_config.lease_seconds
        ),
        enabled=not args.disabled and env_config.enabled,
    )
    from .collector_tasks import collector_handlers

    report = run_once(queue, collector_handlers(), config)
    print(json.dumps(report.as_dict(), ensure_ascii=False))
    return 0


__all__ = ["RunReport", "RunnerConfig", "TaskHandler", "main", "run_once"]


if __name__ == "__main__":  # pragma: no cover - exercised through CLI smoke
    raise SystemExit(main())
