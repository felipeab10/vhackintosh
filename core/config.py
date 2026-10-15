"""
vHackintosh — VM Configuration & Data Store
Gerencia o catálogo de VMs cadastradas, persistência JSON e locks de execução exclusiva.
"""

from __future__ import annotations

import os
import json
import fcntl
import logging
import tempfile
import copy
import dataclasses
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.path.expanduser("~/.config/vhackintosh"))
VMS_FILE = CONFIG_DIR / "vms.json"
LOCK_FILE = Path("/tmp/vhackintosh-vm.lock")
LOGS_DIR = CONFIG_DIR / "logs"


@dataclass
class SMBIOSConfig:
    model: str = "iMac20,1"
    serial_number: str = ""
    board_serial: str = ""  # MLB
    smuuid: str = ""
    rom: str = ""


@dataclass
class VMConfig:
    id: str
    name: str
    macos_version: str  # "tahoe", "sequoia", "sonoma", "ventura", etc.
    vcpus: int = 8
    ram_gb: int = 8
    disk_size_gb: int = 64
    disk_path: str = ""
    auto_start: bool = False
    smbios: SMBIOSConfig = field(default_factory=SMBIOSConfig)
    gpu_mode: str = "reims-vgpu"  # "reims-vgpu" ou "vfio-passthrough"
    vfio_pci_id: Optional[str] = None
    selected_gpu: Optional[str] = None  # Ex: "0000:01:00.0"
    selected_gpu_name: Optional[str] = None  # Ex: "NVIDIA RTX 3050 Mobile"
    audio_device: str = "ich9-intel-hda"  # Padrão ultimate-macOS-KVM (ich9-intel-hda + hda-duplex)
    # Removidos: `force_x11` (a sessão é Wayland nativo desde a migração do
    # appliance) e `display_resolution` (a geometria é resolvida por aspect-fit
    # no reims-vgpu, sem resolução fixa). Ambos eram código morto — nada os lia —
    # mas `from_dict` os ignorava apenas por tolerância a campos extras.
    fullscreen: bool = True
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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> VMConfig:
        # Tolerância a campos desconhecidos: configs gravados por versões
        # anteriores podem conter chaves que já não existem no dataclass (por
        # exemplo `force_x11` e `display_resolution`, removidos na migração para
        # Wayland). Sem este filtro, `cls(**data)` levantaria TypeError e a VM
        # sumiria do gerenciador. Os campos novos já têm default, então a
        # ausência deles também é segura.
        smbios_data = data.pop("smbios", {})
        smbios_fields = {field.name for field in dataclasses.fields(SMBIOSConfig)}
        if isinstance(smbios_data, dict):
            smbios = SMBIOSConfig(**{key: value for key, value in smbios_data.items() if key in smbios_fields})
        else:
            smbios = SMBIOSConfig()
        conhecidos = {f.name for f in dataclasses.fields(cls)}
        extras = {k: v for k, v in data.items() if k not in conhecidos}
        if extras:
            logger.debug(
                "VMConfig.from_dict: ignorando campos desconhecidos: %s",
                ", ".join(sorted(extras)),
            )
        filtrado = {k: v for k, v in data.items() if k in conhecidos}
        return cls(smbios=smbios, **filtrado)


class VMManagerStore:
    """Gerencia a persistência das configurações das VMs em JSON."""

    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            CONFIG_DIR.chmod(0o700)
        except OSError:
            pass
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._vms: Dict[str, VMConfig] = {}
        self.load()

    def load(self) -> None:
        if not VMS_FILE.exists():
            self._vms = {}
            self.save()
            return
        try:
            with open(VMS_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
                self._vms = {
                    vm_id: VMConfig.from_dict(vm_data)
                    for vm_id, vm_data in raw_data.get("vms", {}).items()
                }
        except Exception:
            self._vms = {}

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "version": "1.0",
            "vms": {vm_id: vm.to_dict() for vm_id, vm in self._vms.items()},
        }
        fd, temp_name = tempfile.mkstemp(prefix=".vms.", dir=CONFIG_DIR)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, VMS_FILE)
            os.chmod(VMS_FILE, 0o600)
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

    def add_vm(self, vm: VMConfig) -> None:
        existing = self._vms.get(vm.id)
        if existing and self._has_identity_changed(existing.smbios, vm.smbios):
            raise ValueError("Refusing to overwrite VM with a different SMBIOS identity")
        self._assert_unique_identity(vm)
        if vm.auto_start:
            self._clear_auto_start()
        self._vms[vm.id] = copy.deepcopy(vm)
        self.save()

    def update_vm(self, vm: VMConfig) -> None:
        existing = self._vms.get(vm.id)
        if existing and self._has_identity_changed(existing.smbios, vm.smbios):
            vm_dir = Path(existing.disk_path).parent if existing.disk_path else Path(os.path.expanduser(f"~/.config/vhackintosh/vms/{vm.id}"))
            if not (vm_dir / "smbios-backup-before-regeneration" / "OpenCore.qcow2").is_file():
                raise ValueError("SMBIOS change for an existing VM requires a verified OpenCore backup")
        self._assert_unique_identity(vm)
        if vm.auto_start:
            self._clear_auto_start(exclude_id=vm.id)
        self._vms[vm.id] = copy.deepcopy(vm)
        self.save()

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

    def delete_vm(self, vm_id: str, delete_disk: bool = False) -> bool:
        if vm_id in self._vms:
            vm = self._vms[vm_id]
            if delete_disk and vm.disk_path and os.path.exists(vm.disk_path):
                try:
                    os.remove(vm.disk_path)
                except OSError:
                    pass
            del self._vms[vm_id]
            self.save()
            return True
        return False

    def set_auto_start(self, vm_id: str, enabled: bool = True) -> None:
        if enabled:
            self._clear_auto_start()
            if vm_id in self._vms:
                self._vms[vm_id].auto_start = True
        else:
            if vm_id in self._vms:
                self._vms[vm_id].auto_start = False
        self.save()

    def _clear_auto_start(self, exclude_id: Optional[str] = None) -> None:
        for v_id, vm in self._vms.items():
            if v_id != exclude_id:
                vm.auto_start = False


class ExclusiveVMLock:
    """Garante que apenas uma única VM possa rodar no sistema por vez."""

    def __init__(self):
        self._lock_file = None

    def acquire(self, vm_name: str) -> bool:
        try:
            self._lock_file = open(LOCK_FILE, "w")
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_file.write(f"{vm_name}:{os.getpid()}\n")
            self._lock_file.flush()
            return True
        except (IOError, OSError):
            return False

    def release(self) -> None:
        if self._lock_file:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
                self._lock_file.close()
            except Exception:
                pass
            if LOCK_FILE.exists():
                try:
                    LOCK_FILE.unlink()
                except OSError:
                    pass
            self._lock_file = None

    @staticmethod
    def get_running_vm_info() -> Optional[str]:
        if not LOCK_FILE.exists():
            return None
        try:
            with open(LOCK_FILE, "r") as f:
                content = f.read().strip()
                if content:
                    return content.split(":")[0]
        except Exception:
            pass
        return None
