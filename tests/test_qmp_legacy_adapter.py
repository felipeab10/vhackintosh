"""Integration tests for legacy QMP APIs backed by the process-wide broker."""
from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from core import passthrough
from core.state_machine import QMPClient


class FakeQmpServer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen()
        self.server.settimeout(0.1)
        self.accepted = 0
        self.commands: list[str] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.accepted += 1
            worker = threading.Thread(target=self._client, args=(conn,), daemon=True)
            worker.start()

    def _client(self, conn: socket.socket) -> None:
        with conn:
            conn.sendall(b'{"QMP":{"version":{},"capabilities":[]}}\r\n')
            buf = b""
            while not self.stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line:
                        continue
                    request = json.loads(line)
                    name = request["execute"]
                    self.commands.append(name)
                    if name == "query-status":
                        conn.sendall(b'{"event":"RESET","data":{}}\r\n')
                    if name == "fail":
                        response = {"error": {"class": "GenericError", "desc": "device is busy"}}
                    elif name == "human-monitor-command":
                        response = {"return": "USB Device, ID: usbhost-1-2"}
                    else:
                        response = {"return": {"accepted": True}}
                    if "id" in request:
                        response["id"] = request["id"]
                    conn.sendall(json.dumps(response).encode() + b"\r\n")

    def close(self) -> None:
        self.stop.set()
        self.server.close()
        self.thread.join(timeout=1)


class SharedBrokerAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "qmp.sock"
        self.server = FakeQmpServer(self.path)

    def tearDown(self) -> None:
        passthrough.close_qmp_broker(self.path)
        self.server.close()
        self.temp.cleanup()

    def test_legacy_clients_share_one_socket_and_return_compatible_values(self) -> None:
        client = passthrough.QmpClient(self.path)
        self.assertIsNone(client.execute("query-status"))  # connect still required
        client.connect()
        self.assertEqual(client.command("query-status"), {"accepted": True})
        self.assertEqual(passthrough.attached_ids(self.path), {"usbhost-1-2"})

        state_client = QMPClient(self.path)
        self.assertTrue(state_client.connect(timeout_sec=0.2))
        self.assertEqual(state_client.execute("query-status"), {"return": {"accepted": True}})
        state_client.close()
        client.close()

        # qmp_capabilities and every command flowed through one reader/socket.
        self.assertEqual(self.server.accepted, 1)
        self.assertEqual(self.server.commands.count("qmp_capabilities"), 1)

    def test_errors_are_preserved_and_close_is_only_a_handle_release(self) -> None:
        client = passthrough.QmpClient(self.path)
        client.connect()
        with self.assertRaisesRegex(passthrough.QmpError, "device is busy"):
            client.command("fail")
        client.close()
        self.assertTrue(passthrough._broker_for(self.path, 1).connected)
        self.assertEqual(self.server.accepted, 1)

    def test_event_queue_is_exposed_by_the_single_broker_reader(self) -> None:
        client = passthrough.QmpClient(self.path)
        client.connect()
        client.command("query-status")
        self.assertEqual(client.get_event(timeout=0.2)["event"], "RESET")
        self.assertEqual(self.server.accepted, 1)


if __name__ == "__main__":
    unittest.main()
