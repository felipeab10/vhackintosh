"""Serialize appliance maintenance against VM admission and execution."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from core.config import (
    VM_MAINTENANCE_MARKER,
    VM_RESERVATIONS_DIR,
    VM_RESERVATIONS_LOCK,
    ExclusiveVMLock,
)

MAINTENANCE_MARKER = VM_MAINTENANCE_MARKER


class MaintenanceBlocked(RuntimeError):
    """Maintenance cannot proceed while the VM runtime is active/unknown."""


def _runtime_process_present(proc_root: Path = Path("/proc")) -> bool:
    """Conservative fallback for first boot before reims-ctl is installed."""
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").lower()
            exe = os.readlink(entry / "exe").lower().encode()
        except (OSError, PermissionError):
            continue
        if b"qemu-system" in cmdline or b"reims-ctl" in cmdline or b"qemu-system" in exe:
            return True
    return False


@contextmanager
def maintenance_window(*, marker: Path = MAINTENANCE_MARKER,
                        lock_path: Path = VM_RESERVATIONS_LOCK,
                        reservations_dir: Path = VM_RESERVATIONS_DIR,
                        reservation_vm_id: str | None = None) -> Iterator[None]:
    """Block VM admission and hold an exclusive maintenance marker.

    The same reservation lock serializes the active-VM check with a concurrent
    launch reservation. Admission checks the marker while holding that lock,
    so no new VM can enter after the maintenance check succeeds.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    marker.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            running, ctl_ok = ExclusiveVMLock._running_reims_vms()
            if not ctl_ok:
                # A fresh appliance has the controller in /opt but not yet in
                # its runtime prefix. Permit that initial install only when no
                # QEMU/controller process exists; uncertainty remains fail-closed.
                if _runtime_process_present():
                    raise MaintenanceBlocked("não foi possível confirmar o estado das VMs; manutenção recusada")
                running = {}
            reservations = []
            if reservations_dir.exists():
                for path in reservations_dir.glob("*.json"):
                    try:
                        row = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        # Unknown reservations are treated as active.
                        reservations.append(path.name)
                        continue
                    owner = row.get("owner_pid") if isinstance(row, dict) else None
                    owns_requested_reservation = (
                        reservation_vm_id is not None
                        and owner == os.getpid()
                        and str(row.get("vm_id") or "") == reservation_vm_id
                    )
                    if (not isinstance(owner, int) or ExclusiveVMLock._pid_alive(owner)
                            or str(row.get("vm_id") or "") in running):
                        if owns_requested_reservation:
                            continue
                        reservations.append(str(row.get("name") or row.get("vm_id") or path.name))
            if running or reservations:
                names = sorted(set(running) | set(reservations))
                raise MaintenanceBlocked("VMs ativas ou reservadas: " + ", ".join(names))
            if marker.exists():
                raise MaintenanceBlocked("outra manutenção já está em andamento")
            fd, temp_name = tempfile.mkstemp(prefix=".maintenance.", dir=marker.parent)
            temp = Path(temp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"pid": os.getpid(), "purpose": "runtime-update"}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, marker)
            except Exception:
                temp.unlink(missing_ok=True)
                raise
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    try:
        yield
    finally:
        # Do not remove a marker that another process replaced after a crash
        # recovery/operator action.
        try:
            row = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(row, dict) and row.get("pid") == os.getpid():
                marker.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
