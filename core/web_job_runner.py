"""Bounded, headless runner for appliance operations stored in :mod:`web_jobs`.

Only callables registered by trusted Python code can execute. Job payloads are
data; this module never interprets them as shell commands or argv. In-flight
work is marked interrupted at startup and is never replayed automatically.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import signal
import subprocess
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from core.web_jobs import JobStore


class JobRunnerError(RuntimeError):
    """Base class for runner errors."""


class QueueFullError(JobRunnerError):
    """Raised when the bounded in-memory dispatch queue is full."""


class JobTimeoutError(JobRunnerError):
    """A handler exceeded the allowed time between cooperative checkpoints."""


class JobCancelledError(JobRunnerError):
    """Raised when a handler observes a cooperative cancellation request."""


class JobInterruptedError(JobRunnerError):
    """Raised when a supervised operation is stopped with its runner."""


@dataclass(frozen=True)
class JobEvent:
    sequence: int
    job_id: str
    type: str
    at: float
    data: Mapping[str, Any]


JobHandler = Callable[[Any, "JobContext"], Any]


def _payload_fingerprint(payload: Any) -> str:
    try:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("job payload must be JSON serializable") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class JobContext:
    """Cooperative controls passed to a trusted, in-process job handler."""

    def __init__(self, runner: "WebJobRunner", job_id: str, checkpoint_timeout: float) -> None:
        self._runner = runner
        self.job_id = job_id
        self._timeout = checkpoint_timeout
        self._last_checkpoint = time.monotonic()

    def _guard(self) -> None:
        if time.monotonic() - self._last_checkpoint > self._timeout:
            raise JobTimeoutError(
                f"no cooperative checkpoint for more than {self._timeout:g} seconds"
            )
        job = self._runner.store.get_job(self.job_id)
        if job is None:
            raise KeyError(self.job_id)
        if job["cancel_requested"] or job["state"] == "cancelling":
            raise JobCancelledError("cancellation requested")
        if self._runner._closing:
            raise JobInterruptedError("job runner is shutting down")

    def check_cancelled(self) -> None:
        """Check cancellation and checkpoint-timeout boundaries."""
        self._guard()

    def phase(self, name: str, progress: float | None = None) -> None:
        """Publish current operation phase and optional 0..100 progress."""
        self._guard()
        job = self._runner.store.update_progress(self.job_id, name, progress)
        self._runner._emit(self.job_id, "phase", {"phase": name, "progress": job["progress"]})

    def checkpoint(self, name: str, payload: Any = None, *, completed: bool = False) -> None:
        """Persist a stage checkpoint and reset the cooperative timeout clock."""
        self._guard()
        item = self._runner.store.set_checkpoint(
            self.job_id, name, payload, completed=completed
        )
        self._last_checkpoint = time.monotonic()
        self._runner._emit(
            self.job_id,
            "checkpoint",
            {"name": name, "completed": item["completed"]},
        )

    def completed(self, name: str) -> bool:
        """Return whether a stage has a durable completed checkpoint."""
        self._guard()
        return any(
            item["name"] == name and item["completed"]
            for item in self._runner.store.get_checkpoints(self.job_id)
        )

    def run_step(self, name: str, operation: Callable[[], Any]) -> Any:
        """Run one trusted callable once per durable checkpoint.

        The operation must itself be idempotent across a crash between its
        side effect and the completion checkpoint; the runner cannot provide
        exactly-once semantics for external effects.
        """
        if self.completed(name):
            self._runner._emit(self.job_id, "step_skipped", {"name": name})
            return None
        self.checkpoint(name, {"status": "started"})
        self._guard()
        result = operation()
        self.checkpoint(name, {"status": "completed"}, completed=True)
        return result

    def run_subprocess(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a trusted argv without a shell, polling cancellation and timeout.

        The child starts in a new process group. Cancellation, timeout, or
        runner shutdown terminates that group and escalates to SIGKILL after a
        short grace period, so helper grandchildren do not become orphans.
        Output is discarded here; handlers should send bounded progress/log
        messages through ``JobContext`` instead of buffering unbounded output.
        """
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv:
            raise ValueError("subprocess argv must be a non-empty sequence of tokens")
        if any(not isinstance(token, str) or not token or "\x00" in token for token in argv):
            raise ValueError("subprocess argv tokens must be non-empty strings without NUL")
        effective_timeout = self._timeout if timeout is None else timeout
        if (isinstance(effective_timeout, bool) or not isinstance(effective_timeout, (int, float))
                or not math.isfinite(effective_timeout) or effective_timeout <= 0):
            raise ValueError("subprocess timeout must be positive")
        if effective_timeout > self._timeout:
            raise ValueError("subprocess timeout cannot exceed the runner checkpoint timeout")
        tokens = list(argv)
        self._guard()
        process = subprocess.Popen(
            tokens,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + effective_timeout
        try:
            while True:
                self._guard()
                if time.monotonic() >= deadline:
                    raise JobTimeoutError(f"subprocess exceeded its {effective_timeout:g}s timeout")
                try:
                    returncode = process.wait(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    continue
            result = subprocess.CompletedProcess(tokens, returncode)
            if check and returncode:
                raise subprocess.CalledProcessError(returncode, tokens)
            self._terminate_process_group(process)
            return result
        except BaseException:
            self._terminate_process_group(process)
            raise

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes], *, grace_seconds: float = 1.0) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
        else:
            time.sleep(grace_seconds)
        # The group leader may have exited while a helper child remains alive.
        # Kill the original process group after the grace period either way.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait()

    def log(self, message: str, *, level: str = "info") -> None:
        self._guard()
        self._runner.store.append_log(self.job_id, message, level=level)
        self._runner._emit(self.job_id, "log", {"level": level, "message": message})


class WebJobRunner:
    """Run registered internal operations with bounded parallelism.

    ``start()`` marks any previous running jobs interrupted, then schedules
    only durable queued jobs. Interrupted and failed jobs require an explicit
    ``retry`` call. A running QEMU process is outside this runner's lifetime.
    """

    _REQUEST_CHECKPOINT = "__runner_request_v1"

    def __init__(
        self,
        store: JobStore,
        *,
        workers: int = 2,
        queue_size: int = 32,
        checkpoint_timeout: float = 120.0,
        event_buffer_size: int = 512,
        reconcile_kinds: set[str] | None = None,
    ) -> None:
        if workers < 1 or workers > 32:
            raise ValueError("workers must be between 1 and 32")
        if queue_size < 1 or queue_size > 10_000:
            raise ValueError("queue_size must be between 1 and 10000")
        if checkpoint_timeout <= 0:
            raise ValueError("checkpoint_timeout must be positive")
        if event_buffer_size < 1:
            raise ValueError("event_buffer_size must be positive")
        self.store = store
        self.workers = workers
        self.checkpoint_timeout = checkpoint_timeout
        self.reconcile_kinds = set(reconcile_kinds) if reconcile_kinds is not None else None
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=queue_size)
        self._handlers: dict[str, JobHandler] = {}
        self._lock = threading.RLock()
        self._active: set[str] = set()
        self._started = False
        self._closing = False
        self._threads: list[threading.Thread] = []
        self._events: queue.Queue[JobEvent] = queue.Queue(maxsize=event_buffer_size)
        self._event_sequence = 0
        self._event_lock = threading.Lock()
        self._subscribers: list[queue.Queue[JobEvent]] = []

    def register(self, kind: str, handler: JobHandler) -> None:
        """Register a trusted in-process callable for a job kind."""
        if not isinstance(kind, str) or not kind or len(kind) > 100:
            raise ValueError("kind must contain 1 to 100 characters")
        if not callable(handler):
            raise TypeError("handler must be callable")
        with self._lock:
            if self._started:
                raise RuntimeError("handlers cannot be changed after runner start")
            if kind in self._handlers:
                raise ValueError(f"handler already registered for {kind}")
            self._handlers[kind] = handler

    def start(self) -> None:
        """Start workers and reconcile stale in-flight jobs exactly once."""
        with self._lock:
            if self._started:
                return
            if self._closing:
                raise RuntimeError("runner is closed")
            interrupted = self.store.reconcile_after_restart(self.reconcile_kinds)
            for job_id in interrupted:
                self._emit(job_id, "interrupted", {"reason": "runner_restart"})
            self._started = True
            self._threads = [
                threading.Thread(target=self._worker, name=f"web-job-{i + 1}", daemon=True)
                for i in range(self.workers)
            ]
            for thread in self._threads:
                thread.start()
            # A queued job has no side effect yet. Its payload is persisted in
            # the private request checkpoint so dispatch survives process exit.
            self._fill_queue()

    def submit(
        self,
        *,
        idempotency_key: str,
        kind: str,
        payload: Any,
        target_vm_id: str | None = None,
        vm_revision: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Persist and dispatch a job; repeated keys never dispatch twice."""
        with self._lock:
            self._ensure_started()
            if kind not in self._handlers:
                raise ValueError(f"no internal handler registered for {kind}")
        fingerprint = _payload_fingerprint(payload)
        job, created = self.store.create_job(
            idempotency_key=idempotency_key,
            kind=kind,
            target_vm_id=target_vm_id,
            vm_revision=vm_revision,
        )
        request = self._request(job["id"])
        if created:
            self.store.set_checkpoint(job["id"], self._REQUEST_CHECKPOINT, {
                "payload": payload,
                "fingerprint": fingerprint,
            })
            self.store.append_log(job["id"], "Job accepted")
            self._emit(job["id"], "queued", {"kind": kind})
        elif request is not None and request.get("fingerprint") != fingerprint:
            raise ValueError("idempotency key was already used with a different payload")
        elif request is None:
            raise ValueError("existing job has no dispatch payload and cannot be replayed")
        if job["state"] == "queued":
            self._enqueue(job["id"])
        elif created:
            raise RuntimeError("new job was not queued")
        return self.store.get_job(job["id"]) or job, created

    def retry(self, job_id: str) -> dict[str, Any]:
        """Explicitly requeue a failed/interrupted operation using checkpoints."""
        with self._lock:
            self._ensure_started()
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["kind"] not in self._handlers or self._request(job_id) is None:
            raise ValueError("job has no registered handler or durable request payload")
        if job["state"] not in {"failed", "interrupted"}:
            raise ValueError(f"cannot retry a {job['state']} job")
        updated = self.store.transition(job_id, "queued", expected_state=job["state"], phase="queued")
        self.store.append_log(job_id, "Explicit retry accepted", level="warning")
        self._emit(job_id, "queued", {"retry": True})
        self._enqueue(job_id)
        return updated

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.request_cancel(job_id)
        self._emit(job_id, "cancellation_requested", {"state": job["state"]})
        return job

    def subscribe(self, *, maxsize: int = 128) -> queue.Queue[JobEvent]:
        """Subscribe to live in-process events; slow consumers drop oldest events."""
        if maxsize < 1 or maxsize > 10_000:
            raise ValueError("subscriber maxsize must be between 1 and 10000")
        result: queue.Queue[JobEvent] = queue.Queue(maxsize=maxsize)
        with self._event_lock:
            self._subscribers.append(result)
        return result

    def unsubscribe(self, subscriber: queue.Queue[JobEvent]) -> None:
        with self._event_lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def close(self, *, wait: bool = True) -> None:
        """Stop dispatch threads; active callables finish cooperatively."""
        with self._lock:
            if self._closing:
                return
            self._closing = True
            threads = list(self._threads)
        for _ in threads:
            try:
                self._queue.put(None, timeout=1 if wait else 0)
            except queue.Full:
                if wait:
                    self._queue.put(None)
                else:
                    break
        if wait:
            for thread in threads:
                thread.join()

    def _ensure_started(self) -> None:
        if not self._started or self._closing:
            raise RuntimeError("runner must be started and open")

    def _request(self, job_id: str) -> dict[str, Any] | None:
        for checkpoint in self.store.get_checkpoints(job_id):
            if checkpoint["name"] == self._REQUEST_CHECKPOINT:
                return checkpoint["payload"]
        return None

    def _enqueue(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._active:
                return
            try:
                self._queue.put_nowait(job_id)
            except queue.Full as exc:
                raise QueueFullError("job queue is full; job remains queued") from exc
            self._active.add(job_id)
        self._emit(job_id, "dispatched", {})

    def _worker(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                self._queue.task_done()
                return
            try:
                self._execute(job_id)
            finally:
                with self._lock:
                    self._active.discard(job_id)
                self._queue.task_done()
                self._fill_queue()

    def _fill_queue(self) -> None:
        """Dispatch durable queued requests as bounded slots become available."""
        for job in reversed(self.store.list_jobs(states={"queued"}, limit=500)):
            if job["kind"] not in self._handlers or self._request(job["id"]) is None:
                continue
            with self._lock:
                if len(self._active) >= self._queue.maxsize:
                    return
            try:
                self._enqueue(job["id"])
            except QueueFullError:
                return

    def _execute(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if job is None or job["state"] != "queued":
            return
        request = self._request(job_id)
        handler = self._handlers.get(job["kind"])
        if request is None or handler is None:
            return
        try:
            self.store.transition(job_id, "running", expected_state="queued", phase="starting")
        except RuntimeError:
            return
        context = JobContext(self, job_id, self.checkpoint_timeout)
        self._emit(job_id, "running", {"kind": job["kind"]})
        try:
            result = handler(request["payload"], context)
            current = self.store.get_job(job_id)
            if current and (current["cancel_requested"] or current["state"] == "cancelling"):
                updated = self.store.transition(job_id, "cancelled", expected_state="cancelling", phase="cancelled")
                self._emit(job_id, "cancelled", {"phase": updated["phase"]})
            else:
                updated = self.store.transition(job_id, "succeeded", expected_state="running", phase="completed", progress=100, result=result)
                self._emit(job_id, "succeeded", {"result": result})
        except JobCancelledError:
            current = self.store.get_job(job_id)
            if current and current["state"] == "cancelling":
                self.store.transition(job_id, "cancelled", expected_state="cancelling", phase="cancelled")
            self.store.append_log(job_id, "Job cancelled cooperatively", level="warning")
            self._emit(job_id, "cancelled", {})
        except JobInterruptedError:
            current = self.store.get_job(job_id)
            if current and current["state"] in {"running", "cancelling"}:
                self.store.transition(
                    job_id, "interrupted", expected_state=current["state"],
                    phase="interrupted", error_code="runner_shutdown",
                    error_message="Runner shutdown interrupted the supervised job",
                )
            self._emit(job_id, "interrupted", {"reason": "runner_shutdown"})
        except Exception as exc:
            current = self.store.get_job(job_id)
            if current and current["state"] in {"running", "cancelling"}:
                state = "cancelled" if current["state"] == "cancelling" else "failed"
                code = "checkpoint_timeout" if isinstance(exc, JobTimeoutError) else "handler_error"
                self.store.transition(
                    job_id,
                    state,
                    expected_state=current["state"],
                    phase=state,
                    error_code=code,
                    error_message=str(exc)[:500],
                )
            self.store.append_log(job_id, f"{type(exc).__name__}: {exc}", level="error")
            self._emit(job_id, state if current and current["state"] in {"running", "cancelling"} else "failed", {
                "code": "checkpoint_timeout" if isinstance(exc, JobTimeoutError) else "handler_error"
            })

    def _emit(self, job_id: str, event_type: str, data: Mapping[str, Any]) -> None:
        with self._event_lock:
            self._event_sequence += 1
            event = JobEvent(self._event_sequence, job_id, event_type, time.time(), dict(data))
            targets = [self._events, *self._subscribers]
            for target in targets:
                if target.full():
                    try:
                        target.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    target.put_nowait(event)
                except queue.Full:
                    pass


__all__ = [
    "JobCancelledError",
    "JobContext",
    "JobEvent",
    "JobRunnerError",
    "JobTimeoutError",
    "QueueFullError",
    "WebJobRunner",
]
