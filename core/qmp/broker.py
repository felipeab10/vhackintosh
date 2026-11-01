"""Threaded QMP broker with one persistent connection per VM.

The reader thread is the sole socket reader. Commands are correlated by QMP
IDs, so replies and asynchronous events can arrive interleaved. A failed
connection is never reconnected implicitly: callers choose when it is safe to
call ``reconnect()`` (for example, after confirming the VM process is alive).
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class QmpError(Exception):
    """Transport, protocol, or command failure from a QMP monitor."""


@dataclass
class _Pending:
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: QmpError | None = None


class QmpBroker:
    """Own a persistent QMP monitor connection for one VM.

    ``connect`` performs greeting and capability negotiation. ``command``
    returns the QMP ``return`` value. Events are available through
    ``get_event``. Multiple command callers are supported concurrently.

    ``timeout`` applies to connect/handshake and is the default command wait;
    every JSON line is limited by ``max_message_bytes`` and the event queue is
    bounded by ``event_queue_size`` (oldest events are dropped on overflow).
    Reconnect is explicit and should only be called after the VM is known to
    still exist. No command is automatically retried because QMP cannot tell
    whether a timed-out command took effect.
    """

    def __init__(
        self,
        sock_path: str | Path,
        *,
        timeout: float = 5.0,
        max_message_bytes: int = 1_048_576,
        event_queue_size: int = 256,
        socket_factory: Callable[..., socket.socket] = socket.socket,
    ) -> None:
        if timeout <= 0 or max_message_bytes <= 0 or event_queue_size <= 0:
            raise ValueError("timeout, max_message_bytes e event_queue_size devem ser positivos")
        self.sock_path = str(sock_path)
        self.timeout = timeout
        self.max_message_bytes = max_message_bytes
        self._socket_factory = socket_factory
        self._socket: socket.socket | None = None
        self._buffer = bytearray()
        self._send_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 1
        self._reader: threading.Thread | None = None
        self._connected = False
        self._closed = False
        self._events: queue.Queue[dict[str, Any]] = queue.Queue(event_queue_size)
        self.dropped_events = 0

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._connected

    def connect(self) -> None:
        """Connect once and negotiate QMP capabilities; idempotent if live."""
        with self._state_lock:
            if self._connected:
                return
            self._closed = False
            self._buffer.clear()
            sock = self._socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            try:
                sock.connect(self.sock_path)
                greeting = self._read_handshake_message(sock)
                if not isinstance(greeting.get("QMP"), dict):
                    raise QmpError(f"Greeting QMP inválido em {self.sock_path}")
                sock.settimeout(None)
                self._socket = sock
                self._connected = True
                self._reader = threading.Thread(
                    target=self._reader_loop,
                    name=f"qmp-reader-{Path(self.sock_path).name}",
                    daemon=True,
                )
                self._reader.start()
            except Exception as exc:
                self._connected = False
                self._socket = None
                try:
                    sock.close()
                except OSError:
                    pass
                if isinstance(exc, QmpError):
                    raise
                raise QmpError(f"Falha ao conectar ao QMP em {self.sock_path}: {exc}") from exc
        try:
            self.command("qmp_capabilities", timeout=self.timeout)
        except Exception:
            self.close()
            raise

    def reconnect(self) -> None:
        """Explicitly replace a stale connection and renegotiate capabilities."""
        self.close()
        self.connect()

    def close(self) -> None:
        """Close the connection and fail outstanding commands; safe repeatedly."""
        with self._state_lock:
            self._closed = True
            self._connected = False
            sock, self._socket = self._socket, None
            reader = self._reader
            self._reader = None
            self._buffer.clear()
            pending, self._pending = self._pending, {}
            for item in pending.values():
                item.error = QmpError("Conexão QMP encerrada")
                item.done.set()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=min(self.timeout, 1.0))

    def command(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Execute a named QMP command and return its ``return`` payload."""
        if not name:
            raise ValueError("name do comando QMP não pode ser vazio")
        wait_for = self.timeout if timeout is None else timeout
        if wait_for <= 0:
            raise ValueError("timeout deve ser positivo")
        with self._state_lock:
            if not self._connected or self._socket is None:
                raise QmpError(f"Broker QMP desconectado de {self.sock_path}; chame connect()")
            command_id = self._next_id
            self._next_id += 1
            pending = _Pending()
            self._pending[command_id] = pending
            sock = self._socket
        payload: dict[str, Any] = {"execute": name, "id": command_id}
        if arguments is not None:
            if not isinstance(arguments, dict):
                with self._state_lock:
                    self._pending.pop(command_id, None)
                raise TypeError("arguments do comando QMP precisa ser um dict")
            payload["arguments"] = arguments
        try:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"
        except (TypeError, ValueError) as exc:
            with self._state_lock:
                self._pending.pop(command_id, None)
            raise QmpError(f"Comando QMP não serializável: {exc}") from exc
        if len(data) - 2 > self.max_message_bytes:
            with self._state_lock:
                self._pending.pop(command_id, None)
            raise QmpError(f"Comando QMP excede {self.max_message_bytes} bytes")
        try:
            with self._send_lock:
                sock.sendall(data)
        except OSError as exc:
            self._fail_connection(QmpError(f"Falha ao enviar comando QMP: {exc}"))
        if not pending.done.wait(wait_for):
            with self._state_lock:
                self._pending.pop(command_id, None)
            raise QmpError(f"Timeout de {wait_for:g}s aguardando '{name}' em {self.sock_path}")
        if pending.error is not None:
            raise pending.error
        response = pending.result or {}
        if "error" in response:
            error = response["error"]
            if isinstance(error, dict):
                raise QmpError(str(error.get("desc") or error.get("class") or error))
            raise QmpError(str(error))
        return response.get("return")

    def get_event(self, timeout: float | None = None) -> dict[str, Any]:
        """Return the next event, or raise ``queue.Empty`` when timeout expires."""
        return self._events.get(timeout=timeout)

    def _read_handshake_message(self, sock: socket.socket) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        for _ in range(512):
            message = self._read_message(sock, deadline)
            if "event" not in message:
                return message
            self._enqueue_event(message)
        raise QmpError("QMP enviou eventos demais antes do greeting")

    def _read_message(self, sock: socket.socket, deadline: float | None = None) -> dict[str, Any]:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                if newline > self.max_message_bytes:
                    raise QmpError(f"Mensagem QMP excede {self.max_message_bytes} bytes")
                line = bytes(self._buffer[:newline]).strip()
                del self._buffer[: newline + 1]
                if not line:
                    continue
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise QmpError(f"Mensagem JSON inválida do QMP: {exc}") from exc
                if not isinstance(message, dict):
                    raise QmpError("Mensagem QMP precisa ser um objeto JSON")
                return message
            if len(self._buffer) > self.max_message_bytes:
                raise QmpError(f"Mensagem QMP excede {self.max_message_bytes} bytes")
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise QmpError("Timeout aguardando greeting QMP")
                sock.settimeout(remaining)
            try:
                chunk = sock.recv(min(65536, self.max_message_bytes + 1))
            except socket.timeout as exc:
                raise QmpError("Timeout lendo mensagem QMP") from exc
            except OSError as exc:
                raise QmpError(f"Falha lendo QMP: {exc}") from exc
            if not chunk:
                raise QmpError("Monitor QMP fechou a conexão")
            self._buffer.extend(chunk)
            if len(self._buffer) > self.max_message_bytes and b"\n" not in self._buffer:
                raise QmpError(f"Mensagem QMP excede {self.max_message_bytes} bytes")

    def _reader_loop(self) -> None:
        try:
            while True:
                with self._state_lock:
                    sock = self._socket
                    if not self._connected or sock is None:
                        return
                message = self._read_message(sock)
                if "event" in message:
                    self._enqueue_event(message)
                    continue
                if "id" not in message:
                    continue
                try:
                    command_id = int(message["id"])
                except (ValueError, TypeError):
                    continue
                with self._state_lock:
                    pending = self._pending.pop(command_id, None)
                if pending is not None:
                    pending.result = message
                    pending.done.set()
        except QmpError as exc:
            self._fail_connection(exc)

    def _enqueue_event(self, event: dict[str, Any]) -> None:
        try:
            self._events.put_nowait(event)
        except queue.Full:
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
            self.dropped_events += 1
            self._events.put_nowait(event)

    def _fail_connection(self, error: QmpError) -> None:
        with self._state_lock:
            self._connected = False
            sock, self._socket = self._socket, None
            pending, self._pending = self._pending, {}
            for item in pending.values():
                item.error = error
                item.done.set()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
