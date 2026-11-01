"""Conservative decision helper for optional kiosk host-power synchronization.

This module deliberately does not execute host power actions or acquire the
resource/admission locks. Its result is only a policy preflight, never an
authorization to call ``systemctl``. A caller must hold the same atomic guard
used by VM admission and job submission while checking live VM/job state and
performing the action. Until that broker exists, callers must keep the default
``vm-only`` behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class HostPowerDecision:
    allowed: bool
    reason: str


def evaluate_host_power_sync(
    *,
    mode: str = "vm-only",
    install_phase: str = "pending",
    kiosk_session_exclusive: bool = False,
    active_vm_ids: Iterable[str] = (),
    current_vm_id: str | None = None,
    vm_state_known: bool = False,
    active_job_count: int | None = None,
    job_state_known: bool = False,
    active_storage_tasks: int | None = None,
    storage_state_known: bool = False,
) -> HostPowerDecision:
    """Return whether a *known* snapshot is eligible; never perform the action.

    Fail closed for missing kiosk exclusivity, an unconfirmed installation,
    missing/unknown VM or job snapshots, another running VM, any active job or
    storage task, invalid modes, or invalid counts. The active VM snapshot must
    also prove that the current VM really is running.

    Even ``allowed=True`` is not a race-free authorization: callers still need
    an admission/job guard held through the actual host action.
    The safe and implicit mode is always ``vm-only``.
    """
    if mode == "vm-only":
        return HostPowerDecision(False, "vm-only")
    if mode != "kiosk":
        return HostPowerDecision(False, "unsupported-mode")
    if install_phase != "installed":
        return HostPowerDecision(False, "installation-not-confirmed")
    if not kiosk_session_exclusive:
        return HostPowerDecision(False, "kiosk-session-not-exclusive")
    if not vm_state_known:
        return HostPowerDecision(False, "vm-state-unknown")
    try:
        active = {str(vm_id) for vm_id in active_vm_ids if str(vm_id)}
    except (TypeError, ValueError, OverflowError):
        return HostPowerDecision(False, "vm-state-unknown")
    if not current_vm_id or current_vm_id not in active:
        return HostPowerDecision(False, "current-vm-not-active")
    other_active = active - {current_vm_id}
    if other_active:
        return HostPowerDecision(False, "another-vm-active")

    if not job_state_known:
        return HostPowerDecision(False, "job-state-unknown")
    try:
        job_count = int(active_job_count)
    except (TypeError, ValueError, OverflowError):
        return HostPowerDecision(False, "job-state-unknown")
    if job_count < 0:
        return HostPowerDecision(False, "job-state-unknown")
    if job_count:
        return HostPowerDecision(False, "job-active")

    if not storage_state_known:
        return HostPowerDecision(False, "storage-task-state-unknown")
    try:
        task_count = int(active_storage_tasks)
    except (TypeError, ValueError, OverflowError):
        return HostPowerDecision(False, "storage-task-state-unknown")
    if task_count < 0:
        return HostPowerDecision(False, "storage-task-state-unknown")
    if task_count:
        return HostPowerDecision(False, "storage-task-active")
    return HostPowerDecision(True, "exclusive-installed-kiosk")
