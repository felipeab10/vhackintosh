"""
vHackintosh — VM Configuration & Data Store
Gerencia o catálogo de VMs cadastradas, persistência JSON e reservas de RAM por VM.
"""

from __future__ import annotations

import os
import json
import fcntl
import logging
import errno
import tempfile
import uuid
import copy
import dataclasses
import math
import secrets
import hashlib
import subprocess
import re
import stat
import socket
from contextlib import contextmanager
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

_configured_data_dir = os.environ.get("VHACKINTOSH_CONFIG_DIR")
if _configured_data_dir:
    CONFIG_DIR = Path(_configured_data_dir).expanduser()
    if not CONFIG_DIR.is_absolute():
        raise ValueError("VHACKINTOSH_CONFIG_DIR precisa ser um caminho absoluto.")
else:
    CONFIG_DIR = Path(os.path.expanduser("~/.config/vhackintosh"))
VMS_FILE = CONFIG_DIR / "vms.json"
VM_RESERVATIONS_DIR = Path("/tmp/vhackintosh-vm-reservations")
VM_RESERVATIONS_LOCK = Path("/tmp/vhackintosh-vm-reservations.lock")
VM_MAINTENANCE_MARKER = Path("/run/vhackintosh/maintenance.json")
SYSFS_PCI_DEVICES = Path("/sys/bus/pci/devices")
SYSFS_IOMMU_GROUPS = Path("/sys/kernel/iommu_groups")
LOGS_DIR = CONFIG_DIR / "logs"


@dataclass
class SMBIOSConfig:
    model: str = "iMac20,1"
    serial_number: str = ""
    board_serial: str = ""  # MLB
    smuuid: str = ""
    rom: str = ""


@dataclass(frozen=True)
class AdditionalDiskConfig:
    """An existing writable image attached through the structured Reims schema."""

    path: str
    format: str = "qcow2"
    controller: str = "nvme"
    order: int = 0

    def validate(self, *, expected_order: int) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("additional disk path must be a non-empty string")
        disk_path = Path(self.path).expanduser()
        if any(part in {".", ".."} for part in disk_path.parts):
            raise ValueError("additional disk path cannot contain '.' or '..' components")
        if self.format not in {"qcow2", "raw"}:
            raise ValueError("additional disk format must be qcow2 or raw")
        if self.controller not in {"ahci", "nvme"}:
            raise ValueError("additional disk controller must be ahci or nvme")
        if isinstance(self.order, bool) or not isinstance(self.order, int) or self.order != expected_order:
            raise ValueError("additional disk order must be unique and contiguous from zero")


@dataclass(frozen=True)
class CpuTopologyConfig:
    """Optional structured Reims topology; omission keeps 1 socket × N cores × 1 thread."""

    sockets: int
    cores: int
    threads: int

    def validate(self, *, vcpus: int) -> None:
        values = (self.sockets, self.cores, self.threads)
        if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 256 for value in values):
            raise ValueError("CPU topology factors must be integers between 1 and 256")
        product = self.sockets * self.cores * self.threads
        if product != vcpus:
            raise ValueError("sockets × cores × threads must equal vcpus")


@dataclass
class VMConfig:
    id: str
    name: str
    macos_version: str  # "tahoe", "sequoia", "sonoma", "ventura", etc.
    vcpus: int = 8
    # CPU model passed through the structured [q35] projection. Keep this
    # aligned with reims-ctl's q35.cpu_model field; unknown legacy keys remain
    # preserved by from_dict below.
    cpu_model: str = "Skylake-Client-noTSX-IBRS"
    # None means the structured topology extension is not owned; Reims keeps
    # its current default of one socket, N cores, and one thread per core.
    cpu_topology: CpuTopologyConfig | None = None
    ram_gb: int = 8
    disk_size_gb: int = 64
    disk_path: str = ""
    # None means an older panel does not own this runtime extension; [] means
    # the editor explicitly owns an empty list and may remove its TOML tables.
    additional_disks: list[AdditionalDiskConfig] | None = None
    auto_start: bool = False
    smbios: SMBIOSConfig = field(default_factory=SMBIOSConfig)
    gpu_mode: str = "reims-vgpu"  # "reims-vgpu" ou "vfio-passthrough"
    vfio_pci_id: Optional[str] = None
    selected_gpu: Optional[str] = None  # Ex: "0000:01:00.0"
    selected_gpu_name: Optional[str] = None  # Ex: "NVIDIA RTX 3050 Mobile"
    # Compatibilidade de catálogos antigos; o modelo efetivo é escolhido pelo
    # launcher Reims da release instalada (VirtIO Sound a partir da v1.4.0).
    audio_device: str = "usb-audio"
    # Reims keeps the guest device model fixed; this selects only QEMU's host
    # audio backend. None means Reims chooses the host-native backend.
    audio_enabled: bool = True
    audio_backend: str = "auto"
    network_enabled: bool = True
    # None preserves/creates the Reims-generated 52:54:00 MAC.
    network_mac: Optional[str] = None
    # Host-local TCP port forwarded to guest SSH by Reims user networking.
    # None leaves a legacy Reims value unowned; zero explicitly disables it.
    network_ssh_port: Optional[int] = None
    # As preferências de tela são aplicadas pelo bloco [display] do reims-ctl.
    fullscreen: bool = True
    display_resolution: str = "1920x1080"
    # None means the panel does not own this field and preserves Reims' value.
    display_count: Optional[int] = None
    display_hidpi: bool = False
    display_refresh_hz: Optional[int] = None
    opencore_show_picker: bool = False
    created_at: str = ""
    last_booted_at: Optional[str] = None
    # Ciclo de vida da instalação do macOS (ver core/install_phase.py).
    # Enquanto não for "installed", o host NUNCA é reiniciado/desligado junto
    # com o guest — reboots intermediários do instalador são absorvidos pelo QEMU.
    install_phase: str = "pending"  # "pending" | "installing" | "installed"
    install_reboots: int = 0        # reboots de guest observados durante a vida da VM
    install_evidence: str = "none"  # "none" | "partial" | "complete" (última sonda de disco)
    # Dispositivos USB do host marcados para anexar sempre nesta VM, como
    # "1050:0407" (vendor:product) — anexados via QMP logo que a VM sobe
    # (change appliance-i18n…, grupo 3: pen drive plugado antes do boot).
    usb_auto_attach: list = field(default_factory=list)
    # Seleção exata quando o runtime Reims conhece UUID; BDF continua sendo a
    # localização PCI e é mantido para catálogos mais antigos.
    selected_gpu_uuid: Optional[str] = None
    # Reims structured GPU identity (available only when advertised by its
    # schema). Keep these distinct from selected_gpu_uuid, which is the
    # legacy Vulkan UUID used by older Reims releases.
    selected_gpu_device_uuid: Optional[str] = None
    selected_gpu_pci_address: Optional[str] = None
    selected_gpu_driver_file: Optional[str] = None
    # Argumentos já tokenizados; não são linha de shell e jamais usam shell=True.
    qemu_extra_args: list[str] = field(default_factory=list)
    # Opções ainda sem campo de primeira classe precisam sobreviver ao round-trip.
    extra_config: dict[str, Any] = field(default_factory=dict)
    # No painel o default seguro é desligar somente o convidado.
    power_sync_mode: str = "vm-only"

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.additional_disks is None:
            data.pop("additional_disks", None)
        if self.cpu_topology is None:
            data.pop("cpu_topology", None)
        if self.display_count is None:
            data.pop("display_count", None)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> VMConfig:
        # Tolerância a campos desconhecidos: configs gravados por versões
        # anteriores podem conter chaves que já não existem no dataclass (por
        # exemplo `force_x11`, removido na migração para Wayland). Sem este
        # filtro, `cls(**data)` levantaria TypeError e a VM
        # sumiria do gerenciador. Os campos novos já têm default, então a
        # ausência deles também é segura.
        source = copy.deepcopy(data)
        display_count = source.get("display_count")
        if display_count is not None and (
            isinstance(display_count, bool) or not isinstance(display_count, int) or not 1 <= display_count <= 8
        ):
            raise ValueError("display_count must be an integer from 1 to 8")
        has_additional_disks = "additional_disks" in source
        raw_additional_disks = source.get("additional_disks", [])
        if raw_additional_disks is None:
            if has_additional_disks:
                raise ValueError("additional_disks cannot be null")
            raw_additional_disks = []
        if not isinstance(raw_additional_disks, list) or len(raw_additional_disks) > 15:
            raise ValueError("additional_disks must contain at most 15 disk entries")
        additional_disks: list[AdditionalDiskConfig] = []
        seen_paths: set[str] = set()
        for order, item in enumerate(raw_additional_disks):
            if isinstance(item, AdditionalDiskConfig):
                disk = item
            elif isinstance(item, dict):
                disk = AdditionalDiskConfig(**item)
            else:
                raise ValueError("additional disk entry must be an object")
            disk.validate(expected_order=order)
            key = os.path.normcase(os.path.abspath(os.path.expanduser(disk.path)))
            if key in seen_paths:
                raise ValueError("additional disk paths must be unique")
            seen_paths.add(key)
            additional_disks.append(disk)
        source["additional_disks"] = additional_disks if has_additional_disks else None
        raw_cpu_topology = source.get("cpu_topology")
        if raw_cpu_topology is None:
            source["cpu_topology"] = None
        else:
            try:
                cpu_topology = raw_cpu_topology if isinstance(raw_cpu_topology, CpuTopologyConfig) else CpuTopologyConfig(**raw_cpu_topology)
                cpu_topology.validate(vcpus=source.get("vcpus"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid cpu_topology: {exc}") from exc
            source["cpu_topology"] = cpu_topology
        smbios_data = source.pop("smbios", {})
        smbios_fields = {field.name for field in dataclasses.fields(SMBIOSConfig)}
        if isinstance(smbios_data, dict):
            smbios = SMBIOSConfig(**{key: value for key, value in smbios_data.items() if key in smbios_fields})
        else:
            smbios = SMBIOSConfig()
        conhecidos = {f.name for f in dataclasses.fields(cls)}
        extras = {k: v for k, v in source.items() if k not in conhecidos}
        if extras:
            logger.debug(
                "VMConfig.from_dict: preservando campos desconhecidos: %s",
                ", ".join(sorted(extras)),
            )
        filtrado = {k: v for k, v in source.items() if k in conhecidos}
        stored_extras = filtrado.get("extra_config", {})
        if not isinstance(stored_extras, dict):
            stored_extras = {"_legacy_value": stored_extras}
        filtrado["extra_config"] = {**stored_extras, **extras}
        return cls(smbios=smbios, **filtrado)


class ConcurrentCatalogUpdate(RuntimeError):
    """A configuração enviada foi editada depois que o cliente a leu."""

    def __init__(self, expected: str, current: str | None):
        super().__init__("O catálogo mudou desde que esta configuração foi carregada")
        self.expected = expected
        self.current = current


class SharedVmDiskError(RuntimeError):
    """A VM disk is still referenced by another catalog entry."""


class VMManagerStore:
    """Gerencia a persistência das configurações das VMs em JSON."""

    def __init__(self, catalog_path: str | Path | None = None):
        self.catalog_path = Path(catalog_path) if catalog_path else VMS_FILE
        self.config_dir = self.catalog_path.parent
        self.logs_dir = self.config_dir / "logs"
        self.lock_path = self.catalog_path.with_suffix(self.catalog_path.suffix + ".lock")
        self._revision: str | None = None
        self.config_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.config_dir.chmod(0o700)
        except OSError:
            pass
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._vms: Dict[str, VMConfig] = {}
        self._load_error: Optional[Exception] = None
        self.load()
        if self._revision is None and self._load_error is None and not self.catalog_path.exists():
            # Preserve the legacy first-run behavior while serializing creation
            # across API/TUI processes.
            try:
                self.save()
            except ConcurrentCatalogUpdate:
                self.load()
        with self._exclusive_catalog():
            self.load()
            if self._load_error is None:
                self._recover_pending_disk_deletions_locked()

    @property
    def revision(self) -> str:
        """SHA-256 do catálogo lido, adequado a ETag/If-Match."""
        return self._revision or self._empty_revision()

    @staticmethod
    def _digest(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def _empty_revision(cls) -> str:
        return cls._digest(b"")

    @contextmanager
    def _exclusive_catalog(self) -> Iterator[None]:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+b") as lock_file:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_current_revision(self) -> str | None:
        try:
            return self._digest(self.catalog_path.read_bytes())
        except FileNotFoundError:
            return None

    def _recover_pending_disk_deletions_locked(self) -> None:
        """Reconcile disk-removal staging files after an interrupted delete."""
        for journal in self.config_dir.glob(".disk-delete-*.json"):
            try:
                payload = json.loads(journal.read_text(encoding="utf-8"))
                vm_id = payload["vm_id"]
                disk_path = Path(payload["disk_path"])
                staged_disk = Path(payload["staged_disk"])
                if not isinstance(vm_id, str) or not disk_path.is_absolute() or not staged_disk.is_absolute():
                    raise ValueError("invalid disk deletion journal")
                if (disk_path.parent != staged_disk.parent
                        or not staged_disk.name.startswith(f".{disk_path.name}.delete-")
                        or not disk_path.parent.resolve().is_relative_to((self.config_dir / "vms").resolve())):
                    raise ValueError("disk deletion journal paths do not match")

                if vm_id in self._vms:
                    if staged_disk.exists() and not disk_path.exists():
                        os.replace(staged_disk, disk_path)
                    elif staged_disk.exists() and disk_path.exists():
                        logger.error("Both original and staged VM disks exist; preserving journal %s", journal)
                        continue
                else:
                    if disk_path.exists():
                        # The catalog commit completed only after staging the disk.
                        # An original file here is ambiguous, so preserve it and
                        # leave the journal for operator review.
                        logger.error("Original VM disk exists after catalog removal; preserving %s", journal)
                        continue
                    staged_disk.unlink(missing_ok=True)
                journal.unlink()
            except (OSError, UnicodeError, TypeError, KeyError, ValueError, json.JSONDecodeError):
                logger.exception("Unable to reconcile pending VM disk deletion %s", journal)

    def _write_disk_deletion_journal(self, vm_id: str, disk_path: Path, staged_disk: Path) -> Path:
        journal = self.config_dir / f".disk-delete-{uuid.uuid4().hex}.json"
        fd, temp_name = tempfile.mkstemp(prefix=".disk-delete-journal.", dir=self.config_dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"vm_id": vm_id, "disk_path": str(disk_path), "staged_disk": str(staged_disk)}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, journal)
            return journal
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(temp_name).unlink(missing_ok=True)
            raise

    @staticmethod
    def _remove_disk_deletion_journal(journal: Path | None) -> None:
        if journal is None:
            return
        try:
            journal.unlink(missing_ok=True)
        except OSError:
            # Recovery is idempotent; a leftover journal is safer than turning
            # a completed rollback into a second failure.
            logger.warning("Disk deletion journal will be reconciled on next store load: %s", journal)

    def load(self) -> None:
        try:
            raw = self.catalog_path.read_bytes()
        except FileNotFoundError:
            self._vms = {}
            self._load_error = None
            self._revision = None
            return
        try:
            raw_data = json.loads(raw.decode("utf-8"))
            if not isinstance(raw_data, dict) or not isinstance(raw_data.get("vms", {}), dict):
                raise ValueError("vms.json must contain an object with a 'vms' object")
            loaded_vms = {
                vm_id: VMConfig.from_dict(vm_data)
                for vm_id, vm_data in raw_data.get("vms", {}).items()
            }
            self._vms = loaded_vms
            self._load_error = None
            self._revision = self._digest(raw)
        except (OSError, UnicodeError, TypeError, ValueError, AttributeError) as exc:
            # Keep the original file and refuse later writes from this store
            # instance. Treating a damaged catalog as empty used to make the
            # next add/update silently replace it with an empty catalog.
            self._load_error = exc
            logger.error("Unable to load VM catalog %s; writes are disabled: %s", self.catalog_path, exc)

    def refresh(self) -> None:
        """Reload the authoritative catalog under the same process lock as writers."""
        with self._exclusive_catalog():
            self.load()
            if self._load_error is not None:
                raise RuntimeError(f"Cannot read VM catalog {self.catalog_path}") from self._load_error

    def save(self) -> None:
        with self._exclusive_catalog():
            current = self._read_current_revision()
            if current != self._revision:
                raise ConcurrentCatalogUpdate(self.revision, current)
            self._save_locked()

    def _save_locked(self) -> None:
        if self._load_error is not None and self.catalog_path.exists():
            raise RuntimeError(
                f"Cannot save VM catalog because {self.catalog_path} could not be loaded; "
                "repair the file and reload the store first"
            ) from self._load_error
        self.config_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "version": "1.0",
            "vms": {vm_id: vm.to_dict() for vm_id, vm in self._vms.items()},
        }
        fd, temp_name = tempfile.mkstemp(prefix=".vms.", dir=self.config_dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, self.catalog_path)
            os.chmod(self.catalog_path, 0o600)
            self._revision = self._read_current_revision()
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(temp_name).unlink(missing_ok=True)
            raise

    def list_vms(self) -> List[VMConfig]:
        return list(self._vms.values())

    def get_vm(self, vm_id: str) -> Optional[VMConfig]:
        return self._vms.get(vm_id)

    def get_auto_start_vm(self) -> Optional[VMConfig]:
        for vm in self._vms.values():
            if vm.auto_start:
                return vm
        return None

    def add_vm(self, vm: VMConfig, *, expected_revision: str | None = None) -> None:
        with self._exclusive_catalog():
            self._reload_locked(expected_revision)
            existing = self._vms.get(vm.id)
            if existing and self._has_identity_changed(existing.smbios, vm.smbios):
                raise ValueError("Refusing to overwrite VM with a different SMBIOS identity")
            self._assert_unique_identity(vm)
            if vm.auto_start:
                self._clear_auto_start()
            self._vms[vm.id] = copy.deepcopy(vm)
            self._save_locked()

    def update_vm(self, vm: VMConfig, *, expected_revision: str | None = None) -> None:
        with self._exclusive_catalog():
            self._reload_locked(expected_revision)
            existing = self._vms.get(vm.id)
            if existing and self._has_identity_changed(existing.smbios, vm.smbios):
                vm_dir = Path(existing.disk_path).parent if existing.disk_path else self.config_dir / "vms" / vm.id
                if not (vm_dir / "smbios-backup-before-regeneration" / "OpenCore.qcow2").is_file():
                    raise ValueError("SMBIOS change for an existing VM requires a verified OpenCore backup")
            self._assert_unique_identity(vm)
            if vm.auto_start:
                self._clear_auto_start(exclude_id=vm.id)
            self._vms[vm.id] = copy.deepcopy(vm)
            self._save_locked()

    def _reload_locked(self, expected_revision: str | None = None) -> None:
        current = self._read_current_revision()
        if expected_revision is not None:
            expected = expected_revision.strip('"')
            actual = current or self._empty_revision()
            if expected != actual:
                raise ConcurrentCatalogUpdate(expected, current)
        self.load()
        if self._load_error is not None:
            raise RuntimeError(
                f"Cannot save VM catalog because {self.catalog_path} could not be loaded"
            ) from self._load_error
        self._recover_pending_disk_deletions_locked()

    @staticmethod
    def _has_identity_conflict(left: SMBIOSConfig, right: SMBIOSConfig) -> bool:
        return any(
            a and b and a.lower() == b.lower()
            for a, b in (
                (left.serial_number, right.serial_number),
                (left.board_serial, right.board_serial),
                (left.smuuid, right.smuuid),
                (left.rom, right.rom),
            )
        )

    @staticmethod
    def _has_identity_changed(left: SMBIOSConfig, right: SMBIOSConfig) -> bool:
        had_identity = any(
            bool((a or "").strip())
            for a in (left.serial_number, left.board_serial, left.smuuid, left.rom)
        )
        if not had_identity:
            return False
        return any(
            (a or "").strip().lower() != (b or "").strip().lower()
            for a, b in (
                (left.serial_number, right.serial_number),
                (left.board_serial, right.board_serial),
                (left.smuuid, right.smuuid),
                (left.rom, right.rom),
            )
        )

    def _assert_unique_identity(self, vm: VMConfig) -> None:
        for other_id, other in self._vms.items():
            if other_id != vm.id and self._has_identity_conflict(vm.smbios, other.smbios):
                raise ValueError("SMBIOS identity conflicts with another VM")

    def delete_vm(self, vm_id: str, delete_disk: bool = False, *, expected_revision: str | None = None) -> bool:
        with self._exclusive_catalog():
            self._reload_locked(expected_revision)
            vm = self._vms.get(vm_id)
            if vm is None:
                return False
            staged_disk: Path | None = None
            deletion_journal: Path | None = None
            disk_path = Path(vm.disk_path) if delete_disk and vm.disk_path else None
            if disk_path is not None and disk_path.exists():
                disk_identity = disk_path.expanduser().resolve(strict=False)
                for other_id, other in self._vms.items():
                    if other_id == vm_id or not other.disk_path:
                        continue
                    other_identity = Path(other.disk_path).expanduser().resolve(strict=False)
                    if other_identity == disk_identity:
                        raise SharedVmDiskError(
                            f"O disco também está associado à VM {other.name}; ele foi preservado."
                        )
                staged_disk = disk_path.with_name(f".{disk_path.name}.delete-{uuid.uuid4().hex}")
                deletion_journal = self._write_disk_deletion_journal(vm_id, disk_path, staged_disk)
                # Stage the disk on the same filesystem so a catalog write failure
                # can restore it without losing the VM's data.
                try:
                    os.replace(disk_path, staged_disk)
                except Exception:
                    self._remove_disk_deletion_journal(deletion_journal)
                    raise
            del self._vms[vm_id]
            try:
                self._save_locked()
            except Exception:
                self._vms[vm_id] = vm
                if staged_disk is not None:
                    try:
                        os.replace(staged_disk, disk_path)
                    except OSError as restore_error:
                        raise RuntimeError(
                            f"Catalog deletion failed; disk recovery journal remains at {deletion_journal} "
                            f"and disk data is at {staged_disk}"
                        ) from restore_error
                self._remove_disk_deletion_journal(deletion_journal)
                raise
            if staged_disk is not None:
                try:
                    staged_disk.unlink()
                except OSError:
                    # Keep the catalog and disk in their previous state when the
                    # staged file cannot be removed; never report a successful
                    # deletion while leaving the user's disk behind.
                    self._vms[vm_id] = vm
                    try:
                        os.replace(staged_disk, disk_path)
                        self._save_locked()
                        self._remove_disk_deletion_journal(deletion_journal)
                    except Exception as rollback_error:
                        try:
                            if disk_path.exists() and not staged_disk.exists():
                                os.replace(disk_path, staged_disk)
                        except OSError:
                            pass
                        raise RuntimeError(
                            f"VM deletion rollback failed; inspect journal {deletion_journal} and disk data "
                            f"at {disk_path} and {staged_disk}"
                        ) from rollback_error
                    raise
            return True

    def set_auto_start(self, vm_id: str, enabled: bool = True, *, expected_revision: str | None = None) -> None:
        with self._exclusive_catalog():
            self._reload_locked(expected_revision)
            if enabled:
                self._clear_auto_start()
                if vm_id in self._vms:
                    self._vms[vm_id].auto_start = True
            elif vm_id in self._vms:
                self._vms[vm_id].auto_start = False
            self._save_locked()

    def _clear_auto_start(self, exclude_id: Optional[str] = None) -> None:
        for v_id, vm in self._vms.items():
            if v_id != exclude_id:
                vm.auto_start = False


class ExclusiveVMLock:
    """Reserva memória do host para uma VM sem bloquear outras VMs."""

    def __init__(self):
        self._reservation_path: Optional[Path] = None
        self.last_error: Optional[dict] = None

    def acquire(
        self,
        vm_name: str,
        ram_gb: int = 8,
        vm_id: Optional[str] = None,
        *,
        disk_path: str | os.PathLike[str] | None = None,
        disk_paths: list[str] | tuple[str, ...] | None = None,
        usb_devices: list[str] | tuple[str, ...] | None = None,
        network_ports: list[int] | tuple[int, ...] | None = None,
        vfio_devices: list[str] | tuple[str, ...] | None = None,
    ) -> bool:
        """Admite a VM se a memória sustenta todas as reservas ativas.

        O lock global existe apenas durante a decisão/gravação. Cada sessão
        mantém uma reserva própria enquanto sua VM estiver em execução.
        """
        self.last_error = None
        try:
            requested = max(1, int(ram_gb)) * 1024**3
            disk_keys = self._disk_resource_keys([disk_path, *(disk_paths or [])])
            usb_ids = self._normalize_usb_resources(usb_devices)
            forwarded_ports = self._normalize_network_ports(network_ports)
            registered_claims = self._registered_vm_resources(vm_id) if vm_id else {}
            if registered_claims.get("vfio_group_unknown"):
                raise ValueError("Não foi possível confirmar o grupo IOMMU da GPU VFIO configurada.")
            disk_keys.update(self._disk_resource_keys(registered_claims.get("disk_paths", [])))
            disk_keys.update(self._disk_resource_keys([registered_claims.get("disk_path")]))
            usb_ids = sorted(set(usb_ids) | set(self._normalize_usb_resources(
                registered_claims.get("usb_devices", [])
            )))
            forwarded_ports = sorted(set(forwarded_ports) | set(self._normalize_network_ports(
                registered_claims.get("network_ports", [])
            )))
            vfio_bdfs = sorted(set(self._normalize_vfio_resources(vfio_devices)) | set(
                self._normalize_vfio_resources(registered_claims.get("vfio_devices", []))
            ))
            vfio_groups = self._vfio_group_keys(vfio_bdfs)
            VM_RESERVATIONS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(VM_RESERVATIONS_LOCK, "a+") as guard:
                fcntl.flock(guard, fcntl.LOCK_EX)
                try:
                    if VM_MAINTENANCE_MARKER.exists():
                        self.last_error = {"reason": "maintenance", "active_vms": []}
                        return False
                    reservations = self._read_reservations()
                    running, ctl_ok = self._running_reims_vms()
                    reservations = self._prune_stale(reservations, running, ctl_ok)
                    vm_key = vm_id or vm_name
                    if vm_key in running or any(
                        str(row.get("vm_id") or "") == vm_key for row in reservations
                    ):
                        self.last_error = {"reason": "already_running", "active_vms": [
                            row.get("name", row.get("vm_id", "VM")) for row in reservations
                        ]}
                        return False

                    active_resources: list[tuple[str, dict]] = []
                    reserved_ids = {str(row.get("vm_id") or "") for row in reservations}
                    for row in reservations:
                        active_resources.append((str(row.get("vm_id") or ""), row))
                    # A VM started outside this process may not have a live
                    # reservation. Recover only resource claims from its
                    # registered config where available; the RAM admission
                    # below remains fail-closed for guests whose allocation is
                    # unknown.
                    for running_id in running:
                        if running_id in reserved_ids:
                            continue
                        active_resources.append((running_id, self._registered_vm_resources(running_id)))

                    conflicts = []
                    for active_id, row in active_resources:
                        row_disks = set(row.get("disk_keys", []))
                        row_disks.update(self._disk_resource_keys(row.get("disk_paths", [])))
                        legacy_key = row.get("disk_key") or self._disk_resource_key(row.get("disk_path"))
                        if legacy_key:
                            row_disks.add(legacy_key)
                        row_usb = set(self._normalize_usb_resources(row.get("usb_devices", [])))
                        row_ports = set(self._normalize_network_ports(row.get("network_ports", [])))
                        row_vfio = set(row.get("vfio_groups", []))
                        if disk_keys.intersection(row_disks):
                            conflicts.append((active_id, "disk"))
                        if set(usb_ids).intersection(row_usb):
                            conflicts.append((active_id, "usb"))
                        if set(forwarded_ports).intersection(row_ports):
                            conflicts.append((active_id, "network_port"))
                        if vfio_groups and row.get("vfio_group_unknown"):
                            conflicts.append((active_id, "vfio"))
                        if set(vfio_groups).intersection(row_vfio):
                            conflicts.append((active_id, "vfio"))
                    if conflicts:
                        resources = sorted({kind for _, kind in conflicts})
                        self.last_error = {
                            "reason": "resource_conflict",
                            "resources": resources,
                            "active_vms": sorted({
                                str(next((row.get("name") for owner, row in active_resources if owner == vm), vm))
                                for vm, _ in conflicts
                            }),
                        }
                        return False

                    occupied_ports = [
                        port for port in forwarded_ports
                        if not self._loopback_tcp_port_available(port)
                    ]
                    if occupied_ports:
                        self.last_error = {
                            "reason": "network_port_in_use",
                            "network_ports": occupied_ports,
                            "active_vms": [],
                        }
                        return False

                    total, available = self._host_memory()
                    host_reserve = max(1024**3, math.ceil(total * 0.10))
                    committed = sum(int(row.get("ram_bytes", 0)) for row in reservations)
                    resident = 0
                    unknown_allocations = []

                    for name, pid in running.items():
                        row = next(
                            (item for item in reservations if str(item.get("vm_id") or "") == name),
                            None,
                        )
                        allocation = (
                            int(row.get("ram_bytes", 0)) if row
                            else self._registered_vm_memory(name)
                        )
                        if not row:
                            committed += allocation
                            if not allocation:
                                unknown_allocations.append(name)
                        if allocation:
                            resident += min(self._process_rss(pid), allocation)

                    usable = max(0, available + resident - committed - host_reserve)
                    if unknown_allocations:
                        usable = 0
                    # Preserva o comportamento histórico para a primeira VM.
                    # A política de capacidade passa a decidir apenas quando
                    # já existe uma VM (ou reserva) ocupando o host.
                    first_vm = not reservations and not running
                    if not first_vm and requested > usable:
                        self.last_error = {
                            "requested_gb": round(requested / 1024**3, 1),
                            "available_gb": round(usable / 1024**3, 1),
                            "host_reserve_gb": round(host_reserve / 1024**3, 1),
                            "active_vms": sorted({
                                str(row.get("name", row.get("vm_id", "VM"))) for row in reservations
                            } | set(running) | set(unknown_allocations)),
                        }
                        return False

                    token = secrets.token_hex(12)
                    self._reservation_path = VM_RESERVATIONS_DIR / f"{token}.json"
                    self._write_reservation(self._reservation_path, {
                        "token": token,
                        "name": vm_name,
                        "vm_id": vm_id or vm_name,
                        "ram_bytes": requested,
                        "owner_pid": os.getpid(),
                        "disk_keys": sorted(disk_keys),
                        # Retain a legacy single-key field for older readers of reservations.
                        "disk_key": next(iter(sorted(disk_keys)), None),
                        "usb_devices": usb_ids,
                        "network_ports": forwarded_ports,
                        "vfio_groups": vfio_groups,
                    })
                    return True
                finally:
                    fcntl.flock(guard, fcntl.LOCK_UN)
        except OSError as exc:
            logger.exception("Não foi possível reservar RAM para a VM %s", vm_name)
            if exc.errno == errno.ENOSPC:
                self.last_error = {
                    "reason": "storage_full",
                    "resource": "vm_reservation",
                }
            else:
                self.last_error = {"reason": str(exc)}
            return False
        except (IOError, ValueError, TypeError) as exc:
            logger.exception("Não foi possível reservar RAM para a VM %s", vm_name)
            self.last_error = {"reason": str(exc)}
            return False

    @staticmethod
    def _read_reservations() -> list[dict]:
        rows = []
        for path in VM_RESERVATIONS_DIR.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    row = json.load(fh)
                if isinstance(row, dict) and int(row.get("ram_bytes", 0)) > 0:
                    row["_path"] = str(path)
                    rows.append(row)
            except (OSError, ValueError, TypeError):
                logger.warning("Ignorando reserva de RAM inválida: %s", path)
        return rows

    @staticmethod
    def _running_reims_vms() -> tuple[dict, bool]:
        try:
            from core import reims_ctl

            binary = reims_ctl.reims_ctl_bin()
            if not binary:
                return {}, False
            proc = subprocess.run(
                [str(binary), "list", "--json"], capture_output=True,
                text=True, timeout=10, check=False,
            )
            if proc.returncode != 0:
                return {}, False
            data = json.loads(proc.stdout or "[]")
            if isinstance(data, dict):
                data = data.get("vms", [])
            if not isinstance(data, list):
                return {}, False
            return {
                str(row["name"]): int(row["pid"])
                for row in data
                if isinstance(row, dict) and row.get("state") == "running"
                and row.get("name") and isinstance(row.get("pid"), int)
            }, True
        except (ImportError, OSError, subprocess.SubprocessError, ValueError, TypeError):
            return {}, False

    @staticmethod
    def _prune_stale(rows: list[dict], running: dict, ctl_ok: bool) -> list[dict]:
        active = []
        for row in rows:
            owner_alive = ExclusiveVMLock._pid_alive(int(row.get("owner_pid", -1)))
            vm_id = str(row.get("vm_id") or "")
            stale = not owner_alive and ctl_ok and vm_id not in running
            if stale:
                try:
                    Path(row["_path"]).unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                active.append(row)
        return active

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    @staticmethod
    def _host_memory() -> tuple[int, int]:
        with open("/proc/meminfo", "r", encoding="ascii") as fh:
            values = {
                parts[0].rstrip(":"): int(parts[1].split()[0]) * 1024
                for line in fh if len(parts := line.split()) >= 2
            }
        total = values["MemTotal"]
        return total, values.get("MemAvailable", total // 2)

    @staticmethod
    def _process_rss(pid: int) -> int:
        try:
            with open(f"/proc/{pid}/status", "r", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        return 0

    @staticmethod
    def _registered_vm_memory(vm_id: str) -> int:
        try:
            from core import reims_ctl

            row = reims_ctl.vm_record(vm_id)
            if not row or not row.get("dir"):
                return 0
            with (Path(row["dir"]) / "vm.toml").open("rb") as fh:
                import tomllib

                config = tomllib.load(fh)
            return int(config.get("memory_mib", 0)) * 1024**2
        except (ImportError, OSError, ValueError, TypeError):
            return 0

    @staticmethod
    def _disk_resource_key(value: object) -> str | None:
        """Normalize one writable guest disk path for cross-VM exclusion."""
        if not isinstance(value, (str, os.PathLike)) or not os.fspath(value).strip():
            return None
        try:
            return os.path.normcase(os.path.realpath(os.path.expanduser(os.fspath(value))))
        except (OSError, TypeError, ValueError):
            return None

    @staticmethod
    def _disk_resource_keys(values: object) -> set[str]:
        if isinstance(values, (str, os.PathLike)) or values is None:
            values = [values]
        if not isinstance(values, (list, tuple, set)):
            return set()
        return {
            key for value in values
            if (key := ExclusiveVMLock._disk_resource_key(value)) is not None
        }

    @staticmethod
    def _normalize_usb_resources(values: object) -> list[str]:
        if not isinstance(values, (list, tuple, set)):
            return []
        normalized = []
        for value in values:
            if not isinstance(value, str):
                continue
            item = value.strip().lower()
            if re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{4}", item):
                normalized.append(item)
        return sorted(set(normalized))

    @staticmethod
    def _normalize_network_ports(values: object) -> list[int]:
        if isinstance(values, int) and not isinstance(values, bool):
            values = [values]
        if not isinstance(values, (list, tuple, set)):
            return []
        return sorted({value for value in values if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535})

    @staticmethod
    def _loopback_tcp_port_available(port: int) -> bool:
        """Check the exact IPv4 loopback bind used by Reims' SSH host forward."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

    @staticmethod
    def _normalize_vfio_resources(values: object) -> list[str]:
        """Normalize PCI BDFs without accepting vendor/device IDs or argv text."""
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple, set)):
            return []
        normalized = []
        for value in values:
            if isinstance(value, str):
                item = value.strip().lower()
                if re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", item):
                    normalized.append(item)
                else:
                    raise ValueError("BDF da GPU VFIO inválido.")
            else:
                raise ValueError("BDF da GPU VFIO inválido.")
        return sorted(set(normalized))

    @staticmethod
    def _vfio_group_keys(pci_devices: list[str]) -> list[str]:
        """Resolve PCI BDFs to their IOMMU group, failing closed when unknown."""
        if not pci_devices:
            return []
        try:
            groups_root = SYSFS_IOMMU_GROUPS.resolve(strict=True)
        except OSError as exc:
            raise ValueError("Inventário de grupos IOMMU indisponível para a GPU VFIO.") from exc
        groups = []
        for bdf in pci_devices:
            try:
                group = (SYSFS_PCI_DEVICES / bdf / "iommu_group").resolve(strict=True)
            except OSError as exc:
                raise ValueError("A GPU VFIO não possui grupo IOMMU verificável.") from exc
            try:
                group.relative_to(groups_root)
            except ValueError as exc:
                raise ValueError("O grupo IOMMU da GPU VFIO aponta para um caminho inesperado.") from exc
            if group.parent != groups_root or not group.name.isdecimal():
                raise ValueError("O grupo IOMMU da GPU VFIO tem um identificador inválido.")
            groups.append(f"iommu:{group.name}")
        return sorted(set(groups))

    @staticmethod
    def _registered_vm_resources(vm_id: str) -> dict:
        """Read the resource claims of a running VM missing a reservation."""
        try:
            catalog = os.environ.get("VHACKINTOSH_VM_CATALOG") or str(VMS_FILE)
            catalog_path = Path(catalog)
            info = catalog_path.lstat()
            if stat.S_ISREG(info.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                fd = os.open(catalog_path, flags)
                try:
                    opened = os.fstat(fd)
                    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise OSError("VM catalog changed while reading resource claims")
                    with os.fdopen(fd, "r", encoding="utf-8") as handle:
                        fd = -1
                        data = json.load(handle)
                finally:
                    if fd >= 0:
                        os.close(fd)
                records = data.get("vms", {}) if isinstance(data, dict) else {}
                vm_data = records.get(vm_id) if isinstance(records, dict) else None
                vm = VMConfig.from_dict(vm_data) if isinstance(vm_data, dict) else None
            else:
                vm = None
            claims = {}
            if vm is not None:
                claims = {
                    "name": vm.name,
                    "disk_path": vm.disk_path,
                    "disk_paths": [disk.path for disk in (vm.additional_disks or [])],
                    "usb_devices": list(vm.usb_auto_attach or []),
                    "network_ports": [vm.network_ssh_port] if isinstance(vm.network_ssh_port, int) and 1 <= vm.network_ssh_port <= 65535 else [],
                }
                if vm.gpu_mode == "vfio-passthrough" and vm.vfio_pci_id:
                    claims["vfio_devices"] = [vm.vfio_pci_id]
            if claims.get("vfio_devices"):
                try:
                    vfio_bdfs = ExclusiveVMLock._normalize_vfio_resources(claims["vfio_devices"])
                    if not vfio_bdfs:
                        raise ValueError("BDF da GPU VFIO ausente.")
                    claims["vfio_groups"] = ExclusiveVMLock._vfio_group_keys(vfio_bdfs)
                except (OSError, ValueError):
                    claims["vfio_group_unknown"] = True
            from core import reims_ctl
            row = reims_ctl.vm_record(vm_id)
            if not row or not row.get("dir"):
                return claims
            with (Path(row["dir"]) / "vm.toml").open("rb") as fh:
                import tomllib
                config = tomllib.load(fh)
            vm_dir = Path(row["dir"]).expanduser()
            registered_disk = config.get("disk")
            if isinstance(registered_disk, str):
                registered_disk_path = Path(registered_disk).expanduser()
                if not registered_disk_path.is_absolute():
                    registered_disk_path = vm_dir / registered_disk_path
                registered_disk = str(registered_disk_path)
            claims["name"] = claims.get("name") or row.get("name") or vm_id
            claims["disk_path"] = claims.get("disk_path") or registered_disk
            extra_disks = config.get("additional_disks", [])
            if isinstance(extra_disks, list):
                reims_disk_paths = [
                    str(path if path.is_absolute() else vm_dir / path)
                    for item in extra_disks
                    if isinstance(item, dict) and isinstance(item.get("path"), str)
                    for path in [Path(item["path"]).expanduser()]
                ]
                claims["disk_paths"] = sorted(set(
                    [*claims.get("disk_paths", []), *reims_disk_paths]
                ))
            claims["usb_devices"] = sorted(set(
                ExclusiveVMLock._normalize_usb_resources(claims.get("usb_devices", []))
                + ExclusiveVMLock._normalize_usb_resources(config.get("usb_auto_attach", []))
            ))
            network = config.get("network")
            ssh_port = network.get("ssh_port") if isinstance(network, dict) else None
            claims["network_ports"] = sorted(set(
                set(ExclusiveVMLock._normalize_network_ports(claims.get("network_ports", []))
                    + ExclusiveVMLock._normalize_network_ports(ssh_port))
            ))
            return claims
        except (ImportError, OSError, ValueError, TypeError, AttributeError):
            return {}

    @staticmethod
    def _write_reservation(path: Path, payload: dict) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=".reservation-", dir=VM_RESERVATIONS_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def release(self) -> None:
        if self._reservation_path:
            try:
                with open(VM_RESERVATIONS_LOCK, "a+") as guard:
                    fcntl.flock(guard, fcntl.LOCK_EX)
                    try:
                        self._reservation_path.unlink(missing_ok=True)
                    finally:
                        fcntl.flock(guard, fcntl.LOCK_UN)
            except OSError:
                logger.exception("Não foi possível liberar reserva de RAM %s", self._reservation_path)
            self._reservation_path = None

    @staticmethod
    def get_running_vm_info() -> Optional[str]:
        try:
            names = [row.get("name") for row in ExclusiveVMLock._read_reservations() if row.get("name")]
            return ", ".join(names) or None
        except OSError:
            return None
