"""Headless orchestration for creating a macOS VM and its boot artifacts.

This service contains the decisions that used to be collected by the TUI
wizard, but accepts them as data and reports progress through a callback. It
does not launch the resulting VM. TUI/CLI calls remain synchronous; the web job
path supplies a JobContext that supervises external commands and checks
cancellation while streaming downloads. Partial artifacts are retained for
inspection and recovery after cancellation or timeout.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from core.config import CONFIG_DIR, CpuTopologyConfig, VMConfig, VMManagerStore
from core.display import validate_display_options
from core.qemu_args import validate_qemu_extra_args
from core.smbios import GenSMBIOS

if TYPE_CHECKING:
    from core.disk import VMDiskBundle


class InstallerPolicy(str, Enum):
    """How provisioning obtains an installer image."""

    CACHED = "cached"          # require an already-cached installer
    DOWNLOAD = "download"      # use cache, then explicitly download if missing
    NONE = "none"              # create the VM without installer media


class ProvisioningCancelled(RuntimeError):
    """Raised at a safe checkpoint after a caller requests cancellation."""


class ProvisioningTimedOut(TimeoutError):
    """Raised when the deadline expires at a safe checkpoint."""


class ProvisioningError(RuntimeError):
    """Invalid input or a failed provisioning dependency."""


@dataclass(frozen=True)
class VMProvisioningRequest:
    name: str
    macos_version: str
    vcpus: int
    ram_gb: int
    cpu_model: str = "Skylake-Client-noTSX-IBRS"
    cpu_topology: CpuTopologyConfig | None = None
    disk_size_gb: int = 64
    auto_start: bool = False
    selected_gpu: str | None = None
    selected_gpu_name: str | None = None
    selected_gpu_uuid: str | None = None
    selected_gpu_device_uuid: str | None = None
    selected_gpu_pci_address: str | None = None
    selected_gpu_driver_file: str | None = None
    installer_policy: InstallerPolicy = InstallerPolicy.NONE
    installer_path: str | Path | None = None
    vm_id: str | None = None
    fullscreen: bool = True
    display_resolution: str = "1920x1080"
    display_count: int = 1
    display_hidpi: bool = False
    display_refresh_hz: int | None = None
    opencore_show_picker: bool | None = None
    usb_auto_attach: list[str] = field(default_factory=list)
    qemu_extra_args: list[str] = field(default_factory=list)
    audio_enabled: bool = True
    audio_device: str = "usb-audio"
    audio_backend: str = "auto"
    network_enabled: bool = True
    network_mac: str | None = None
    network_ssh_port: int = 0


@dataclass(frozen=True)
class ProvisioningEvent:
    stage: str
    status: str
    progress: int
    message: str = ""
    vm_id: str | None = None


@dataclass(frozen=True)
class VMProvisioningResult:
    vm: VMConfig
    bundle: VMDiskBundle
    installer_path: Path | None


class _Store(Protocol):
    def list_vms(self) -> list[VMConfig]: ...
    def add_vm(self, vm: VMConfig, **kwargs: Any) -> None: ...


EventCallback = Callable[[ProvisioningEvent], None]
GPUValidator = Callable[[str], bool | tuple[bool, str]]


class VMProvisioningService:
    """Build and register a VM without asking questions or starting it.

    Dependencies are injectable to make orchestration testable without
    touching the host, downloading Apple media, or invoking QEMU tooling.
    ``timeout_sec`` and ``cancel_event`` are checked between stages. When
    ``job_context`` is supplied, host commands are also supervised and downloads
    poll cancellation while streaming. Cancellation can leave an unregistered
    VM directory; artifacts are retained for inspection rather than rolled back.
    """

    _ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
    _VERSIONS = frozenset({"tahoe", "sequoia", "sonoma", "ventura"})

    def __init__(
        self,
        *,
        store: _Store | None = None,
        disk_provisioner: Any | None = None,
        smbios_generator: Any = GenSMBIOS,
        cached_installer: Callable[[str], Path | None] | None = None,
        prepare_installer: Callable[[str], Path | None] | None = None,
        gpu_validator: GPUValidator | None = None,
        storage_root: str | Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] | None = None,
        supported_versions: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.store = store or VMManagerStore()
        self.disk_provisioner = disk_provisioner
        self.smbios_generator = smbios_generator
        self.cached_installer = cached_installer
        self.prepare_installer = prepare_installer
        self.gpu_validator = gpu_validator or self._validate_gpu_selection
        self.storage_root = Path(storage_root) if storage_root is not None else CONFIG_DIR / "vms"
        self.clock = clock
        self.id_factory = id_factory or (lambda: secrets.token_hex(8))
        self.supported_versions = frozenset(supported_versions or self._VERSIONS)

    def provision(
        self,
        request: VMProvisioningRequest,
        *,
        on_event: EventCallback | None = None,
        cancel_event: threading.Event | None = None,
        timeout_sec: float | None = None,
        job_context: Any | None = None,
    ) -> VMProvisioningResult:
        """Provision artifacts and add the completed VM to the catalog."""
        self._validate(request)
        if timeout_sec is not None and timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        deadline = self.clock() + timeout_sec if timeout_sec is not None else None
        emit = on_event or (lambda event: None)

        retrying_existing_job = bool(job_context is not None and job_context.completed("validation"))
        vm_id = self._reserve_id(request.vm_id, request.name, allow_existing_artifacts=retrying_existing_job)
        self._checkpoint(cancel_event, deadline)
        emit(ProvisioningEvent("validation", "completed", 10, "Configuração validada", vm_id))

        installer = self._resolve_installer(request, job_context=job_context)
        self._checkpoint(cancel_event, deadline)
        emit(ProvisioningEvent("installer", "completed", 30, "Mídia de instalação resolvida", vm_id))

        smbios = self.smbios_generator.generate(request.macos_version)
        self._checkpoint(cancel_event, deadline)
        emit(ProvisioningEvent("identity", "completed", 45, "Identidade OpenCore gerada", vm_id))

        vm = VMConfig(
            id=vm_id,
            name=request.name.strip(),
            macos_version=request.macos_version.lower(),
            vcpus=request.vcpus,
            cpu_model=request.cpu_model,
            cpu_topology=request.cpu_topology,
            ram_gb=request.ram_gb,
            disk_size_gb=request.disk_size_gb,
            disk_path="",
            auto_start=request.auto_start,
            selected_gpu=request.selected_gpu,
            selected_gpu_name=request.selected_gpu_name,
            selected_gpu_uuid=request.selected_gpu_uuid,
            selected_gpu_device_uuid=request.selected_gpu_device_uuid,
            selected_gpu_pci_address=request.selected_gpu_pci_address,
            selected_gpu_driver_file=request.selected_gpu_driver_file,
            audio_device=request.audio_device,
            audio_enabled=request.audio_enabled,
            audio_backend=request.audio_backend,
            network_enabled=request.network_enabled,
            network_mac=request.network_mac,
            network_ssh_port=request.network_ssh_port,
            smbios=smbios,
            opencore_show_picker=(bool(installer) if request.opencore_show_picker is None else request.opencore_show_picker),
            fullscreen=request.fullscreen,
            display_resolution=request.display_resolution,
            display_count=request.display_count,
            display_hidpi=request.display_hidpi,
            display_refresh_hz=request.display_refresh_hz,
            created_at=datetime.now(timezone.utc).astimezone().isoformat(),
            power_sync_mode="vm-only",
            usb_auto_attach=list(request.usb_auto_attach),
            qemu_extra_args=list(request.qemu_extra_args),
        )

        self._checkpoint(cancel_event, deadline)
        emit(ProvisioningEvent("disk_opencore", "started", 50, "Criando disco e OpenCore", vm_id))
        try:
            # Reserve the per-VM directory atomically so another request cannot
            # win the same ID between the availability check and DiskProvisioner.
            # The existing provisioner accepts this directory (exist_ok=True).
            (self.storage_root / vm_id).mkdir(
                parents=True, exist_ok=retrying_existing_job, mode=0o700
            )
            disk_provisioner = self.disk_provisioner
            if disk_provisioner is None:
                # Import the Rich-backed legacy provisioner only when actual
                # host provisioning is requested. API consumers can import and
                # construct this orchestration layer without pulling UI code.
                from core.disk import DiskProvisioner

                disk_provisioner = DiskProvisioner
            if job_context is None:
                bundle = disk_provisioner.provision(vm, installer_img=installer)
            else:
                bundle = disk_provisioner.provision(vm, installer_img=installer, job_context=job_context)
        except Exception as exc:
            # Preserve job control flow so the runner records cancelled,
            # timed-out, and interrupted states instead of disk failure.
            from core.web_job_runner import JobRunnerError
            if isinstance(exc, (ProvisioningCancelled, ProvisioningTimedOut, JobRunnerError)):
                raise
            raise ProvisioningError(f"Falha ao provisionar disco/OpenCore: {exc}") from exc
        self._checkpoint(cancel_event, deadline)

        if not bundle.hdd_path.is_file() or not bundle.opencore_path.is_file():
            raise ProvisioningError("O provisionador não confirmou os arquivos de disco e OpenCore")
        vm.disk_path = str(bundle.hdd_path)
        emit(ProvisioningEvent("disk_opencore", "completed", 90, "Disco e OpenCore prontos", vm_id))

        self._checkpoint(cancel_event, deadline)
        expected_revision = getattr(self.store, "revision", None)
        add_kwargs = {"expected_revision": expected_revision} if expected_revision else {}
        try:
            self.store.add_vm(vm, **add_kwargs)
        except Exception as exc:
            # Disk artifacts remain available for inspection/recovery. Never
            # remove them automatically when catalog registration fails.
            raise ProvisioningError(f"VM provisionada, mas não foi registrada no catálogo: {exc}") from exc
        emit(ProvisioningEvent("catalog", "completed", 100, "VM criada e registrada", vm_id))
        return VMProvisioningResult(vm=vm, bundle=bundle, installer_path=installer)

    def _validate(self, request: VMProvisioningRequest) -> None:
        if not isinstance(request, VMProvisioningRequest):
            raise TypeError("request must be a VMProvisioningRequest")
        if not request.name.strip() or len(request.name.strip()) > 100:
            raise ValueError("name must contain 1 to 100 characters")
        if request.macos_version.lower() not in self.supported_versions:
            raise ValueError("unsupported macOS version")
        if isinstance(request.vcpus, bool) or not 1 <= request.vcpus <= 256:
            raise ValueError("vcpus must be between 1 and 256")
        if request.cpu_topology is not None:
            if not isinstance(request.cpu_topology, CpuTopologyConfig):
                raise ValueError("cpu_topology must be a validated CpuTopologyConfig")
            request.cpu_topology.validate(vcpus=request.vcpus)
        if not isinstance(request.cpu_model, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,100}", request.cpu_model):
            raise ValueError("cpu_model must be a QEMU model name without options")
        if isinstance(request.ram_gb, bool) or not 1 <= request.ram_gb <= 4096:
            raise ValueError("ram_gb must be between 1 and 4096")
        if isinstance(request.disk_size_gb, bool) or not 16 <= request.disk_size_gb <= 65536:
            raise ValueError("disk_size_gb must be between 16 and 65536")
        if isinstance(request.display_count, bool) or not isinstance(request.display_count, int) or not 1 <= request.display_count <= 8:
            raise ValueError("display_count must be between 1 and 8")
        validate_display_options(request.display_resolution, request.display_hidpi, request.display_refresh_hz)
        validate_qemu_extra_args(request.qemu_extra_args)
        if not isinstance(request.audio_backend, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", request.audio_backend):
            raise ValueError("audio_backend must be auto or a QEMU backend name")
        if not isinstance(request.audio_enabled, bool):
            raise ValueError("audio_enabled must be a boolean")
        if request.audio_device not in {"usb-audio", "virtio-sound"}:
            raise ValueError("audio_device must be usb-audio or virtio-sound")
        if not isinstance(request.network_enabled, bool):
            raise ValueError("network_enabled must be a boolean")
        if isinstance(request.network_ssh_port, bool) or not isinstance(request.network_ssh_port, int) or not 0 <= request.network_ssh_port <= 65535:
            raise ValueError("network_ssh_port must be zero or a TCP port between 1 and 65535")
        if request.network_ssh_port and not request.network_enabled:
            raise ValueError("network_ssh_port requires network_enabled")
        if request.network_mac is not None and (
            not isinstance(request.network_mac, str)
            or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", request.network_mac)
            or int(request.network_mac[:2], 16) & 1
        ):
            raise ValueError("network_mac must be a unicast MAC address")
        if len(request.usb_auto_attach) > 32 or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", value)
            for value in request.usb_auto_attach
        ):
            raise ValueError("usb_auto_attach must contain at most 32 vendor:product identifiers")
        normalized_usb = [value.lower() for value in request.usb_auto_attach]
        if len(normalized_usb) != len(set(normalized_usb)):
            raise ValueError("usb_auto_attach cannot contain duplicate identifiers")
        try:
            policy = InstallerPolicy(request.installer_policy)
        except ValueError as exc:
            raise ValueError("installer_policy must be cached, download or none") from exc
        if policy is InstallerPolicy.NONE and request.installer_path:
            raise ValueError("installer_path cannot be set when installer_policy is none")
        if policy is not InstallerPolicy.NONE and request.installer_path:
            if not Path(request.installer_path).is_file():
                raise ValueError("installer_path must refer to an existing file")
        if request.selected_gpu:
            outcome = self.gpu_validator(request.selected_gpu)
            if isinstance(outcome, tuple):
                supported, reason = outcome
            else:
                supported, reason = outcome, "GPU selection cannot be mapped uniquely to a Reims Vulkan ICD"
            if not supported:
                raise ValueError(reason)
        elif request.selected_gpu_name or request.selected_gpu_uuid:
            raise ValueError("GPU name/UUID requires an explicit PCI slot selection")

    def _reserve_id(
        self, requested_id: str | None, name: str, *, allow_existing_artifacts: bool = False
    ) -> str:
        known = {vm.id for vm in self.store.list_vms()}
        if requested_id:
            candidate = requested_id.strip().lower()
            if not self._ID_RE.fullmatch(candidate):
                raise ValueError("vm_id must be a lowercase slug of at most 63 characters")
            if candidate in known or ((self.storage_root / candidate).exists() and not allow_existing_artifacts):
                raise ValueError("vm_id is already registered or has existing VM artifacts")
            return candidate
        for _ in range(16):
            # Use a stable readable prefix when possible, with a random suffix
            # large enough to avoid the wizard's former four-hex collision risk.
            base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:46].strip("-") or "vm"
            candidate = f"{base}-{self.id_factory()}".strip("-")
            if self._ID_RE.fullmatch(candidate) and candidate not in known and not (self.storage_root / candidate).exists():
                return candidate
        raise ProvisioningError("Não foi possível reservar um identificador de VM livre")

    def _resolve_installer(self, request: VMProvisioningRequest, *, job_context: Any | None = None) -> Path | None:
        policy = InstallerPolicy(request.installer_policy)
        if policy is InstallerPolicy.NONE:
            return None
        if request.installer_path:
            return Path(request.installer_path)
        cached_lookup = self.cached_installer
        prepare = self.prepare_installer
        if cached_lookup is None or prepare is None:
            # The historical downloader owns the Rich presentation. Load it
            # only for policies that actually need its cached/download flow.
            from core.downloader import MacOSDownloader

            cached_lookup = cached_lookup or MacOSDownloader.get_cached_image_path
            prepare = prepare or MacOSDownloader.prepare_installer
        cached = cached_lookup(request.macos_version.lower())
        if cached and Path(cached).is_file():
            return Path(cached)
        if policy is InstallerPolicy.CACHED:
            raise ProvisioningError("Não há uma imagem de instalação em cache para essa versão")
        if job_context is not None and getattr(prepare, "__module__", "") == "core.downloader":
            downloaded = prepare(request.macos_version.lower(), job_context=job_context)
        else:
            downloaded = prepare(request.macos_version.lower())
        if downloaded and Path(downloaded).is_file():
            return Path(downloaded)
        raise ProvisioningError("Não foi possível obter uma imagem de instalação válida")

    @staticmethod
    def _validate_gpu_selection(slot: str) -> tuple[bool, str]:
        if not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", slot):
            return False, "GPU selection must be a PCI BDF such as 0000:01:00.0"
        # Reims chooses an ICD driver, rather than a PCI function. Accept only
        # when this slot maps to exactly one enumerated ICD entry; a guessed
        # default could silently start the VM on another host GPU.
        try:
            from core.reims_ctl import _gpus_do_reims_ctl, _vendor_device_do_slot

            vendor_device = _vendor_device_do_slot(slot)
            if not vendor_device:
                return False, "Selected PCI GPU is not present in sysfs"
            matches = [gpu for gpu in _gpus_do_reims_ctl() if gpu.get("vendor_device") == vendor_device]
            if len(matches) != 1 or not matches[0].get("icd"):
                return False, "Selected GPU has no unique Reims Vulkan ICD mapping"
            return True, ""
        except Exception as exc:
            return False, f"Unable to verify the selected GPU: {exc}"

    def _checkpoint(
        self,
        cancel_event: threading.Event | None,
        deadline: float | None,
    ) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ProvisioningCancelled("Provisioning was cancelled at a safe checkpoint")
        if deadline is not None and self.clock() >= deadline:
            raise ProvisioningTimedOut("Provisioning deadline elapsed at a safe checkpoint")
