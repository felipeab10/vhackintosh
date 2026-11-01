"""Local RPC protocol, peer credential, and size-limit tests."""

import json
import os
import socket
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from core.web_rpc import (
    PROTOCOL_VERSION,
    RpcError,
    RpcRemoteError,
    UnixRpcClient,
    UnixRpcServer,
)


class UnixRpcTests(unittest.TestCase):
    def test_registered_method_round_trips_and_server_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.sock"
            server = UnixRpcServer(
                path,
                {"sum": lambda data: {"value": data["left"] + data["right"]}},
                allowed_uids={os.getuid()},
                max_clients=2,
            )
            server.start()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = UnixRpcClient(path)
                self.assertEqual(client.call("sum", {"left": 4, "right": 7}), {"value": 11})
                with self.assertRaises(RpcRemoteError) as error:
                    client.call("arbitrary-shell", {"command": "id"})
                self.assertEqual(error.exception.code, "unknown_method")
            finally:
                server.close()
                thread.join(timeout=1)
                self.assertFalse(thread.is_alive())
                self.assertFalse(path.exists())

    def test_peer_uid_is_checked_before_handler_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.sock"
            calls = []
            server = UnixRpcServer(
                path,
                {"read": lambda _data: calls.append(True)},
                allowed_uids={os.getuid() + 1_000_000},
            )
            server.start()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaises(RpcRemoteError) as error:
                    UnixRpcClient(path).call("read")
                self.assertEqual(error.exception.code, "peer_denied")
                self.assertEqual(calls, [])
            finally:
                server.close()
                thread.join(timeout=1)

    def test_oversized_and_invalid_protocol_requests_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.sock"
            server = UnixRpcServer(
                path,
                {"read": lambda _data: "never"},
                allowed_uids={os.getuid()},
                max_message_bytes=256,
            )
            server.start()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                oversized = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                oversized.settimeout(1)
                oversized.connect(str(path))
                oversized.sendall(b"{" + b" " * 512 + b"}\n")
                response = json.loads(oversized.recv(1024).split(b"\n", 1)[0])
                self.assertFalse(response["ok"])
                oversized.close()

                invalid = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                invalid.settimeout(1)
                invalid.connect(str(path))
                invalid.sendall(json.dumps({"version": PROTOCOL_VERSION + 1, "id": uuid.uuid4().hex,
                                            "method": "read", "payload": {}}).encode() + b"\n")
                response = json.loads(invalid.recv(1024).split(b"\n", 1)[0])
                self.assertEqual(response["error"]["code"], "version_mismatch")
                invalid.close()
            finally:
                server.close()
                thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
