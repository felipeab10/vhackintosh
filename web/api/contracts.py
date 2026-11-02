"""Stable, typed HTTP contracts shared by the API and generated UI client.

The models deliberately describe observed/available data separately from
configured values.  They contain no QEMU or shell execution behavior.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.display import validate_display_options
from core.qemu_args import validate_qemu_extra_args


DEFAULT_MACOS_CPU_MODEL = "Skylake-Client-noTSX-IBRS"
_CPU_MODEL_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,100}$")
_AUDIO_BACKEND_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_NETWORK_MAC_RE = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def _validate_cpu_model(value: str) -> str:
    if not isinstance(value, str) or not _CPU_MODEL_RE.fullmatch(value):
        raise ValueError("Modelo de CPU inválido; use um nome de modelo QEMU, sem opções ou argumentos.")
    return value


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApiError(Contract):
    model_config = ConfigDict(
        extra="forbid", frozen=True,
        json_schema_extra={"examples": [{
            "code": "catalog_conflict",
            "message": "O catálogo mudou desde a última leitura. Recarregue as configurações.",
            "request_id": "7d3a10595e933acd2f2c2d2b28a2c20e",
            "details": {},
        }]},
    )
    code: str
    message: str
    request_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class AuditEvent(Contract):
    created_at: datetime
    actor: str
    action: str
    target: str
    outcome: int = Field(ge=100, le=599)
    request_id: str
    operation_id: str


class AuditListResponse(Contract):
    items: list[AuditEvent]
    limit: int = Field(ge=1, le=1000)


class PrivateTransferView(Contract):
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    filename: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=1)
    format: Literal["iso", "qcow2", "dmg"]


class PrivateTransferListResponse(Contract):
    items: list[PrivateTransferView]
    max_file_bytes: int = Field(ge=1)
    max_total_bytes: int = Field(ge=1)
    min_free_bytes: int = Field(ge=0)


class MediaConvertRequest(Contract):
    output_format: Literal["raw", "qcow2"]
    idempotency_key: str = Field(pattern=r"^[0-9a-f-]{36}$")


class AppleRecoveryDownloadRequest(Contract):
    macos_version: Literal["tahoe", "sequoia", "ventura"]
    idempotency_key: str = Field(pattern=r"^[0-9a-f-]{36}$")


class StorageItem(Contract):
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    name: str = Field(min_length=1, max_length=255)
    location: Literal["managed", "external"]
    availability: Literal["available", "missing", "external"]
    virtual_size_bytes: int | None = Field(default=None, ge=0)
    allocated_bytes: int | None = Field(default=None, ge=0)
    references: list[str] = Field(default_factory=list)


class AdditionalDiskConfig(Contract):
    media_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    path: str = Field(min_length=1, max_length=4096)
    format: Literal["qcow2", "raw"]
    controller: Literal["ahci", "nvme"]
    order: int = Field(ge=0, le=14)


class AdditionalDiskDraft(Contract):
    media_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    controller: Literal["ahci", "nvme"]
    order: int = Field(ge=0, le=14)


class CpuTopology(Contract):
    sockets: int = Field(ge=1, le=256, strict=True)
    cores: int = Field(ge=1, le=256, strict=True)
    threads: int = Field(ge=1, le=256, strict=True)


class StorageInventory(Contract):
    items: list[StorageItem]
    free_bytes: int | None = Field(default=None, ge=0)
    managed_volume: str


class VmBackupItem(Contract):
    backup_id: str = Field(pattern=r"^[0-9a-f]{32}\.vhb$")
    created_at: datetime
    size_bytes: int = Field(ge=0)
    file_count: int = Field(ge=0, le=100)
    metadata_valid: bool
    integrity_note: str = Field(max_length=200)


class VmBackupListResponse(Contract):
    vm_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    items: list[VmBackupItem]


class Health(Contract):
    status: Literal["ok", "degraded", "unavailable"]
    version: str
    agent_connected: bool
    checked_at: datetime


class SessionView(Contract):
    authenticated: bool
    username: str | None = None
    expires_at: datetime | None = None
    bootstrap_required: bool = False
    must_change_password: bool = False
    default_credentials_available: bool = False
    tls_fingerprint: str | None = None


class WebActivationView(Contract):
    appliance_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    activation_code: str = Field(pattern=r"^VH-(?:[A-F0-9]{4}-){5}[A-F0-9]{4}$")
    status: Literal["not_submitted", "pending", "approved", "rejected"]
    updated_at: datetime
    portal_url: str
    inventory_changed: bool = False


class WebActivationSubmitResponse(Contract):
    status: Literal["pending", "approved", "rejected"]
    updated_at: datetime


class BootstrapRequest(Contract):
    code: str = Field(min_length=8, max_length=128)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=12, max_length=1024)


class LoginRequest(Contract):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class PasswordChangeRequest(Contract):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class VmRunState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class InstallPhase(StrEnum):
    PENDING = "pending"
    INSTALLING = "installing"
    INSTALLED = "installed"


class GpuVerification(StrEnum):
    CONFIRMED = "confirmed"
    UNAVAILABLE = "unavailable"
    MISMATCH = "mismatch"
    AMBIGUOUS = "ambiguous"


class GpuDevice(Contract):
    pci_bdf: str
    vendor_id: str
    device_id: str
    name: str
    kind: str
    render_node: str | None = None
    vulkan_uuid: str | None = None
    driver_file: str | None = None
    reims_device_uuid: str | None = None
    reims_pci_address: str | None = None
    reims_shares_driver: bool | None = None
    vulkan_available: bool
    used_by_host: bool | None = None
    assigned_vm_id: str | None = None
    selection_supported: bool
    limitation: str | None = None


class GpuInventoryResponse(Contract):
    items: list[GpuDevice]
    stale: bool = False
    message: str | None = None


class HostUsbDevice(Contract):
    device_id: str = Field(pattern=r"^[0-9a-f]{4}:[0-9a-f]{4}$")
    label: str = Field(min_length=1, max_length=160)
    device_count: int = Field(ge=1, le=256)
    auto_attach_supported: bool
    limitation: str | None = None


class HostUsbInventory(Contract):
    items: list[HostUsbDevice]
    stale: bool = False
    message: str | None = None


def _normalize_usb_auto_attach(values: list[str]) -> list[str]:
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", value) for value in values):
        raise ValueError("Seleção USB deve usar identificadores vendor:product válidos.")
    normalized = [value.lower() for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError("Um dispositivo USB não pode ser selecionado mais de uma vez.")
    return normalized


class GpuAssignment(Contract):
    configured_bdf: str | None = None
    configured_uuid: str | None = None
    configured_device_uuid: str | None = None
    configured_pci_address: str | None = None
    configured_driver_file: str | None = None
    effective_bdf: str | None = None
    effective_uuid: str | None = None
    verification: GpuVerification
    message: str | None = None


class SmbiosConfig(Contract):
    model: str
    serial_number: str
    board_serial: str
    smuuid: str
    rom: str


class VmConfig(Contract):
    model_config = ConfigDict(
        extra="forbid", frozen=True,
        json_schema_extra={"examples": [{
            "id": "macos-tahoe",
            "name": "macOS Tahoe",
            "macos_version": "tahoe",
            "vcpus": 8,
            "cpu_model": DEFAULT_MACOS_CPU_MODEL,
            "cpu_topology": None,
            "ram_gb": 16,
            "disk_size_gb": 128,
            "disk_path": "/var/lib/vhackintosh/vms/macos-tahoe/macos.qcow2",
            "additional_disks": [],
            "auto_start": False,
            "smbios": {"model": "iMac20,1", "serial_number": "", "board_serial": "", "smuuid": "", "rom": ""},
            "gpu_mode": "reims-vgpu",
            "vfio_pci_id": None,
            "selected_gpu": None,
            "selected_gpu_name": None,
            "selected_gpu_uuid": None,
            "selected_gpu_device_uuid": None,
            "selected_gpu_pci_address": None,
            "selected_gpu_driver_file": None,
            "audio_device": "usb-audio",
            "audio_enabled": True,
            "audio_backend": "auto",
            "network_enabled": True,
            "network_mac": None,
            "network_ssh_port": None,
            "fullscreen": True,
            "display_resolution": "1920x1080",
            "display_count": 1,
            "display_hidpi": False,
            "display_refresh_hz": None,
            "opencore_show_picker": False,
            "created_at": "2026-01-15T10:30:00Z",
            "last_booted_at": None,
            "install_phase": "installed",
            "install_reboots": 2,
            "install_evidence": "complete",
            "usb_auto_attach": [],
            "qemu_extra_args": [],
            "extra_config": {},
            "power_sync_mode": "vm-only",
            "revision": "a1" * 32,
        }]},
    )
    id: str
    name: str
    macos_version: str
    vcpus: int = Field(ge=1)
    cpu_model: str = DEFAULT_MACOS_CPU_MODEL
    cpu_topology: CpuTopology | None = None
    ram_gb: int = Field(ge=1)
    disk_size_gb: int = Field(ge=1)
    disk_path: str = ""
    additional_disks: list[AdditionalDiskConfig] = Field(default_factory=list, max_length=15)
    auto_start: bool = False
    smbios: SmbiosConfig
    gpu_mode: str = "reims-vgpu"
    vfio_pci_id: str | None = None
    selected_gpu: str | None = None
    selected_gpu_name: str | None = None
    selected_gpu_uuid: str | None = None
    selected_gpu_device_uuid: str | None = None
    selected_gpu_pci_address: str | None = None
    selected_gpu_driver_file: str | None = None
    audio_device: str
    audio_enabled: bool = True
    audio_backend: str = "auto"
    network_enabled: bool = True
    network_mac: str | None = None
    network_ssh_port: int | None = Field(default=None, ge=0, le=65535)
    fullscreen: bool
    display_resolution: str = "1920x1080"
    display_count: int = Field(default=1, ge=1, le=8)
    display_hidpi: bool = False
    display_refresh_hz: int | None = Field(default=None, ge=1, le=1000)
    opencore_show_picker: bool
    created_at: str
    last_booted_at: datetime | None = None
    install_phase: InstallPhase
    install_reboots: int = Field(ge=0)
    install_evidence: str
    usb_auto_attach: list[str] = Field(default_factory=list)
    qemu_extra_args: list[str] = Field(default_factory=list)
    extra_config: dict[str, Any] = Field(default_factory=dict)
    power_sync_mode: Literal["vm-only", "kiosk"] = "vm-only"
    revision: str


class VmConfigDraft(Contract):
    """User-editable settings that the appliance can currently persist safely."""

    model_config = ConfigDict(
        extra="forbid", frozen=True,
        json_schema_extra={"examples": [{
            "name": "macOS Tahoe",
            "macos_version": "tahoe",
            "vcpus": 8,
            "cpu_model": DEFAULT_MACOS_CPU_MODEL,
            "ram_gb": 16,
            "disk_size_gb": 128,
            "additional_disks": [],
            "auto_start": False,
            "audio_device": "usb-audio",
            "audio_enabled": True,
            "audio_backend": "auto",
            "fullscreen": True,
            "display_resolution": "1920x1080",
            "display_count": 1,
            "display_hidpi": False,
            "display_refresh_hz": None,
            "opencore_show_picker": True,
            "gpu_mode": "reims-vgpu",
            "selected_gpu": None,
            "selected_gpu_uuid": None,
            "selected_gpu_device_uuid": None,
            "network_enabled": True,
            "network_mac": None,
            "network_ssh_port": 0,
            "power_sync_mode": "vm-only",
            "qemu_extra_args": [],
            "usb_auto_attach": [],
        }]},
    )

    name: str = Field(min_length=1, max_length=80)
    macos_version: str = Field(min_length=1, max_length=32)
    vcpus: int = Field(ge=1, le=256)
    cpu_model: str = Field(default=DEFAULT_MACOS_CPU_MODEL, min_length=1, max_length=100)
    cpu_topology: CpuTopology | None = None
    ram_gb: int = Field(ge=1, le=2048)
    disk_size_gb: int = Field(ge=16, le=4096)
    additional_disks: list[AdditionalDiskDraft] = Field(default_factory=list, max_length=15)
    auto_start: bool = False
    audio_device: Literal["usb-audio", "virtio-sound"] = "usb-audio"
    audio_enabled: bool = True
    # `auto` is mapped to an omitted Reims backend so its host-native default applies.
    audio_backend: str = Field(default="auto", min_length=1, max_length=32)
    fullscreen: bool = True
    display_resolution: str = Field(default="1920x1080", min_length=1, max_length=32)
    display_count: int = Field(default=1, ge=1, le=8)
    display_hidpi: bool = False
    display_refresh_hz: int | None = Field(default=None, ge=1, le=1000)
    opencore_show_picker: bool = False
    gpu_mode: Literal["reims-vgpu"] = "reims-vgpu"
    # Explicitly represented so the API can return a clear capability error
    # instead of silently storing a selection Reims cannot apply.
    selected_gpu: str | None = None
    selected_gpu_uuid: str | None = None
    selected_gpu_device_uuid: str | None = Field(default=None, max_length=128)
    network_enabled: bool = True
    network_mac: str | None = None
    # Zero explicitly disables Reims' localhost-only SSH host forward.
    network_ssh_port: int | None = Field(default=0, ge=0, le=65535)
    power_sync_mode: Literal["vm-only"] = "vm-only"
    # Arguments are always tokens (never a command string); non-empty values
    # are currently rejected until the Reims launch contract supports them.
    qemu_extra_args: list[str] = Field(default_factory=list, max_length=64)
    usb_auto_attach: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("display_resolution")
    @classmethod
    def normalize_display_resolution(cls, value: str) -> str:
        return value.strip().lower().replace("×", "x")

    @field_validator("cpu_model")
    @classmethod
    def validate_cpu_model(cls, value: str) -> str:
        return _validate_cpu_model(value)

    @field_validator("audio_backend")
    @classmethod
    def validate_audio_backend_name(cls, value: str) -> str:
        if not _AUDIO_BACKEND_RE.fullmatch(value):
            raise ValueError("Backend de áudio inválido.")
        return value

    @field_validator("network_mac")
    @classmethod
    def validate_network_mac(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.lower()
        if not _NETWORK_MAC_RE.fullmatch(value) or int(value[:2], 16) & 1:
            raise ValueError("Informe um MAC unicast com seis pares hexadecimais.")
        return value

    @field_validator("usb_auto_attach")
    @classmethod
    def validate_usb_auto_attach(cls, values: list[str]) -> list[str]:
        return _normalize_usb_auto_attach(values)

    @model_validator(mode="after")
    def validate_display_options(self) -> VmConfigDraft:
        validate_display_options(self.display_resolution, self.display_hidpi, self.display_refresh_hz)
        if self.network_ssh_port and not self.network_enabled:
            raise ValueError("Habilite a rede da VM para encaminhar a porta SSH.")
        if self.cpu_topology is not None and self.cpu_topology.sockets * self.cpu_topology.cores * self.cpu_topology.threads != self.vcpus:
            raise ValueError("sockets × cores × threads deve ser igual à quantidade de vCPUs.")
        if [disk.order for disk in self.additional_disks] != list(range(len(self.additional_disks))):
            raise ValueError("A ordem dos discos adicionais deve ser única e sequencial a partir de zero.")
        if len({disk.media_id for disk in self.additional_disks}) != len(self.additional_disks):
            raise ValueError("Selecione cada mídia adicional uma única vez.")
        return self

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Informe um nome sem caracteres de controle.")
        return value

    @field_validator("selected_gpu")
    @classmethod
    def validate_gpu_identifier(cls, value: str | None) -> str | None:
        if value is not None and (len(value) > 128 or any(ord(char) < 32 for char in value)):
            raise ValueError("Identificador de GPU inválido.")
        return value

    @field_validator("selected_gpu_uuid")
    @classmethod
    def validate_gpu_uuid(cls, value: str | None) -> str | None:
        if value is not None and (len(value) > 128 or any(ord(char) < 32 for char in value)):
            raise ValueError("UUID de GPU inválido.")
        return value

    @field_validator("selected_gpu_device_uuid")
    @classmethod
    def validate_reims_gpu_uuid(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > 128 or any(ord(char) < 32 for char in value)):
            raise ValueError("UUID Reims da GPU inválido.")
        return value

    @field_validator("qemu_extra_args")
    @classmethod
    def validate_qemu_tokens(cls, value: list[str]) -> list[str]:
        return validate_qemu_extra_args(value)


class VmConfigCreate(VmConfigDraft):
    model_config = ConfigDict(
        extra="forbid", frozen=True,
        json_schema_extra={"examples": [{
            "id": "macos-tahoe",
            "name": "macOS Tahoe",
            "macos_version": "tahoe",
            "vcpus": 8,
            "ram_gb": 16,
            "disk_size_gb": 128,
            "auto_start": False,
            "audio_device": "usb-audio",
            "audio_enabled": True,
            "fullscreen": True,
            "opencore_show_picker": True,
            "gpu_mode": "reims-vgpu",
            "selected_gpu": None,
            "selected_gpu_uuid": None,
            "network_enabled": True,
            "network_ssh_port": 0,
            "display_count": 1,
            "power_sync_mode": "vm-only",
            "qemu_extra_args": [],
            "usb_auto_attach": [],
        }]},
    )
    id: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")


class VmConfigUpdate(VmConfigDraft):
    """Full editable snapshot; identity, disk path and install state stay server-owned."""


class VmDeleteRequest(Contract):
    """Deletion is explicit; VM disks are preserved unless separately selected."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    confirmation: str = Field(min_length=1, max_length=63)
    delete_disk: bool = False


class VmDeleteResponse(Contract):
    id: str
    deleted: Literal[True]
    revision: str


class InstallerPolicy(StrEnum):
    CACHED = "cached"
    DOWNLOAD = "download"
    NONE = "none"


class VmProvisionRequest(Contract):
    """Request to provision boot artifacts. Installer policy is always explicit."""

    model_config = ConfigDict(
        extra="forbid", frozen=True,
        json_schema_extra={"examples": [{
            "idempotency_key": "provision-request-01",
            "id": "macos-tahoe",
            "name": "macOS Tahoe",
            "macos_version": "tahoe",
            "vcpus": 8,
            "cpu_model": DEFAULT_MACOS_CPU_MODEL,
            "cpu_topology": None,
            "audio_device": "usb-audio",
            "audio_backend": "auto",
            "audio_enabled": True,
            "ram_gb": 16,
            "disk_size_gb": 128,
            "auto_start": False,
            "network_enabled": True,
            "selected_gpu_bdf": None,
            "selected_gpu_uuid": None,
            "installer_policy": "download",
            "fullscreen": True,
            "display_resolution": "1920x1080",
            "display_count": 1,
            "display_hidpi": False,
            "display_refresh_hz": None,
            "opencore_show_picker": True,
            "usb_auto_attach": [],
        }]},
    )

    idempotency_key: str = Field(min_length=8, max_length=128)
    id: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=100)
    macos_version: str = Field(min_length=1, max_length=32)
    vcpus: int = Field(ge=1, le=256)
    cpu_model: str = Field(default=DEFAULT_MACOS_CPU_MODEL, min_length=1, max_length=100)
    cpu_topology: CpuTopology | None = None
    ram_gb: int = Field(ge=1, le=2048)
    disk_size_gb: int = Field(default=64, ge=16, le=4096)
    auto_start: bool = False
    selected_gpu_bdf: str | None = Field(default=None, max_length=32)
    selected_gpu_uuid: str | None = Field(default=None, max_length=128)
    selected_gpu_device_uuid: str | None = Field(default=None, max_length=128)
    installer_policy: InstallerPolicy
    fullscreen: bool = True
    display_resolution: str = Field(default="1920x1080", min_length=1, max_length=32)
    display_count: int = Field(default=1, ge=1, le=8)
    display_hidpi: bool = False
    display_refresh_hz: int | None = Field(default=None, ge=1, le=1000)
    opencore_show_picker: bool | None = None
    usb_auto_attach: list[str] = Field(default_factory=list, max_length=32)
    qemu_extra_args: list[str] = Field(default_factory=list, max_length=64)
    audio_backend: str = Field(default="auto", min_length=1, max_length=32)
    audio_enabled: bool = True
    audio_device: Literal["usb-audio", "virtio-sound"] = "usb-audio"
    network_enabled: bool = True
    network_mac: str | None = None
    network_ssh_port: int = Field(default=0, ge=0, le=65535)

    @model_validator(mode="after")
    def validate_display_options(self) -> VmProvisionRequest:
        validate_display_options(self.display_resolution, self.display_hidpi, self.display_refresh_hz)
        if self.network_ssh_port and not self.network_enabled:
            raise ValueError("Habilite a rede da VM para encaminhar a porta SSH.")
        if self.cpu_topology is not None and self.cpu_topology.sockets * self.cpu_topology.cores * self.cpu_topology.threads != self.vcpus:
            raise ValueError("sockets × cores × threads deve ser igual à quantidade de vCPUs.")
        if self.selected_gpu_device_uuid is not None and self.selected_gpu_bdf is None:
            raise ValueError("A identidade Reims exige uma placa selecionada.")
        return self

    @field_validator("cpu_model")
    @classmethod
    def validate_cpu_model(cls, value: str) -> str:
        return _validate_cpu_model(value)

    @field_validator("selected_gpu_device_uuid")
    @classmethod
    def validate_provision_reims_gpu_uuid(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > 128 or any(ord(char) < 32 for char in value)):
            raise ValueError("UUID Reims da GPU inválido.")
        return value

    @field_validator("audio_backend")
    @classmethod
    def validate_audio_backend_name(cls, value: str) -> str:
        if not _AUDIO_BACKEND_RE.fullmatch(value):
            raise ValueError("Backend de áudio inválido.")
        return value

    @field_validator("network_mac")
    @classmethod
    def validate_network_mac(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.lower()
        if not _NETWORK_MAC_RE.fullmatch(value) or int(value[:2], 16) & 1:
            raise ValueError("Informe um MAC unicast com seis pares hexadecimais.")
        return value

    @field_validator("usb_auto_attach")
    @classmethod
    def validate_usb_auto_attach(cls, values: list[str]) -> list[str]:
        return _normalize_usb_auto_attach(values)

    @field_validator("qemu_extra_args")
    @classmethod
    def validate_qemu_tokens(cls, value: list[str]) -> list[str]:
        return validate_qemu_extra_args(value)

    @field_validator("name")
    @classmethod
    def validate_provision_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Informe um nome sem caracteres de controle.")
        return value


class JobCheckpoint(Contract):
    name: str
    payload: dict[str, Any] = Field(default_factory=dict)
    completed: bool
    updated_at: datetime


class JobLog(Contract):
    id: int
    created_at: datetime
    level: Literal["debug", "info", "warning", "error"]
    message: str


class JobDetail(Contract):
    model_config = ConfigDict(
        extra="forbid", frozen=True,
        json_schema_extra={"examples": [{
            "id": "7d3a10595e933acd2f2c2d2b28a2c20e",
            "kind": "vm.start",
            "state": "running",
            "stage": "starting_guest",
            "progress_percent": None,
            "created_at": "2026-01-15T10:30:00Z",
            "updated_at": "2026-01-15T10:30:03Z",
            "result": {},
            "error": None,
            "cancellable": False,
            "retryable": False,
            "checkpoints": [{"name": "vm_start_prepared", "payload": {}, "completed": True, "updated_at": "2026-01-15T10:30:01Z"}],
            "logs": [{"id": 1, "created_at": "2026-01-15T10:30:02Z", "level": "info", "message": "Iniciando VM."}],
        }]},
    )
    id: str
    kind: str
    state: Literal["queued", "running", "succeeded", "failed", "cancelling", "cancelled", "interrupted"]
    stage: str
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] = Field(default_factory=dict)
    error: ApiError | None = None
    cancellable: bool = False
    retryable: bool = False
    checkpoints: list[JobCheckpoint] = Field(default_factory=list)
    logs: list[JobLog] = Field(default_factory=list)


class JobSummary(Contract):
    id: str
    kind: str
    state: Literal["queued", "running", "succeeded", "failed", "cancelling", "cancelled", "interrupted"]
    stage: str
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    target_vm_id: str | None = None
    created_at: datetime
    updated_at: datetime
    cancellable: bool = False
    retryable: bool = False


class JobListResponse(Contract):
    items: list[JobSummary]
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0, le=10000)
    total: int = Field(ge=0)


class VmSupervisorStatus(Contract):
    """Read-only systemd identity, separate from the guest QEMU process."""

    unit: str
    load_state: str
    active_state: str
    sub_state: str
    main_pid: int | None = Field(default=None, ge=1)
    main_process_started_at: datetime | None = None
    result: str


class VmRuntime(Contract):
    model_config = ConfigDict(
        extra="forbid", frozen=True,
        json_schema_extra={"examples": [{
            "id": "macos-tahoe",
            "state": "running",
            "pid": 4242,
            "process_started_at": "2026-01-15T10:30:00Z",
            "qmp_available": True,
            "install_phase": "installed",
            "supervisor": {
                "unit": "vhackintosh-vm@macos-tahoe.service",
                "load_state": "loaded",
                "active_state": "active",
                "sub_state": "running",
                "main_pid": 4200,
                "main_process_started_at": "2026-01-15T10:29:58Z",
                "result": "success",
            },
            "gpu": {"configured_bdf": "0000:01:00.0", "configured_uuid": "gpu-uuid", "effective_bdf": None, "effective_uuid": None, "verification": "unavailable", "message": "O runtime não informa a GPU efetiva."},
            "observed_at": "2026-01-15T10:30:03Z",
            "message": None,
        }]},
    )
    id: str
    state: VmRunState
    pid: int | None = None
    process_started_at: datetime | None = None
    qmp_available: bool
    install_phase: InstallPhase
    supervisor: VmSupervisorStatus | None = None
    gpu: GpuAssignment | None = None
    observed_at: datetime
    message: str | None = None


class VmRuntimeList(Contract):
    items: list[VmRuntime]
    observed_at: datetime


class VmSerialConsole(Contract):
    """Bounded tail of the serial file emitted by Reims for one VM."""
    vm_id: str
    available: bool
    content: str = Field(max_length=65_536)
    updated_at: datetime | None = None
    truncated: bool = False
    message: str | None = Field(default=None, max_length=240)


class HostSample(Contract):
    timestamp: datetime
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    cpu_per_core_percent: list[float] = Field(default_factory=list)
    load_average: tuple[float, float, float] | None = None
    memory_total_bytes: int | None = Field(default=None, ge=0)
    memory_available_bytes: int | None = Field(default=None, ge=0)
    swap_total_bytes: int | None = Field(default=None, ge=0)
    swap_used_bytes: int | None = Field(default=None, ge=0)
    uptime_seconds: float | None = Field(default=None, ge=0)
    disk_read_bytes_per_sec: float | None = Field(default=None, ge=0)
    disk_write_bytes_per_sec: float | None = Field(default=None, ge=0)
    network: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    temperatures_celsius: dict[str, float | None] = Field(default_factory=dict)
    unavailable: dict[str, str] = Field(default_factory=dict)


class VmSample(Contract):
    timestamp: datetime
    vm_id: str
    state: VmRunState
    vcpus_configured: int
    cpu_percent_of_host: float | None = Field(default=None, ge=0)
    cpu_percent_of_vm_capacity: float | None = Field(default=None, ge=0)
    memory_configured_bytes: int = Field(ge=0)
    process_rss_bytes: int | None = Field(default=None, ge=0)
    memory_guest_used_bytes: int | None = Field(default=None, ge=0)
    disk_read_bytes_per_sec: float | None = Field(default=None, ge=0)
    disk_write_bytes_per_sec: float | None = Field(default=None, ge=0)
    network_receive_bytes_per_sec: float | None = Field(default=None, ge=0)
    network_send_bytes_per_sec: float | None = Field(default=None, ge=0)
    sources: dict[str, str] = Field(default_factory=dict)
    unavailable: dict[str, str] = Field(default_factory=dict)


class TelemetrySample(Contract):
    name: str
    value: float | int | str | None = None
    timestamp: datetime
    unit: str
    source: str
    quality: str = "measured"
    availability: Literal["available", "unavailable"] = "available"
    labels: dict[str, str] = Field(default_factory=dict)


class HostTelemetry(Contract):
    model_config = ConfigDict(
        extra="forbid", frozen=True,
        json_schema_extra={"examples": [{
            "samples": [{
                "name": "host.cpu.percent", "value": 18.5,
                "timestamp": "2026-01-15T10:30:03Z", "unit": "%",
                "source": "psutil", "quality": "measured",
                "availability": "available", "labels": {},
            }],
        }]},
    )
    samples: list[TelemetrySample]


class TelemetrySettingsView(Contract):
    history_retention_hours: Literal[1, 6, 12, 24]
    history_persistence_enabled: bool


class TelemetrySettingsUpdate(Contract):
    history_retention_hours: Literal[1, 6, 12, 24]


class HostAlert(Contract):
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    code: Literal["cpu.high", "memory.high", "temperature.high", "filesystem.low"]
    title: str = Field(min_length=1, max_length=96)
    severity: Literal["warning", "critical"]
    unit: Literal["%", "°C"]
    value: float
    first_seen: datetime
    last_seen: datetime
    acknowledged_at: datetime | None = None
    cleared_at: datetime | None = None


class HostAlertList(Contract):
    items: list[HostAlert]
    persistence_enabled: bool
    limit: int = Field(ge=1, le=100)


class ReimsRelease(Contract):
    tag: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=160)
    published_at: datetime | None = None
    asset_size_bytes: int = Field(ge=0)
    prerelease: bool = False


class ReimsReleaseList(Contract):
    repository: str
    current_release: str | None = None
    active_release: str | None = None
    items: list[ReimsRelease]


class ReimsReleaseUpdateRequest(Contract):
    idempotency_key: str = Field(pattern=r"^[0-9a-f-]{36}$")
    tag: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


class VmTelemetry(Contract):
    model_config = ConfigDict(
        extra="forbid", frozen=True,
        json_schema_extra={"examples": [{
            "vm_id": "macos-tahoe",
            "samples": [{
                "name": "vm.process.cpu_percent", "value": 24.2,
                "timestamp": "2026-01-15T10:30:03Z", "unit": "%",
                "source": "procfs", "quality": "measured",
                "availability": "available", "labels": {"vcpus": "8"},
            }],
            "message": None,
        }]},
    )
    vm_id: str
    samples: list[TelemetrySample]
    message: str | None = None


class TelemetryHistoryPoint(Contract):
    timestamp: datetime
    resolution_seconds: Literal[10, 60]
    average: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    sample_count: int = Field(ge=0)
    unavailable_count: int = Field(ge=0)
    availability: Literal["available", "partial", "unavailable"]
    unit: str
    source: str
    labels: dict[str, str] = Field(default_factory=dict)


class TelemetryHistory(Contract):
    name: str
    resolution_seconds: Literal[10, 60]
    points: list[TelemetryHistoryPoint]
    vm_id: str | None = None


class Capability(Contract):
    id: str
    label: str
    supported: bool
    available: bool
    requires_stopped_vm: bool = True
    description: str | None = None
    limitation: str | None = None


class QemuDevicePropertyInventory(Contract):
    """Bounded help evidence for a single allowlisted QEMU device model."""

    status: Literal["reported", "unknown"]
    source: str
    properties: list[str] = Field(default_factory=list)
    meaning: str


class RuntimeInventory(Contract):
    status: Literal["available", "partial", "missing"]
    reims_version: str | None = None
    reims_release_tag: str | None = None
    reims_schema_version: int | None = Field(default=None, ge=1, le=1000)
    reims_schema_features: list[str] = Field(default_factory=list, max_length=128)
    qemu_version: str | None = None
    qemu_options: list[str] = Field(default_factory=list)
    qemu_command_options: list[str] = Field(default_factory=list)
    qemu_devices: list[str] = Field(default_factory=list)
    qemu_machine_types: list[str] = Field(default_factory=list)
    qemu_cpu_models: list[str] = Field(default_factory=list)
    qemu_audio_backends: list[str] = Field(default_factory=list)
    qemu_device_categories: dict[str, list[str]] = Field(default_factory=dict)
    qemu_device_properties: dict[str, QemuDevicePropertyInventory] = Field(default_factory=dict)
    reims_set_options: list[str] = Field(default_factory=list)
    reims_create_options: list[str] = Field(default_factory=list)
    reims_update_opencore_picker: bool = False
    # Versioned, evidence-backed report from the selected binary CLI help.
    # Values are inventory only; this contract never treats help output as
    # proof that a device or option works in a guest.
    capability_inventory: dict[str, Any] = Field(default_factory=dict)
    binary_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class VmConfigDifference(Contract):
    field: str
    saved_value: str
    last_projection_value: str


class VmConfigEnvelope(Contract):
    model_config = ConfigDict(
        extra="forbid", frozen=True,
        json_schema_extra={"examples": [{
            "config": {
                "id": "macos-tahoe", "name": "macOS Tahoe", "macos_version": "tahoe",
                "vcpus": 8, "cpu_model": DEFAULT_MACOS_CPU_MODEL, "ram_gb": 16, "disk_size_gb": 128,
                "disk_path": "/var/lib/vhackintosh/vms/macos-tahoe/macos.qcow2", "auto_start": False,
                "smbios": {"model": "iMac20,1", "serial_number": "", "board_serial": "", "smuuid": "", "rom": ""},
                "gpu_mode": "reims-vgpu", "vfio_pci_id": None, "selected_gpu": None,
                "selected_gpu_name": None, "selected_gpu_uuid": None, "audio_device": "usb-audio",
                "audio_enabled": True, "audio_backend": "auto", "network_enabled": True, "network_mac": None,
                "fullscreen": True, "display_resolution": "1920x1080", "display_hidpi": False,
                "display_refresh_hz": None, "opencore_show_picker": False,
                "created_at": "2026-01-15T10:30:00Z", "last_booted_at": None,
                "install_phase": "installed", "install_reboots": 2, "install_evidence": "complete",
                "usb_auto_attach": [], "qemu_extra_args": [], "extra_config": {},
                "power_sync_mode": "vm-only", "revision": "a1" * 32,
            },
            "revision": "a1" * 32,
            "runtime_config_differs": True,
            "pending_restart_fields": ["ram_gb"],
            "projection_status": "available",
            "config_differences": [{"field": "ram_gb", "saved_value": "16", "last_projection_value": "8"}],
        }]},
    )
    config: VmConfig
    revision: str
    runtime_config_differs: bool = False
    pending_restart_fields: list[str] = Field(default_factory=list)
    projection_status: Literal["available", "unknown"] = "unknown"
    config_differences: list[VmConfigDifference] = Field(default_factory=list)


class Operation(StrEnum):
    START = "start"
    SHUTDOWN = "shutdown"
    FORCE_STOP = "force_stop"
    RESTART = "restart"
    PAUSE = "pause"
    RESUME = "resume"


class VmActionRequest(Contract):
    model_config = ConfigDict(
        extra="forbid", frozen=True,
        json_schema_extra={"examples": [{
            "operation": "shutdown",
            "expected_revision": "a1" * 32,
            "confirmation": None,
            "idempotency_key": "vm-action-01",
        }]},
    )
    operation: Operation
    expected_revision: str | None = None
    confirmation: str | None = None
    idempotency_key: str = Field(min_length=8, max_length=128)


class VmInstallConfirmRequest(Contract):
    """Explicit, target-bound confirmation that macOS installation is complete."""

    model_config = ConfigDict(
        extra="forbid", frozen=True,
        json_schema_extra={"examples": [{"confirmation": "macos-tahoe"}]},
    )
    confirmation: str = Field(min_length=1, max_length=63)


class VmOpenCorePickerRequest(Contract):
    """Explicit confirmation for regenerating a VM's managed OpenCore image."""

    model_config = ConfigDict(
        extra="forbid", frozen=True,
        json_schema_extra={"examples": [{
            "boot_selection": True,
            "confirmation": "macos-tahoe",
            "idempotency_key": "00000000-0000-4000-8000-000000000001",
        }]},
    )
    boot_selection: bool
    confirmation: str = Field(min_length=1, max_length=63)
    idempotency_key: str = Field(pattern=r"^[0-9a-f-]{36}$")


class VmSmbiosDiagnosticRequest(Contract):
    """Idempotent request for a read-only SMBIOS/OpenCore consistency check."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    idempotency_key: str = Field(pattern=r"^[0-9a-f-]{36}$")


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class Job(Contract):
    id: str
    kind: str
    state: JobState
    stage: str
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] = Field(default_factory=dict)
    error: ApiError | None = None
    cancellable: bool = False


class JobAccepted(Contract):
    model_config = ConfigDict(
        extra="forbid", frozen=True,
        json_schema_extra={"examples": [{
            "job_id": "7d3a10595e933acd2f2c2d2b28a2c20e",
            "state": "queued",
            "events_url": "/api/v1/jobs/7d3a10595e933acd2f2c2d2b28a2c20e",
        }]},
    )
    job_id: str
    state: JobState = JobState.QUEUED
    events_url: str


class VmBackupRequest(Contract):
    idempotency_key: str = Field(pattern=r"^[0-9a-f-]{36}$")


class VmBackupRestoreRequest(VmBackupRequest):
    confirmation: str = Field(min_length=1, max_length=128)


class VmCloneRequest(Contract):
    idempotency_key: str = Field(pattern=r"^[0-9a-f-]{36}$")
    clone_id: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=80)
    confirmation: str = Field(min_length=1, max_length=128)


class Event(Contract):
    sequence: int = Field(ge=0)
    epoch: str
    kind: str
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class HostOverview(Contract):
    hostname: str
    architecture: str
    cpu_model: str
    logical_cpus: int = Field(ge=1)
    memory_total_bytes: int = Field(ge=0)
    memory_available_bytes: int = Field(ge=0)
    uptime_seconds: float = Field(ge=0)
    addresses: list[str] = Field(default_factory=list)
    web_port: int = Field(default=8443, ge=1, le=65535)
    tls_fingerprint: str | None = None


class VmListResponse(Contract):
    items: list[VmConfigEnvelope]
    revision: str


class ReimsImportCandidate(Contract):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    name: str = Field(min_length=1, max_length=80)
    directory: str = Field(max_length=2048)
    disk_path: str = Field(max_length=4096)
    vcpus: int = Field(ge=0, le=256)
    ram_gb: int = Field(ge=0, le=2048)
    disk_size_gb: int = Field(ge=0, le=4096)
    mac: str | None = None
    display_resolution: str = Field(max_length=32)
    display_hidpi: bool
    importable: bool
    runtime_state: Literal["running", "stopped", "unknown"] = "unknown"
    runtime_pid: int | None = Field(default=None, ge=1)
    limitation: str | None = Field(default=None, max_length=512)


class ReimsImportCandidateList(Contract):
    items: list[ReimsImportCandidate]


class ReimsImportRequest(Contract):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    macos_version: Literal["tahoe", "sequoia", "ventura"]


class CapabilitiesResponse(Contract):
    """Runtime capability snapshot; fingerprint identifies these reported facts."""

    model_config = ConfigDict(
        extra="forbid", frozen=True,
        json_schema_extra={"examples": [{
            "runtime_fingerprint": "a" * 64,
            "generated_at": "2026-01-15T10:30:03Z",
            "items": [{
                "id": "vm.network.bridge", "label": "Rede bridge/tap", "supported": False,
                "available": False, "requires_stopped_vm": True,
                "description": "O runtime atual usa user/NAT.",
                "limitation": "O runtime precisa oferecer configuração estruturada de bridge/tap.",
            }],
            "runtime": {
                "status": "available", "reims_version": "reims-ctl 1.4.0", "reims_release_tag": "v1.4.0",
                "qemu_version": "QEMU 11.1", "qemu_options": ["-netdev"],
                "qemu_command_options": ["-netdev"], "qemu_devices": ["virtio-net-pci"],
                "qemu_machine_types": ["q35"], "qemu_cpu_models": [DEFAULT_MACOS_CPU_MODEL],
                "qemu_audio_backends": ["pipewire"], "qemu_device_categories": {"network": ["virtio-net-pci"]},
                "reims_set_options": ["--network", "--mac"], "reims_create_options": ["--network"],
                "binary_fingerprint": "b" * 64,
            },
        }]},
    )

    runtime_fingerprint: str = Field(description="SHA-256 of the normalized capability snapshot, not an artifact checksum.")
    generated_at: datetime
    items: list[Capability]
    runtime: RuntimeInventory | None = None
