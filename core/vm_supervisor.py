"""Entry point used only by a per-VM systemd unit.

The unit owns this process tree for the entire Reims lifecycle. Keeping the
launcher out of the API/agent cgroup lets those management services restart
without implicitly terminating or duplicating an existing guest.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import stat
from typing import Callable, Sequence

from core.config import VMManagerStore, VMS_FILE
from core.display_session import discover_graphical_environment
from core.vm_lifecycle import LaunchResult, VMLifecycleService

_VM_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LOG = logging.getLogger("vhackintosh.vm_supervisor")


def _resolve_wayland_display(runtime_dir: str, configured: str) -> str:
    """Prefer Sway's published socket name, falling back to the configured one."""
    marker = os.path.join(runtime_dir, "wayland-display") if runtime_dir else ""
    candidates: list[str] = []
    if marker:
        try:
            with open(marker, encoding="utf-8") as marker_file:
                published = marker_file.read().strip()
        except OSError:
            published = ""
        if published:
            candidates.append(published)
    if configured and configured not in candidates:
        candidates.append(configured)

    for name in candidates:
        path = name if name.startswith("/") else os.path.join(runtime_dir, name)
        try:
            if stat.S_ISSOCK(os.stat(path).st_mode):
                return name
        except OSError:
            pass

    if runtime_dir:
        try:
            with os.scandir(runtime_dir) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            entries = []
        for entry in entries:
            if not re.fullmatch(r"wayland-\d+", entry.name):
                continue
            try:
                if stat.S_ISSOCK(entry.stat(follow_symlinks=True).st_mode):
                    return entry.name
            except OSError:
                pass
    return configured


def _display_diagnostics() -> dict[str, object]:
    """Return a small, non-secret snapshot of the graphical service context."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    display_name = _resolve_wayland_display(runtime_dir, os.environ.get("WAYLAND_DISPLAY", ""))
    socket_path = (
        display_name
        if display_name.startswith("/")
        else os.path.join(runtime_dir, display_name) if runtime_dir and display_name else ""
    )
    try:
        tty = os.ttyname(0)
    except OSError:
        tty = "unavailable"
    try:
        drm_nodes = sorted(
            name for name in os.listdir("/dev/dri")
            if name.startswith(("card", "renderD"))
        )
    except OSError:
        drm_nodes = []
    return {
        "uid": os.getuid(),
        "tty": tty,
        "display": os.environ.get("DISPLAY", ""),
        "wayland_display": display_name,
        "wayland_socket": socket_path,
        "wayland_socket_ready": bool(socket_path and os.path.exists(socket_path)),
        "runtime_dir": runtime_dir,
        "drm_nodes": drm_nodes,
        "wlr_drm_devices": os.environ.get("WLR_DRM_DEVICES", ""),
        "invocation_id": os.environ.get("INVOCATION_ID", ""),
    }


def run_vm(
    vm_id: str,
    *,
    store_factory: Callable[[str | None], VMManagerStore] | None = None,
    lifecycle_factory: Callable[[VMManagerStore], VMLifecycleService] | None = None,
) -> int:
    """Run the existing shared launch pipeline for one validated catalog ID."""
    if not isinstance(vm_id, str) or not _VM_ID.fullmatch(vm_id):
        raise ValueError("invalid VM ID for systemd instance")

    # Desktop packages run the supervisor as root, outside the login user's
    # graphical session. Reuse that session's socket so QEMU opens a normal
    # desktop window instead of trying to seize a second DRM/VT session. The
    # appliance unit deliberately keeps its own shared compositor environment.
    if os.environ.get("VHACKINTOSH_VM_DISPLAY_MODE") == "desktop":
        display_environment = discover_graphical_environment()
        if display_environment:
            os.environ.update(display_environment)
            print(
                "vm-supervisor: using the active desktop graphical session "
                f"({display_environment.get('XDG_SESSION_TYPE', 'unknown')})",
                flush=True,
            )
        else:
            print(
                "vm-supervisor: no active desktop display found; "
                "falling back to the shared compositor",
                flush=True,
            )

    catalog_path = os.environ.get("VHACKINTOSH_VM_CATALOG") or str(VMS_FILE)
    _LOG.info(
        "VM start supervisor entered: vm_id=%s catalog=%s context=%s",
        vm_id,
        catalog_path,
        json.dumps(_display_diagnostics(), ensure_ascii=False, sort_keys=True),
    )
    display = _display_diagnostics()
    if display["wayland_socket_ready"] and display["wayland_display"]:
        os.environ["WAYLAND_DISPLAY"] = str(display["wayland_display"])
    try:
        store = (store_factory or VMManagerStore)(catalog_path)
        vm = store.get_vm(vm_id)
    except Exception:
        _LOG.exception("Could not load VM catalog entry: vm_id=%s catalog=%s", vm_id, catalog_path)
        raise
    if vm is None:
        _LOG.error("VM is not present in catalog: vm_id=%s catalog=%s", vm_id, catalog_path)
        print(f"vm-supervisor: VM {vm_id} is not present in the catalog", flush=True)
        return 2

    try:
        result: LaunchResult = (lifecycle_factory or VMLifecycleService)(store).start(vm)
    except Exception:
        _LOG.exception("VM lifecycle raised an exception: vm_id=%s", vm_id)
        raise
    _LOG.info(
        "VM lifecycle returned: vm_id=%s state=%s phase=%s error_code=%s message=%s details=%s",
        vm_id,
        result.state,
        result.phase,
        result.error_code,
        result.message,
        json.dumps(result.details or {}, ensure_ascii=False, sort_keys=True),
    )
    if result.state == "completed":
        print(f"vm-supervisor: VM {vm_id} stopped; lifecycle completed", flush=True)
        return 0

    print(
        f"vm-supervisor: VM {vm_id} {result.state}: "
        f"{result.error_code or 'launch_failed'}: {result.message or 'lifecycle did not start'}",
        flush=True,
    )
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a VM under its dedicated systemd unit.")
    parser.add_argument("--vm-id", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return run_vm(args.vm_id)
    except ValueError as exc:
        parser.error(str(exc))
    except Exception:
        _LOG.exception("Unexpected VM supervisor failure: vm_id=%s", args.vm_id)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
