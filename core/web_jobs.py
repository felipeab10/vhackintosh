"""Durable operational job metadata for the appliance web manager.

This module owns persistence only. It deliberately does not execute commands,
launch subprocesses, or make privilege decisions; a supervisor consumes these
records and performs typed operations elsewhere.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal


JobState = Literal[
    "queued", "running", "succeeded", "failed", "cancelling",
    "cancelled", "interrupted",
]

_STATES = frozenset({
    "queued", "running", "succeeded", "failed", "cancelling",
    "cancelled", "interrupted",
})
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
_TRANSITIONS = {
    "queued": {"running", "cancelled", "failed", "interrupted"},
    "running": {"succeeded", "failed", "cancelling", "interrupted"},
    "cancelling": {"cancelled", "failed", "interrupted"},
    "interrupted": {"queued", "running", "cancelled", "failed"},
    "failed": {"queued"},
    "cancelled": set(),
    "succeeded": set(),
}
_SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


class JobStore:
    """SQLite-backed job state with bounded per-job logs.

    A new connection is used per operation, so instances are safe to share
    between worker threads and processes. SQLite serializes writers; the
    unique idempotency key and compare-state updates protect concurrent calls.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        log_limit_bytes: int = 256 * 1024,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if log_limit_bytes < 256:
            raise ValueError("log_limit_bytes must be at least 256")
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self.db_path = Path(db_path)
        self.log_limit_bytes = log_limit_bytes
        self.busy_timeout_ms = busy_timeout_ms
        self._init_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._init_lock:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if version > _SCHEMA_VERSION:
                    raise RuntimeError(
                        f"job database schema {version} is newer than supported {_SCHEMA_VERSION}"
                    )
                if version == 0:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        # Recheck while holding the database write lock: another
                        # process may have completed first-boot migration.
                        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                        if version == 0:
                            conn.execute("""CREATE TABLE jobs (
                            id TEXT PRIMARY KEY,
                            idempotency_key TEXT NOT NULL UNIQUE,
                            kind TEXT NOT NULL,
                            target_vm_id TEXT,
                            vm_revision TEXT,
                            state TEXT NOT NULL CHECK (state IN (
                                'queued','running','succeeded','failed',
                                'cancelling','cancelled','interrupted'
                            )),
                            phase TEXT NOT NULL DEFAULT 'queued',
                            progress REAL CHECK (progress IS NULL OR (progress >= 0 AND progress <= 100)),
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            started_at TEXT,
                            finished_at TEXT,
                            cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
                            result_json TEXT,
                            error_code TEXT,
                            error_message TEXT
                        )""")
                            conn.execute("CREATE INDEX jobs_state_updated ON jobs(state, updated_at DESC)")
                            conn.execute("CREATE INDEX jobs_vm_created ON jobs(target_vm_id, created_at DESC)")
                            conn.execute("""CREATE TABLE job_checkpoints (
                            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                            name TEXT NOT NULL,
                            payload_json TEXT NOT NULL,
                            completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0,1)),
                            updated_at TEXT NOT NULL,
                            PRIMARY KEY(job_id, name)
                        )""")
                            conn.execute("""CREATE TABLE job_logs (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                            created_at TEXT NOT NULL,
                            level TEXT NOT NULL CHECK (level IN ('debug','info','warning','error')),
                            message TEXT NOT NULL,
                            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0)
                        )""")
                            conn.execute("CREATE INDEX job_logs_order ON job_logs(job_id, id)")
                            conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                        conn.execute("COMMIT")
                    except BaseException:
                        if conn.in_transaction:
                            conn.execute("ROLLBACK")
                        raise
            finally:
                conn.close()

    @staticmethod
    def _decode_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["cancel_requested"] = bool(item["cancel_requested"])
        item["result"] = json.loads(item.pop("result_json")) if item["result_json"] else None
        return item

    def create_job(
        self,
        *,
        idempotency_key: str,
        kind: str,
        target_vm_id: str | None = None,
        vm_revision: str | None = None,
        job_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create once per idempotency key, returning (job, created)."""
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("idempotency_key must contain 1 to 200 characters")
        if not kind or len(kind) > 100:
            raise ValueError("kind must contain 1 to 100 characters")
        now = _now()
        identifier = job_id or str(uuid.uuid4())
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO jobs (id,idempotency_key,kind,target_vm_id,vm_revision,
                   state,phase,created_at,updated_at) VALUES (?,?,?,?,?,'queued','queued',?,?)
                   ON CONFLICT(idempotency_key) DO NOTHING""",
                (identifier, idempotency_key, kind, target_vm_id, vm_revision, now, now),
            )
            created = conn.execute("SELECT changes()").fetchone()[0] == 1
            row = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if row is not None and not created and (
                row["kind"] != kind
                or row["target_vm_id"] != target_vm_id
                or row["vm_revision"] != vm_revision
            ):
                raise ValueError("idempotency_key was already used for a different operation")
            return self._decode_job(row) or {}, created

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._reader() as conn:
            return self._decode_job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def get_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        with self._reader() as conn:
            return self._decode_job(
                conn.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
            )

    def list_jobs(
        self, *, states: set[str] | None = None, limit: int = 100, offset: int = 0,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if not 0 <= offset <= 100_000:
            raise ValueError("offset must be between 0 and 100000")
        if states is not None:
            if not states or not states <= _STATES:
                raise ValueError("states must contain valid job states")
            marks = ",".join("?" for _ in states)
            sql = f"SELECT * FROM jobs WHERE state IN ({marks}) ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?"
            params: tuple[Any, ...] = (*sorted(states), limit, offset)
        else:
            sql = "SELECT * FROM jobs ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?"
            params = (limit, offset)
        with self._reader() as conn:
            return [self._decode_job(row) or {} for row in conn.execute(sql, params)]

    def count_jobs(self, *, states: set[str] | None = None) -> int:
        if states is not None:
            if not states or not states <= _STATES:
                raise ValueError("states must contain valid job states")
            marks = ",".join("?" for _ in states)
            sql = f"SELECT COUNT(*) FROM jobs WHERE state IN ({marks})"
            params: tuple[Any, ...] = tuple(sorted(states))
        else:
            sql = "SELECT COUNT(*) FROM jobs"
            params = ()
        with self._reader() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def transition(
        self,
        job_id: str,
        new_state: JobState,
        *,
        expected_state: JobState | None = None,
        phase: str | None = None,
        progress: float | None = None,
        result: Any = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        if new_state not in _STATES:
            raise ValueError("invalid job state")
        if progress is not None and not 0 <= progress <= 100:
            raise ValueError("progress must be between 0 and 100")
        now = _now()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = row["state"]
            if expected_state is not None and current != expected_state:
                raise RuntimeError(f"job state changed: expected {expected_state}, found {current}")
            if new_state != current and new_state not in _TRANSITIONS[current]:
                raise ValueError(f"invalid job transition: {current} -> {new_state}")
            started = row["started_at"] or (now if new_state == "running" else None)
            finished = now if new_state in _TERMINAL else None
            conn.execute(
                """UPDATE jobs SET state=?, phase=COALESCE(?,phase),
                   progress=COALESCE(?,progress), updated_at=?, started_at=?, finished_at=?,
                   result_json=CASE WHEN ? THEN ? ELSE result_json END,
                   error_code=CASE WHEN ? THEN ? ELSE error_code END,
                   error_message=CASE WHEN ? THEN ? ELSE error_message END,
                   cancel_requested=CASE WHEN ?='cancelled' THEN 0 ELSE cancel_requested END
                   WHERE id=?""",
                (
                    new_state, phase, progress, now, started, finished,
                    result is not None, _json(result) if result is not None else None,
                    error_code is not None, error_code,
                    error_message is not None, error_message,
                    new_state, job_id,
                ),
            )
            return self._decode_job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()) or {}

    def update_progress(self, job_id: str, phase: str, progress: float | None = None) -> dict[str, Any]:
        if progress is not None and not 0 <= progress <= 100:
            raise ValueError("progress must be between 0 and 100")
        now = _now()
        with self._transaction() as conn:
            row = conn.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["state"] != "running":
                raise ValueError("progress can only be updated for running jobs")
            conn.execute(
                "UPDATE jobs SET phase=?,progress=?,updated_at=? WHERE id=?",
                (phase, progress, now, job_id),
            )
            return self._decode_job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()) or {}

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        """Record intent only; the supervisor decides if the current phase is safe."""
        now = _now()
        with self._transaction() as conn:
            row = conn.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            state = row["state"]
            if state == "queued":
                conn.execute(
                    """UPDATE jobs SET state='cancelled',phase='cancelled',cancel_requested=0,
                       updated_at=?,finished_at=? WHERE id=?""",
                    (now, now, job_id),
                )
            elif state == "running":
                conn.execute(
                    "UPDATE jobs SET state='cancelling',cancel_requested=1,updated_at=? WHERE id=?",
                    (now, job_id),
                )
            elif state == "cancelling":
                pass
            else:
                raise ValueError(f"cannot cancel a {state} job")
            return self._decode_job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()) or {}

    def set_checkpoint(
        self, job_id: str, name: str, payload: Any, *, completed: bool = False
    ) -> dict[str, Any]:
        if not name or len(name) > 200:
            raise ValueError("checkpoint name must contain 1 to 200 characters")
        now = _now()
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is None:
                raise KeyError(job_id)
            conn.execute(
                """INSERT INTO job_checkpoints(job_id,name,payload_json,completed,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(job_id,name) DO UPDATE SET
                   payload_json=excluded.payload_json,completed=excluded.completed,
                   updated_at=excluded.updated_at""",
                (job_id, name, _json(payload), int(completed), now),
            )
            return self._decode_checkpoint(
                conn.execute("SELECT * FROM job_checkpoints WHERE job_id=? AND name=?", (job_id, name)).fetchone()
            ) or {}

    @staticmethod
    def _decode_checkpoint(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["completed"] = bool(item["completed"])
        return item

    def get_checkpoints(self, job_id: str) -> list[dict[str, Any]]:
        with self._reader() as conn:
            return [
                self._decode_checkpoint(row) or {}
                for row in conn.execute(
                    "SELECT * FROM job_checkpoints WHERE job_id=? ORDER BY updated_at,name", (job_id,)
                )
            ]

    def append_log(self, job_id: str, message: str, *, level: str = "info") -> None:
        if level not in {"debug", "info", "warning", "error"}:
            raise ValueError("invalid log level")
        if not isinstance(message, str):
            raise TypeError("message must be a string")
        max_entry = max(128, self.log_limit_bytes // 2)
        encoded = message.encode("utf-8")
        if len(encoded) > max_entry:
            encoded = encoded[-max_entry:]
            message = "[earlier log content truncated] " + encoded.decode("utf-8", errors="replace")
        size = len(message.encode("utf-8"))
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is None:
                raise KeyError(job_id)
            conn.execute(
                "INSERT INTO job_logs(job_id,created_at,level,message,size_bytes) VALUES(?,?,?,?,?)",
                (job_id, _now(), level, message, size),
            )
            total = conn.execute("SELECT COALESCE(SUM(size_bytes),0) FROM job_logs WHERE job_id=?", (job_id,)).fetchone()[0]
            while total > self.log_limit_bytes:
                oldest = conn.execute(
                    "SELECT id,size_bytes FROM job_logs WHERE job_id=? ORDER BY id LIMIT 1", (job_id,)
                ).fetchone()
                if oldest is None:
                    break
                conn.execute("DELETE FROM job_logs WHERE id=?", (oldest["id"],))
                total -= oldest["size_bytes"]

    def get_logs(self, job_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        if not 1 <= limit <= 2_000:
            raise ValueError("limit must be between 1 and 2000")
        with self._reader() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT id,created_at,level,message FROM job_logs WHERE job_id=? ORDER BY id DESC LIMIT ?",
                (job_id, limit),
            )][::-1]

    def reconcile_after_restart(self, kinds: set[str] | None = None) -> list[str]:
        """Mark in-flight work interrupted; never retry side effects implicitly."""
        now = _now()
        with self._transaction() as conn:
            if kinds is None:
                rows = conn.execute(
                    "SELECT id FROM jobs WHERE state IN ('running','cancelling') ORDER BY created_at"
                ).fetchall()
            elif not kinds:
                return []
            else:
                placeholders = ",".join("?" for _ in kinds)
                rows = conn.execute(
                    f"SELECT id FROM jobs WHERE state IN ('running','cancelling') AND kind IN ({placeholders}) ORDER BY created_at",
                    tuple(sorted(kinds)),
                ).fetchall()
            ids = [row["id"] for row in rows]
            conn.executemany(
                "UPDATE jobs SET state='interrupted',phase='interrupted',updated_at=?,finished_at=? WHERE id=?",
                [(now, now, job_id) for job_id in ids],
            )
            return ids


__all__ = ["JobState", "JobStore"]
