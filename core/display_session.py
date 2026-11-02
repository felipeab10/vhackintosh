"""Discover a logged-in user's graphical sockets for desktop VM launches."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Callable


_DISPLAY_KEYS = {
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "PULSE_SERVER",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_TYPE",
}
_LOCAL_X_DISPLAY = re.compile(r"^(?:localhost)?:(\d+)(?:\.\d+)?$")


def _properties(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            result[key] = value
    return result


def _active_gui_users(run: Callable[..., subprocess.CompletedProcess[str]]) -> list[tuple[int, str]]:
    try:
        sessions = run(
            ["/usr/bin/loginctl", "list-sessions", "--no-legend", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if sessions.returncode != 0:
        return []

    users: list[tuple[int, str]] = []
    seen: set[int] = set()
    for line in sessions.stdout.splitlines():
        fields = line.split()
        if not fields or not fields[0].isdigit():
            continue
        session_id = fields[0]
        try:
            detail = run(
                [
                    "/usr/bin/loginctl",
                    "show-session",
                    session_id,
                    "--no-pager",
                    "--property=Active",
                    "--property=Remote",
                    "--property=Type",
                    "--property=Class",
                    "--property=User",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if detail.returncode != 0:
            continue
        props = _properties(detail.stdout)
        session_type = props.get("Type", "").lower()
        raw_uid = props.get("User", "")
        if (
            props.get("Active") != "yes"
            or props.get("Remote") != "no"
            or props.get("Class") not in {"user", "user-early"}
            or session_type not in {"wayland", "x11"}
            or not raw_uid.isdigit()
        ):
            continue
        uid = int(raw_uid)
        if uid not in seen:
            users.append((uid, session_type))
            seen.add(uid)
    return users


def _session_environment_for_uid(
    uid: int,
    session_type: str,
    *,
    proc_root: Path,
    runtime_root: Path,
) -> dict[str, str] | None:
    runtime_dir = runtime_root / str(uid)
    try:
        runtime_stat = runtime_dir.stat()
    except OSError:
        return None
    if (
        not stat.S_ISDIR(runtime_stat.st_mode)
        or runtime_stat.st_uid != uid
        or runtime_stat.st_mode & 0o022
    ):
        return None

    try:
        entries = sorted(
            (entry for entry in os.scandir(proc_root) if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except OSError:
        return None

    for entry in entries:
        try:
            if entry.stat(follow_symlinks=False).st_uid != uid:
                continue
            raw_environment = (Path(entry.path) / "environ").read_bytes()
        except OSError:
            continue
        environment: dict[str, str] = {}
        for item in raw_environment.split(b"\0"):
            key, separator, value = item.partition(b"=")
            if not separator:
                continue
            decoded_key = key.decode("ascii", errors="ignore")
            if decoded_key in _DISPLAY_KEYS:
                environment[decoded_key] = value.decode("utf-8", errors="replace")

        process_type = environment.get("XDG_SESSION_TYPE", session_type).lower()
        if process_type != session_type:
            continue

        result: dict[str, str] = {
            "XDG_RUNTIME_DIR": str(runtime_dir),
            "XDG_SESSION_TYPE": session_type,
        }
        wayland_display = environment.get("WAYLAND_DISPLAY", "")
        if wayland_display:
            socket_path = Path(wayland_display)
            if not socket_path.is_absolute():
                socket_path = runtime_dir / socket_path
            try:
                socket_stat = socket_path.stat()
                if (
                    socket_path.parent.resolve() == runtime_dir.resolve()
                    and stat.S_ISSOCK(socket_stat.st_mode)
                    and socket_stat.st_uid == uid
                ):
                    result["WAYLAND_DISPLAY"] = wayland_display
            except OSError:
                pass

        display = environment.get("DISPLAY", "")
        match = _LOCAL_X_DISPLAY.fullmatch(display)
        if match and (Path("/tmp/.X11-unix") / f"X{match.group(1)}").is_socket():
            result["DISPLAY"] = display
            xauthority = environment.get("XAUTHORITY", "")
            if xauthority:
                try:
                    authority_stat = Path(xauthority).stat()
                    if authority_stat.is_file() and authority_stat.st_uid == uid:
                        result["XAUTHORITY"] = xauthority
                except OSError:
                    pass

        dbus_address = environment.get("DBUS_SESSION_BUS_ADDRESS", "")
        expected_dbus = f"unix:path={runtime_dir}/bus"
        try:
            dbus_stat = (runtime_dir / "bus").stat()
        except OSError:
            dbus_stat = None
        if (
            dbus_address == expected_dbus
            and dbus_stat is not None
            and stat.S_ISSOCK(dbus_stat.st_mode)
            and dbus_stat.st_uid == uid
        ):
            result["DBUS_SESSION_BUS_ADDRESS"] = dbus_address

        pulse_server = environment.get("PULSE_SERVER", "")
        if pulse_server.startswith(f"unix:{runtime_dir}/"):
            pulse_path = Path(pulse_server.removeprefix("unix:"))
            try:
                pulse_stat = pulse_path.stat()
                if (
                    pulse_path.is_socket()
                    and pulse_stat.st_uid == uid
                    and pulse_path.resolve().is_relative_to(runtime_dir.resolve())
                ):
                    result["PULSE_SERVER"] = pulse_server
            except OSError:
                pass

        if result.get("WAYLAND_DISPLAY") or result.get("DISPLAY"):
            return result
    return None


def discover_graphical_environment(
    *,
    proc_root: Path = Path("/proc"),
    runtime_root: Path = Path("/run/user"),
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    """Return validated local Wayland/X11 variables for an active logind user.

    Only display-related variables are copied; credentials and the user's full
    environment never leave /proc. An empty mapping means no usable desktop is
    active, in which case the caller can start the appliance-style compositor.
    """
    for uid, session_type in _active_gui_users(run):
        environment = _session_environment_for_uid(
            uid,
            session_type,
            proc_root=proc_root,
            runtime_root=runtime_root,
        )
        if environment:
            return environment
    return {}
