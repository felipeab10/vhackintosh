"""Keep the appliance's one shared Wayland compositor alive for VM windows."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


POLL_SECONDS = 2.0
STARTUP_GRACE_SECONDS = 120.0
IDLE_GRACE_SECONDS = 60.0
_LOG = logging.getLogger("vhackintosh.display_session_guard")
_LAST_INVENTORY_FAILURE: str | None = None


def _inventory_failure(message: str) -> None:
    global _LAST_INVENTORY_FAILURE
    if message != _LAST_INVENTORY_FAILURE:
        _LOG.warning("Reims VM inventory query failed: %s", message)
    _LAST_INVENTORY_FAILURE = message


def _display_context() -> dict[str, str]:
    try:
        tty = os.ttyname(0) if os.isatty(0) else "not-a-tty"
    except OSError:
        tty = "unavailable"
    return {
        "runtime_dir": os.environ.get("XDG_RUNTIME_DIR", ""),
        "wayland_display": os.environ.get("WAYLAND_DISPLAY", ""),
        "tty": tty,
        "diagnostics": os.environ.get("VHACKINTOSH_DIAGNOSTICS", "0"),
    }


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
    global _LAST_INVENTORY_FAILURE
    binary = _reims_ctl()
    if not binary:
        _inventory_failure("reims-ctl executable was not found")
        return None
    try:
        result = subprocess.run(
            [binary, "list", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _inventory_failure(f"could not run {binary}: {exc}")
        return None
    if result.returncode != 0:
        _inventory_failure(
            f"{binary} exited {result.returncode}: "
            f"{(result.stderr or result.stdout or 'no output').strip()[:1000]}"
        )
        return None
    try:
        data: Any = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        _inventory_failure(f"invalid JSON from {binary}: {exc}; output={result.stdout[:1000]}")
        return None
    if isinstance(data, dict):
        data = data.get("vms")
    if not isinstance(data, list):
        _inventory_failure("JSON does not contain a VM list")
        return None
    _LAST_INVENTORY_FAILURE = None

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
    logging.basicConfig(
        level=(logging.DEBUG if os.environ.get("VHACKINTOSH_DIAGNOSTICS") == "1" else logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    started = time.monotonic()
    ever_active = False
    idle_since: float | None = None
    previous_runtime_available: bool | None = None
    previous_active: set[int] | None = None
    _LOG.info(
        "Shared display guard started: startup_grace=%.0fs idle_grace=%.0fs context=%s",
        STARTUP_GRACE_SECONDS,
        IDLE_GRACE_SECONDS,
        json.dumps(_display_context(), sort_keys=True),
    )

    while True:
        now = time.monotonic()
        active = _live_vm_pids()
        if active is None:
            if previous_runtime_available is not False:
                _LOG.warning("VM inventory unavailable; keeping the shared display alive")
            previous_runtime_available = False
            # Keep the display up while the runtime is temporarily unavailable;
            # never tear down windows based on an uncertain inventory result.
            idle_since = None
        else:
            if previous_runtime_available is not True:
                _LOG.info("VM inventory is available")
            previous_runtime_available = True
            if active != previous_active:
                _LOG.info("Active VM process IDs changed: %s", sorted(active))
            previous_active = set(active)
        if active:
            ever_active = True
            idle_since = None
        elif not ever_active:
            if now - started >= STARTUP_GRACE_SECONDS:
                _LOG.info("No VM became active within startup grace; stopping shared display")
                return 0
        elif active is not None:
            if idle_since is None:
                idle_since = now
            elif now - idle_since >= IDLE_GRACE_SECONDS:
                _LOG.info("No active VMs for %.0fs; stopping shared display", IDLE_GRACE_SECONDS)
                return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
