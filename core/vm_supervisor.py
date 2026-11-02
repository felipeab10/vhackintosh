"""Entry point used only by a per-VM systemd unit.

The unit owns this process tree for the entire Reims lifecycle. Keeping the
launcher out of the API/agent cgroup lets those management services restart
without implicitly terminating or duplicating an existing guest.
"""

from __future__ import annotations

import argparse
import os
import re
from typing import Callable, Sequence

from core.config import VMManagerStore, VMS_FILE
from core.display_session import discover_graphical_environment
from core.vm_lifecycle import LaunchResult, VMLifecycleService

_VM_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


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
    store = (store_factory or VMManagerStore)(catalog_path)
    vm = store.get_vm(vm_id)
    if vm is None:
        print(f"vm-supervisor: VM {vm_id} is not present in the catalog", flush=True)
        return 2

    result: LaunchResult = (lifecycle_factory or VMLifecycleService)(store).start(vm)
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
    try:
        return run_vm(args.vm_id)
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
