"""Power monitoring consumes events from the process-wide QMP broker."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import passthrough
from core.power import QMPPowerMonitor
from core.qmp.relay import QmpControlClient


class EventQmpServer:
    def __init__(self, path: Path, event: dict):
        self.path = path
        self.event = event
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(1)
        self.listener.settimeout(0.1)
        self.stop = threading.Event()
        self.connection: socket.socket | None = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            self.connection, _ = self.listener.accept()
            self.connection.sendall(b'{"QMP":{"version":{},"capabilities":[]}}\r\n')
            buffer = b""
            while not self.stop.is_set():
                data = self.connection.recv(4096)
                if not data:
                    return
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line:
                        continue
                    request = json.loads(line)
                    if request["execute"] == "qmp_capabilities":
                        self.connection.sendall(json.dumps(self.event).encode() + b"\r\n")
                    self.connection.sendall(json.dumps({"return": {}, "id": request["id"]}).encode() + b"\r\n")
        except (OSError, TimeoutError):
            pass

    def close(self):
        self.stop.set()
        if self.connection:
            self.connection.close()
        self.listener.close()
        self.thread.join(timeout=1)


class PowerQmpBrokerTests(unittest.TestCase):
    def test_power_monitor_gets_guest_reset_without_opening_a_second_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qmp.sock"
            control_path = Path(directory) / "control.sock"
            server = EventQmpServer(path, {"event": "RESET", "data": {}})
            monitor = QMPPowerMonitor(
                sock_path=str(path), discovery_timeout_sec=1, control_socket_path=str(control_path)
            )
            try:
                monitor.start()
                deadline = time.monotonic() + 2
                while monitor.reset_count == 0 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(monitor.reset_count, 1)
                self.assertEqual(monitor.last_reason, "guest-reset")
                self.assertEqual(server.listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN), 1)
                self.assertIsNotNone(passthrough._BROKERS.get(str(path)))
                while not control_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(control_path.exists())
                with patch("core.qmp.relay.qmp_control_socket", return_value=control_path):
                    self.assertEqual(QmpControlClient("vm-1").command("query-status"), {})
            finally:
                monitor.stop()
                server.close()

    def test_shutdown_event_preserves_legacy_reason_policy(self):
        monitor = QMPPowerMonitor()
        monitor._handle_event({"event": "SHUTDOWN", "data": {"reason": "guest-shutdown"}})
        self.assertEqual(monitor.last_reason, "guest-shutdown")
        self.assertEqual(monitor.shutdown_count, 1)
        monitor._handle_event({"event": "SHUTDOWN", "data": {"reason": "guest-reset"}})
        self.assertEqual(monitor.last_reason, "guest-reset")
        self.assertEqual(monitor.reset_count, 1)


if __name__ == "__main__":
    unittest.main()
