"""Restricted local QMP relay owned by the VM supervisor process."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

from core.web_rpc import UnixRpcClient, UnixRpcServer


_VM_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_COMMANDS = {
    "qmp.status": "query-status",
    "qmp.shutdown": "system_powerdown",
    "qmp.quit": "quit",
    "qmp.block": "query-block",
    "qmp.memory": "query-memory-size-summary",
}


def qmp_control_socket(vm_id: str) -> Path:
    """Resolve the short, per-VM control socket shared by supervisor and agent."""
    if not isinstance(vm_id, str) or not _VM_ID_RE.fullmatch(vm_id):
        raise ValueError("invalid VM ID for QMP control socket")
    explicit = os.environ.get("VHACKINTOSH_VM_CONTROL_BASE")
    if explicit:
        base = Path(explicit).expanduser()
    elif os.geteuid() == 0:
        base = Path("/run")
    else:
        base = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    return base / f"vh-{vm_id}" / "q.sock"


class QmpControlRelay:
    """Expose a fixed allowlist while retaining one QMP connection per VM."""

    def __init__(self, client: Any, socket_path: str | Path, *, allowed_uids: set[int] | None = None):
        self.client = client
        self.socket_path = Path(socket_path)
        uid = os.getuid()
        self.server = UnixRpcServer(
            self.socket_path,
            {name: self._handler(name) for name in _COMMANDS},
            allowed_uids=allowed_uids or {uid},
            socket_gid=os.getgid(),
            socket_mode=0o660,
            timeout=5.0,
            max_clients=8,
            backlog=8,
        )
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.server.start()
        self._thread = threading.Thread(
            target=self.server.serve_forever,
            name=f"qmp-relay-{self.socket_path.parent.name}",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self.server.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def _handler(self, method: str):
        command = _COMMANDS[method]

        def execute(payload: dict[str, Any]) -> Any:
            if payload:
                raise ValueError("QMP relay methods do not accept arguments")
            result = self.client.command(command)
            return result if result is not None else {"accepted": True}

        return execute


class QmpControlClient:
    """QmpClient-compatible adapter backed by the supervisor's QMP connection."""

    def __init__(self, vm_id: str, *, timeout: float = 3.0):
        self.vm_id = vm_id
        self.timeout = timeout
        self.rpc = UnixRpcClient(qmp_control_socket(vm_id), timeout=timeout)

    def command(self, name: str, **arguments: Any) -> Any:
        if arguments:
            raise ValueError("QMP relay methods do not accept arguments")
        method = next((rpc for rpc, qmp in _COMMANDS.items() if qmp == name), None)
        if method is None:
            raise ValueError("QMP command is not exposed by the local relay")
        return self.rpc.call(method, {})

    def close(self) -> None:
        # Each relay call owns a short-lived Unix connection; the QMP client
        # remains owned by the supervisor until the VM process exits.
        return None
