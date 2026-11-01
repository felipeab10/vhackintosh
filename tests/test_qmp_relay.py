from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.qmp.relay import QmpControlClient, QmpControlRelay, qmp_control_socket
from core.web_rpc import RpcRemoteError


class FakeQmp:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def command(self, name: str):
        self.commands.append(name)
        if name == "query-status":
            return {"running": True}
        if name == "query-block":
            return [{"device": "disk0"}]
        if name == "query-memory-size-summary":
            return {"base-memory": 8192}
        return {}


class QmpRelayTests(unittest.TestCase):
    def test_client_uses_restricted_relay_without_opening_a_second_qmp_connection(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"VHACKINTOSH_VM_CONTROL_BASE": directory}
        ):
            backend = FakeQmp()
            relay = QmpControlRelay(backend, qmp_control_socket("vm-1"))
            relay.start()
            try:
                client = QmpControlClient("vm-1")
                self.assertEqual(client.command("query-status"), {"running": True})
                self.assertEqual(client.command("query-block"), [{"device": "disk0"}])
                self.assertEqual(client.command("query-memory-size-summary"), {"base-memory": 8192})
                self.assertEqual(client.command("system_powerdown"), {})
                client.close()

                self.assertEqual(
                    backend.commands,
                    ["query-status", "query-block", "query-memory-size-summary", "system_powerdown"],
                )
                self.assertEqual(relay.socket_path.stat().st_mode & 0o777, 0o660)
                with self.assertRaises(ValueError):
                    client.command("device_add", driver="usb-host")
                with self.assertRaises(ValueError):
                    client.command("human-monitor-command")
            finally:
                relay.close()
            self.assertFalse(relay.socket_path.exists())

    def test_relay_rejects_rpc_payload_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeQmp()
            relay = QmpControlRelay(backend, Path(directory) / "q.sock")
            relay.start()
            try:
                client = QmpControlClient("vm-1")
                # A raw RPC caller cannot smuggle QMP arguments through a fixed method.
                with patch("core.qmp.relay.qmp_control_socket", return_value=relay.socket_path):
                    from core.web_rpc import UnixRpcClient

                    raw_client = UnixRpcClient(relay.socket_path)
                    with self.assertRaises(RpcRemoteError):
                        raw_client.call("qmp.status", {"command": "quit"})
                self.assertEqual(backend.commands, [])
            finally:
                relay.close()


if __name__ == "__main__":
    unittest.main()
