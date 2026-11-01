"""Keep the appliance's one shared Wayland compositor alive for VM windows."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


POLL_SECONDS = 2.0
STARTUP_GRACE_SECONDS = 120.0
IDLE_GRACE_SECONDS = 60.0


def _reims_ctl() -> str | None:
    for candidate in (
        "/opt/reims-vgpu/bin/reims-ctl",
        "/opt/reims-vgpu/reims-ctl",
        "/root/.cargo/bin/reims-ctl",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("reims-ctl")


def _live_vm_pids() -> set[int] | None:
    """Return live QEMU PIDs; None means the runtime could not be queried."""
    binary = _reims_ctl()
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "list", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data: Any = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        data = data.get("vms")
    if not isinstance(data, list):
        return None

    active: set[int] = set()
    for vm in data:
        if not isinstance(vm, dict):
            continue
        pid = vm.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1 and _pid_alive(pid):
            active.add(pid)
    return active


def _pid_alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return False
    # The process name may contain spaces and closing parentheses.
    state = stat.rsplit(") ", 1)[-1][:1]
    return state not in {"", "Z", "X"}


def main() -> int:
    started = time.monotonic()
    ever_active = False
    idle_since: float | None = None

    while True:
        now = time.monotonic()
        active = _live_vm_pids()
        if active is None:
            # Keep the display up while the runtime is temporarily unavailable;
            # never tear down windows based on an uncertain inventory result.
            idle_since = None
        elif active:
            ever_active = True
            idle_since = None
        elif not ever_active:
            if now - started >= STARTUP_GRACE_SECONDS:
                return 0
        else:
            if idle_since is None:
                idle_since = now
            elif now - idle_since >= IDLE_GRACE_SECONDS:
                return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
