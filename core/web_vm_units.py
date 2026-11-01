"""Narrow systemd adapter for per-VM units.

This module deliberately owns no launch policy or QEMU arguments. Callers may
only address a VM by its validated catalog ID and request one of the fixed
systemd operations implemented here.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

import psutil


_VM_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SHOW_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "Result",
)
_KNOWN_ACTIVE_STATES = {
    "active", "reloading", "inactive", "failed", "activating",
    "deactivating", "maintenance",
}


class VmUnitError(RuntimeError):
    """A systemd operation failed or returned data we cannot trust."""


class InvalidVmUnitId(ValueError):
    """The supplied identifier cannot safely address a VM unit."""


@dataclass(frozen=True)
class VmUnitStatus:
    vm_id: str
    unit: str
    load_state: str
    active_state: str
    sub_state: str
    main_pid: int | None
    process_create_time: float | None
    result: str


class SystemdVmUnitManager:
    """Query and control only ``vhackintosh-vm@<id>.service`` units."""

    def __init__(
        self,
        *,
        systemctl: str = "systemctl",
        scope: str = "system",
        timeout: float = 15.0,
        start_timeout: float = 135.0,
        run: Callable[..., Any] = subprocess.run,
        process_factory: Callable[[int], Any] = psutil.Process,
    ) -> None:
        if not isinstance(systemctl, str) or not systemctl or "\x00" in systemctl:
            raise ValueError("systemctl executable must be a non-empty path")
        if scope not in {"system", "user"}:
            raise ValueError("systemd scope must be 'system' or 'user'")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("systemctl timeout must be positive")
        if isinstance(start_timeout, bool) or not isinstance(start_timeout, (int, float)) or start_timeout <= 0:
            raise ValueError("systemctl start timeout must be positive")
        self.systemctl = systemctl
        self.scope = scope
        self.timeout = float(timeout)
        self.start_timeout = float(start_timeout)
        self._run = run
        self._process_factory = process_factory

    @staticmethod
    def unit_name(vm_id: str) -> str:
        """Validate a catalog ID before interpolating it into a unit instance."""
        if not isinstance(vm_id, str) or not _VM_ID.fullmatch(vm_id):
            raise InvalidVmUnitId("VM ID must be a lowercase slug of at most 63 characters")
        return f"vhackintosh-vm@{vm_id}.service"

    def query(self, vm_id: str) -> VmUnitStatus:
        unit = self.unit_name(vm_id)
        result = self._invoke([
            self.systemctl,
            "show",
            "--no-pager",
            *(f"--property={property_name}" for property_name in _SHOW_PROPERTIES),
            unit,
        ])
        properties = self._parse_show(result.stdout or "")
        missing = [name for name in _SHOW_PROPERTIES if name not in properties]
        if missing:
            raise VmUnitError("systemctl show omitted required properties: " + ", ".join(missing))

        load_state = properties["LoadState"].strip()
        active_state = properties["ActiveState"].strip()
        sub_state = properties["SubState"].strip()
        result_state = properties["Result"].strip()
        if not load_state or not sub_state or active_state not in _KNOWN_ACTIVE_STATES:
            raise VmUnitError("systemctl show returned an unknown or incomplete unit state")

        raw_pid = properties["MainPID"].strip()
        try:
            pid_value = int(raw_pid, 10)
        except (TypeError, ValueError) as exc:
            raise VmUnitError("systemctl show returned an invalid MainPID") from exc
        if pid_value < 0:
            raise VmUnitError("systemctl show returned a negative MainPID")
        pid = pid_value or None
        create_time = self._process_create_time(pid) if pid is not None else None
        return VmUnitStatus(
            vm_id=vm_id,
            unit=unit,
            load_state=load_state,
            active_state=active_state,
            sub_state=sub_state,
            main_pid=pid,
            process_create_time=create_time,
            result=result_state,
        )

    def start(self, vm_id: str) -> VmUnitStatus:
        unit = self.unit_name(vm_id)
        # Starting the VM also starts the shared Wayland service, whose
        # systemd unit may spend up to 120 seconds preparing the compositor.
        self._invoke([self.systemctl, "start", unit], timeout=self.start_timeout)
        return self.query(vm_id)

    def stop(self, vm_id: str) -> VmUnitStatus:
        unit = self.unit_name(vm_id)
        self._invoke([self.systemctl, "stop", unit])
        return self.query(vm_id)

    def admission_failure_message(self, vm_id: str) -> str | None:
        """Return only allowlisted resource-admission diagnostics from the unit journal."""
        unit = self.unit_name(vm_id)
        try:
            completed = self._run(
                [
                    "/usr/bin/journalctl",
                    *(["--user"] if self.scope == "user" else []),
                    f"--unit={unit}",
                    "--no-pager",
                    "--output=cat",
                    "--lines=50",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None

        prefix = f"vm-supervisor: VM {vm_id} rejected: "
        safe_codes = {
            "insufficient_host_memory",
            "reservation_storage_full",
            "resource_conflict",
        }
        for line in reversed((completed.stdout or "").splitlines()):
            if not line.startswith(prefix):
                continue
            code, separator, message = line[len(prefix):].partition(": ")
            if separator and code in safe_codes and message:
                # Keep the result concise and strip control characters before
                # exposing a service log line in the browser.
                cleaned = "".join(char for char in message if char.isprintable()).strip()
                return cleaned[:500] or None
        return None

    def _invoke(self, command: list[str], *, timeout: float | None = None) -> Any:
        effective_timeout = self.timeout if timeout is None else timeout
        try:
            completed = self._run(
                self._scoped(command),
                capture_output=True,
                text=True,
                check=False,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise VmUnitError(f"systemctl timed out after {effective_timeout:g} seconds") from exc
        except OSError as exc:
            raise VmUnitError(f"could not execute systemctl: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            if len(detail) > 512:
                detail = detail[:512] + "…"
            raise VmUnitError(
                f"systemctl {command[1]} failed with exit code {completed.returncode}"
                + (f": {detail}" if detail else "")
            )
        return completed

    def _scoped(self, command: list[str]) -> list[str]:
        """Select the user manager only when explicitly configured."""
        scope_args = ["--user"] if self.scope == "user" else []
        return [self.systemctl, *scope_args, *command[1:]]

    @staticmethod
    def _parse_show(output: str) -> dict[str, str]:
        properties: dict[str, str] = {}
        for line in output.splitlines():
            if not line:
                continue
            name, separator, value = line.partition("=")
            if not separator or not name or name in properties:
                raise VmUnitError("systemctl show returned malformed properties")
            properties[name] = value
        return properties

    def _process_create_time(self, pid: int) -> float | None:
        try:
            return float(self._process_factory(pid).create_time())
        except (psutil.Error, OSError, ValueError, TypeError):
            # The unit's PID can exit between `systemctl show` and inspection;
            # absence of a process timestamp does not invalidate unit state.
            return None
