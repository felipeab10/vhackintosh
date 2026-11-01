"""Read-only Linux PCI/DRM GPU inventory for the local management API.

This module separates discovery from selection support. Reims selects by Vulkan
manifest and optional PCI device ID, not BDF, so a web choice is enabled only
when a bounded Reims inventory and an explicit Vulkan BDF probe jointly prove
that the mapping is unique.
"""

from __future__ import annotations

import json
import logging
import os
import re
import selectors
import signal
import shutil
import subprocess
import tempfile
import time
import threading
import uuid as uuidlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping


_BDF = re.compile(r"^(?P<domain>[0-9a-fA-F]{4}):(?P<bus>[0-9a-fA-F]{2}):(?P<slot>[0-9a-fA-F]{2})\.(?P<function>[0-7])$")
_MAX_VULKAN_OUTPUT = 256 * 1024
_VULKAN_TIMEOUT_SECONDS = 2.0
_MAX_REIMS_OUTPUT = 64 * 1024
_REIMS_TIMEOUT_SECONDS = 2.0
GPU_INVENTORY_CACHE_SECONDS = 30.0

Runner = Callable[..., subprocess.CompletedProcess[str]]
logger = logging.getLogger(__name__)


class CachedGpuInventory:
    """Share the bounded, relatively expensive GPU inventory across callers.

    The lock deliberately covers refresh so simultaneous API/RPC clients cannot
    launch duplicate Vulkan/Reims probes. Inventory remains read-only; on a
    transient refresh failure, the last-known-good snapshot remains available
    for display while callers can see the error and must disable new selections.
    """

    def __init__(self, provider: Callable[[], list[object]], *, ttl: float = GPU_INVENTORY_CACHE_SECONDS,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if ttl <= 0:
            raise ValueError("GPU inventory cache TTL must be positive")
        self.provider = provider
        self.ttl = ttl
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._cached_at: float | None = None
        self._items: list[object] = []
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        """Return the last refresh failure without exposing exception details."""
        with self._lock:
            return self._last_error

    def __call__(self) -> list[object]:
        with self._lock:
            now = self.monotonic()
            if self._cached_at is not None and now - self._cached_at < self.ttl:
                return list(self._items)
            try:
                items = list(self.provider())
            except Exception as exc:
                logger.warning("GPU inventory refresh failed: %s", type(exc).__name__)
                self._last_error = f"Sonda de GPU indisponível ({type(exc).__name__})."
                self._cached_at = now
                return list(self._items)
            self._items = items
            self._last_error = None
            self._cached_at = now
            return list(items)


@dataclass(frozen=True)
class GpuInventoryItem:
    pci_bdf: str
    vendor_id: str
    device_id: str
    name: str
    kind: str
    render_node: str | None
    vulkan_uuid: str | None
    driver_file: str | None
    vulkan_available: bool
    used_by_host: bool | None
    assigned_vm_id: str | None = None
    selection_supported: bool = False
    limitation: str | None = None
    reims_device_uuid: str | None = None
    reims_pci_address: str | None = None
    reims_shares_driver: bool | None = None

    def as_dict(self) -> dict[str, object]:
        """Serialize using the stable fields expected by ``GpuDevice``."""
        return {
            "pci_bdf": self.pci_bdf,
            "vendor_id": self.vendor_id,
            "device_id": self.device_id,
            "name": self.name,
            "kind": self.kind,
            "render_node": self.render_node,
            "vulkan_uuid": self.vulkan_uuid,
            "driver_file": self.driver_file,
            "vulkan_available": self.vulkan_available,
            "used_by_host": self.used_by_host,
            "assigned_vm_id": self.assigned_vm_id,
            "selection_supported": self.selection_supported,
            "limitation": self.limitation,
            "reims_device_uuid": self.reims_device_uuid,
            "reims_pci_address": self.reims_pci_address,
            "reims_shares_driver": self.reims_shares_driver,
        }


@dataclass(frozen=True)
class VulkanProbe:
    available: bool
    devices_by_bdf: Mapping[str, str]
    limitation: str | None = None
    pci_ids_by_bdf: Mapping[str, tuple[str | None, str | None]] | None = None


@dataclass(frozen=True)
class ReimsGpu:
    vendor_id: str
    device_id: str
    driver_file: str
    name: str | None = None
    device_uuid: str | None = None
    pci_address: str | None = None
    shares_driver: bool | None = None
    kind: str | None = None


@dataclass(frozen=True)
class ReimsProbe:
    available: bool
    devices: tuple[ReimsGpu, ...]
    limitation: str | None = None


_VENDORS = {
    "0x8086": ("Intel", "intel"),
    "0x1002": ("AMD", "amd"),
    "0x10de": ("NVIDIA", "nvidia"),
}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except (OSError, UnicodeError):
        return None


def _normalize_id(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    if value.startswith("0x"):
        value = value[2:]
    return value.zfill(4) if re.fullmatch(r"[0-9a-f]{1,4}", value) else None


def _canonical_bdf(value: object) -> str | None:
    if isinstance(value, str) and _BDF.fullmatch(value):
        return value.lower()
    return None


def _vulkan_id(value: object) -> str | None:
    if isinstance(value, int) and 0 <= value <= 0xFFFF:
        return f"{value:04x}"
    if isinstance(value, str):
        return _normalize_id(value)
    return None


def _vulkan_devices(value: object) -> Iterable[tuple[str, str, str | None, str | None]]:
    """Yield only UUIDs tied to an explicit PCI bus tuple in probe JSON."""
    if isinstance(value, dict):
        uuid = value.get("deviceUUID") or value.get("deviceUuid")
        pci = value.get("pciBusInfo") or value.get("pci_bus_info")
        device_properties = value.get("VkPhysicalDeviceProperties")
        vulkan11_properties = value.get("VkPhysicalDeviceVulkan11Properties")
        pci_properties = value.get("VkPhysicalDevicePCIBusInfoPropertiesEXT")
        if not isinstance(device_properties, dict):
            device_properties = {}
        if not isinstance(vulkan11_properties, dict):
            vulkan11_properties = {}
        if not isinstance(pci_properties, dict):
            pci_properties = {}
        if pci is None:
            pci = pci_properties
        if uuid is None:
            uuid = vulkan11_properties.get("deviceUUID")
        vendor_id = value.get("vendorID", value.get("vendorId", device_properties.get("vendorID")))
        device_id = value.get("deviceID", value.get("deviceId", device_properties.get("deviceID")))
        if isinstance(uuid, str) and isinstance(pci, dict):
            try:
                domain = int(pci.get("pciDomain", pci.get("domain")))
                bus = int(pci.get("pciBus", pci.get("bus")))
                slot = int(pci.get("pciDevice", pci.get("device")))
                function = int(pci.get("pciFunction", pci.get("function")))
            except (TypeError, ValueError):
                pass
            else:
                bdf = _canonical_bdf(f"{domain:04x}:{bus:02x}:{slot:02x}.{function}")
                if bdf:
                    yield (
                        bdf,
                        uuid,
                        _vulkan_id(vendor_id),
                        _vulkan_id(device_id),
                    )
        elif isinstance(uuid, list) and isinstance(pci, dict):
            try:
                domain = int(pci.get("pciDomain", pci.get("domain")))
                bus = int(pci.get("pciBus", pci.get("bus")))
                slot = int(pci.get("pciDevice", pci.get("device")))
                function = int(pci.get("pciFunction", pci.get("function")))
                raw_uuid = bytes(uuid)
                if len(raw_uuid) != 16:
                    raise ValueError("UUID Vulkan deve ter 16 bytes")
                bdf = _canonical_bdf(f"{domain:04x}:{bus:02x}:{slot:02x}.{function}")
            except (TypeError, ValueError):
                pass
            else:
                if bdf:
                    yield (
                        bdf,
                        str(uuidlib.UUID(bytes=raw_uuid)),
                        _vulkan_id(vendor_id),
                        _vulkan_id(device_id),
                    )
        for nested in value.values():
            yield from _vulkan_devices(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _vulkan_devices(nested)


def _run_vulkan_bounded(
    argv: list[str], *, timeout: float, cwd: str | os.PathLike[str] | None = None,
    max_output: int = _MAX_VULKAN_OUTPUT,
) -> subprocess.CompletedProcess[str]:
    """Collect at most the configured cap from a process before terminating it."""
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "HOME": "/", "LANG": "C", "LC_ALL": "C"},
        cwd=cwd,
        start_new_session=True,
        close_fds=True,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    output: dict[int, bytearray] = {stdout_fd: bytearray(), stderr_fd: bytearray()}
    streams = {stdout_fd: process.stdout, stderr_fd: process.stderr}
    selector = selectors.DefaultSelector()
    for fd, stream in streams.items():
        os.set_blocking(fd, False)
        selector.register(stream, selectors.EVENT_READ, fd)
    started = time.monotonic()
    exceeded = False
    try:
        while selector.get_map():
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in selector.select(min(remaining, 0.1)):
                fd = key.data
                chunk = os.read(fd, min(8192, max_output + 1 - len(output[fd])))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output[fd].extend(chunk)
                if sum(map(len, output.values())) > max_output:
                    exceeded = True
                    raise RuntimeError("Vulkan output limit exceeded")
            if process.poll() is not None and not selector.get_map():
                break
    except (subprocess.TimeoutExpired, RuntimeError):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.wait()
        if exceeded:
            return subprocess.CompletedProcess(argv, 124, "", "output limit exceeded")
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    try:
        return_code = process.wait(timeout=max(0.01, timeout - (time.monotonic() - started)))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.wait()
        raise
    return subprocess.CompletedProcess(
        argv,
        return_code,
        bytes(output[stdout_fd]).decode("utf-8", errors="replace"),
        bytes(output[stderr_fd]).decode("utf-8", errors="replace"),
    )


def probe_vulkan(runner: Runner | None = None) -> VulkanProbe:
    """Run one bounded, non-shell Vulkan inventory probe.

    Missing tools, timeout, failed probes, oversized output, and JSON without
    PCI identity all produce explicit unavailability. Names and IDs alone do
    not establish a physical-device mapping.
    """
    try:
        if runner is None:
            executable = shutil.which("vulkaninfo", path="/usr/bin:/bin")
            if not executable:
                return VulkanProbe(False, {}, "Sonda Vulkan indisponível: vulkaninfo não encontrado")
            # vulkaninfo's --json writes a device-named file in its cwd instead
            # of emitting JSON to stdout on current Vulkan SDK releases. Isolate
            # that output from the appliance state/project and discard it once
            # the bounded probe finishes.
            with tempfile.TemporaryDirectory(prefix="vhackintosh-vulkan-") as temp_dir:
                result = _run_vulkan_bounded(
                    [executable, "--json"], timeout=_VULKAN_TIMEOUT_SECONDS, cwd=temp_dir
                )
                return _parse_vulkan_result(result, output_dir=Path(temp_dir))
        result = runner(
            ["vulkaninfo", "--json"],
            capture_output=True,
            text=True,
            timeout=_VULKAN_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return VulkanProbe(False, {}, f"Sonda Vulkan indisponível: {type(exc).__name__}")

    return _parse_vulkan_result(result)


def _parse_vulkan_result(
    result: subprocess.CompletedProcess[str], *, output_dir: Path | None = None
) -> VulkanProbe:
    """Parse bounded stdout or vulkaninfo's private working-directory JSON."""
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    if len(stdout.encode("utf-8", errors="replace")) > _MAX_VULKAN_OUTPUT or len(stderr.encode("utf-8", errors="replace")) > _MAX_VULKAN_OUTPUT:
        return VulkanProbe(False, {}, "Saída da sonda Vulkan excedeu o limite seguro")
    if getattr(result, "returncode", 1) != 0:
        return VulkanProbe(False, {}, "Sonda Vulkan terminou com erro")
    if not stdout and output_dir is not None:
        try:
            output_files = [
                path for path in output_dir.iterdir()
                if not path.is_symlink() and path.is_file()
            ]
            if len(output_files) == 1:
                output_path = output_files[0]
                if output_path.stat().st_size <= _MAX_VULKAN_OUTPUT:
                    stdout = output_path.read_text(encoding="utf-8", errors="replace")
                else:
                    return VulkanProbe(False, {}, "Saída da sonda Vulkan excedeu o limite seguro")
        except OSError:
            stdout = ""
    try:
        document = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return VulkanProbe(False, {}, "Sonda Vulkan não retornou JSON válido")
    mapped: dict[str, str] = {}
    pci_ids: dict[str, tuple[str | None, str | None]] = {}
    ambiguous: set[str] = set()
    for bdf, uuid, vendor_id, device_id in _vulkan_devices(document):
        if bdf in ambiguous:
            continue
        identity = (vendor_id, device_id)
        if bdf in mapped and (
            mapped[bdf] != uuid
            or (bdf in pci_ids and identity != (None, None) and pci_ids[bdf] != identity)
        ):
            mapped.pop(bdf, None)
            pci_ids.pop(bdf, None)
            ambiguous.add(bdf)
            continue
        mapped[bdf] = uuid
        if identity != (None, None):
            pci_ids[bdf] = identity
    if not mapped:
        return VulkanProbe(False, {}, "A sonda Vulkan não forneceu UUID associado a endereço PCI")
    return VulkanProbe(True, mapped, pci_ids_by_bdf=pci_ids)


_REIMS_GPU_LINE = re.compile(
    r"^\s*\d+\.\s+(?P<name>.+?)\s+\[(?P<vendor>[0-9a-fA-F]{4}):(?P<device>[0-9a-fA-F]{4})\]\s+(?P<driver>.+?)\s*$"
)


def _reims_json_features() -> set[str]:
    try:
        from core.runtime_inventory import inspect_runtime

        features = inspect_runtime().get("reims_schema_features", [])
    except Exception:
        return set()
    return set(features) if isinstance(features, list) and all(isinstance(x, str) for x in features) else set()


def _parse_reims_gpu_json(stdout: str) -> ReimsProbe:
    try:
        document = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return ReimsProbe(False, (), "Inventário Reims JSON inválido")
    if not isinstance(document, list) or not document or len(document) > 128:
        return ReimsProbe(False, (), "Inventário Reims JSON vazio ou fora do limite")
    devices: list[ReimsGpu] = []
    seen_uuid: set[str] = set()
    seen_bdf: set[str] = set()
    for item in document:
        if not isinstance(item, dict):
            return ReimsProbe(False, (), "Registro de GPU Reims inválido")
        name, vendor, device, kind = item.get("name"), item.get("vendor_id"), item.get("device_id"), item.get("kind")
        identity, pci, driver, shares = (
            item.get("device_uuid"), item.get("pci_address"), item.get("driver_file"), item.get("shares_driver")
        )
        vendor_id, device_id = _normalize_id(vendor), _normalize_id(device)
        bdf = _canonical_bdf(pci)
        if (
            not isinstance(name, str) or not name.strip() or len(name) > 256
            or not isinstance(kind, str) or not kind.strip() or len(kind) > 32
            or any(ord(char) < 32 for char in kind)
            or vendor_id is None or device_id is None
            or not isinstance(identity, str) or not identity.strip() or len(identity) > 128
            or any(ord(char) < 32 for char in identity)
            or bdf is None
            or not isinstance(driver, str) or not driver.startswith("/") or len(driver) > 4096
            or any(ord(char) < 32 for char in driver)
            or not isinstance(shares, bool)
        ):
            return ReimsProbe(False, (), "Inventário Reims JSON não contém identidade completa válida")
        identity = identity.strip()
        if identity in seen_uuid or bdf in seen_bdf:
            return ReimsProbe(False, (), "Inventário Reims contém UUID ou BDF ambíguo")
        seen_uuid.add(identity)
        seen_bdf.add(bdf)
        devices.append(ReimsGpu(
            vendor_id=vendor_id, device_id=device_id, driver_file=driver,
            name=name.strip(), device_uuid=identity, pci_address=bdf, shares_driver=shares, kind=kind.strip(),
        ))
    return ReimsProbe(True, tuple(devices))


def probe_reims_gpus(runner: Runner | None = None, *, schema_features: set[str] | None = None) -> ReimsProbe:
    """Read the actual Reims Vulkan manifest/device choices with strict process bounds.

    `reims-ctl gpus` is currently the supported source for the ICD manifest path and
    Vulkan device ID accepted by the runtime. Its text output is intentionally parsed
    narrowly; an unfamiliar CLI format disables GPU selection instead of guessing.
    """
    use_json = "gpu_inventory_json" in (schema_features if schema_features is not None else _reims_json_features())
    args = ["reims-ctl", "gpus", "--json"] if use_json else ["reims-ctl", "gpus"]
    try:
        if runner is None:
            # Match the appliance runtime's pinned install location first.
            # systemd's restricted PATH deliberately excludes cargo's bin, so
            # `shutil.which(..., path=/usr/bin:/bin)` missed the actual engine.
            candidates = (
                Path(os.path.expanduser("~/.cargo/bin/reims-ctl")),
                Path("/root/.cargo/bin/reims-ctl"),
                Path("/usr/local/bin/reims-ctl"),
                Path("/usr/bin/reims-ctl"),
                Path("/bin/reims-ctl"),
            )
            executable = next(
                (str(candidate) for candidate in candidates
                 if candidate.is_file() and os.access(candidate, os.X_OK)),
                None,
            )
            if executable is None:
                executable = shutil.which("reims-ctl", path="/usr/bin:/bin")
            if not executable:
                return ReimsProbe(False, (), "Inventário Reims indisponível: reims-ctl não encontrado")
            result = _run_vulkan_bounded(
                [executable, *args[1:]], timeout=_REIMS_TIMEOUT_SECONDS,
                max_output=_MAX_REIMS_OUTPUT,
            )
        else:
            result = runner(
                args,
                capture_output=True,
                text=True,
                timeout=_REIMS_TIMEOUT_SECONDS,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return ReimsProbe(False, (), "Inventário Reims excedeu o tempo limite")
    except (OSError, subprocess.SubprocessError) as exc:
        return ReimsProbe(False, (), f"Inventário Reims indisponível: {type(exc).__name__}")
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    if len(stdout.encode("utf-8", errors="replace")) > _MAX_REIMS_OUTPUT or len(stderr.encode("utf-8", errors="replace")) > _MAX_REIMS_OUTPUT:
        return ReimsProbe(False, (), "Saída do inventário Reims excedeu o limite seguro")
    if getattr(result, "returncode", 1) != 0:
        if "output limit exceeded" in stderr.lower():
            return ReimsProbe(False, (), "Saída do inventário Reims excedeu o limite seguro")
        return ReimsProbe(False, (), "Inventário Reims terminou com erro")
    if use_json:
        return _parse_reims_gpu_json(stdout)
    devices: list[ReimsGpu] = []
    for line in stdout.splitlines():
        match = _REIMS_GPU_LINE.fullmatch(line)
        if not match:
            continue
        devices.append(
            ReimsGpu(
                vendor_id=match.group("vendor").lower(),
                device_id=match.group("device").lower(),
                driver_file=match.group("driver").strip(),
                name=match.group("name").strip(),
            )
        )
    if not devices:
        return ReimsProbe(False, (), "Inventário Reims não retornou opções Vulkan reconhecíveis")
    return ReimsProbe(True, tuple(devices))


def _render_nodes(device_path: Path, dev_root: Path) -> list[str]:
    found: list[str] = []
    drm = device_path / "drm"
    try:
        entries = sorted(drm.iterdir(), key=lambda item: item.name)
    except OSError:
        return found
    for entry in entries:
        if not re.fullmatch(r"renderD[0-9]+", entry.name):
            continue
        node = dev_root / entry.name
        try:
            if node.exists():
                found.append(str(node))
        except OSError:
            continue
    return found


def _host_display_usage(device_path: Path, sysfs_root: Path) -> bool | None:
    """Return True only when a framebuffer is explicitly linked to this PCI device."""
    graphics = sysfs_root / "class/graphics"
    try:
        fb_nodes = list(graphics.glob("fb*/device"))
    except OSError:
        return None
    if not graphics.exists():
        return None
    try:
        target = device_path.resolve(strict=True)
        for fb in fb_nodes:
            try:
                if fb.resolve(strict=True) == target:
                    return True
            except OSError:
                continue
        return False
    except OSError:
        return None


def _device_kind(vendor: str) -> tuple[str, str]:
    label, kind = _VENDORS.get(vendor.lower(), ("GPU", "unknown"))
    return label, kind


def inventory_gpus(
    *,
    sysfs_root: str | os.PathLike[str] = "/sys",
    dev_root: str | os.PathLike[str] = "/dev/dri",
    runner: Runner | None = None,
    vulkan_probe_result: VulkanProbe | None = None,
    reims_probe_result: ReimsProbe | None = None,
    reims_runner: Runner | None = None,
) -> list[GpuInventoryItem]:
    """Discover PCI display controllers with available DRM render nodes.

    Paths and the process runner are injectable for deterministic tests. No
    sysfs write, privileged operation, shell, or device-open is performed.
    """
    sysfs = Path(sysfs_root)
    dev = Path(dev_root)
    pci_devices = sysfs / "bus/pci/devices"
    try:
        entries = sorted(pci_devices.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return []
    probe = vulkan_probe_result if vulkan_probe_result is not None else probe_vulkan(runner)
    reims = reims_probe_result if reims_probe_result is not None else probe_reims_gpus(reims_runner)
    pci_ids_by_bdf = probe.pci_ids_by_bdf or {}
    physical_counts: dict[tuple[str, str], int] = {}
    for ids in pci_ids_by_bdf.values():
        if ids[0] is not None and ids[1] is not None:
            key = ids[0], ids[1]
            physical_counts[key] = physical_counts.get(key, 0) + 1
    result: list[GpuInventoryItem] = []
    for entry in entries:
        bdf = _canonical_bdf(entry.name)
        if not bdf:
            continue
        class_id = _read_text(entry / "class")
        if not class_id or not class_id.lower().startswith("0x03"):
            continue
        vendor_raw = _read_text(entry / "vendor")
        device_raw = _read_text(entry / "device")
        vendor_id = _normalize_id(vendor_raw)
        device_id = _normalize_id(device_raw)
        if not vendor_id or not device_id:
            continue
        vendor_hex = f"0x{vendor_id}"
        vendor_label, kind = _device_kind(vendor_hex)
        pair = (vendor_id, device_id)
        matching_reims = [
            item for item in reims.devices
            if item.vendor_id == pair[0] and item.device_id == pair[1]
        ] if reims.available else []
        exact_reims = [item for item in reims.devices if item.pci_address == bdf] if reims.available else []
        if len(exact_reims) == 1 and exact_reims[0].kind:
            kind = exact_reims[0].kind
        display_name = (
            matching_reims[0].name
            if len(matching_reims) == 1 and matching_reims[0].name
            else f"{vendor_label} [{vendor_id}:{device_id}]"
        )
        render_nodes = _render_nodes(entry, dev)
        associated_uuid = probe.devices_by_bdf.get(bdf) if probe.available else None
        vk_ids = pci_ids_by_bdf.get(bdf)
        selection_supported = False
        driver_file = None
        limitation: str | None = None
        if exact_reims:
            identity = exact_reims[0]
            if identity.vendor_id != pair[0] or identity.device_id != pair[1]:
                limitation = "A identidade PCI informada pelo Reims diverge do inventário do host"
            elif associated_uuid and vk_ids and vk_ids != pair:
                limitation = "A identidade Vulkan diverge dos IDs PCI informados pelo Reims"
            else:
                selection_supported = True
                driver_file = identity.driver_file
        elif not reims.available:
            limitation = reims.limitation or "Inventário Reims indisponível"
        elif not associated_uuid:
            limitation = "Sonda Vulkan não associou UUID a este BDF exato"
        elif not vk_ids or vk_ids != pair:
            limitation = "Sonda Vulkan não confirmou vendor/device IDs para este BDF"
        elif len(pci_ids_by_bdf) != len(probe.devices_by_bdf):
            limitation = "Sonda Vulkan omitiu IDs de alguma placa; não é possível excluir GPU homônima"
        elif physical_counts.get(pair, 0) > 1:
            limitation = "Placas homônimas com o mesmo device_id: filtro Reims não distingue o BDF com segurança"
        elif len(matching_reims) != 1:
            limitation = (
                "Nenhuma opção Reims corresponde unicamente ao par vendor/device Vulkan"
                if not matching_reims
                else "Várias opções Reims correspondem ao mesmo par vendor/device"
            )
        else:
            selection_supported = True
            driver_file = matching_reims[0].driver_file
        if not render_nodes:
            limitation = (limitation + "; " if limitation else "") + "nó DRM render não disponível"
        if not probe.available:
            limitation = (limitation + "; " if limitation else "") + f"Vulkan indisponível: {probe.limitation or 'sem evidência verificável'}"
        result.append(
            GpuInventoryItem(
                pci_bdf=bdf,
                vendor_id=vendor_id,
                device_id=device_id,
                name=display_name,
                kind=kind,
                render_node=render_nodes[0] if render_nodes else None,
                vulkan_uuid=associated_uuid,
                driver_file=driver_file,
                vulkan_available=bool(associated_uuid),
                used_by_host=_host_display_usage(entry, sysfs),
                selection_supported=selection_supported,
                limitation=limitation,
                reims_device_uuid=exact_reims[0].device_uuid if len(exact_reims) == 1 else None,
                reims_pci_address=exact_reims[0].pci_address if len(exact_reims) == 1 else None,
                reims_shares_driver=exact_reims[0].shares_driver if len(exact_reims) == 1 else None,
            )
        )
    return result
