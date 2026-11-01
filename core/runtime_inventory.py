"""Bounded, side-effect-free inventory of the selected Reims/QEMU runtime."""

from __future__ import annotations

import copy
import os
import hashlib
import json
import selectors
import signal
import subprocess
import threading
import time
import re
from pathlib import Path


_MAX_OUTPUT = 128 * 1024
_PROBE_TIMEOUT = 4.0
_INVENTORY_TIMEOUT = 8.0
_MAX_PROPERTY_OUTPUT = 32 * 1024
_CACHE_TTL = 30.0
_MAX_CATALOG_ITEMS = 512
_MAX_DEVICE_PROPERTIES = 128
# Only query generic QEMU models that vHackintosh commonly exposes. Never pass
# host paths, PCI addresses, USB IDs, or values from a VM/host configuration to
# QEMU's device parser.
_PROPERTY_PROBE_DEVICES = (
    "virtio-net-pci", "virtio-blk-pci", "qemu-xhci", "usb-tablet",
    "usb-audio", "ich9-intel-hda", "virtio-sound-pci", "vmware-svga",
)
_cache_lock = threading.Lock()
_cache: tuple[float, tuple[object, ...], dict[str, object]] | None = None


def _qemu_path() -> Path:
    override = os.environ.get("REIMS_CTL_QEMU")
    if override:
        return Path(override).expanduser()
    data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))).expanduser()
    return data_home / "reims-ctl" / "qemu" / "current" / "bin" / "qemu-system-x86_64"


def _selected_binary_paths() -> tuple[Path | None, Path]:
    """Resolve the installed pair, or the tagged pair bundled in a fresh ISO.

    The appliance ships a complete prebuilt release under ``/opt/reims-vgpu``
    and installs it into the selected user runtime prefix on the first VM
    launch. Before that first launch, settings should still inventory the exact
    tagged pair that launch will install instead of reporting an empty runtime.
    Explicit probe arguments and ``REIMS_CTL_QEMU`` remain authoritative.
    """
    from core.reims_ctl import reims_ctl_bin

    installed_reims = reims_ctl_bin()
    installed_qemu = _qemu_path()
    if os.environ.get("REIMS_CTL_QEMU"):
        return (Path(installed_reims).expanduser() if installed_reims else None, installed_qemu)

    def executable(path: Path | None) -> bool:
        return bool(path and path.is_file() and os.access(path, os.X_OK))

    installed_reims_path = Path(installed_reims).expanduser() if installed_reims else None
    if executable(installed_reims_path) and executable(installed_qemu):
        return installed_reims_path, installed_qemu

    try:
        from core.reims_build import (
            REIMS_SEARCH_PATHS, find_reims_dir, prebuilt_qemu, prebuilt_reims_ctl,
        )

        # Inventory the runtime embedded in a fresh ISO even if its release
        # marker is absent or has not yet been written by a release-preparation
        # task. The executable pair itself is the authority for these read-only
        # probes; requiring the marker here made the dashboard look empty until
        # users redundantly prepared the same package already shipped in ISO.
        candidates: list[Path] = []
        discovered = find_reims_dir()
        if discovered is not None:
            candidates.append(discovered)
        configured = os.environ.get("REIMS_VGPU_DIR")
        if configured:
            candidates.append(Path(configured).expanduser())
        candidates.extend(Path(value).expanduser() for value in REIMS_SEARCH_PATHS)
        seen: set[Path] = set()
        for bundle in candidates:
            try:
                identity = bundle.resolve(strict=False)
            except OSError:
                identity = bundle
            if identity in seen:
                continue
            seen.add(identity)
            bundled_reims = prebuilt_reims_ctl(bundle)
            bundled_qemu = prebuilt_qemu(bundle)
            if executable(bundled_reims) and executable(bundled_qemu):
                return bundled_reims, bundled_qemu
    except (ImportError, OSError, RuntimeError, ValueError):
        # Keep reporting the installed paths if a local/developer bundle is
        # incomplete; callers will receive a truthful partial/missing result.
        pass
    return installed_reims_path, installed_qemu


def _probe(
    binary: Path, *arguments: str, deadline: float | None = None,
    max_output: int = _MAX_OUTPUT,
) -> str | None:
    """Run only a version/help query with a deadline and strict output bound."""
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        return None
    local_deadline = time.monotonic() + _PROBE_TIMEOUT
    if deadline is not None:
        local_deadline = min(local_deadline, deadline)
    if local_deadline <= time.monotonic() or max_output < 1:
        return None
    try:
        proc = subprocess.Popen(
            [str(binary), *arguments], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            close_fds=True, start_new_session=True,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.stdout is None:
        proc.kill()
        proc.wait()
        return None

    output = bytearray()
    selector = selectors.DefaultSelector()
    os.set_blocking(proc.stdout.fileno(), False)
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = local_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            for key, _ in selector.select(min(remaining, 0.1)):
                chunk = os.read(key.fd, min(8192, max_output + 1 - len(output)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > max_output:
                    raise OverflowError
        if proc.wait(timeout=max(0.01, local_deadline - time.monotonic())) != 0:
            return None
        return output.decode("utf-8", errors="replace")
    except (TimeoutError, OverflowError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.wait()
        return None
    finally:
        selector.close()
        proc.stdout.close()


def _identity(path: Path | None) -> tuple[object, ...]:
    """Return cache identity for both present and absent selected binaries.

    Including the selected path when it is missing prevents two not-yet-installed
    runtimes from sharing a cache entry, and lets installation invalidate that
    entry as soon as the executable appears.
    """
    if path is None:
        return (None, "not-selected")
    try:
        resolved = path.resolve(strict=False)
        info = resolved.stat()
    except OSError:
        try:
            selected = str(path.expanduser().resolve(strict=False))
        except OSError:
            selected = str(path.expanduser())
        return (selected, "missing")
    return (
        str(resolved), info.st_dev, info.st_ino, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns, info.st_mode,
    )


def _device_names(output: str | None) -> list[str]:
    if not output:
        return []
    names = re.findall(r'^\s*name\s+"([^"\n]+)"', output, re.MULTILINE)
    return list(dict.fromkeys(names))[:_MAX_CATALOG_ITEMS]


def _catalog_names(output: str | None, header: str) -> list[str]:
    """Extract names from QEMU's human-readable machine/CPU help tables."""
    if not output:
        return []
    lines = output.splitlines()
    start = next((index + 1 for index, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return []
    names: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            if names:
                break
            continue
        match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9_.+-]*)\s{2,}", line)
        if match:
            names.append(match.group(1))
        elif names:
            break
    return list(dict.fromkeys(names))[:_MAX_CATALOG_ITEMS]


def _simple_names(output: str | None) -> list[str]:
    """Parse QEMU help lists whose contract is one backend name per line."""
    if not output:
        return []
    names: list[str] = []
    for line in output.splitlines():
        value = line.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
            names.append(value)
    return list(dict.fromkeys(names))[:64]


def _reims_schema_features(output: str | None) -> tuple[int | None, list[str]]:
    """Parse only the versioned, bounded JSON capability contract from Reims."""
    if not output or len(output) > 16 * 1024:
        return None, []
    try:
        data = json.loads(output)
    except (TypeError, ValueError):
        return None, []
    if not isinstance(data, dict):
        return None, []
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= 1000:
        return None, []
    values = data.get("schema_features")
    if not isinstance(values, list) or len(values) > 128:
        return None, []
    features = [
        value for value in values
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)
    ]
    if len(features) != len(values) or len(set(features)) != len(features):
        return None, []
    return version, sorted(features)


def _device_property_names(output: str | None) -> list[str]:
    """Parse property names from `-device MODEL,help`, never property values."""
    if not output:
        return []
    names: list[str] = []
    # QEMU emits typed option rows such as `  bootindex=<int32>` and may emit
    # untyped forms such as `  mac=<...>`. Ignore headings and values entirely.
    pattern = re.compile(
        r"^\s{1,8}([A-Za-z][A-Za-z0-9_-]{0,63})\s*=\s*"
        r"(?:<[^>\r\n]{1,128}>|[^\s\r\n]{1,128})(?:\s+.*)?$"
    )
    for line in output.splitlines()[:4096]:
        match = pattern.match(line)
        if match:
            names.append(match.group(1))
            if len(names) >= _MAX_DEVICE_PROPERTIES:
                break
    return list(dict.fromkeys(names))[:_MAX_DEVICE_PROPERTIES]


def _option_names(output: str | None, *, prefix: str = "-") -> list[str]:
    """Extract option spellings from the selected QEMU/Reims help output."""
    if not output:
        return []
    pattern = re.compile(r"^\s*(-{1,2}[A-Za-z0-9][A-Za-z0-9_-]{0,63})(?=\s|,|$)")
    names: list[str] = []
    for line in output.splitlines():
        match = pattern.match(line)
        if match and match.group(1).startswith(prefix):
            names.append(match.group(1))
    return list(dict.fromkeys(names))[:_MAX_CATALOG_ITEMS]


def _device_categories(devices: list[str]) -> dict[str, list[str]]:
    """Group exact QEMU device names for inventory display, never enablement."""
    patterns = {
        "storage": ("ide-hd", "scsi-hd", "virtio-blk-pci", "nvme", "usb-storage"),
        "network": ("virtio-net-pci", "e1000", "rtl8139"),
        "audio": ("usb-audio", "ich9-intel-hda", "hda-output", "virtio-sound"),
        "usb": ("qemu-xhci", "usb-kbd", "usb-tablet", "usb-host"),
        "display": ("virtio-vga", "virtio-gpu", "VGA", "vmware-svga", "apple-gfx"),
    }
    return {
        category: [name for name in devices if any(token in name for token in tokens)]
        for category, tokens in patterns.items()
    }


def _evidence(source: str, values: list[str]) -> dict[str, object]:
    """Describe positive help evidence without treating omission as lack of support."""
    return {
        "status": "reported" if values else "unknown",
        "source": source,
        "values": values,
        "meaning": "help output lists these names; this does not prove runtime compatibility"
        if values else "help output did not establish availability; omission is not proof of unsupported",
    }


def _device_property_inventory(
    binary: Path,
    devices: list[str],
    *,
    deadline: float,
) -> dict[str, dict[str, object]]:
    """Query a small allowlist using QEMU's help-only device parser mode."""
    listed = set(devices)
    result: dict[str, dict[str, object]] = {}
    for model in _PROPERTY_PROBE_DEVICES:
        if model not in listed:
            continue
        # QEMU documents this form as a property-help query. The allowlisted
        # model is the only argument derived from inventory; no device is
        # realized and no guest, disk, PCI host device, or USB device is opened.
        output = _probe(
            binary, "-device", f"{model},help", deadline=deadline,
            max_output=_MAX_PROPERTY_OUTPUT,
        )
        properties = _device_property_names(output)
        result[model] = {
            "status": "reported" if properties else "unknown",
            "source": f"qemu-system-x86_64 -device {model},help",
            "properties": properties,
            "meaning": "QEMU help lists property names only; values, defaults and runtime compatibility are unverified"
            if properties else "property help was unavailable or inconclusive; this does not prove the device is unsupported",
        }
    return result


def _capability_inventory(
    *,
    qemu_options: list[str],
    qemu_command_options: list[str],
    qemu_devices: list[str],
    qemu_machines: list[str],
    qemu_cpus: list[str],
    qemu_audio_backends: list[str],
    qemu_device_properties: dict[str, dict[str, object]],
    reims_set_options: list[str],
    reims_create_options: list[str],
    reims_update_opencore_picker: bool,
    reims_schema_version: int | None,
    reims_schema_features: list[str],
) -> dict[str, object]:
    """Build a versioned, evidence-based schema without implying operability.

    A name in a help listing means that the selected binary reports it; it does
    not establish that a particular guest, device, host, or configuration works.
    """
    categories = _device_categories(qemu_devices)
    return {
        "schema_version": 2,
        "evidence_basis": "selected binary CLI help; reported entries are not runtime validation; omitted or inconclusive entries are unknown, not unsupported",
        "reims": {
            "schema": {
                "status": "reported" if reims_schema_version is not None else "unknown",
                "source": "reims-ctl capabilities --json",
                "schema_version": reims_schema_version,
                "features": reims_schema_features,
                "meaning": "runtime reports a versioned structured schema feature contract"
                if reims_schema_version is not None else "selected runtime did not report a valid schema capability document",
            },
            "set_options": _evidence("reims-ctl set --help", reims_set_options),
            "create_options": _evidence("reims-ctl create --help", reims_create_options),
            "update_opencore_picker": {
                "status": "reported" if reims_update_opencore_picker else "unknown",
                "source": "reims-ctl update-opencore --help",
                "values": ["--boot-selection <NAME>"] if reims_update_opencore_picker else [],
                "meaning": "help lists the option; execution is not validated" if reims_update_opencore_picker
                else "help did not establish availability; omission is not proof of unsupported",
            },
        },
        "qemu": {
            "safe_options": _evidence("qemu-system-x86_64 -help", qemu_options),
            "command_options": _evidence("qemu-system-x86_64 -help", qemu_command_options),
            "devices": _evidence("qemu-system-x86_64 -device help", qemu_devices),
            "machine_types": _evidence("qemu-system-x86_64 -machine help", qemu_machines),
            "cpu_models": _evidence("qemu-system-x86_64 -cpu help", qemu_cpus),
            "audio_backends": _evidence("qemu-system-x86_64 -audiodev help", qemu_audio_backends),
            "device_categories": {
                name: _evidence("qemu-system-x86_64 -device help", values)
                for name, values in categories.items()
            },
            "device_properties": qemu_device_properties,
        },
    }


def inspect_runtime(
    *, reims_binary: str | Path | None = None,
    qemu_binary: str | Path | None = None,
    use_cache: bool = True,
) -> dict[str, object]:
    """Report the exact binaries and safe QEMU options currently available.

    The probes do not start a guest or inspect VM disks/devices. Results are
    cached briefly and invalidated when either selected binary changes.
    """
    if reims_binary is None and qemu_binary is None:
        selected_reims, qemu = _selected_binary_paths()
        resolved_reims = str(selected_reims) if selected_reims is not None else None
    else:
        if reims_binary is None:
            from core.reims_ctl import reims_ctl_bin

            resolved_reims = reims_ctl_bin()
        else:
            resolved_reims = str(reims_binary)
        qemu = Path(qemu_binary).expanduser() if qemu_binary is not None else _qemu_path()
    reims = Path(resolved_reims).expanduser() if resolved_reims else None
    try:
        from core.reims_release import current_release

        reims_release_tag = current_release()
    except (ImportError, OSError):  # source-only/dev installations may have no release marker
        reims_release_tag = None
    identities = (_identity(reims), _identity(qemu))
    key: tuple[object, ...] = (
        ("reims_ctl", *identities[0]), ("qemu", *identities[1]),
        ("reims_release_tag", reims_release_tag),
    )

    global _cache
    now = time.monotonic()
    if use_cache:
        with _cache_lock:
            if _cache and now - _cache[0] < _CACHE_TTL and _cache[1] == key:
                return copy.deepcopy(_cache[2])

    deadline = now + _INVENTORY_TIMEOUT
    reims_output = _probe(reims, "--version", deadline=deadline) if reims else None
    reims_capabilities = _probe(reims, "capabilities", "--json", deadline=deadline) if reims else None
    reims_set_help = _probe(reims, "set", "--help", deadline=deadline) if reims else None
    reims_create_help = _probe(reims, "create", "--help", deadline=deadline) if reims else None
    reims_update_opencore_help = _probe(reims, "update-opencore", "--help", deadline=deadline) if reims else None
    qemu_version_output = _probe(qemu, "--version", deadline=deadline)
    qemu_help = _probe(qemu, "-help", deadline=deadline)
    qemu_devices_output = _probe(qemu, "-device", "help", deadline=deadline)
    qemu_machines_output = _probe(qemu, "-machine", "help", deadline=deadline)
    qemu_cpus_output = _probe(qemu, "-cpu", "help", deadline=deadline)
    qemu_audio_output = _probe(qemu, "-audiodev", "help", deadline=deadline)
    reims_version = next((line.strip() for line in (reims_output or "").splitlines() if line.strip()), None)
    reims_schema_version, reims_schema_features = _reims_schema_features(reims_capabilities)
    qemu_version = next((line.strip() for line in (qemu_version_output or "").splitlines() if line.strip()), None)
    options = []
    if qemu_help:
        for option in ("-rtc", "-msg"):
            if any(line.lstrip().startswith(option + " ") for line in qemu_help.splitlines()):
                options.append(option)
    qemu_devices = _device_names(qemu_devices_output)
    qemu_machines = _catalog_names(qemu_machines_output, "Supported machines are:")
    qemu_cpus = _catalog_names(qemu_cpus_output, "Available CPUs:")
    qemu_audio_backends = _simple_names(qemu_audio_output)
    qemu_device_properties = _device_property_inventory(qemu, qemu_devices, deadline=deadline)
    qemu_command_options = _option_names(qemu_help)
    reims_set_options = _option_names(reims_set_help, prefix="--")
    reims_create_options = _option_names(reims_create_help, prefix="--")
    complete = bool(
        reims_version and qemu_version and len(options) == 2
        and qemu_devices and qemu_machines and qemu_cpus and qemu_command_options
        and reims_set_options and reims_create_options
    )
    binary_fingerprint = hashlib.sha256(
        json.dumps(key, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()
    result: dict[str, object] = {
        "status": "available" if complete else "partial" if reims_version or qemu_version else "missing",
        "reims_version": reims_version,
        "reims_release_tag": reims_release_tag,
        "reims_schema_version": reims_schema_version,
        "reims_schema_features": reims_schema_features,
        "qemu_version": qemu_version,
        "qemu_options": options,
        "qemu_command_options": qemu_command_options,
        "qemu_devices": qemu_devices,
        "qemu_machine_types": qemu_machines,
        "qemu_cpu_models": qemu_cpus,
        "qemu_audio_backends": qemu_audio_backends,
        "qemu_device_properties": qemu_device_properties,
        "qemu_device_categories": _device_categories(qemu_devices),
        "capability_inventory": _capability_inventory(
            qemu_options=options,
            qemu_command_options=qemu_command_options,
            qemu_devices=qemu_devices,
            qemu_machines=qemu_machines,
            qemu_cpus=qemu_cpus,
            qemu_audio_backends=qemu_audio_backends,
            qemu_device_properties=qemu_device_properties,
            reims_set_options=reims_set_options,
            reims_create_options=reims_create_options,
            reims_update_opencore_picker=bool(
                reims_update_opencore_help
                and "--boot-selection" in reims_update_opencore_help
                and "<NAME>" in reims_update_opencore_help
            ),
            reims_schema_version=reims_schema_version,
            reims_schema_features=reims_schema_features,
        ),
        "reims_set_options": reims_set_options,
        "reims_create_options": reims_create_options,
        "reims_update_opencore_picker": bool(
            reims_update_opencore_help
            and "--boot-selection" in reims_update_opencore_help
            and "<NAME>" in reims_update_opencore_help
        ),
        "binary_fingerprint": binary_fingerprint,
    }
    if use_cache:
        with _cache_lock:
            _cache = (now, key, copy.deepcopy(result))
    return copy.deepcopy(result)
