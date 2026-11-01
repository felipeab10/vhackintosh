"""Run the loopback development dashboard against this Linux user's real vHackintosh catalog."""

from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path

import uvicorn

from core.config import VMS_FILE
from core.web_agent import WebAgentService
from core.web_rpc import UnixRpcClient, UnixRpcServer
from core.web_vm_units import SystemdVmUnitManager
from web.api.agent import RpcAgentClient
from web.api.app import ActivationCode, create_app


def main() -> None:
    home = Path.home()
    state_root = Path(os.environ.get("XDG_STATE_HOME", str(home / ".local" / "state"))) / "vhackintosh-web-dev"
    runtime_root = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "vhackintosh-web-dev"
    catalog = Path(os.environ.get("VHACKINTOSH_WEB_DEV_CATALOG", str(VMS_FILE))).expanduser()
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_root.chmod(0o700)
    runtime_root.chmod(0o700)

    jobs_db = state_root / "jobs.sqlite"
    transfer_root = state_root / "transfers"
    transfer_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    service = WebAgentService(
        catalog,
        jobs_db_path=jobs_db,
        telemetry_history_path=state_root / "telemetry.sqlite",
        vm_unit_manager=SystemdVmUnitManager(scope="user", timeout=2.0),
        transfer_root=transfer_root,
    )
    service.start_jobs()

    socket_path = runtime_root / "agent.sock"
    rpc_server = UnixRpcServer(
        socket_path,
        service.handlers(),
        allowed_uids={os.getuid()},
        socket_gid=os.getgid(),
        socket_mode=0o600,
    )
    rpc_server.start()
    rpc_thread = threading.Thread(target=rpc_server.serve_forever, name="vhackintosh-dev-rpc", daemon=True)
    rpc_thread.start()

    activation_code = os.environ.get("VHACKINTOSH_WEB_DEV_ACTIVATION_CODE") or secrets.token_urlsafe(12)
    app = create_app(
        agent=RpcAgentClient(UnixRpcClient(socket_path, timeout=5.0)),
        activation_code=ActivationCode(activation_code, expires_at=time.time() + 600),
        auth_db_path=state_root / "auth.sqlite",
        audit_db_path=state_root / "audit.sqlite",
        transfer_directory=transfer_root,
        cookie_secure=False,
        web_port=5173,
    )
    print(f"vHackintosh dev API: http://127.0.0.1:8765; catalog: {catalog}", flush=True)
    print(f"First-user activation code (valid 10 minutes): {activation_code}", flush=True)
    print("VM lifecycle uses the current user's systemd manager and real Reims/QEMU installation.", flush=True)
    try:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=8765,
            access_log=False,
            server_header=False,
            timeout_graceful_shutdown=5,
        )
    finally:
        rpc_server.close()
        service.close_jobs()


if __name__ == "__main__":
    main()
