"""Focused socketpair tests for the persistent QMP transport."""

from __future__ import annotations

import json
import socket
import threading

import pytest

from core.qmp import QmpBroker, QmpError


class FakeMonitor:
    def __init__(self) -> None:
        self.client, self.server = socket.socketpair()
        self.broker = QmpBroker(
            "fake.sock",
            timeout=0.3,
            max_message_bytes=256,
            event_queue_size=2,
            socket_factory=lambda *_args: _SocketAdapter(self.client),
        )
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _send(self, message: dict) -> None:
        self.server.sendall(json.dumps(message).encode() + b"\r\n")

    def _serve(self) -> None:
        # Deliberately fragment the greeting to exercise newline framing.
        raw = b'{"QMP":{"version":{"qemu":{"major":8}},"capabilities":[]}}\r\n'
        self.server.sendall(raw[:13])
        self.server.sendall(raw[13:])
        stream = self.server.makefile("rb")
        try:
            for line in stream:
                command = json.loads(line)
                if command["execute"] == "qmp_capabilities":
                    self._send({"return": {}, "id": command["id"]})
                elif command["execute"] == "query-status":
                    self._send({"event": "RESET", "data": {}})
                    self._send({"return": {"running": True}, "id": command["id"]})
                elif command["execute"] == "query-error":
                    self._send({"error": {"class": "GenericError", "desc": "expected"}, "id": command["id"]})
                elif command["execute"] == "query-slow":
                    continue
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        self.broker.close()
        self.server.close()
        self.thread.join(timeout=0.5)


class _SocketAdapter:
    """Make a socketpair endpoint satisfy the AF_UNIX connect interface."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock

    def settimeout(self, timeout: float | None) -> None:
        self.sock.settimeout(timeout)

    def connect(self, _path: str) -> None:
        return

    def recv(self, size: int) -> bytes:
        return self.sock.recv(size)

    def sendall(self, data: bytes) -> None:
        self.sock.sendall(data)

    def shutdown(self, how: int) -> None:
        self.sock.shutdown(how)

    def close(self) -> None:
        self.sock.close()


def test_handshake_correlates_reply_and_preserves_interleaved_event() -> None:
    fake = FakeMonitor()
    try:
        fake.broker.connect()
        assert fake.broker.connected
        assert fake.broker.command("query-status") == {"running": True}
        assert fake.broker.get_event(timeout=0.2)["event"] == "RESET"
    finally:
        fake.close()


def test_qmp_errors_are_reported_and_timeout_does_not_retry() -> None:
    fake = FakeMonitor()
    try:
        fake.broker.connect()
        with pytest.raises(QmpError, match="expected"):
            fake.broker.command("query-error")
        with pytest.raises(QmpError, match="Timeout"):
            fake.broker.command("query-slow", timeout=0.03)
        assert fake.broker.connected
    finally:
        fake.close()


def test_event_queue_is_bounded_and_disconnect_requires_explicit_reconnect() -> None:
    fake = FakeMonitor()
    try:
        fake.broker.connect()
        fake.broker._enqueue_event({"event": "A"})
        fake.broker._enqueue_event({"event": "B"})
        fake.broker._enqueue_event({"event": "C"})
        assert fake.broker.dropped_events == 1
        assert fake.broker.get_event(timeout=0.1)["event"] == "B"
        fake.broker.close()
        with pytest.raises(QmpError, match="desconectado"):
            fake.broker.command("query-status")
    finally:
        fake.close()


def test_rejects_oversized_message() -> None:
    local, remote = socket.socketpair()
    broker = QmpBroker("fake.sock", max_message_bytes=8)
    try:
        remote.sendall(b'{"long":"this is too large"}')
        with pytest.raises(QmpError, match="excede"):
            broker._read_message(local)
    finally:
        local.close()
        remote.close()
