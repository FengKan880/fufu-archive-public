"""A small persistent SQLite task queue core.

This module deliberately has no default database path and no worker loop.  A
caller must supply a queue database path explicitly; the queue never opens the
Fufu Archive knowledge database.  It is intended to provide durable task
state for a future integration, not to enable scheduling by itself.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TASK_STATUSES = frozenset({"queued", "running", "succeeded", "failed"})
_MAX_TASK_TYPE_LENGTH = 128
_MAX_IDEMPOTENCY_KEY_LENGTH = 512
_MAX_WORKER_ID_LENGTH = 128
_MAX_ERROR_LENGTH = 4_000
_MAX_REASON_LENGTH = 500
# Tables that identify the formal Fufu Archive knowledge database. The queue
# must never be initialized inside it: a shared file would let queue writes and
# archive writes contend for one SQLite writer lock, and a mistyped --queue-db
# would silently create queue tables inside the knowledge store.
_ARCHIVE_MARKER_TABLES = frozenset(
    {"documents", "chunks", "chunks_fts", "candidates", "metadata"}
)
DEFAULT_MAX_MANUAL_RETRIES = 3
_QUEUE_REQUIRED_COLUMNS = {
    "tasks": {
        "id", "task_type", "idempotency_key", "payload_json", "status", "attempts",
        "available_at", "lease_owner", "lease_token", "lease_expires_at", "created_at",
        "updated_at", "last_error", "result_metadata_json", "manual_retries",
    },
    "queue_control": {"id", "paused"},
    "queue_settings": {
        "id", "max_attempts", "base_backoff", "max_backoff", "max_manual_retries",
    },
    "task_events": {"id", "task_id", "event", "reason", "manual_retries", "created_at"},
}


class TaskQueueError(Exception):
    """Base class for queue errors."""


class TaskValidationError(ValueError, TaskQueueError):
    """Raised when a task or queue argument is malformed."""


class TaskNotFound(KeyError, TaskQueueError):
    """Raised when a task id does not exist."""


class TaskLeaseError(TaskQueueError):
    """Raised when a worker does not hold a live lease for a task."""


@dataclass(frozen=True)
class Task:
    """A durable task snapshot returned by :class:`TaskQueue`."""

    id: str
    task_type: str
    idempotency_key: str
    payload: Any
    status: str
    attempts: int
    available_at: float
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: float | None
    created_at: float
    updated_at: float
    last_error: str | None
    result_metadata: Any | None
    manual_retries: int = 0

    def as_dict(self, *, redact: bool = False) -> dict[str, Any]:
        """Return the snapshot as a JSON-serializable dictionary.

        ``redact=True`` is the inspection/CLI projection: payloads, lease
        tokens and result metadata may carry credentials or private paths and
        must not be printed by read-only inspection surfaces. Operational
        callers that need the real values keep the default projection.
        """
        if redact:
            return {
                "id": self.id,
                "task_type": self.task_type,
                "idempotency_key": self.idempotency_key,
                "payload": "[REDACTED]",
                "status": self.status,
                "attempts": self.attempts,
                "manual_retries": self.manual_retries,
                "available_at": self.available_at,
                "lease_owner": self.lease_owner,
                "lease_token": (
                    None if self.lease_token is None else "[REDACTED]"
                ),
                "lease_expires_at": self.lease_expires_at,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "last_error": self.last_error,
                "result_metadata": (
                    None if self.result_metadata is None else "[REDACTED]"
                ),
            }
        return {
            "id": self.id,
            "task_type": self.task_type,
            "idempotency_key": self.idempotency_key,
            "payload": self.payload,
            "status": self.status,
            "attempts": self.attempts,
            "available_at": self.available_at,
            "lease_owner": self.lease_owner,
            "lease_token": self.lease_token,
            "lease_expires_at": self.lease_expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
            "result_metadata": self.result_metadata,
            "manual_retries": self.manual_retries,
        }


Clock = Callable[[], float | datetime]


def _validate_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise TaskValidationError(f"{name} must be a non-empty string")
    if len(value) > maximum:
        raise TaskValidationError(f"{name} must be at most {maximum} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise TaskValidationError(f"{name} must contain valid Unicode") from error
    return value


def _validate_json_value(value: object, name: str) -> str:
    """Validate strict JSON-compatible values and return canonical JSON text."""
    def visit(item: object, path: str) -> None:
        if item is None or isinstance(item, (bool, int)):
            return
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError as error:
                raise TaskValidationError(
                    f"{name} contains invalid Unicode at {path}"
                ) from error
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise TaskValidationError(f"{name} contains a non-finite number at {path}")
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise TaskValidationError(
                        f"{name} object keys must be strings at {path}"
                    )
                visit(child, f"{path}.{key}")
            return
        if isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")
            return
        raise TaskValidationError(f"{name} contains a non-JSON value at {path}")

    try:
        visit(value, "$")
    except RecursionError as error:
        raise TaskValidationError(f"{name} is too deeply nested or cyclic") from error
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise TaskValidationError(f"{name} must be valid JSON") from error


def _validate_positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TaskValidationError(f"{name} must be a positive finite number")
    try:
        result = float(value)
    except OverflowError as error:
        # An integer too large for a float is a malformed option, not an
        # unexpected crash inside the calling code.
        raise TaskValidationError(f"{name} must be a positive finite number") from error
    if not math.isfinite(result) or result <= 0:
        raise TaskValidationError(f"{name} must be a positive finite number")
    return result


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        task_type=row["task_type"],
        idempotency_key=row["idempotency_key"],
        payload=json.loads(row["payload_json"]),
        status=row["status"],
        attempts=row["attempts"],
        available_at=row["available_at"],
        lease_owner=row["lease_owner"],
        lease_token=row["lease_token"],
        lease_expires_at=row["lease_expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_error=row["last_error"],
        result_metadata=(
            None
            if row["result_metadata_json"] is None
            else json.loads(row["result_metadata_json"])
        ),
        manual_retries=row["manual_retries"],
    )


class TaskQueue:
    """Durable task state backed by an explicitly supplied SQLite path.

    Each public write uses a short ``BEGIN IMMEDIATE`` transaction.  SQLite's
    writer serialization makes the claim update atomic across processes, so a
    task cannot be claimed twice while its lease is live.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Clock | None = None,
        max_attempts: int | None = None,
        base_backoff: float | None = None,
        max_backoff: float | None = None,
        max_manual_retries: int | None = None,
    ) -> None:
        if not isinstance(db_path, (str, Path)) or not str(db_path).strip():
            raise TaskValidationError("db_path must be an explicitly supplied path")
        if max_attempts is not None and (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise TaskValidationError("max_attempts must be a positive integer")
        self.db_path = Path(db_path)
        self._requested_max_attempts = max_attempts
        self._requested_base_backoff = base_backoff
        self._requested_max_backoff = max_backoff
        if max_manual_retries is not None and (
            isinstance(max_manual_retries, bool)
            or not isinstance(max_manual_retries, int)
            or max_manual_retries < 0
        ):
            raise TaskValidationError(
                "max_manual_retries must be a non-negative integer"
            )
        self._requested_max_manual_retries = max_manual_retries
        self.max_manual_retries = (
            DEFAULT_MAX_MANUAL_RETRIES if max_manual_retries is None else max_manual_retries
        )
        self.max_attempts = 3 if max_attempts is None else max_attempts
        self.base_backoff = (
            1.0
            if base_backoff is None
            else _validate_positive_number(base_backoff, "base_backoff")
        )
        self.max_backoff = (
            300.0
            if max_backoff is None
            else _validate_positive_number(max_backoff, "max_backoff")
        )
        self._clock = time.time if clock is None else clock
        if not callable(self._clock):
            raise TaskValidationError("clock must be callable")
        self._initialize()

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            result = value.timestamp()
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            result = float(value)
        else:
            raise TaskValidationError("clock must return a timestamp or datetime")
        if not math.isfinite(result):
            raise TaskValidationError("clock must return a finite time")
        return result

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.db_path), timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _reject_archive_database(self) -> None:
        """Refuse a queue database path that already holds archive tables.

        The probe opens the file read-only so a rejected path is not even
        switched into WAL mode as a side effect of the check.
        """
        path = self.db_path
        if not path.is_file():
            return
        try:
            connection = sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro", uri=True
            )
        except sqlite3.Error as error:  # pragma: no cover - unreadable file
            raise TaskValidationError(
                f"queue database is not readable: {path}"
            ) from error
        try:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
        except sqlite3.Error as error:
            raise TaskValidationError(
                f"queue database is not readable: {path}"
            ) from error
        finally:
            connection.close()
        overlap = sorted(names & _ARCHIVE_MARKER_TABLES)
        if overlap:
            raise TaskValidationError(
                "refusing to use the formal archive database as a task queue: "
                f"{path} already contains {overlap}. The queue database must be "
                "a separate file."
            )

    def _initialize(self) -> None:
        self._reject_archive_database()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'running', 'succeeded', 'failed')
                    ),
                    attempts INTEGER NOT NULL CHECK (attempts >= 0),
                    available_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_error TEXT,
                    result_metadata_json TEXT,
                    manual_retries INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "lease_token" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN lease_token TEXT")
            if "manual_retries" not in columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN manual_retries INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    reason TEXT,
                    manual_retries INTEGER NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS task_events_task_idx
                ON task_events(task_id, id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS queue_control (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    paused INTEGER NOT NULL CHECK (paused IN (0, 1))
                )
                """
            )
            connection.execute(
                "INSERT INTO queue_control(id, paused) VALUES (1, 0) "
                "ON CONFLICT(id) DO NOTHING"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS queue_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    max_attempts INTEGER NOT NULL,
                    base_backoff REAL NOT NULL,
                    max_backoff REAL NOT NULL,
                    max_manual_retries INTEGER NOT NULL DEFAULT 3
                )
                """
            )
            connection.execute(
                """
                INSERT INTO queue_settings(
                    id, max_attempts, base_backoff, max_backoff, max_manual_retries
                ) VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    self.max_attempts,
                    self.base_backoff,
                    self.max_backoff,
                    self.max_manual_retries,
                ),
            )
            setting_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(queue_settings)").fetchall()
            }
            if "max_manual_retries" not in setting_columns:
                connection.execute(
                    "ALTER TABLE queue_settings ADD COLUMN "
                    "max_manual_retries INTEGER NOT NULL DEFAULT 3"
                )
                connection.execute(
                    "UPDATE queue_settings SET max_manual_retries = ? WHERE id = 1",
                    (self.max_manual_retries,),
                )
            settings = connection.execute(
                "SELECT max_attempts, base_backoff, max_backoff, max_manual_retries "
                "FROM queue_settings WHERE id = 1"
            ).fetchone()
            if settings is None:
                raise RuntimeError("queue settings row was not initialized")
            if (
                self._requested_max_attempts is not None
                and settings["max_attempts"] != self.max_attempts
            ) or (
                self._requested_base_backoff is not None
                and settings["base_backoff"] != self.base_backoff
            ) or (
                self._requested_max_backoff is not None
                and settings["max_backoff"] != self.max_backoff
            ):
                raise TaskValidationError(
                    "queue retry settings conflict with the persisted queue configuration"
                )
            if (
                self._requested_max_manual_retries is not None
                and settings["max_manual_retries"] != self.max_manual_retries
            ):
                raise TaskValidationError(
                    "queue retry settings conflict with the persisted queue configuration"
                )
            self.max_attempts = settings["max_attempts"]
            self.base_backoff = settings["base_backoff"]
            self.max_backoff = settings["max_backoff"]
            self.max_manual_retries = settings["max_manual_retries"]
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS tasks_runnable_idx
                ON tasks(status, available_at, created_at, id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS tasks_status_updated_idx
                ON tasks(status, updated_at, id)
                """
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _task_id(task_id: object) -> str:
        return _validate_text(task_id, "task_id", 128)

    @staticmethod
    def _worker_id(worker_id: object) -> str:
        return _validate_text(worker_id, "worker_id", _MAX_WORKER_ID_LENGTH)

    @staticmethod
    def _lease_token(lease_token: object) -> str:
        return _validate_text(lease_token, "lease_token", 128)

    @staticmethod
    def _lease_seconds(lease_seconds: object) -> float:
        return _validate_positive_number(lease_seconds, "lease_seconds")

    def enqueue(
        self,
        task_type: str,
        idempotency_key: str,
        payload: Any,
    ) -> Task:
        """Insert a task once, returning the existing task for a duplicate key."""
        task_type = _validate_text(task_type, "task_type", _MAX_TASK_TYPE_LENGTH)
        idempotency_key = _validate_text(
            idempotency_key, "idempotency_key", _MAX_IDEMPOTENCY_KEY_LENGTH
        )
        payload_json = _validate_json_value(payload, "payload")
        now = self._now()
        with self._write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO tasks(
                    id, task_type, idempotency_key, payload_json, status, attempts,
                    available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (str(uuid.uuid4()), task_type, idempotency_key, payload_json, now, now, now),
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if row is None:  # pragma: no cover - protected by the unique key
                raise RuntimeError("SQLite did not return the enqueued task")
            if row["task_type"] != task_type or row["payload_json"] != payload_json:
                raise TaskValidationError(
                    "idempotency key already exists with different task data"
                )
            return _row_to_task(row)

    def claim(self, worker_id: str, *, lease_seconds: float = 60.0) -> Task | None:
        """Atomically claim the oldest runnable task for ``worker_id``."""
        worker_id = self._worker_id(worker_id)
        lease_seconds = self._lease_seconds(lease_seconds)
        with self._write_transaction() as connection:
            now = self._now()
            control = connection.execute(
                "SELECT paused FROM queue_control WHERE id = 1"
            ).fetchone()
            if control is not None and bool(control["paused"]):
                return None
            self._reclaim_expired_locked(connection, now)
            row = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'queued' AND available_at <= ?
                ORDER BY available_at, created_at, id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            lease_expires_at = now + lease_seconds
            lease_token = str(uuid.uuid4())
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = 'running', attempts = attempts + 1,
                    lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (worker_id, lease_token, lease_expires_at, now, row["id"]),
            )
            if cursor.rowcount != 1:  # pragma: no cover - BEGIN IMMEDIATE prevents this
                return None
            claimed = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (row["id"],)
            ).fetchone()
            if claimed is None:  # pragma: no cover - row was just updated
                raise RuntimeError("SQLite did not return the claimed task")
            return _row_to_task(claimed)

    def renew_lease(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_token: str,
        lease_seconds: float,
    ) -> Task:
        """Extend a still-live lease without changing attempts or task state."""
        task_id = self._task_id(task_id)
        worker_id = self._worker_id(worker_id)
        lease_token = self._lease_token(lease_token)
        lease_seconds = self._lease_seconds(lease_seconds)
        with self._write_transaction() as connection:
            now = self._now()
            cursor = connection.execute(
                """
                UPDATE tasks
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                    AND lease_token = ? AND lease_expires_at > ?
                """,
                (now + lease_seconds, now, task_id, worker_id, lease_token, now),
            )
            self._ensure_lease_update(
                connection, cursor.rowcount, task_id, worker_id, lease_token, now
            )
            return self._fetch_task(connection, task_id)

    def acknowledge(
        self,
        task_id: str,
        worker_id: str,
        result_metadata: Any | None = None,
        *,
        lease_token: str,
    ) -> Task:
        """Mark a live worker lease successful and persist result metadata."""
        task_id = self._task_id(task_id)
        worker_id = self._worker_id(worker_id)
        lease_token = self._lease_token(lease_token)
        metadata_json = (
            None
            if result_metadata is None
            else _validate_json_value(result_metadata, "result_metadata")
        )
        with self._write_transaction() as connection:
            now = self._now()
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = 'succeeded', lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL, updated_at = ?,
                    result_metadata_json = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                    AND lease_token = ? AND lease_expires_at > ?
                """,
                (now, metadata_json, task_id, worker_id, lease_token, now),
            )
            self._ensure_lease_update(
                connection, cursor.rowcount, task_id, worker_id, lease_token, now
            )
            return self._fetch_task(connection, task_id)

    def ack_success(
        self,
        task_id: str,
        worker_id: str,
        result_metadata: Any | None = None,
        *,
        lease_token: str,
    ) -> Task:
        """Alias for :meth:`acknowledge`."""
        return self.acknowledge(
            task_id, worker_id, result_metadata, lease_token=lease_token
        )

    def retry(
        self, task_id: str, worker_id: str, error: str, *, lease_token: str
    ) -> Task:
        """Retry a task with bounded exponential backoff or fail permanently."""
        task_id = self._task_id(task_id)
        worker_id = self._worker_id(worker_id)
        lease_token = self._lease_token(lease_token)
        error = _validate_text(error, "error", _MAX_ERROR_LENGTH)
        with self._write_transaction() as connection:
            now = self._now()
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFound(task_id)
            if not self._has_live_lease(row, worker_id, lease_token, now):
                raise TaskLeaseError(f"worker {worker_id!r} does not hold task {task_id!r}")
            if row["attempts"] >= self.max_attempts:
                status = "failed"
                available_at = now
            else:
                status = "queued"
                available_at = now + self._backoff_seconds(row["attempts"])
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, available_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    updated_at = ?, last_error = ?
                WHERE id = ?
                """,
                (status, available_at, now, error, task_id),
            )
            return self._fetch_task(connection, task_id)

    def reclaim_expired_leases(self) -> int:
        """Return expired running leases to the queue, or fail exhausted tasks."""
        with self._write_transaction() as connection:
            now = self._now()
            return self._reclaim_expired_locked(connection, now)

    def set_paused(self, paused: bool) -> bool:
        """Persist whether runners must refrain from claiming new tasks."""
        if not isinstance(paused, bool):
            raise TaskValidationError("paused must be a boolean")
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE queue_control SET paused = ? WHERE id = 1",
                (int(paused),),
            )
        return paused

    def is_paused(self) -> bool:
        """Return the persisted queue pause state without changing tasks."""
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT paused FROM queue_control WHERE id = 1"
            ).fetchone()
            return bool(row["paused"]) if row is not None else False
        finally:
            connection.close()

    def requeue_failed(self, task_id: str, *, reason: str) -> Task:
        """Requeue one failed task once, bounded and durably audited.

        Automatic retries are bounded by ``max_attempts``; without a separate
        bound an operator could otherwise re-drive a permanently failing task
        forever. Every accepted requeue increments ``manual_retries`` and
        appends an immutable ``task_events`` row recording who acted and why.
        """
        task_id = self._task_id(task_id)
        reason = _validate_text(reason, "reason", _MAX_REASON_LENGTH)
        with self._write_transaction() as connection:
            now = self._now()
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFound(task_id)
            if row["status"] != "failed":
                raise TaskValidationError(
                    f"task {task_id!r} is not failed and cannot be requeued"
                )
            manual_retries = int(row["manual_retries"])
            if manual_retries >= self.max_manual_retries:
                raise TaskValidationError(
                    f"task {task_id!r} reached the manual retry limit "
                    f"({self.max_manual_retries}); create a new task instead"
                )
            manual_retries += 1
            connection.execute(
                """
                UPDATE tasks
                SET status = 'queued', available_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    updated_at = ?, last_error = NULL, manual_retries = ?
                WHERE id = ? AND status = 'failed'
                """,
                (now, now, manual_retries, task_id),
            )
            connection.execute(
                """
                INSERT INTO task_events(
                    task_id, event, reason, manual_retries, created_at
                ) VALUES (?, 'manual_requeue', ?, ?, ?)
                """,
                (task_id, reason, manual_retries, now),
            )
            return self._fetch_task(connection, task_id)

    def list_events(self, task_id: str) -> list[dict[str, Any]]:
        """Return the append-only audit trail for one task, oldest first."""
        task_id = self._task_id(task_id)
        connection = self._connect()
        try:
            return [
                {
                    "task_id": row["task_id"],
                    "event": row["event"],
                    "reason": row["reason"],
                    "manual_retries": row["manual_retries"],
                    "created_at": row["created_at"],
                }
                for row in connection.execute(
                    "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
                    (task_id,),
                )
            ]
        finally:
            connection.close()

    def get(self, task_id: str) -> Task | None:
        """Inspect one task by id without changing its state."""
        task_id = self._task_id(task_id)
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return None if row is None else _row_to_task(row)
        finally:
            connection.close()

    def list_tasks(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[Task]:
        """List durable task snapshots, optionally filtered by status."""
        if status is not None and status not in TASK_STATUSES:
            raise TaskValidationError(f"unknown task status: {status!r}")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise TaskValidationError("limit must be an integer from 1 to 10000")
        connection = self._connect()
        try:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM tasks ORDER BY created_at, id LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM tasks WHERE status = ?
                    ORDER BY created_at, id LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
            return [_row_to_task(row) for row in rows]
        finally:
            connection.close()

    def status_counts(self) -> dict[str, int]:
        """Return counts for every task status, including zero-count states."""
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            ).fetchall()
            counts = {status: 0 for status in TASK_STATUSES}
            counts.update({row["status"]: row["count"] for row in rows})
            return counts
        finally:
            connection.close()

    def summary(self) -> dict[str, Any]:
        """Return a read-only queue summary, including lease visibility.

        Inspection does not reclaim leases or update task rows. ``active`` and
        ``expired`` are a snapshot relative to the queue clock; expired tasks
        remain visible as running until an explicit reclaim or claim operation.
        """
        connection = self._connect()
        try:
            now = self._now()
            counts = {status: 0 for status in TASK_STATUSES}
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            ):
                counts[row["status"]] = row["count"]
            running = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN lease_expires_at IS NOT NULL
                              AND lease_expires_at > ? THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN lease_expires_at IS NOT NULL
                              AND lease_expires_at <= ? THEN 1 ELSE 0 END) AS expired,
                    SUM(CASE WHEN lease_expires_at IS NULL THEN 1 ELSE 0 END) AS missing
                FROM tasks WHERE status = 'running'
                """,
                (now, now),
            ).fetchone()
            owners = [
                row["lease_owner"]
                for row in connection.execute(
                    """
                    SELECT DISTINCT lease_owner FROM tasks
                    WHERE status = 'running' AND lease_owner IS NOT NULL
                    ORDER BY lease_owner
                    """
                )
            ]
            control = connection.execute(
                "SELECT paused FROM queue_control WHERE id = 1"
            ).fetchone()
            return {
                "paused": bool(control["paused"]) if control is not None else False,
                "counts": counts,
                "lease_visibility": {
                    "active": int(running["active"] or 0),
                    "expired": int(running["expired"] or 0),
                    "missing_expiry": int(running["missing"] or 0),
                    "owners": owners,
                },
            }
        finally:
            connection.close()

    @classmethod
    def _open_read_only_queue(cls, db_path: str | Path) -> sqlite3.Connection:
        """Open a healthy queue without allowing schema creation or migration."""
        health = cls.read_only_health(db_path)
        if not health.get("ok"):
            detail = health.get("error") or "queue schema is incomplete"
            raise TaskValidationError(f"read-only queue action refused: {detail}")
        path = Path(db_path).resolve().as_posix()
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    @classmethod
    def read_only_list_tasks(
        cls, db_path: str | Path, *, status: str | None = None, limit: int = 100
    ) -> list[Task]:
        """List task projections without opening a writable queue connection."""
        if status is not None and status not in TASK_STATUSES:
            raise TaskValidationError(f"unknown task status: {status!r}")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise TaskValidationError("limit must be an integer from 1 to 10000")
        connection = cls._open_read_only_queue(db_path)
        try:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM tasks ORDER BY created_at, id LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY created_at, id LIMIT ?",
                    (status, limit),
                ).fetchall()
            return [_row_to_task(row) for row in rows]
        finally:
            connection.close()

    @classmethod
    def read_only_control_status(cls, db_path: str | Path) -> bool:
        """Read persisted pause state without creating or migrating a queue."""
        connection = cls._open_read_only_queue(db_path)
        try:
            row = connection.execute("SELECT paused FROM queue_control WHERE id = 1").fetchone()
            return bool(row["paused"]) if row is not None else False
        finally:
            connection.close()

    @classmethod
    def read_only_summary(cls, db_path: str | Path) -> dict[str, Any]:
        """Return queue counts and lease visibility without any write-capable open."""
        connection = cls._open_read_only_queue(db_path)
        try:
            now = time.time()
            counts = {status: 0 for status in TASK_STATUSES}
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            ):
                counts[row["status"]] = row["count"]
            running = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN lease_expires_at IS NOT NULL AND lease_expires_at > ? THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN lease_expires_at IS NOT NULL AND lease_expires_at <= ? THEN 1 ELSE 0 END) AS expired,
                    SUM(CASE WHEN lease_expires_at IS NULL THEN 1 ELSE 0 END) AS missing
                FROM tasks WHERE status = 'running'
                """,
                (now, now),
            ).fetchone()
            owners = [
                row["lease_owner"]
                for row in connection.execute(
                    "SELECT DISTINCT lease_owner FROM tasks WHERE status = 'running' "
                    "AND lease_owner IS NOT NULL ORDER BY lease_owner"
                )
            ]
            control = connection.execute("SELECT paused FROM queue_control WHERE id = 1").fetchone()
            return {
                "paused": bool(control["paused"]) if control is not None else False,
                "counts": counts,
                "lease_visibility": {
                    "active": int(running["active"] or 0),
                    "expired": int(running["expired"] or 0),
                    "missing_expiry": int(running["missing"] or 0),
                    "owners": owners,
                },
            }
        finally:
            connection.close()

    def health(self) -> dict[str, Any]:
        """Run a strictly read-only queue health check.

        This method never creates a missing database, enables WAL, reclaims
        leases, or initializes schema, so it is safe for offline diagnostics.
        """
        return self.read_only_health(self.db_path)

    @classmethod
    def read_only_health(cls, db_path: str | Path) -> dict[str, Any]:
        """Check an explicit queue path without constructing or mutating it."""
        path = Path(db_path)
        if not path.is_file():
            return {
                "ok": False,
                "database": str(path),
                "error": "queue database does not exist",
            }
        resolved = path.resolve().as_posix()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            missing = sorted(set(_QUEUE_REQUIRED_COLUMNS) - tables)
            missing_columns = {
                table: sorted(
                    _QUEUE_REQUIRED_COLUMNS[table]
                    - {
                        row["name"]
                        for row in connection.execute(f'PRAGMA table_info("{table}")')
                    }
                )
                for table in sorted(set(_QUEUE_REQUIRED_COLUMNS) & tables)
            }
            missing_columns = {
                table: columns for table, columns in missing_columns.items() if columns
            }
            result: dict[str, Any] = {
                "ok": integrity == "ok" and not missing and not missing_columns,
                "database": str(path),
                "integrity": integrity,
                "missing_tables": missing,
                "missing_columns": missing_columns,
            }
            if result["ok"]:
                counts = {status: 0 for status in TASK_STATUSES}
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
                ):
                    counts[row["status"]] = int(row["count"])
                result["counts"] = counts
            return result
        except (OSError, sqlite3.Error) as error:
            return {
                "ok": False,
                "database": str(path),
                "error": f"{error.__class__.__name__}: queue database is not readable",
            }
        finally:
            if connection is not None:
                connection.close()

    def _fetch_task(self, connection: sqlite3.Connection, task_id: str) -> Task:
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:  # pragma: no cover - callers already hold the row's write lock
            raise TaskNotFound(task_id)
        return _row_to_task(row)

    def _ensure_lease_update(
        self,
        connection: sqlite3.Connection,
        rowcount: int,
        task_id: str,
        worker_id: str,
        lease_token: str,
        now: float,
    ) -> None:
        if rowcount == 1:
            return
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        if not self._has_live_lease(row, worker_id, lease_token, now):
            raise TaskLeaseError(f"worker {worker_id!r} does not hold task {task_id!r}")
        raise TaskLeaseError(f"task {task_id!r} is not running")

    @staticmethod
    def _has_live_lease(
        row: sqlite3.Row, worker_id: str, lease_token: str, now: float
    ) -> bool:
        return (
            row["status"] == "running"
            and row["lease_owner"] == worker_id
            and row["lease_token"] == lease_token
            and row["lease_expires_at"] is not None
            and row["lease_expires_at"] > now
        )

    def _backoff_seconds(self, attempts: int) -> float:
        """Bounded exponential backoff for a task that has ``attempts`` tries.

        Doubling is done explicitly rather than with ``2 ** n`` so that an
        absurd attempt count returns the cap instead of overflowing.
        """
        try:
            exponent = max(0, int(attempts) - 1)
        except (TypeError, ValueError):
            return float(self.max_backoff)
        delay = float(self.base_backoff)
        for _ in range(exponent):
            delay *= 2.0
            if delay >= self.max_backoff:
                return float(self.max_backoff)
        return float(min(self.max_backoff, delay))

    def _reclaim_expired_locked(self, connection: sqlite3.Connection, now: float) -> int:
        # A crashed worker must not turn into an immediate hot retry loop:
        # reclaimed work waits out the same bounded exponential backoff used by
        # an explicit retry before any worker may claim it again.
        expired = connection.execute(
            """
            SELECT id, attempts FROM tasks
            WHERE status = 'running' AND lease_expires_at IS NOT NULL
                AND lease_expires_at <= ?
            """,
            (now,),
        ).fetchall()
        reclaimed = 0
        for row in expired:
            if row["attempts"] >= self.max_attempts:
                status = "failed"
                available_at = now
            else:
                status = "queued"
                available_at = now + self._backoff_seconds(row["attempts"])
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = ?, available_at = ?,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?, last_error = 'lease expired'
                WHERE id = ? AND status = 'running'
                """,
                (status, available_at, now, row["id"]),
            )
            reclaimed += cursor.rowcount
        return reclaimed


__all__ = [
    "TASK_STATUSES",
    "Task",
    "TaskLeaseError",
    "TaskNotFound",
    "TaskQueue",
    "TaskQueueError",
    "TaskValidationError",
]
