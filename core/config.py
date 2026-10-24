"""
vHackintosh — VM Configuration & Data Store
Gerencia o catálogo de VMs cadastradas, persistência JSON e reservas de RAM por VM.
"""

from __future__ import annotations

import os
import json
import fcntl
import logging
import tempfile
import copy
import dataclasses
import math
import secrets
import subprocess
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.path.expanduser("~/.config/vhackintosh"))
VMS_FILE = CONFIG_DIR / "vms.json"
VM_RESERVATIONS_DIR = Path("/tmp/vhackintosh-vm-reservations")
VM_RESERVATIONS_LOCK = Path("/tmp/vhackintosh-vm-reservations.lock")
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
    audio_device: str = "usb-audio"  # macOS Tahoe fornece AppleUSBAudio, mas não AppleHDA x86-64
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
        self._load_error: Optional[Exception] = None
        self.load()

    def load(self) -> None:
        if not VMS_FILE.exists():
            self._vms = {}
            self._load_error = None
            self.save()
            return
        try:
            with open(VMS_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
                if not isinstance(raw_data, dict) or not isinstance(raw_data.get("vms", {}), dict):
                    raise ValueError("vms.json must contain an object with a 'vms' object")
                loaded_vms = {
                    vm_id: VMConfig.from_dict(vm_data)
                    for vm_id, vm_data in raw_data.get("vms", {}).items()
                }
            self._vms = loaded_vms
            self._load_error = None
        except (OSError, UnicodeError, TypeError, ValueError, AttributeError) as exc:
            # Keep the original file and refuse later writes from this store
            # instance. Treating a damaged catalog as empty used to make the
            # next add/update silently replace it with an empty catalog.
            self._load_error = exc
            logger.error("Unable to load VM catalog %s; writes are disabled: %s", VMS_FILE, exc)

    def save(self) -> None:
        if self._load_error is not None and VMS_FILE.exists():
            raise RuntimeError(
                f"Cannot save VM catalog because {VMS_FILE} could not be loaded; "
                "repair the file and reload the store first"
            ) from self._load_error
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
    """Reserva memória do host para uma VM sem bloquear outras VMs."""

    def __init__(self):
        self._reservation_path: Optional[Path] = None
        self.last_error: Optional[dict] = None

    def acquire(self, vm_name: str, ram_gb: int = 8, vm_id: Optional[str] = None) -> bool:
        """Admite a VM se a memória sustenta todas as reservas ativas.

        O lock global existe apenas durante a decisão/gravação. Cada sessão
        mantém uma reserva própria enquanto sua VM estiver em execução.
        """
        self.last_error = None
        try:
            requested = max(1, int(ram_gb)) * 1024**3
            VM_RESERVATIONS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(VM_RESERVATIONS_LOCK, "a+") as guard:
                fcntl.flock(guard, fcntl.LOCK_EX)
                try:
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
                    })
                    return True
                finally:
                    fcntl.flock(guard, fcntl.LOCK_UN)
        except (IOError, OSError, ValueError, TypeError) as exc:
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
