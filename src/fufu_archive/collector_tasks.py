"""Queue handlers for explicitly requested Hermes collector runs.

The handler only adapts a queue payload to the repository collector. It does
not discover Hermes paths, create tasks, schedule itself, or write the formal
knowledge database.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .session_collector import (
    _incremental_report_paths,
    _reject_incremental_collisions,
    _validate_profile,
    incremental_run,
)
from .task_queue import Task, TaskQueue
from .task_runner import TaskHandler

COLLECT_INCREMENTAL_TASK = "collect.hermes.incremental"
_REQUIRED_PATHS = (
    "db_path",
    "checkpoint_path",
    "source_archive_path",
    "candidate_payload_dir",
    "report_output",
)


def _collector_kwargs(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("collector payload must be an object")
    missing = [name for name in _REQUIRED_PATHS if not payload.get(name)]
    if missing:
        raise ValueError(f"collector payload missing required paths: {', '.join(missing)}")

    kwargs: dict[str, Any] = {
        name: Path(payload[name]) for name in _REQUIRED_PATHS
    }
    if "profile" in payload:
        kwargs["profile"] = _validate_profile(payload["profile"])
    for name in ("min_effective_messages", "min_score"):
        if name in payload:
            value = payload[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
            kwargs[name] = value
    _reject_incremental_collisions(
        kwargs["db_path"],
        kwargs["checkpoint_path"],
        kwargs["source_archive_path"],
        kwargs["candidate_payload_dir"],
        _incremental_report_paths(kwargs["report_output"]),
    )
    return kwargs


def handle_incremental_collection(payload: Any) -> dict[str, Any]:
    """Run one collector batch from a fully explicit JSON-compatible payload."""
    report = incremental_run(**_collector_kwargs(payload))
    return {
        "run_mode": report.get("run_mode"),
        "captured_count": report.get("captured_count", 0),
        "checkpoint_advanced": report.get("checkpoint_advanced", False),
        "active_sessions": report.get("active_sessions"),
    }


def enqueue_incremental_collection(
    queue: TaskQueue, idempotency_key: str, payload: Any
) -> Task:
    """Create one queued collector task without executing or scheduling it."""
    _collector_kwargs(payload)
    return queue.enqueue(COLLECT_INCREMENTAL_TASK, idempotency_key, payload)


def collector_handlers() -> dict[str, TaskHandler]:
    """Return the explicitly supported collector task handler mapping."""
    return {COLLECT_INCREMENTAL_TASK: handle_incremental_collection}


__all__ = [
    "COLLECT_INCREMENTAL_TASK",
    "collector_handlers",
    "enqueue_incremental_collection",
    "handle_incremental_collection",
]
