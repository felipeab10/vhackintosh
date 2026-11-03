"""FastAPI surface for the appliance manager.

The HTTP process only validates and authorizes requests. Runtime operations are
delegated to an injected agent client; there is deliberately no shell/QEMU path
in this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Protocol
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request as UrlRequest, build_opener

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from core.web_rpc import RpcRemoteError
from core.web_activation import WebActivationStore
from core.telemetry.alerts import HostAlertStore
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError
from starlette.concurrency import run_in_threadpool

from .contracts import (
    ApiError,
    AuditListResponse,
    BootstrapRequest,
    CapabilitiesResponse,
    Health,
    HostOverview,
    GpuInventoryResponse,
    HostUsbInventory,
    StorageInventory,
    HostTelemetry,
    JobAccepted,
    LoginRequest,
    Operation,
    PasswordChangeRequest,
    SessionView,
    WebActivationView,
    WebActivationSubmitResponse,
    VmActionRequest,
    VmInstallConfirmRequest,
    VmOpenCorePickerRequest,
    VmSmbiosDiagnosticRequest,
    VmBackupRequest,
    VmBackupRestoreRequest,
    VmCloneRequest,
    VmBackupListResponse,
    VmConfig,
    VmConfigCreate,
    VmConfigEnvelope,
    VmListResponse,
    VmRuntime,
    VmRuntimeList,
    VmSerialConsole,
    VmTelemetry,
    TelemetryHistory,
    TelemetrySettingsView,
    TelemetrySettingsUpdate,
    HostAlertList,
    ReimsReleaseList,
    ReimsReleaseUpdateRequest,
    ControlReleaseList,
    ControlReleaseUpdateRequest,
    ReimsImportCandidateList,
    ReimsImportRequest,
    VmConfigUpdate,
    VmDeleteRequest,
    VmDeleteResponse,
    VmProvisionRequest,
    JobDetail,
    JobListResponse,
    PrivateTransferListResponse,
    PrivateTransferView,
    MediaConvertRequest,
    AppleRecoveryDownloadRequest,
)
from .transfers import (
    InvalidTransfer,
    PrivateTransferStore,
    TransferNotFound,
    TransferQuotaExceeded,
    TransferTooLarge,
)


class AgentClient(Protocol):
    """Typed business boundary; implementations speak only the local agent RPC."""

    def is_connected(self) -> bool: ...
    def host_overview(self) -> HostOverview: ...
    def storage_inventory(self) -> StorageInventory: ...
    def host_usb_inventory(self) -> HostUsbInventory: ...
    def import_media_transfer(self, transfer_id: str) -> JobAccepted: ...
    def convert_managed_media(self, media_id: str, request: MediaConvertRequest) -> JobAccepted: ...
    def download_apple_recovery(self, request: AppleRecoveryDownloadRequest) -> JobAccepted: ...
    def create_vm_backup(self, vm_id: str, request: VmBackupRequest) -> JobAccepted: ...
    def verify_vm_backup(self, vm_id: str, backup_id: str, request: VmBackupRequest) -> JobAccepted: ...
    def restore_vm_backup(self, vm_id: str, backup_id: str, request: VmBackupRestoreRequest) -> JobAccepted: ...
    def clone_vm(self, vm_id: str, request: VmCloneRequest) -> JobAccepted: ...
    def list_vm_backups(self, vm_id: str) -> VmBackupListResponse: ...
    def list_reims_releases(self) -> ReimsReleaseList: ...
    def update_reims_release(self, request: ReimsReleaseUpdateRequest) -> JobAccepted: ...
    def list_control_releases(self) -> ControlReleaseList: ...
    def update_control_release(self, request: ControlReleaseUpdateRequest) -> JobAccepted: ...
    def list_vms(self) -> VmListResponse: ...
    def list_reims_import_candidates(self) -> ReimsImportCandidateList: ...
    def import_reims_vm(self, request: ReimsImportRequest, expected_revision: str) -> VmConfigEnvelope: ...
    def list_vm_runtimes(self) -> VmRuntimeList: ...
    def get_vm_serial_console(self, vm_id: str) -> VmSerialConsole: ...
    def get_vm_config(self, vm_id: str) -> VmConfigEnvelope | None: ...
    def patch_vm_config(self, vm_id: str, fields: dict[str, Any], expected_revision: str) -> VmConfigEnvelope: ...
    def update_vm_opencore_picker(self, vm_id: str, request: VmOpenCorePickerRequest, expected_revision: str) -> JobAccepted: ...
    def diagnose_vm_smbios(self, vm_id: str, request: VmSmbiosDiagnosticRequest, expected_revision: str) -> JobAccepted: ...
    def vm_action(self, vm_id: str, action: VmActionRequest) -> JobAccepted: ...


class ActivationCode:
    """One-time console-issued code with a short TTL."""

    def __init__(self, code: str | None = None, *, expires_at: float | None = None):
        self._digest = self._hash(code) if code else None
        self._expires_at = (expires_at if expires_at is not None else time.time() + 600) if code else None
        self._lock = threading.Lock()

    @staticmethod
    def _hash(code: str) -> bytes:
        return hashlib.sha256(code.encode("utf-8")).digest()

    def replace(self, code: str, *, ttl_seconds: int = 600) -> None:
        with self._lock:
            self._digest = self._hash(code)
            self._expires_at = time.time() + ttl_seconds

    def consume(self, candidate: str) -> bool:
        with self._lock:
            if self._digest is None or self._expires_at is None or time.time() >= self._expires_at:
                return False
            matches = hmac.compare_digest(self._digest, self._hash(candidate))
            if matches:
                self._digest = None
                self._expires_at = None
            return matches

    @property
    def available(self) -> bool:
        with self._lock:
            return bool(self._digest and self._expires_at and time.time() < self._expires_at)


def _activation_portal_post(
    portal_url: str,
    route: str,
    payload: dict[str, Any],
    *,
    client_version: str = "0.1.0",
) -> dict[str, Any]:
    """Post only to the configured HTTPS portal and refuse redirects."""
    request = UrlRequest(
        f"{portal_url.rstrip('/')}{route}",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Cloudflare Browser Integrity Check rejects urllib's default
            # "Python-urllib/..." signature. Identify the real API client;
            # do not impersonate a browser.
            "User-Agent": f"vHackintosh-Appliance/{client_version}",
        },
        method="POST",
    )

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
            return None

    opener = build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=8) as response:
            if response.status < 200 or response.status >= 300:
                raise ValueError("portal_http_error")
            raw = response.read(16 * 1024 + 1)
    except HTTPError as exc:
        # Keep the response body out of logs (it may contain infrastructure
        # details), but retain the status so an appliance journal can identify
        # whether the portal rejected the payload, rate-limited it, or failed.
        raise _ActivationPortalHTTPError(exc.code) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ConnectionError("portal_unavailable") from exc
    if len(raw) > 16 * 1024:
        raise ValueError("portal_response_too_large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("portal_invalid_response")
    return value


class _ActivationPortalHTTPError(ValueError):
    """Safe portal failure metadata; never stores or exposes the response body."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__("portal_http_error")


def _activation_failure_label(exc: Exception) -> str:
    if isinstance(exc, _ActivationPortalHTTPError):
        return f"portal_http_status={exc.status_code}"
    if isinstance(exc, json.JSONDecodeError):
        return "portal_invalid_json"
    if isinstance(exc, ValueError):
        reason = str(exc)
        if reason in {
            "portal_response_too_large",
            "portal_invalid_response",
            "portal_http_error",
            "Estado remoto inválido.",
            "Estado de ativação remoto inválido.",
        }:
            return reason.replace(" ", "_").lower()
        return "invalid_local_or_portal_data"
    if isinstance(exc, OSError):
        return "local_state_io_error"
    return type(exc).__name__


class SQLiteAuthStore:
    """Small local account/session store. Passwords are Argon2id hashes."""

    def __init__(self, path: str | Path, *, session_ttl: timedelta = timedelta(hours=12)):
        self.path = Path(path)
        self.session_ttl = session_ttl
        self._hasher = PasswordHasher()
        self._dummy_hash = self._hasher.hash("vHackintosh-invalid-account-timing-padding")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level="IMMEDIATE")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS administrator (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    must_change_password INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS web_session (
                    token_hash TEXT PRIMARY KEY,
                    csrf_hash TEXT NOT NULL,
                    username TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS web_session_expiry ON web_session(expires_at);
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(administrator)")}
            if "must_change_password" not in columns:
                db.execute(
                    "ALTER TABLE administrator ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"
                )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def has_admin(self) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM administrator WHERE id=1").fetchone() is not None

    def create_admin(self, username: str, password: str, *, must_change_password: bool = False) -> None:
        password_hash = self._hasher.hash(password)
        with self._db() as db:
            db.execute(
                "INSERT INTO administrator(id, username, password_hash, created_at, must_change_password) VALUES(1, ?, ?, ?, ?)",
                (username, password_hash, datetime.now(timezone.utc).isoformat(), int(must_change_password)),
            )

    def ensure_default_admin(self, username: str = "admin", password: str = "vhackintosh") -> bool:
        """Create the one-time package account without overwriting an existing admin."""
        password_hash = self._hasher.hash(password)
        with self._db() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO administrator(id, username, password_hash, created_at, must_change_password) "
                "VALUES(1, ?, ?, ?, 1)",
                (username, password_hash, datetime.now(timezone.utc).isoformat()),
            )
            return cursor.rowcount == 1

    def change_password(self, username: str, password: str) -> bool:
        password_hash = self._hasher.hash(password)
        with self._db() as db:
            cursor = db.execute(
                "UPDATE administrator SET password_hash=?, must_change_password=0 WHERE id=1 AND username=?",
                (password_hash, username),
            )
            if cursor.rowcount != 1:
                return False
            # Password rotation invalidates every browser session, including
            # sessions on other devices. The caller creates one fresh session.
            db.execute("DELETE FROM web_session")
        return True

    def authenticate(self, username: str, password: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT password_hash FROM administrator WHERE id=1 AND username=?", (username,)).fetchone()
        if row is None:
            # Do comparable Argon2 work for unknown usernames.
            try:
                self._hasher.verify(self._dummy_hash, password)
            except (VerificationError, InvalidHashError):
                pass
            return False
        try:
            return self._hasher.verify(row["password_hash"], password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def create_session(self, username: str) -> tuple[str, str, datetime]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + self.session_ttl
        with self._db() as db:
            db.execute("DELETE FROM web_session WHERE expires_at <= ?", (time.time(),))
            db.execute(
                "INSERT INTO web_session(token_hash, csrf_hash, username, expires_at, created_at) VALUES(?, ?, ?, ?, ?)",
                (self._token_hash(token), self._token_hash(csrf), username, expires.timestamp(), time.time()),
            )
        return token, csrf, expires

    def get_session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self._db() as db:
            row = db.execute(
                "SELECT s.csrf_hash, s.username, s.expires_at, a.must_change_password "
                "FROM web_session s JOIN administrator a ON a.id=1 AND a.username=s.username "
                "WHERE s.token_hash=? AND s.expires_at > ?",
                (self._token_hash(token), time.time()),
            ).fetchone()
        if row is None:
            return None
        return {
            "csrf_hash": row["csrf_hash"],
            "username": row["username"],
            "expires_at": row["expires_at"],
            "must_change_password": bool(row["must_change_password"]),
        }

    def default_admin_password_pending(self) -> bool:
        with self._db() as db:
            return db.execute(
                "SELECT 1 FROM administrator WHERE id=1 AND username='admin' AND must_change_password=1"
            ).fetchone() is not None

    def revoke_session(self, token: str | None) -> None:
        if token:
            with self._db() as db:
                db.execute("DELETE FROM web_session WHERE token_hash=?", (self._token_hash(token),))

    def verify_csrf(self, token: str | None, expected_hash: str) -> bool:
        """Verify a CSRF token without exposing the private hashing helper."""
        return bool(token) and hmac.compare_digest(self._token_hash(token), expected_hash)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SQLiteAuditStore:
    """Bounded audit trail for authenticated mutations, without request data."""

    def __init__(self, path: str | Path, *, max_entries: int = 10000):
        self.path = Path(path)
        self.max_entries = max_entries
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS audit_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                outcome INTEGER NOT NULL,
                request_id TEXT NOT NULL,
                operation_id TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS audit_event_created ON audit_event(id)")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def record(
        self, *, actor: str, action: str, target: str, outcome: int,
        request_id: str, operation_id: str,
    ) -> None:
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute(
                "INSERT INTO audit_event(created_at, actor, action, target, outcome, request_id, operation_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), actor[:64], action[:24], target[:256],
                 int(outcome), request_id[:64], operation_id[:128]),
            )
            db.execute(
                "DELETE FROM audit_event WHERE id <= COALESCE((SELECT id FROM audit_event "
                "ORDER BY id DESC LIMIT 1 OFFSET ?), 0)",
                (self.max_entries,),
            )

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with sqlite3.connect(self.path, timeout=5) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT created_at, actor, action, target, outcome, request_id, operation_id "
                "FROM audit_event ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


class DeferredSQLiteAuditStore:
    """Initialize audit storage on first mutation rather than module import."""

    def __init__(self, path: str | Path):
        self.path = path
        self._instance: SQLiteAuditStore | None = None
        self._lock = threading.Lock()

    def record(self, **event: Any) -> None:
        with self._lock:
            if self._instance is None:
                self._instance = SQLiteAuditStore(self.path)
            instance = self._instance
        instance.record(**event)

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            if self._instance is None:
                self._instance = SQLiteAuditStore(self.path)
            instance = self._instance
        return instance.recent(limit)


class LoginRateLimiter:
    def __init__(self, *, attempts: int = 5, window_seconds: int = 900):
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            failures = self._failures[key]
            while failures and now - failures[0] >= self.window_seconds:
                failures.popleft()
            return len(failures) >= self.attempts

    def failed(self, key: str) -> None:
        with self._lock:
            self._failures[key].append(time.monotonic())

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class _HostTelemetryStream:
    """One bounded, shared host sampler with per-session subscriber queues."""

    def __init__(self, sample: Any, session_is_valid: Any, *, interval: float = 1.0, queue_size: int = 2):
        self.sample = sample
        self.session_is_valid = session_is_valid
        self.interval = interval
        self.queue_size = queue_size
        self.epoch = uuid.uuid4().hex
        self.sequence = 0
        self.latest: dict[str, Any] | None = None
        self.subscribers: dict[int, tuple[str, asyncio.Queue[dict[str, Any]]]] = {}
        self._next_subscriber = 0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def subscribe(self, token: str) -> tuple[int, asyncio.Queue[dict[str, Any]]]:
        async with self._lock:
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.queue_size)
            self._next_subscriber += 1
            subscriber_id = self._next_subscriber
            self.subscribers[subscriber_id] = (token, queue)
            if self.latest is not None:
                queue.put_nowait(self.latest)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run())
            return subscriber_id, queue

    async def unsubscribe(self, subscriber_id: int) -> None:
        async with self._lock:
            self.subscribers.pop(subscriber_id, None)
            if not self.subscribers and self._task is not None:
                self._task.cancel()
                self._task = None

    async def _run(self) -> None:
        try:
            while True:
                async with self._lock:
                    subscribers = list(self.subscribers.items())
                if not subscribers:
                    return
                # A single agent call fans out to all active clients.
                try:
                    data = await self.sample()
                    payload = {
                        "epoch": self.epoch,
                        "sequence": self.sequence,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "data": data.model_dump(mode="json") if hasattr(data, "model_dump") else data,
                    }
                    self.sequence += 1
                    self.latest = payload
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Agent/RPC details are intentionally never serialized into SSE.
                    await asyncio.sleep(self.interval)
                    continue

                revoked: list[int] = []
                for subscriber_id, (token, queue) in subscribers:
                    try:
                        if not await self.session_is_valid(token):
                            revoked.append(subscriber_id)
                            continue
                    except Exception:
                        revoked.append(subscriber_id)
                        continue
                    if queue.full():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        queue.put_nowait(payload)
                    except asyncio.QueueFull:
                        pass
                if revoked:
                    async with self._lock:
                        for subscriber_id in revoked:
                            self.subscribers.pop(subscriber_id, None)
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            return


class DeferredSQLiteAuthStore:
    """Avoid filesystem writes during module import (e.g. OpenAPI inspection)."""

    def __init__(self, path: str | Path):
        self.path = path
        self._instance: SQLiteAuthStore | None = None
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        with self._lock:
            if self._instance is None:
                self._instance = SQLiteAuthStore(self.path)
            instance = self._instance
        return getattr(instance, name)


def create_app(
    *,
    agent: AgentClient | None = None,
    auth_store: SQLiteAuthStore | None = None,
    activation_code: ActivationCode | None = None,
    auth_db_path: str | Path = "/var/lib/vhackintosh/web/auth.sqlite",
    audit_store: SQLiteAuditStore | DeferredSQLiteAuditStore | None = None,
    audit_db_path: str | Path | None = None,
    host_alerts_db_path: str | Path | None = None,
    cookie_secure: bool = True,
    version: str = "0.1.0",
    web_port: int = 8443,
    tls_fingerprint: str | None = None,
    transfer_directory: str | Path | None = None,
    transfer_max_file_bytes: int = 64 * 1024**3,
    transfer_max_total_bytes: int = 128 * 1024**3,
    transfer_min_free_bytes: int = 1024**3,
    activation_portal_url: str | None = None,
    allow_local_activation_portal: bool | None = None,
    activation_store: WebActivationStore | None = None,
) -> FastAPI:
    """Create an API instance with injected agent/auth dependencies."""
    auth = auth_store or DeferredSQLiteAuthStore(auth_db_path)
    auth_path = getattr(auth, "path", auth_db_path)
    audit_path = audit_db_path or Path(auth_path).with_name("audit.sqlite")
    audit = audit_store or DeferredSQLiteAuditStore(audit_path)
    activation = activation_code or ActivationCode()
    portal_url = (activation_portal_url or os.environ.get("VHACKINTOSH_ACTIVATION_PORTAL_URL") or "https://vhackintosh.proclin.com.br").rstrip("/")
    portal = urlsplit(portal_url)
    local_portal_enabled = allow_local_activation_portal
    if local_portal_enabled is None:
        local_portal_enabled = os.environ.get("VHACKINTOSH_ACTIVATION_PORTAL_ALLOW_LOCAL_HTTP") == "1"
    local_portal = (
        local_portal_enabled
        and portal.scheme == "http"
        and portal.hostname in {"127.0.0.1", "localhost"}
        and portal.port is not None
        and 1 <= portal.port <= 65535
        and not portal.username
        and not portal.password
        and not portal.query
        and not portal.fragment
        and portal.path in {"", "/"}
    )
    production_portal = (
        portal.scheme == "https"
        and portal.hostname == "vhackintosh.proclin.com.br"
        and portal.port in {None, 443}
        and not portal.username
        and not portal.password
        and not portal.query
        and not portal.fragment
        and portal.path in {"", "/"}
    )
    if not (local_portal or production_portal):
        raise ValueError("Activation portal must be https://vhackintosh.proclin.com.br")
    web_activation = activation_store or WebActivationStore(Path(auth_path).with_name("activation.json"))
    limiter = LoginRateLimiter()
    app = FastAPI(title="vHackintosh Control API", version=version, openapi_url="/api/v1/openapi.json")
    app.state.agent = agent
    app.state.auth_store = auth
    app.state.audit_store = audit
    host_alerts = HostAlertStore(host_alerts_db_path or Path(auth_path).with_name("host-alerts.sqlite"))
    app.state.host_alert_store = host_alerts
    app.state.activation_code = activation
    app.state.web_activation_store = web_activation
    app.state.activation_portal_url = portal_url
    app.state.login_limiter = limiter
    app.state.cookie_secure = cookie_secure
    app.state.version = version
    app.state.web_port = web_port
    app.state.tls_fingerprint = tls_fingerprint
    transfer_path = transfer_directory or Path(auth_path).with_name("transfers")
    transfers = PrivateTransferStore(
        transfer_path,
        max_file_bytes=transfer_max_file_bytes,
        max_total_bytes=transfer_max_total_bytes,
        min_free_bytes=transfer_min_free_bytes,
    )
    app.state.private_transfers = transfers

    @app.middleware("http")
    async def api_security_headers(request: Request, call_next: Any) -> Response:
        request.state.request_id = secrets.token_hex(16)
        try:
            response = await call_next(request)
        except Exception as exc:
            # ServerErrorMiddleware normally emits a plain-text 500 outside
            # this middleware, which loses the request id and skips auditing.
            # Turn unexpected route failures into the same safe API contract
            # used by expected errors, then continue through headers and audit.
            import logging

            # Do not persist exception text or tracebacks here: lower layers
            # can include private host paths, command lines, or guest data.
            # The request ID links this safe record to the client-visible
            # problem and audit event; structured job logs carry redacted
            # operational detail when available.
            logging.getLogger(__name__).error(
                "Unhandled web API exception request_id=%s method=%s path=%s exception_type=%s",
                request.state.request_id,
                request.method,
                request.url.path,
                type(exc).__name__,
            )
            problem = ApiError(
                code="internal_error",
                message=f"Erro interno ao processar a solicitação. Identificador: {request.state.request_id}.",
                request_id=request.state.request_id,
            )
            response = JSONResponse(status_code=500, content=problem.model_dump(mode="json"))
        response.headers.setdefault("X-Request-ID", request.state.request_id)
        if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and getattr(request.state, "actor", None):
            route = request.scope.get("route")
            route_template = getattr(route, "path", request.url.path)
            path_params = request.scope.get("path_params") or {}
            target = getattr(request.state, "audit_target", None)
            if target is None:
                target = route_template if not path_params else f"{route_template} {json.dumps(path_params, sort_keys=True)}"
            try:
                await asyncio.to_thread(
                    audit.record,
                    actor=request.state.actor,
                    action=getattr(request.state, "audit_action", request.method.upper()),
                    target=target,
                    outcome=response.status_code,
                    request_id=request.state.request_id,
                    operation_id=getattr(request.state, "operation_id", request.state.request_id),
                )
            except Exception:
                # Audit storage must not make a working appliance API unavailable.
                import logging
                logging.getLogger(__name__).exception("Failed to persist web API audit event")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        body = ApiError(
            code=str(detail.get("code", "http_error")),
            message=str(detail.get("message", "A solicitação não pôde ser atendida.")),
            request_id=getattr(request.state, "request_id", None),
            details=detail.get("details", {}),
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Do not echo Pydantic's `input` value: requests can contain passwords,
        # activation codes, and serial numbers.
        errors = [
            {"location": list(item.get("loc", ())), "type": item.get("type", "invalid"), "message": item.get("msg", "Valor inválido.")}
            for item in exc.errors()
        ]
        body = ApiError(
            code="validation_error",
            message="A solicitação contém campos inválidos.",
            request_id=getattr(request.state, "request_id", None),
            details={"errors": errors},
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    async def current_session(request: Request) -> dict[str, Any] | None:
        return await _call(auth.get_session, request.cookies.get("vh_session"))

    async def session_token_is_valid(token: str) -> bool:
        return await _call(auth.get_session, token) is not None

    async def collect_host_metrics() -> Any:
        current = app.state.agent
        if current is None:
            raise RuntimeError("agent unavailable")
        method = getattr(current, "host_metrics", None)
        if method is None:
            raise RuntimeError("telemetry unavailable")
        result = await _call(method)
        host_alerts.observe(result.get("samples", []) if isinstance(result, dict) else getattr(result, "samples", []))
        return result

    telemetry_stream = _HostTelemetryStream(collect_host_metrics, session_token_is_valid)
    app.state.host_telemetry_stream = telemetry_stream
    vm_telemetry_streams: dict[str, _HostTelemetryStream] = {}
    vm_telemetry_streams_lock = asyncio.Lock()
    app.state.vm_telemetry_streams = vm_telemetry_streams

    async def require_session(request: Request) -> dict[str, Any]:
        session = await current_session(request)
        if session is None:
            raise HTTPException(status_code=401, detail={"code": "authentication_required", "message": "Autentique-se para continuar."})
        if session.get("must_change_password"):
            raise HTTPException(status_code=403, detail={"code": "password_change_required", "message": "Defina uma nova senha antes de continuar."})
        request.state.actor = session["username"]
        return session

    async def require_csrf(request: Request, session: dict[str, Any] = Depends(require_session)) -> dict[str, Any]:
        _validate_browser_origin(request)
        csrf = request.headers.get("x-csrf-token", "")
        if not await _call(auth.verify_csrf, csrf, session["csrf_hash"]):
            raise HTTPException(status_code=403, detail={"code": "csrf_failed", "message": "Token de proteção inválido."})
        return session

    async def require_password_change_csrf(request: Request) -> dict[str, Any]:
        session = await current_session(request)
        if session is None:
            raise HTTPException(status_code=401, detail={"code": "authentication_required", "message": "Autentique-se para continuar."})
        _validate_browser_origin(request)
        csrf = request.headers.get("x-csrf-token", "")
        if not await _call(auth.verify_csrf, csrf, session["csrf_hash"]):
            raise HTTPException(status_code=403, detail={"code": "csrf_failed", "message": "Token de proteção inválido."})
        return session

    async def get_agent() -> AgentClient:
        current = app.state.agent
        if current is None:
            raise _dependency_unavailable()
        connected = await _call(current.is_connected)
        if not connected:
            raise _dependency_unavailable()
        return current

    async def _activation_inventory(svc: AgentClient) -> dict[str, Any]:
        from core.reims_release import REIMS_REPO, current_release

        host = await _call(svc.host_overview)
        if not str(host.cpu_model).strip() or not 256 * 1024**2 <= host.memory_total_bytes <= 16 * 1024**4:
            raise HTTPException(status_code=503, detail={"code": "activation_inventory_unavailable", "message": "O inventário do host ainda não fornece modelo do processador e memória válidos para solicitar ativação."})
        gpu_method = getattr(svc, "host_gpus", None)
        gpu_inventory = await _call(gpu_method) if gpu_method is not None else None
        devices = getattr(gpu_inventory, "items", []) if gpu_inventory is not None else []
        vendor_names = {"10de": "NVIDIA", "1002": "AMD", "8086": "Intel"}
        gpus = []
        for device in devices[:16]:
            name = " ".join(str(getattr(device, "name", "GPU")).split())[:160]
            if not name:
                continue
            vendor_id = str(getattr(device, "vendor_id", "")).lower()
            gpus.append({"vendor": vendor_names.get(vendor_id, "GPU"), "model": name})
        gpus.sort(key=lambda gpu: (gpu["vendor"].casefold(), gpu["model"].casefold()))
        release_tag = await asyncio.to_thread(current_release)
        return {
            "cpu_model": " ".join(str(host.cpu_model).split())[:160] or "desconhecido",
            "memory_total_bytes": host.memory_total_bytes,
            "gpus": gpus,
            "reims_vgpu_repository": REIMS_REPO,
            "reims_vgpu_release": release_tag,
        }

    def transfer_error(exc: Exception) -> HTTPException:
        if isinstance(exc, TransferNotFound):
            return HTTPException(status_code=404, detail={"code": "transfer_not_found", "message": "Transferência não encontrada."})
        if isinstance(exc, (TransferTooLarge, TransferQuotaExceeded)):
            return HTTPException(status_code=413, detail={"code": "transfer_quota_exceeded", "message": "O limite de armazenamento da transferência foi atingido."})
        if isinstance(exc, InvalidTransfer):
            return HTTPException(status_code=422, detail={"code": "invalid_transfer", "message": str(exc)})
        return HTTPException(status_code=500, detail={"code": "transfer_failed", "message": "Não foi possível concluir a transferência."})

    def transfer_view(item: Any) -> PrivateTransferView:
        return PrivateTransferView(
            id=item.id,
            filename=item.filename,
            size_bytes=item.size_bytes,
            format=item.format,
        )

    @app.get("/api/v1/transfers", response_model=PrivateTransferListResponse)
    async def list_transfers(_: dict[str, Any] = Depends(require_session)) -> PrivateTransferListResponse:
        items = await _call(transfers.list)
        return PrivateTransferListResponse(
            items=[transfer_view(item) for item in items],
            max_file_bytes=transfers.max_file_bytes,
            max_total_bytes=transfers.max_total_bytes,
            min_free_bytes=transfers.min_free_bytes,
        )

    @app.post("/api/v1/transfers", response_model=PrivateTransferView, status_code=201)
    async def upload_transfer(
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
        filename: str = Header(
            alias="X-Transfer-Filename",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}$",
            description="Basename ASCII sem componentes de caminho; a extensão deve corresponder ao formato.",
        ),
        fmt: Literal["iso", "qcow2", "dmg"] = Header(
            alias="X-Transfer-Format",
            description="Formato com assinatura reconhecida pelo spool privado.",
        ),
    ) -> PrivateTransferView:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/octet-stream":
            raise HTTPException(status_code=415, detail={"code": "unsupported_media_type", "message": "Envie um fluxo binário application/octet-stream."})
        declared_length = request.headers.get("content-length")
        if declared_length is not None:
            try:
                length = int(declared_length)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail={"code": "invalid_content_length", "message": "Content-Length inválido."}) from exc
            if length < 0:
                raise HTTPException(status_code=400, detail={"code": "invalid_content_length", "message": "Content-Length inválido."})
            if length > transfers.max_file_bytes:
                raise transfer_error(TransferTooLarge())
        try:
            writer = await run_in_threadpool(transfers.begin, filename, fmt)
        except (InvalidTransfer, TransferQuotaExceeded) as exc:
            raise transfer_error(exc) from exc
        try:
            async for chunk in request.stream():
                await run_in_threadpool(writer.write, chunk)
            item = await run_in_threadpool(writer.commit)
        except (InvalidTransfer, TransferQuotaExceeded, TransferTooLarge) as exc:
            await run_in_threadpool(writer.abort)
            raise transfer_error(exc) from exc
        except BaseException:
            await run_in_threadpool(writer.abort)
            raise
        request.state.audit_action = "transfer.upload"
        request.state.operation_id = item.id
        return transfer_view(item)

    @app.get("/api/v1/transfers/{transfer_id}/content")
    async def download_transfer(
        transfer_id: str,
        request: Request,
        _: dict[str, Any] = Depends(require_session),
    ) -> StreamingResponse:
        try:
            item, file = await run_in_threadpool(transfers.open_download, transfer_id)
        except TransferNotFound as exc:
            raise transfer_error(exc) from exc
        token = request.cookies.get("vh_session")

        async def stream_file():
            try:
                while True:
                    if not token or not await session_token_is_valid(token):
                        return
                    chunk = await run_in_threadpool(file.read, 64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await run_in_threadpool(file.close)

        return StreamingResponse(
            stream_file(),
            media_type=transfers.MEDIA_TYPES[item.format],
            headers={
                "Content-Length": str(item.size_bytes),
                "Content-Disposition": f'attachment; filename="{item.filename}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.delete("/api/v1/transfers/{transfer_id}", status_code=204)
    async def delete_transfer(
        transfer_id: str,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
    ) -> Response:
        try:
            await _call(transfers.delete, transfer_id)
        except TransferNotFound as exc:
            raise transfer_error(exc) from exc
        request.state.audit_action = "transfer.delete"
        request.state.operation_id = transfer_id
        return Response(status_code=204)

    @app.get("/api/v1/health", response_model=Health)
    async def health() -> Health:
        connected = bool(agent and await _call(agent.is_connected))
        return Health(
            status="ok" if connected else "degraded",
            version=version,
            agent_connected=connected,
            checked_at=datetime.now(timezone.utc),
        )

    @app.get("/api/v1/session", response_model=SessionView)
    async def session_view(request: Request) -> SessionView:
        session = await current_session(request)
        return SessionView(
            authenticated=session is not None,
            username=session["username"] if session else None,
            expires_at=datetime.fromtimestamp(session["expires_at"], timezone.utc) if session else None,
            bootstrap_required=not await _call(auth.has_admin),
            must_change_password=bool(session and session.get("must_change_password")),
            default_credentials_available=await _call(auth.default_admin_password_pending),
            tls_fingerprint=app.state.tls_fingerprint,
        )

    @app.get("/api/v1/audit", response_model=AuditListResponse)
    async def list_audit(
        limit: int = Query(default=100, ge=1, le=1000),
        _: dict[str, Any] = Depends(require_session),
    ) -> AuditListResponse:
        return AuditListResponse.model_validate({"items": await asyncio.to_thread(audit.recent, limit), "limit": limit})

    @app.post("/api/v1/session/bootstrap", response_model=SessionView)
    async def bootstrap(payload: BootstrapRequest, request: Request, response: Response) -> SessionView:
        request.state.actor = "".join(char for char in payload.username[:64] if char.isprintable()) or "unknown"
        request.state.audit_action = "session.bootstrap"
        _validate_browser_origin(request)
        if await _call(auth.has_admin):
            raise HTTPException(status_code=409, detail={"code": "bootstrap_closed", "message": "A configuração inicial já foi concluída."})
        key = f"bootstrap:{request.client.host if request.client else 'unknown'}"
        if limiter.blocked(key):
            raise HTTPException(status_code=429, detail={"code": "bootstrap_rate_limited", "message": "Muitas tentativas de configuração inicial. Aguarde antes de tentar novamente."}, headers={"Retry-After": "900"})
        if not activation.consume(payload.code):
            limiter.failed(key)
            raise HTTPException(status_code=401, detail={"code": "invalid_activation_code", "message": "Código de configuração do administrador inválido ou expirado."})
        limiter.succeeded(key)
        try:
            await _call(auth.create_admin, payload.username, payload.password)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail={"code": "bootstrap_closed", "message": "A configuração inicial já foi concluída."}) from None
        bootstrap_code_file = getattr(app.state, "bootstrap_code_file", None)
        if bootstrap_code_file is not None:
            try:
                Path(bootstrap_code_file).unlink(missing_ok=True)
            except OSError:
                # The administrator record closes bootstrap even if tmpfs cleanup fails.
                pass
        token, csrf, expires = await _call(auth.create_session, payload.username)
        active_session = await _call(auth.get_session, token)
        request.state.operation_id = request.state.request_id
        _set_session_cookies(response, token, csrf, cookie_secure)
        return SessionView(
            authenticated=True, username=payload.username, expires_at=expires, bootstrap_required=False,
            must_change_password=bool(active_session and active_session.get("must_change_password")),
            default_credentials_available=await _call(auth.default_admin_password_pending),
        )

    @app.post("/api/v1/session/login", response_model=SessionView)
    async def login(payload: LoginRequest, request: Request, response: Response) -> SessionView:
        request.state.actor = "".join(char for char in payload.username[:64] if char.isprintable()) or "unknown"
        request.state.audit_action = "session.login"
        _validate_browser_origin(request)
        key = request.client.host if request.client else "unknown"
        if limiter.blocked(key):
            raise HTTPException(status_code=429, detail={"code": "login_rate_limited", "message": "Muitas tentativas. Aguarde antes de tentar novamente."}, headers={"Retry-After": "900"})
        if not await _call(auth.authenticate, payload.username, payload.password):
            limiter.failed(key)
            raise HTTPException(status_code=401, detail={"code": "invalid_credentials", "message": "Usuário ou senha inválidos."})
        limiter.succeeded(key)
        token, csrf, expires = await _call(auth.create_session, payload.username)
        active_session = await _call(auth.get_session, token)
        _set_session_cookies(response, token, csrf, cookie_secure)
        return SessionView(
            authenticated=True, username=payload.username, expires_at=expires, bootstrap_required=False,
            must_change_password=bool(active_session and active_session.get("must_change_password")),
            default_credentials_available=await _call(auth.default_admin_password_pending),
        )

    @app.post("/api/v1/session/logout", response_model=SessionView)
    async def logout(request: Request, response: Response, session: dict[str, Any] = Depends(require_csrf)) -> SessionView:
        request.state.audit_action = "session.logout"
        request.state.operation_id = request.state.request_id
        await _call(auth.revoke_session, request.cookies.get("vh_session"))
        _clear_session_cookies(response, cookie_secure)
        return SessionView(authenticated=False, bootstrap_required=False)

    @app.post("/api/v1/session/password", response_model=SessionView)
    async def change_password(
        payload: PasswordChangeRequest,
        request: Request,
        response: Response,
        session: dict[str, Any] = Depends(require_password_change_csrf),
    ) -> SessionView:
        request.state.actor = session["username"]
        request.state.audit_action = "session.password_change"
        request.state.operation_id = request.state.request_id
        _validate_browser_origin(request)
        key = f"password:{request.client.host if request.client else 'unknown'}"
        if limiter.blocked(key):
            raise HTTPException(status_code=429, detail={"code": "password_rate_limited", "message": "Muitas tentativas. Aguarde antes de tentar novamente."}, headers={"Retry-After": "900"})
        if payload.current_password == payload.new_password:
            raise HTTPException(status_code=422, detail={"code": "password_unchanged", "message": "Escolha uma senha diferente da atual."})
        if not await _call(auth.authenticate, session["username"], payload.current_password):
            limiter.failed(key)
            raise HTTPException(status_code=401, detail={"code": "invalid_current_password", "message": "A senha atual está incorreta."})
        limiter.succeeded(key)
        initial_password_rotation = bool(session.get("must_change_password"))
        changed = await _call(auth.change_password, session["username"], payload.new_password)
        if not changed:
            raise HTTPException(status_code=409, detail={"code": "administrator_missing", "message": "A conta administrativa não está disponível. Faça login novamente."})
        token, csrf, expires = await _call(auth.create_session, session["username"])
        _set_session_cookies(response, token, csrf, cookie_secure)
        reims_update_status: Literal["queued", "unavailable"] | None = None
        reims_update_job_id: str | None = None
        if initial_password_rotation:
            # Password rotation must succeed even when the agent is offline.
            # The agent job checks the latest release and skips installation
            # when the appliance already has that version.
            reims_update_status = "unavailable"
            svc = app.state.agent
            if svc is not None:
                method = getattr(svc, "update_reims_release", None)
                try:
                    if method is not None and await _call(svc.is_connected):
                        accepted = JobAccepted.model_validate(await _call(method, ReimsReleaseUpdateRequest(
                            idempotency_key=str(uuid.uuid4()),
                            tag="latest",
                        )))
                        reims_update_status = "queued"
                        reims_update_job_id = accepted.job_id
                except Exception:
                    # The user can continue; the UI will point to the manual
                    # Reims update action if this optional enqueue fails.
                    pass
        return SessionView(
            authenticated=True,
            username=session["username"],
            expires_at=expires,
            bootstrap_required=False,
            must_change_password=False,
            default_credentials_available=False,
            tls_fingerprint=app.state.tls_fingerprint,
            reims_update_status=reims_update_status,
            reims_update_job_id=reims_update_job_id,
        )

    @app.get("/api/v1/host", response_model=HostOverview)
    async def host_overview(_: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent)) -> HostOverview:
        host = await _call(svc.host_overview)
        return host.model_copy(update={"web_port": app.state.web_port, "tls_fingerprint": app.state.tls_fingerprint})

    @app.get("/api/v1/activation", response_model=WebActivationView)
    async def web_activation_view(_: dict[str, Any] = Depends(require_session)) -> WebActivationView:
        value = await asyncio.to_thread(web_activation.get_or_create)
        inventory_changed = False
        if value.get("inventory") is not None and value["status"] != "not_submitted":
            try:
                svc = app.state.agent
                if svc is not None and await _call(svc.is_connected):
                    current_inventory = await _activation_inventory(svc)
                    inventory_changed = current_inventory != value["inventory"]
            except Exception:
                # Keep activation status visible if a host capability is temporarily unavailable.
                inventory_changed = False
        return WebActivationView(
            appliance_id=value["appliance_id"],
            activation_code=value["activation_code"],
            status=value["status"],
            updated_at=datetime.fromisoformat(value["updated_at"]),
            portal_url=portal_url,
            inventory_changed=inventory_changed,
        )

    @app.post("/api/v1/activation/submit", response_model=WebActivationSubmitResponse, status_code=202)
    async def submit_web_activation(
        request: Request,
        session: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> WebActivationSubmitResponse:
        request.state.actor = session["username"]
        request.state.audit_action = "activation.submit"
        request.state.operation_id = request.state.request_id
        try:
            inventory = await _activation_inventory(svc)
            local = await asyncio.to_thread(web_activation.get_or_create)
            portal_payload = {
                "applianceId": local["appliance_id"],
                "activationCode": local["activation_code"],
                "inventory": {
                    "cpuModel": inventory["cpu_model"],
                    "memoryTotalBytes": inventory["memory_total_bytes"],
                    "gpus": inventory["gpus"],
                    "reimsVgpuRepository": inventory["reims_vgpu_repository"],
                    "reimsVgpuRelease": inventory["reims_vgpu_release"],
                },
            }
            result = await asyncio.to_thread(
                _activation_portal_post,
                portal_url,
                "/api/appliance/activations",
                portal_payload,
                client_version=app.state.version,
            )
            remote_status = result.get("status")
            request_id = result.get("requestId")
            if remote_status not in {"pending", "approved", "rejected"} or not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
                raise ValueError("portal_invalid_response")
            await asyncio.to_thread(web_activation.save_submission, inventory, portal_request_id=request_id)
            saved = await asyncio.to_thread(web_activation.update_portal_status, remote_status)
            return WebActivationSubmitResponse(status=saved["status"], updated_at=datetime.fromisoformat(saved["updated_at"]))
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail={"code": "activation_portal_unavailable", "message": "Não foi possível acessar o portal de ativação. Tente novamente quando a conexão com a internet estiver disponível."}) from exc
        except HTTPException:
            raise
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Activation request failed (%s)", _activation_failure_label(exc))
            raise HTTPException(status_code=502, detail={"code": "activation_request_failed", "message": "O portal não confirmou o pedido de ativação. Nenhum status foi alterado; tente novamente."}) from None

    @app.post("/api/v1/activation/sync", response_model=WebActivationSubmitResponse)
    async def sync_web_activation(
        request: Request,
        session: dict[str, Any] = Depends(require_csrf),
    ) -> WebActivationSubmitResponse:
        request.state.actor = session["username"]
        request.state.audit_action = "activation.status_sync"
        value = await asyncio.to_thread(web_activation.get_or_create)
        if value["status"] == "not_submitted":
            raise HTTPException(status_code=409, detail={"code": "activation_not_submitted", "message": "Envie primeiro o pedido de ativação."})
        try:
            result = await asyncio.to_thread(
                _activation_portal_post,
                portal_url,
                "/api/appliance/activations/status",
                {"applianceId": value["appliance_id"], "activationCode": value["activation_code"]},
                client_version=app.state.version,
            )
            remote_status = result.get("status")
            if remote_status not in {"pending", "approved", "rejected"}:
                raise ValueError("portal_invalid_response")
            saved = await asyncio.to_thread(web_activation.update_portal_status, remote_status)
            return WebActivationSubmitResponse(status=saved["status"], updated_at=datetime.fromisoformat(saved["updated_at"]))
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail={"code": "activation_portal_unavailable", "message": "Não foi possível consultar o portal. Tente novamente mais tarde."}) from exc
        except HTTPException:
            raise
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Activation status sync failed (%s)", _activation_failure_label(exc))
            raise HTTPException(status_code=502, detail={"code": "activation_status_failed", "message": "Não foi possível atualizar a situação da ativação."}) from None

    @app.get("/api/v1/capabilities", response_model=CapabilitiesResponse)
    async def runtime_capabilities(
        _: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent),
    ) -> CapabilitiesResponse:
        method = getattr(svc, "runtime_capabilities", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não informa capacidades verificadas do runtime.")
        return CapabilitiesResponse.model_validate(await _call(method))

    @app.get("/api/v1/vms", response_model=VmListResponse)
    async def list_vms(response: Response, _: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent)) -> VmListResponse:
        result = await _call(svc.list_vms)
        response.headers["ETag"] = _format_etag(result.revision)
        return result

    @app.get("/api/v1/vms/import-candidates", response_model=ReimsImportCandidateList)
    async def list_reims_import_candidates(
        _: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent),
    ) -> ReimsImportCandidateList:
        method = getattr(svc, "list_reims_import_candidates", None)
        if method is None:
            raise _dependency_unavailable("O agente não oferece a revisão de VMs registradas no Reims.")
        return ReimsImportCandidateList.model_validate(await _call(method))

    @app.post("/api/v1/vms/import", status_code=201, response_model=VmConfigEnvelope)
    async def import_reims_vm(
        payload: ReimsImportRequest,
        request: Request,
        response: Response,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> VmConfigEnvelope:
        request.state.audit_action = "vm.import_reims"
        expected = _require_if_match(request)
        method = getattr(svc, "import_reims_vm", None)
        if method is None:
            raise _dependency_unavailable("O agente não oferece a importação revisável do Reims.")
        item = VmConfigEnvelope.model_validate(await _call(method, payload, expected))
        response.headers["ETag"] = _format_etag(item.revision)
        return item

    @app.get("/api/v1/vms/runtime", response_model=VmRuntimeList)
    async def list_vm_runtimes(_: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent)) -> VmRuntimeList:
        method = getattr(svc, "list_vm_runtimes", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não fornece o estado das VMs.")
        return VmRuntimeList.model_validate(await _call(method))

    @app.get("/api/v1/vms/{vm_id}/config", response_model=VmConfigEnvelope)
    async def get_vm_config(vm_id: str, response: Response, _: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent)) -> VmConfigEnvelope:
        item = await _call(svc.get_vm_config, vm_id)
        if item is None:
            raise HTTPException(status_code=404, detail={"code": "vm_not_found", "message": "VM não encontrada."})
        response.headers["ETag"] = _format_etag(item.revision)
        return item

    @app.post("/api/v1/vms", status_code=201, response_model=VmConfigEnvelope)
    async def create_vm(
        payload: VmConfigCreate,
        request: Request,
        response: Response,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> VmConfigEnvelope:
        request.state.audit_action = "vm.create_config"
        expected = _require_if_match(request)
        method = getattr(svc, "create_vm_config", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não permite cadastrar configurações de VM.")
        item = await _call(method, payload, expected)
        response.headers["ETag"] = _format_etag(item.revision)
        return item

    @app.put("/api/v1/vms/{vm_id}/config", response_model=VmConfigEnvelope)
    async def update_vm(
        vm_id: str,
        payload: VmConfigUpdate,
        request: Request,
        response: Response,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> VmConfigEnvelope:
        request.state.audit_action = "vm.update_config"
        expected = _require_if_match(request)
        method = getattr(svc, "update_vm_config", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não permite editar configurações de VM.")
        item = await _call(method, vm_id, payload, expected)
        response.headers["ETag"] = _format_etag(item.revision)
        return item

    @app.post("/api/v1/vms/{vm_id}/install/confirm", response_model=VmConfigEnvelope)
    async def confirm_vm_installation(
        vm_id: str,
        payload: VmInstallConfirmRequest,
        request: Request,
        response: Response,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> VmConfigEnvelope:
        request.state.audit_action = "vm.install.confirm"
        request.state.audit_target = vm_id
        request.state.operation_id = vm_id
        if payload.confirmation != vm_id:
            raise HTTPException(
                status_code=422,
                detail={"code": "confirmation_required", "message": "Confirme informando o identificador exato da VM."},
            )
        expected = _require_if_match(request)
        method = getattr(svc, "patch_vm_config", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não permite confirmar a fase de instalação.")
        item = await _call(
            method,
            vm_id,
            {"install_phase": "installed", "install_evidence": "complete"},
            expected,
        )
        response.headers["ETag"] = _format_etag(item.revision)
        return VmConfigEnvelope.model_validate(item)

    @app.post("/api/v1/vms/{vm_id}/opencore/picker", response_model=JobAccepted, status_code=202)
    async def update_vm_opencore_picker(
        vm_id: str,
        payload: VmOpenCorePickerRequest,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> JobAccepted:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", vm_id):
            raise HTTPException(status_code=422, detail={"code": "invalid_vm_id", "message": "Identificador da VM inválido."})
        if payload.confirmation != vm_id:
            raise HTTPException(
                status_code=422,
                detail={"code": "confirmation_required", "message": "Confirme informando o identificador exato da VM."},
            )
        expected = _require_if_match(request)
        method = getattr(svc, "update_vm_opencore_picker", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece regeneração segura do OpenCore.")
        result = JobAccepted.model_validate(await _call(method, vm_id, payload, expected))
        request.state.audit_action = "vm.picker.update"
        request.state.audit_target = vm_id
        request.state.operation_id = result.job_id
        return result

    @app.post("/api/v1/vms/{vm_id}/smbios/diagnostics", response_model=JobAccepted, status_code=202)
    async def diagnose_vm_smbios(
        vm_id: str,
        payload: VmSmbiosDiagnosticRequest,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> JobAccepted:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", vm_id):
            raise HTTPException(status_code=422, detail={"code": "invalid_vm_id", "message": "Identificador da VM inválido."})
        expected = _require_if_match(request)
        method = getattr(svc, "diagnose_vm_smbios", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece diagnóstico de SMBIOS.")
        result = JobAccepted.model_validate(await _call(method, vm_id, payload, expected))
        request.state.audit_action = "vm.smbios.check"
        request.state.audit_target = vm_id
        request.state.operation_id = result.job_id
        return result

    @app.delete("/api/v1/vms/{vm_id}", response_model=VmDeleteResponse)
    async def delete_vm(
        vm_id: str,
        payload: VmDeleteRequest,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> VmDeleteResponse:
        request.state.audit_action = "vm.delete"
        request.state.audit_target = vm_id
        request.state.operation_id = vm_id
        if payload.confirmation != vm_id:
            raise HTTPException(
                status_code=422,
                detail={"code": "confirmation_required", "message": "Confirme informando o identificador exato da VM."},
            )
        expected = _require_if_match(request)
        method = getattr(svc, "delete_vm_config", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não permite remover VMs do catálogo.")
        result = await _call(method, vm_id, expected, payload.delete_disk)
        return VmDeleteResponse.model_validate(result)

    @app.post("/api/v1/vms/provision", status_code=202, response_model=JobAccepted)
    async def provision_vm(
        payload: VmProvisionRequest,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> JobAccepted:
        request.state.audit_action = "vm.provision"
        method = getattr(svc, "provision_vm", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece provisionamento assíncrono.")
        accepted = await _call(method, payload)
        request.state.operation_id = accepted.job_id
        return accepted

    @app.get("/api/v1/jobs", response_model=JobListResponse)
    async def list_jobs(
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10000),
        state: list[str] | None = Query(default=None),
        _: dict[str, Any] = Depends(require_session),
        svc: AgentClient = Depends(get_agent),
    ) -> JobListResponse:
        valid_states = {"queued", "running", "succeeded", "failed", "cancelling", "cancelled", "interrupted"}
        if state is not None and (not state or any(value not in valid_states for value in state)):
            raise HTTPException(status_code=422, detail={"code": "invalid_job_query", "message": "O filtro de estado contém valores inválidos."})
        method = getattr(svc, "list_jobs", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece listagem de tarefas.")
        return JobListResponse.model_validate(await _call(method, limit, offset, state))

    @app.get("/api/v1/jobs/{job_id}", response_model=JobDetail)
    async def get_job(job_id: str, _: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent)) -> JobDetail:
        method = getattr(svc, "get_job", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece consulta de tarefas.")
        result = await _call(method, job_id)
        if result is None:
            raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "Tarefa não encontrada."})
        return JobDetail.model_validate(result)

    @app.post("/api/v1/jobs/{job_id}/cancel", response_model=JobDetail)
    async def cancel_job(job_id: str, request: Request, _: dict[str, Any] = Depends(require_csrf), svc: AgentClient = Depends(get_agent)) -> JobDetail:
        request.state.audit_action = "job.cancel"
        request.state.operation_id = job_id
        method = getattr(svc, "cancel_job", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece cancelamento de tarefas.")
        result = await _call(method, job_id)
        if result is None:
            raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "Tarefa não encontrada."})
        request.state.operation_id = job_id
        return JobDetail.model_validate(result)

    @app.post("/api/v1/jobs/{job_id}/retry", status_code=202, response_model=JobAccepted)
    async def retry_job(job_id: str, request: Request, _: dict[str, Any] = Depends(require_csrf), svc: AgentClient = Depends(get_agent)) -> JobAccepted:
        request.state.audit_action = "job.retry"
        request.state.operation_id = job_id
        method = getattr(svc, "retry_job", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece repetição explícita de tarefas.")
        result = await _call(method, job_id)
        if result is None:
            raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "Tarefa não encontrada."})
        accepted = JobAccepted.model_validate(result)
        request.state.operation_id = accepted.job_id
        return accepted

    @app.post("/api/v1/vms/{vm_id}/actions", status_code=202, response_model=JobAccepted)
    async def vm_action(vm_id: str, action: VmActionRequest, request: Request, _: dict[str, Any] = Depends(require_csrf), svc: AgentClient = Depends(get_agent)) -> JobAccepted:
        request.state.audit_action = f"vm.{action.operation.value}"
        if action.operation is Operation.FORCE_STOP and action.confirmation != vm_id:
            raise HTTPException(status_code=422, detail={"code": "confirmation_required", "message": "Confirme a parada informando o identificador exato da VM."})
        accepted = await _call(svc.vm_action, vm_id, action)
        request.state.operation_id = accepted.job_id
        return accepted

    @app.get("/api/v1/host/metrics", response_model=HostTelemetry)
    async def host_metrics(_: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent)) -> Any:
        method = getattr(svc, "host_metrics", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não fornece métricas do host.")
        result = await _call(method)
        host_alerts.observe(result.get("samples", []) if isinstance(result, dict) else getattr(result, "samples", []))
        return result

    @app.get("/api/v1/host/alerts", response_model=HostAlertList)
    async def host_alerts_list(
        include_cleared: bool = False,
        limit: int = Query(100, ge=1, le=100),
        _: dict[str, Any] = Depends(require_session),
    ) -> HostAlertList:
        return HostAlertList(items=host_alerts.list(include_cleared=include_cleared, limit=limit),
                             persistence_enabled=host_alerts.enabled, limit=limit)

    @app.post("/api/v1/host/alerts/{alert_id}/acknowledge", status_code=204)
    async def acknowledge_host_alert(
        alert_id: str,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
    ) -> Response:
        if not re.fullmatch(r"[0-9a-f]{32}", alert_id):
            raise HTTPException(status_code=404, detail={"code": "alert_not_found", "message": "Alerta não encontrado ou já encerrado."})
        if not host_alerts.acknowledge(alert_id):
            raise HTTPException(status_code=404, detail={"code": "alert_not_found", "message": "Alerta não encontrado ou já encerrado."})
        request.state.audit_action = "host.alert.acknowledge"
        request.state.audit_target = alert_id
        return Response(status_code=204)

    @app.get("/api/v1/host/metrics/history", response_model=TelemetryHistory)
    async def host_metrics_history(
        name: str = Query(..., min_length=1, max_length=96),
        resolution_seconds: int = Query(10, ge=10, le=60),
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = Query(5000, ge=1, le=5000),
        labels: str | None = Query(None, max_length=1024),
        _: dict[str, Any] = Depends(require_session),
        svc: AgentClient = Depends(get_agent),
    ) -> Any:
        return await _metrics_history_call(svc, "host_metrics_history", name, resolution_seconds, since, until, limit, labels)

    @app.get("/api/v1/host/metrics/stream")
    async def host_metrics_stream(request: Request, session: dict[str, Any] = Depends(require_session), _: AgentClient = Depends(get_agent)) -> StreamingResponse:
        token = request.cookies.get("vh_session")
        if not token:
            raise HTTPException(status_code=401, detail={"code": "authentication_required", "message": "Autentique-se para continuar."})
        subscriber_id, queue = await telemetry_stream.subscribe(token)

        async def events() -> Any:
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=10)
                    except asyncio.TimeoutError:
                        if not await session_token_is_valid(token):
                            return
                        yield ": keep-alive\n\n"
                        continue
                    if not await session_token_is_valid(token):
                        return
                    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                    yield f"event: snapshot\ndata: {encoded}\n\n"
            finally:
                await telemetry_stream.unsubscribe(subscriber_id)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-store, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.get("/api/v1/host/gpus", response_model=GpuInventoryResponse)
    async def host_gpus(_: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent)) -> Any:
        method = getattr(svc, "host_gpus", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não fornece inventário de GPU.")
        result = await _call(method)
        if isinstance(result, list):
            result = {"items": result}
        return GpuInventoryResponse.model_validate(result)

    @app.get("/api/v1/host/usb", response_model=HostUsbInventory)
    async def host_usb_inventory(_: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent)) -> HostUsbInventory:
        method = getattr(svc, "host_usb_inventory", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não fornece inventário USB.")
        return HostUsbInventory.model_validate(await _call(method))

    @app.get("/api/v1/storage", response_model=StorageInventory)
    async def storage_inventory(_: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent)) -> StorageInventory:
        method = getattr(svc, "storage_inventory", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não fornece inventário de armazenamento.")
        return StorageInventory.model_validate(await _call(method))

    @app.post("/api/v1/storage/{media_id}/convert", response_model=JobAccepted, status_code=202)
    async def convert_managed_media(
        media_id: str,
        payload: MediaConvertRequest,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> JobAccepted:
        if not re.fullmatch(r"[0-9a-f]{32}", media_id):
            raise HTTPException(status_code=422, detail={"code": "invalid_media", "message": "Identificador da mídia inválido."})
        method = getattr(svc, "convert_managed_media", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece conversão de mídias gerenciadas.")
        result = JobAccepted.model_validate(await _call(method, media_id, payload))
        request.state.audit_action = "storage.media.convert"
        request.state.operation_id = result.job_id
        return result

    @app.post("/api/v1/storage/apple-recovery", response_model=JobAccepted, status_code=202)
    async def download_apple_recovery(
        payload: AppleRecoveryDownloadRequest,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> JobAccepted:
        method = getattr(svc, "download_apple_recovery", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece downloads Apple pelo painel.")
        result = JobAccepted.model_validate(await _call(method, payload))
        request.state.audit_action = "storage.apple.download"
        request.state.operation_id = result.job_id
        return result

    @app.get("/api/v1/settings/telemetry", response_model=TelemetrySettingsView)
    async def get_telemetry_settings(
        _: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent),
    ) -> TelemetrySettingsView:
        method = getattr(svc, "get_telemetry_settings", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece configurações de telemetria.")
        return TelemetrySettingsView.model_validate(await _call(method))

    @app.put("/api/v1/settings/telemetry", response_model=TelemetrySettingsView)
    async def update_telemetry_settings(
        payload: TelemetrySettingsUpdate,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> TelemetrySettingsView:
        method = getattr(svc, "set_telemetry_settings", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não permite alterar a retenção de telemetria.")
        result = TelemetrySettingsView.model_validate(await _call(method, payload.history_retention_hours))
        request.state.audit_action = "telemetry.retention.set"
        request.state.operation_id = request.state.request_id
        return result

    @app.get("/api/v1/settings/reims/releases", response_model=ReimsReleaseList)
    async def list_reims_runtime_releases(
        _: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent),
    ) -> ReimsReleaseList:
        method = getattr(svc, "list_reims_releases", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não consulta releases do runtime Reims.")
        return ReimsReleaseList.model_validate(await _call(method))

    @app.post("/api/v1/settings/reims/releases", response_model=JobAccepted, status_code=202)
    async def update_reims_runtime_release(
        payload: ReimsReleaseUpdateRequest,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> JobAccepted:
        method = getattr(svc, "update_reims_release", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece atualização controlada do runtime Reims.")
        accepted = JobAccepted.model_validate(await _call(method, payload))
        request.state.audit_action = "reims.release.update"
        request.state.operation_id = accepted.job_id
        return accepted

    @app.get("/api/v1/settings/control/releases", response_model=ControlReleaseList)
    async def list_control_runtime_releases(
        _: dict[str, Any] = Depends(require_session),
        svc: AgentClient = Depends(get_agent),
    ) -> ControlReleaseList:
        method = getattr(svc, "list_control_releases", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não consulta releases do Control.")
        return ControlReleaseList.model_validate(await _call(method))

    @app.post("/api/v1/settings/control/releases", response_model=JobAccepted, status_code=202)
    async def update_control_runtime_release(
        payload: ControlReleaseUpdateRequest,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> JobAccepted:
        method = getattr(svc, "update_control_release", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece atualização controlada do Control.")
        accepted = JobAccepted.model_validate(await _call(method, payload))
        request.state.audit_action = "control.release.update"
        request.state.operation_id = accepted.job_id
        return accepted

    @app.post("/api/v1/transfers/{transfer_id}/import", response_model=JobAccepted, status_code=202)
    async def import_transfer(
        transfer_id: str,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> JobAccepted:
        if not re.fullmatch(r"[0-9a-f]{32}", transfer_id):
            raise HTTPException(status_code=422, detail={"code": "invalid_transfer", "message": "Identificador da transferência inválido."})
        method = getattr(svc, "import_media_transfer", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece importação para a biblioteca gerenciada.")
        result = JobAccepted.model_validate(await _call(method, transfer_id))
        request.state.audit_action = "transfer.import"
        request.state.operation_id = result.job_id
        return result

    @app.post("/api/v1/vms/{vm_id}/backups", response_model=JobAccepted, status_code=202)
    async def create_vm_backup(
        vm_id: str,
        payload: VmBackupRequest,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> JobAccepted:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", vm_id):
            raise HTTPException(status_code=422, detail={"code": "invalid_vm_id", "message": "Identificador da VM inválido."})
        method = getattr(svc, "create_vm_backup", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece backup offline de VM.")
        result = JobAccepted.model_validate(await _call(method, vm_id, payload))
        request.state.audit_action = "vm.backup"
        request.state.operation_id = result.job_id
        return result

    @app.get("/api/v1/vms/{vm_id}/backups", response_model=VmBackupListResponse)
    async def list_vm_backups(
        vm_id: str,
        _: dict[str, Any] = Depends(require_session),
        svc: AgentClient = Depends(get_agent),
    ) -> VmBackupListResponse:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", vm_id):
            raise HTTPException(status_code=422, detail={"code": "invalid_vm_id", "message": "Identificador da VM inválido."})
        method = getattr(svc, "list_vm_backups", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece o inventário de backups.")
        return VmBackupListResponse.model_validate(await _call(method, vm_id))

    @app.post("/api/v1/vms/{vm_id}/backups/{backup_id}/verify", response_model=JobAccepted, status_code=202)
    async def verify_vm_backup(
        vm_id: str,
        backup_id: str,
        payload: VmBackupRequest,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> JobAccepted:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", vm_id) or not re.fullmatch(r"[0-9a-f]{32}\.vhb", backup_id):
            raise HTTPException(status_code=422, detail={"code": "invalid_backup_id", "message": "Identificador do backup inválido."})
        method = getattr(svc, "verify_vm_backup", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece verificação de integridade de backups.")
        result = JobAccepted.model_validate(await _call(method, vm_id, backup_id, payload))
        request.state.audit_action = "vm.backup.verify"
        request.state.operation_id = result.job_id
        return result

    @app.post("/api/v1/vms/{vm_id}/backups/{backup_id}/restore", response_model=JobAccepted, status_code=202)
    async def restore_vm_backup(
        vm_id: str,
        backup_id: str,
        payload: VmBackupRestoreRequest,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> JobAccepted:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", vm_id) or not re.fullmatch(r"[0-9a-f]{32}\.vhb", backup_id):
            raise HTTPException(status_code=422, detail={"code": "invalid_backup_id", "message": "Identificador do backup inválido."})
        method = getattr(svc, "restore_vm_backup", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece restauração de backup.")
        result = JobAccepted.model_validate(await _call(method, vm_id, backup_id, payload))
        request.state.audit_action = "vm.backup.restore"
        request.state.operation_id = result.job_id
        return result

    @app.post("/api/v1/vms/{vm_id}/clone", response_model=JobAccepted, status_code=202)
    async def clone_vm(
        vm_id: str,
        payload: VmCloneRequest,
        request: Request,
        _: dict[str, Any] = Depends(require_csrf),
        svc: AgentClient = Depends(get_agent),
    ) -> JobAccepted:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", vm_id):
            raise HTTPException(status_code=422, detail={"code": "invalid_vm_id", "message": "Identificador da VM inválido."})
        method = getattr(svc, "clone_vm", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece clonagem offline de VM.")
        result = JobAccepted.model_validate(await _call(method, vm_id, payload))
        request.state.audit_action = "vm.clone"
        request.state.operation_id = result.job_id
        return result

    @app.get("/api/v1/vms/{vm_id}/runtime", response_model=VmRuntime)
    async def vm_runtime(vm_id: str, _: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent)) -> Any:
        method = getattr(svc, "get_vm_runtime", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não fornece estado de execução.")
        result = await _call(method, vm_id)
        if result is None:
            raise HTTPException(status_code=404, detail={"code": "vm_not_found", "message": "VM não encontrada."})
        return result

    @app.get("/api/v1/vms/{vm_id}/serial", response_model=VmSerialConsole)
    async def vm_serial_console(vm_id: str, _: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent)) -> Any:
        method = getattr(svc, "get_vm_serial_console", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não fornece a saída serial do Reims.")
        result = await _call(method, vm_id)
        if result is None:
            raise HTTPException(status_code=404, detail={"code": "vm_not_found", "message": "VM não encontrada."})
        return result

    @app.get("/api/v1/vms/{vm_id}/metrics", response_model=VmTelemetry)
    async def vm_metrics(vm_id: str, _: dict[str, Any] = Depends(require_session), svc: AgentClient = Depends(get_agent)) -> Any:
        method = getattr(svc, "get_vm_metrics", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não fornece métricas da VM.")
        result = await _call(method, vm_id)
        if result is None:
            raise HTTPException(status_code=404, detail={"code": "vm_not_found", "message": "VM não encontrada."})
        return result

    @app.get("/api/v1/vms/{vm_id}/metrics/stream")
    async def vm_metrics_stream(
        vm_id: str,
        request: Request,
        session: dict[str, Any] = Depends(require_session),
        svc: AgentClient = Depends(get_agent),
    ) -> StreamingResponse:
        token = request.cookies.get("vh_session")
        if not token:
            raise HTTPException(status_code=401, detail={"code": "authentication_required", "message": "Autentique-se para continuar."})
        metrics_method = getattr(svc, "get_vm_metrics", None)
        if metrics_method is None:
            raise _dependency_unavailable("O agente ainda não fornece métricas da VM.")
        config = await _call(svc.get_vm_config, vm_id)
        if config is None:
            raise HTTPException(status_code=404, detail={"code": "vm_not_found", "message": "VM não encontrada."})

        async with vm_telemetry_streams_lock:
            stream = vm_telemetry_streams.get(vm_id)
            if stream is None:
                async def collect_vm_metrics() -> Any:
                    current = app.state.agent
                    if current is None:
                        raise RuntimeError("agent unavailable")
                    current_method = getattr(current, "get_vm_metrics", None)
                    if current_method is None:
                        raise RuntimeError("telemetry unavailable")
                    result = await _call(current_method, vm_id)
                    if result is None:
                        raise RuntimeError("vm unavailable")
                    return result

                stream = _HostTelemetryStream(collect_vm_metrics, session_token_is_valid)
                vm_telemetry_streams[vm_id] = stream
            subscriber_id, queue = await stream.subscribe(token)

        async def events() -> Any:
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=10)
                    except asyncio.TimeoutError:
                        if not await session_token_is_valid(token):
                            return
                        yield ": keep-alive\n\n"
                        continue
                    if not await session_token_is_valid(token):
                        return
                    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                    yield f"event: snapshot\ndata: {encoded}\n\n"
            finally:
                await stream.unsubscribe(subscriber_id)
                async with vm_telemetry_streams_lock:
                    if not stream.subscribers and vm_telemetry_streams.get(vm_id) is stream:
                        vm_telemetry_streams.pop(vm_id, None)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-store, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.get("/api/v1/vms/{vm_id}/metrics/history", response_model=TelemetryHistory)
    async def vm_metrics_history(
        vm_id: str,
        name: str = Query(..., min_length=1, max_length=96),
        resolution_seconds: int = Query(10, ge=10, le=60),
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = Query(5000, ge=1, le=5000),
        labels: str | None = Query(None, max_length=1024),
        _: dict[str, Any] = Depends(require_session),
        svc: AgentClient = Depends(get_agent),
    ) -> Any:
        method = getattr(svc, "get_vm_metrics_history", None)
        if method is None:
            raise _dependency_unavailable("O agente ainda não oferece histórico de métricas da VM.")
        if resolution_seconds not in {10, 60}:
            raise HTTPException(status_code=422, detail={"code": "invalid_metric_resolution", "message": "A resolução deve ser 10 ou 60 segundos."})
        if (since and since.tzinfo is None) or (until and until.tzinfo is None):
            raise HTTPException(status_code=422, detail={"code": "invalid_metric_range", "message": "Os timestamps devem incluir fuso horário."})
        parsed_labels = _parse_metric_labels(labels)
        if since and until and since > until:
            raise HTTPException(status_code=422, detail={"code": "invalid_metric_range", "message": "O início do período deve ser anterior ao fim."})
        result = await _call(method, vm_id, name, parsed_labels, resolution_seconds, _iso_or_none(since), _iso_or_none(until), limit)
        if result is None:
            raise HTTPException(status_code=404, detail={"code": "vm_not_found", "message": "VM não encontrada."})
        return result

    # Runtime exception handlers return ApiError, including validation errors.
    # FastAPI's default OpenAPI document instead advertises its own validation
    # shape and omits unexpected 500 responses. Keep the published contract in
    # sync with the safe JSON envelope actually returned by this API.
    original_openapi = app.openapi

    def openapi_with_api_errors() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = original_openapi()
        api_error = {
            "description": "Erro padronizado da API.",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ApiError"},
                    "examples": {
                        "error": {
                            "summary": "Erro da solicitação",
                            "value": ApiError(
                                code="validation_error",
                                message="A solicitação contém campos inválidos.",
                                request_id="7d3a10595e933acd2f2c2d2b28a2c20e",
                                details={"errors": [{"location": ["body", "name"], "type": "missing", "message": "Field required"}]},
                            ).model_dump(mode="json"),
                        }
                    },
                }
            },
        }
        for path, path_item in schema.get("paths", {}).items():
            if not path.startswith("/api/v1/"):
                continue
            for method, operation in path_item.items():
                if method not in {"get", "post", "put", "patch", "delete", "options", "head"}:
                    continue
                responses = operation.setdefault("responses", {})
                # 422 is emitted by FastAPI for typed request validation, but
                # its runtime handler intentionally redacts inputs and returns
                # ApiError instead of HTTPValidationError.
                if "422" in responses:
                    responses["422"] = {**api_error, "description": "Solicitação inválida."}
                # The middleware catches unexpected failures and returns this
                # same safe envelope for every API route.
                responses.setdefault("500", {**api_error, "description": "Erro interno inesperado."})
        app.openapi_schema = schema
        return schema

    app.openapi = openapi_with_api_errors  # type: ignore[method-assign]
    return app


def _validate_browser_origin(request: Request) -> None:
    """Reject requests that would permit cross-site mutation or DNS rebinding."""
    host = request.headers.get("host", "")
    if not host or host != request.url.netloc:
        raise HTTPException(status_code=400, detail={"code": "invalid_host", "message": "Cabeçalho Host inválido."})
    origin = request.headers.get("origin")
    if not origin:
        raise HTTPException(status_code=403, detail={"code": "origin_required", "message": "Origem da solicitação não informada."})
    parsed = urlsplit(origin)
    if parsed.scheme not in {"https", "http"} or parsed.netloc.lower() != host.lower() or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise HTTPException(status_code=403, detail={"code": "origin_rejected", "message": "Origem da solicitação não autorizada."})


async def _call(method: Any, *args: Any) -> Any:
    """Run blocking local RPC adapters off the HTTP event loop."""
    try:
        if inspect.iscoroutinefunction(method):
            return await method(*args)
        result = await run_in_threadpool(method, *args)
        if inspect.isawaitable(result):
            return await result
        return result
    except RpcRemoteError as exc:
        mappings = {
            "invalid_request": (422, "Os parâmetros da solicitação são inválidos."),
            "invalid_vm_id": (422, "Identificador de VM inválido."),
            "invalid_job_id": (422, "Identificador de tarefa inválido."),
            "invalid_job_query": (422, "Os filtros da lista de tarefas são inválidos."),
            "invalid_backup_id": (422, "Identificador do backup inválido."),
            "invalid_idempotency_key": (422, "Chave de idempotência inválida."),
            "invalid_vm_action": (422, "A ação solicitada para a VM é inválida."),
            "confirmation_required": (422, "Confirme a ação informando o identificador exato da VM."),
            "invalid_vm_config": (422, "A configuração da VM contém opções inválidas ou não suportadas."),
            "invalid_vm_patch": (422, "A alteração de configuração da VM é inválida."),
            "gpu_selection_unavailable": (422, "A seleção de GPU ainda não é suportada pelo Reims neste appliance."),
            "job_queue_full": (503, "A fila do provisionador está cheia; tente novamente em instantes."),
            "job_not_cancellable": (409, "A tarefa já terminou e não pode ser cancelada."),
            "job_not_retryable": (409, "A tarefa não pode ser repetida neste estado."),
            "opencore_regeneration_required": (409, "A alteração exige regenerar os arquivos do OpenCore."),
            "opencore_picker_unchanged": (409, "A VM já está configurada com essa opção de picker."),
            "opencore_update_unavailable": (503, "A regeneração do OpenCore não está disponível nesta versão do Reims."),
            "network_configuration_unavailable": (422, "A configuração de rede solicitada ainda não é suportada pelo Reims."),
            "qemu_arguments_unavailable": (422, "Argumentos QEMU personalizados ainda não são suportados pelo Reims."),
            "disk_resize_unsupported": (409, "O disco provisionado não pode ser redimensionado por esta operação."),
            "catalog_revision_required": (428, "Envie a revisão atual do catálogo no cabeçalho If-Match."),
            "catalog_conflict": (409, "O catálogo mudou desde a última leitura. Recarregue as configurações."),
            "vm_already_exists": (409, "Já existe uma VM com este identificador."),
            "reims_import_unavailable": (503, "Não foi possível consultar as VMs registradas no Reims."),
            "reims_import_rejected": (422, "A VM Reims não passou pela revisão de importação."),
            "vm_running": (409, "Pare a VM antes de alterar a configuração."),
            "disk_delete_forbidden": (422, "O disco está fora do diretório gerenciado desta VM; ele foi preservado."),
            "disk_shared": (409, "Outra VM está associada a este disco; ele foi preservado."),
            "vm_not_found": (404, "VM não encontrada."),
            "vm_not_running": (409, "A VM não está em execução."),
            "runtime_state_unavailable": (503, "Não foi possível confirmar o estado atual da VM."),
            "job_not_found": (404, "Tarefa não encontrada."),
            "capability_unavailable": (503, "Esta operação ainda não está disponível neste appliance."),
            "release_catalog_unavailable": (503, "Não foi possível consultar as releases do runtime Reims."),
            "backup_inventory_unavailable": (503, "O inventário de backups está temporariamente indisponível."),
            "catalog_unavailable": (503, "O catálogo de VMs está temporariamente indisponível."),
        }
        code = exc.code if exc.code in mappings else "agent_operation_failed"
        status_code, message = mappings.get(exc.code, (503, "O agente não conseguiu concluir a operação."))
        if exc.code in {
            "invalid_request",
            "invalid_vm_config", "gpu_selection_unavailable", "network_configuration_unavailable",
            "qemu_arguments_unavailable", "disk_resize_unsupported", "catalog_revision_required",
            "catalog_conflict", "vm_already_exists", "vm_running",
            "reims_import_rejected",
            "disk_delete_forbidden",
            "disk_shared",
            "opencore_regeneration_required",
            "opencore_picker_unchanged", "opencore_update_unavailable",
        } and str(exc):
            # These are allowlisted domain-validation errors from our local
            # agent; preserve their field-specific explanation for the UI.
            message = str(exc)
        raise HTTPException(status_code=status_code, detail={"code": code, "message": message}) from None


async def _metrics_history_call(
    svc: AgentClient, method_name: str, name: str, resolution: int,
    since: datetime | None, until: datetime | None, limit: int, labels: str | None,
) -> Any:
    method = getattr(svc, method_name, None)
    if method is None:
        raise _dependency_unavailable("O agente ainda não oferece histórico de métricas.")
    if resolution not in {10, 60}:
        raise HTTPException(status_code=422, detail={"code": "invalid_metric_resolution", "message": "A resolução deve ser 10 ou 60 segundos."})
    if since and since.tzinfo is None or until and until.tzinfo is None:
        raise HTTPException(status_code=422, detail={"code": "invalid_metric_range", "message": "Os timestamps devem incluir fuso horário."})
    if since and until and since > until:
        raise HTTPException(status_code=422, detail={"code": "invalid_metric_range", "message": "O início do período deve ser anterior ao fim."})
    parsed_labels = _parse_metric_labels(labels)
    result = await _call(method, name, parsed_labels, resolution, _iso_or_none(since), _iso_or_none(until), limit)
    return result


def _parse_metric_labels(raw: str | None) -> dict[str, str]:
    if raw is None or raw == "":
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        value = None
    if not isinstance(value, dict) or len(value) > 8 or any(
        not isinstance(key, str) or not isinstance(item, str) or
        not key or len(key) > 96 or len(item) > 96 for key, item in value.items()
    ):
        raise HTTPException(status_code=422, detail={"code": "invalid_metric_labels", "message": "Os filtros de série devem ser um objeto JSON com até oito pares curtos de texto."})
    return value


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _set_session_cookies(response: Response, token: str, csrf: str, secure: bool) -> None:
    response.set_cookie("vh_session", token, max_age=12 * 60 * 60, httponly=True, secure=secure, samesite="strict", path="/")
    response.set_cookie("vh_csrf", csrf, max_age=12 * 60 * 60, httponly=False, secure=secure, samesite="strict", path="/")


def _clear_session_cookies(response: Response, secure: bool) -> None:
    response.delete_cookie("vh_session", path="/", secure=secure, httponly=True, samesite="strict")
    response.delete_cookie("vh_csrf", path="/", secure=secure, httponly=False, samesite="strict")


def _dependency_unavailable(message: str = "O agente de VMs não está conectado.") -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"code": "agent_unavailable", "message": message}, headers={"Retry-After": "3"})


def _format_etag(revision: str) -> str:
    return f'"{revision}"'


def _require_if_match(request: Request) -> str:
    value = request.headers.get("if-match", "").strip()
    if not value:
        raise HTTPException(status_code=428, detail={"code": "catalog_revision_required", "message": "Envie a revisão atual do catálogo no cabeçalho If-Match."})
    if value == "*" or value.startswith("W/") or "," in value or len(value) > 130 or not (value.startswith('"') and value.endswith('"')):
        raise HTTPException(status_code=400, detail={"code": "invalid_if_match", "message": "If-Match deve conter uma única revisão ETag forte."})
    revision = value[1:-1]
    if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision.lower()):
        raise HTTPException(status_code=400, detail={"code": "invalid_if_match", "message": "A revisão ETag tem formato inválido."})
    return value


app = create_app()
