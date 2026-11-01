"""Bounded, versioned local RPC transport over Unix-domain sockets.

The wire protocol intentionally exposes only registered method names. It is
not a shell or QMP tunnel. Each call uses a fresh connection, is limited to one
newline-delimited JSON request and one response, and authenticates the peer
using Linux ``SO_PEERCRED``.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


PROTOCOL_VERSION = 1
DEFAULT_MAX_MESSAGE_BYTES = 1_048_576


class RpcError(RuntimeError):
    """Transport or wire-protocol failure."""


class RpcRemoteError(RpcError):
    """The peer returned a structured application error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int


def _peer_credentials(sock: socket.socket) -> PeerCredentials:
    if not hasattr(socket, "SO_PEERCRED"):
        raise RpcError("SO_PEERCRED indisponível neste sistema")
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def _read_line(sock: socket.socket, maximum: int) -> bytes:
    data = bytearray()
    while len(data) <= maximum:
        chunk = sock.recv(min(4096, maximum + 1 - len(data)))
        if not chunk:
            break
        newline = chunk.find(b"\n")
        if newline >= 0:
            data.extend(chunk[:newline])
            if len(data) > maximum:
                raise RpcError("Mensagem RPC excede o limite configurado")
            return bytes(data)
        data.extend(chunk)
    if len(data) > maximum:
        raise RpcError("Mensagem RPC excede o limite configurado")
    if not data:
        raise RpcError("Conexão RPC encerrada antes da mensagem")
    raise RpcError("Mensagem RPC sem delimitador de linha")


def _write_message(sock: socket.socket, value: Mapping[str, Any], maximum: int) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RpcError(f"Resposta RPC não serializável: {exc}") from exc
    if len(encoded) > maximum:
        raise RpcError("Mensagem RPC excede o limite configurado")
    sock.sendall(encoded + b"\n")


class UnixRpcClient:
    """Client for the fixed-method local agent protocol."""

    def __init__(self, socket_path: str | os.PathLike[str], *, timeout: float = 3.0,
                 max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> None:
        if timeout <= 0 or max_message_bytes <= 0:
            raise ValueError("timeout e limite de mensagem devem ser positivos")
        self.socket_path = str(socket_path)
        self.timeout = timeout
        self.max_message_bytes = max_message_bytes

    def call(self, method: str, payload: Mapping[str, Any] | None = None) -> Any:
        if not method or len(method) > 128:
            raise ValueError("método RPC inválido")
        request_id = uuid.uuid4().hex
        request = {
            "version": PROTOCOL_VERSION,
            "id": request_id,
            "method": method,
            "payload": dict(payload or {}),
        }
        try:
            encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"payload RPC não serializável: {exc}") from exc
        if len(encoded) > self.max_message_bytes:
            raise RpcError("Requisição RPC excede o limite configurado")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout)
            try:
                client.connect(self.socket_path)
                client.sendall(encoded + b"\n")
                raw = _read_line(client, self.max_message_bytes)
            except (OSError, TimeoutError) as exc:
                raise RpcError(f"Falha na chamada RPC local: {type(exc).__name__}") from exc
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RpcError("Resposta RPC inválida") from exc
        if not isinstance(response, dict):
            raise RpcError("Resposta RPC precisa ser um objeto")
        if response.get("version") != PROTOCOL_VERSION or response.get("id") != request_id:
            raise RpcError("Versão ou id divergente na resposta RPC")
        if response.get("ok") is True:
            return response.get("result")
        error = response.get("error")
        if not isinstance(error, dict):
            raise RpcError("Resposta RPC sem resultado ou erro válido")
        raise RpcRemoteError(str(error.get("code", "remote_error")), str(error.get("message", "Falha remota")))


class UnixRpcServer:
    """Bounded threaded server with a strict handler registry and peer UID gate."""

    def __init__(self, socket_path: str | os.PathLike[str], handlers: Mapping[str, Callable[[dict[str, Any]], Any]],
                 *, allowed_uids: set[int], socket_gid: int | None = None, socket_mode: int = 0o660,
                 timeout: float = 3.0, max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
                 max_clients: int = 16, backlog: int = 16) -> None:
        if not handlers or not allowed_uids or timeout <= 0 or max_message_bytes <= 0 or max_clients < 1:
            raise ValueError("handlers, allowed_uids e limites positivos são obrigatórios")
        if socket_mode & ~0o777:
            raise ValueError("modo de socket inválido")
        self.socket_path = Path(socket_path)
        if len(os.fsencode(self.socket_path)) >= 104:
            raise ValueError("caminho do socket Unix muito longo")
        self.handlers = dict(handlers)
        self.allowed_uids = frozenset(int(uid) for uid in allowed_uids)
        self.socket_gid = socket_gid
        self.socket_mode = socket_mode
        self.timeout = timeout
        self.max_message_bytes = max_message_bytes
        self.backlog = backlog
        self._slots = threading.BoundedSemaphore(max_clients)
        self._executor = ThreadPoolExecutor(max_workers=max_clients, thread_name_prefix="vh-web-rpc")
        self._listener: socket.socket | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._listener is not None:
            return
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(info.st_mode):
                raise RpcError("O destino do socket RPC existe e não é socket")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.2)
                probe.connect(str(self.socket_path))
            except OSError:
                pass
            else:
                raise RpcError("Já existe um agente escutando neste socket")
            finally:
                probe.close()
            self.socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, self.socket_mode)
            if self.socket_gid is not None:
                os.chown(self.socket_path, -1, self.socket_gid)
            listener.listen(self.backlog)
            listener.settimeout(0.25)
        except Exception:
            listener.close()
            self.socket_path.unlink(missing_ok=True)
            raise
        self._listener = listener
        self._stop.clear()

    def serve_forever(self) -> None:
        if self._listener is None:
            self.start()
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            if not self._slots.acquire(blocking=False):
                client.close()
                continue
            self._executor.submit(self._serve_client, client)

    def _serve_client(self, client: socket.socket) -> None:
        with client:
            client.settimeout(self.timeout)
            request_id = ""
            try:
                raw = _read_line(client, self.max_message_bytes)
                request = json.loads(raw.decode("utf-8"))
                if not isinstance(request, dict):
                    raise RpcError("Requisição RPC precisa ser um objeto")
                request_id = request.get("id") if isinstance(request.get("id"), str) else ""
                peer = _peer_credentials(client)
                if peer.uid not in self.allowed_uids:
                    raise RpcRemoteError("peer_denied", "Credencial do processo não autorizada")
                if request.get("version") != PROTOCOL_VERSION:
                    raise RpcRemoteError("version_mismatch", "Versão do protocolo RPC incompatível")
                if not request_id or len(request_id) > 128:
                    raise RpcError("id de requisição RPC inválido")
                method = request.get("method")
                if not isinstance(method, str) or method not in self.handlers:
                    raise RpcRemoteError("unknown_method", "Método RPC não permitido")
                payload = request.get("payload", {})
                if not isinstance(payload, dict):
                    raise RpcRemoteError("invalid_payload", "Payload RPC precisa ser um objeto")
                result = self.handlers[method](payload)
                response = {"version": PROTOCOL_VERSION, "id": request_id, "ok": True, "result": result}
            except RpcRemoteError as exc:
                response = {"version": PROTOCOL_VERSION, "id": request_id, "ok": False,
                            "error": {"code": exc.code, "message": str(exc)}}
            except (RpcError, UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError) as exc:
                response = {"version": PROTOCOL_VERSION, "id": request_id, "ok": False,
                            "error": {"code": "invalid_request", "message": str(exc)}}
            except Exception:
                response = {"version": PROTOCOL_VERSION, "id": request_id, "ok": False,
                            "error": {"code": "handler_failed", "message": "A operação local falhou"}}
            try:
                _write_message(client, response, self.max_message_bytes)
            except (OSError, RpcError):
                pass
            finally:
                self._slots.release()

    def close(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        self._executor.shutdown(wait=True, cancel_futures=True)
        try:
            info = self.socket_path.lstat()
            if stat.S_ISSOCK(info.st_mode):
                self.socket_path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "UnixRpcServer":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
