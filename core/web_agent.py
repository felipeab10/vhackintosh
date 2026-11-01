"""Read-only business services and allowlisted RPC handlers for the web agent."""

from __future__ import annotations

import os
import platform
import socket
import argparse
import grp
import pwd
import re
import hashlib
import json
import shutil
import stat
import subprocess
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psutil

from core.config import ConcurrentCatalogUpdate, SharedVmDiskError, SMBIOSConfig, VMConfig, VMManagerStore
from core.telemetry import TelemetryCollector, TelemetryHistoryStore, VMProcessIdentity
from core.web_gpu import CachedGpuInventory, inventory_gpus
from core.web_vm_units import SystemdVmUnitManager
from core.web_rpc import RpcRemoteError
from core.web_rpc import UnixRpcServer
from core.config import VMS_FILE
from web.api.contracts import (
    HostOverview,
    InstallPhase,
    VmConfig as ApiVmConfig,
    VmConfigEnvelope,
    VmConfigCreate,
    VmConfigUpdate,
    SmbiosConfig as ApiSmbiosConfig,
    VmListResponse,
    VmRunState,
    VmRuntime,
    VmSerialConsole,
    VmProvisionRequest,
    VmActionRequest,
    Operation,
    JobAccepted,
    JobListResponse,
    ReimsReleaseList,
    ReimsReleaseUpdateRequest,
    ReimsImportCandidateList,
    ReimsImportRequest,
    Capability,
    CapabilitiesResponse,
)

VM_SHUTDOWN_TIMEOUT_SECONDS = 180
VM_START_READY_TIMEOUT_SECONDS = 30


def _qcow2_virtual_size(path: Path, expected_stat: os.stat_result) -> int | None:
    """Read only a stable QCOW2 header; never traverse backing-file metadata."""
    if expected_stat.st_size < 32:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        actual = os.fstat(fd)
        if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (expected_stat.st_dev, expected_stat.st_ino):
            return None
        header = os.pread(fd, 32, 0)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(header) != 32 or header[:4] != b"QFI\xfb":
        return None
    version = int.from_bytes(header[4:8], "big")
    virtual_size = int.from_bytes(header[24:32], "big")
    # Keep values exact in the JSON/JavaScript client and reject malformed headers.
    if version not in {2, 3} or not 0 < virtual_size <= (2**53 - 1):
        return None
    return virtual_size


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith(("model name", "hardware")) and ":" in line:
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "Unknown"


def _addresses() -> list[str]:
    results: set[str] = set()
    for interface, entries in psutil.net_if_addrs().items():
        lowered = interface.lower()
        if interface == "lo" or lowered.startswith(("docker", "podman", "virbr", "vnet", "tap", "tun", "br-", "tailscale")):
            continue
        for entry in entries:
            if entry.family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            value = entry.address.split("%", 1)[0]
            try:
                import ipaddress

                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
                continue
            results.add(address.compressed)
    return sorted(results)


def _sample_dict(sample: Any) -> dict[str, Any]:
    return {
        "name": sample.name,
        "value": sample.value,
        "timestamp": sample.timestamp.isoformat(),
        "unit": sample.unit,
        "source": sample.source,
        "quality": sample.quality,
        "availability": sample.availability,
        "labels": dict(sample.labels),
    }


class WebAgentService:
    """One catalog and telemetry owner shared by fixed RPC handlers."""

    def __init__(
        self,
        catalog_path: str | os.PathLike[str],
        *,
        telemetry: TelemetryCollector | None = None,
        gpu_inventory: Callable[[], list[Any]] = inventory_gpus,
        usb_inventory: Callable[[], list[Any]] | None = None,
        running_vms: Callable[[], dict[str, int]] | None = None,
        qmp_factory: Callable[[VMConfig], Any] | None = None,
        smbios_factory: Callable[[str], SMBIOSConfig] | None = None,
        jobs_db_path: str | os.PathLike[str] | None = None,
        telemetry_history_path: str | os.PathLike[str] | None = None,
        telemetry_history_enabled: bool | None = None,
        provisioning_service: Any | None = None,
        job_runner: Any | None = None,
        vm_unit_manager: Any | None = None,
        transfer_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.store = VMManagerStore(catalog_path)
        self.telemetry = telemetry or TelemetryCollector()
        if telemetry_history_enabled is None:
            try:
                from core.mode import is_live_media
                telemetry_history_enabled = not is_live_media()
            except Exception:
                telemetry_history_enabled = True
        history_path = telemetry_history_path or (self.store.catalog_path.parent / "telemetry.sqlite")
        self.telemetry_history = TelemetryHistoryStore(history_path if telemetry_history_enabled else None)
        if hasattr(self.telemetry, "sample_sink"):
            # The collector invokes the sink only for a fresh collection batch;
            # cached host snapshots are consequently persisted once, not per client.
            self.telemetry.sample_sink = self.telemetry_history.record
        # Vulkan/Reims inventory is bounded but can take seconds. Cache it in
        # the single agent process so browser/RPC clients share one probe.
        self.gpu_inventory = CachedGpuInventory(gpu_inventory)
        if usb_inventory is None:
            from core.passthrough import list_usb_devices
            usb_inventory = list_usb_devices
        self.usb_inventory = usb_inventory
        self._running_vms = running_vms
        self._qmp_factory = qmp_factory
        self.vm_unit_manager = vm_unit_manager
        self.transfer_root = Path(transfer_root or os.environ.get("VHACKINTOSH_WEB_TRANSFER_DIR", "/var/lib/vhackintosh/web/transfers"))
        if smbios_factory is None:
            from core.smbios import GenSMBIOS

            smbios_factory = lambda version: GenSMBIOS.generate(macos_version=version)
        self._smbios_factory = smbios_factory
        self.job_store = None
        self.job_runner = job_runner
        self.media_job_runner = None
        self.backup_root = Path(os.environ.get("VHACKINTOSH_VM_BACKUP_DIR", str(self.store.config_dir / "backups")))
        self.provisioning_service = provisioning_service
        if jobs_db_path is not None:
            from core.web_jobs import JobStore
            from core.web_job_runner import WebJobRunner
            from core.vm_provisioning import VMProvisioningService

            self.job_store = JobStore(jobs_db_path)
            self.job_runner = self.job_runner or WebJobRunner(
                self.job_store, workers=1, queue_size=8, checkpoint_timeout=3600,
                reconcile_kinds={"vm.provision", "vm.shutdown", "vm.backup", "vm.backup.verify", "vm.backup.restore", "vm.clone", "vm.start", "vm.force_stop", "vm.restart", "vm.opencore.picker", "vm.smbios.diagnose", "reims.release.update"},
            )
            self.media_job_runner = WebJobRunner(
                self.job_store, workers=1, queue_size=8, checkpoint_timeout=3600,
                reconcile_kinds={"media.import", "media.convert", "media.apple.download"},
            )
            self.provisioning_service = self.provisioning_service or VMProvisioningService(store=self.store)
            self.job_runner.register("vm.provision", self._run_provision_job)
            self.job_runner.register("vm.shutdown", self._run_vm_shutdown_job)
            self.job_runner.register("vm.start", self._run_vm_start_job)
            self.job_runner.register("vm.force_stop", self._run_vm_force_stop_job)
            self.job_runner.register("vm.restart", self._run_vm_restart_job)
            self.job_runner.register("vm.backup", self._run_vm_backup_job)
            self.job_runner.register("vm.backup.verify", self._run_vm_backup_verify_job)
            self.job_runner.register("vm.backup.restore", self._run_vm_backup_restore_job)
            self.job_runner.register("vm.clone", self._run_vm_clone_job)
            self.job_runner.register("vm.opencore.picker", self._run_vm_opencore_picker_job)
            self.job_runner.register("vm.smbios.diagnose", self._run_vm_smbios_diagnostic_job)
            self.job_runner.register("reims.release.update", self._run_reims_release_update_job)
            self.media_job_runner.register("media.import", self._run_media_import_job)
            self.media_job_runner.register("media.convert", self._run_media_convert_job)
            self.media_job_runner.register("media.apple.download", self._run_apple_recovery_download_job)

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        """Return the narrow local method allowlist; no generic execution exists."""
        return {
            "agent.health": self.agent_health,
            "host.overview": self.host_overview,
            "host.metrics": self.host_metrics,
            "host.metrics.history": self.host_metrics_history,
            "telemetry.settings.get": self.get_telemetry_settings,
            "telemetry.settings.set": self.set_telemetry_settings,
            "reims.releases.list": self.list_reims_releases,
            "reims.release.update": self.submit_reims_release_update,
            "host.gpus": self.host_gpus,
            "host.usb": self.host_usb_inventory,
            "storage.inventory": self.storage_inventory,
            "storage.media.import": self.submit_media_import_job,
            "storage.media.convert": self.submit_media_convert_job,
            "storage.apple.download": self.submit_apple_recovery_download_job,
            "vms.backups.list": self.list_vm_backups,
            "vms.backup": self.submit_vm_backup_job,
            "vms.backup.verify": self.submit_vm_backup_verify_job,
            "vms.backup.restore": self.submit_vm_backup_restore_job,
            "vms.clone": self.submit_vm_clone_job,
            "host.capabilities": self.runtime_capabilities,
            "vms.list": self.list_vms,
            "vms.import.candidates": self.list_reims_import_candidates,
            "vms.import": self.import_reims_vm,
            "vms.config.get": self.get_vm_config,
            "vms.config.create": self.create_vm_config,
            "vms.config.update": self.update_vm_config,
            "vms.config.patch": self.patch_vm_config,
            "vms.opencore.picker": self.submit_vm_opencore_picker,
            "vms.smbios.diagnose": self.submit_vm_smbios_diagnostic,
            "vms.config.delete": self.delete_vm_config,
            "vms.autostart.get": self.get_auto_start_vm,
            "vms.autostart.set": self.set_auto_start_vm,
            "vms.runtime.get": self.get_vm_runtime,
            "vms.runtime.list": self.list_vm_runtimes,
            "vms.serial.get": self.get_vm_serial_console,
            "vms.metrics.get": self.get_vm_metrics,
            "vms.metrics.history": self.get_vm_metrics_history,
            "vms.action": self.vm_action,
            "vms.provision": self.submit_provision_job,
            "jobs.list": self.list_jobs,
            "jobs.get": self.get_job,
            "jobs.cancel": self.cancel_job,
            "jobs.retry": self.retry_job,
        }

    @staticmethod
    def agent_health(_payload: dict[str, Any]) -> dict[str, str]:
        return {"status": "ok", "version": "1"}

    def host_overview(self, _payload: dict[str, Any]) -> dict[str, Any]:
        memory = psutil.virtual_memory()
        return HostOverview(
            hostname=socket.gethostname(),
            architecture=platform.machine() or "unknown",
            cpu_model=_cpu_model(),
            logical_cpus=max(1, psutil.cpu_count(logical=True) or 1),
            memory_total_bytes=memory.total,
            memory_available_bytes=memory.available,
            uptime_seconds=max(0.0, datetime.now(timezone.utc).timestamp() - psutil.boot_time()),
            addresses=_addresses(),
        ).model_dump(mode="json")

    def host_metrics(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {"samples": [_sample_dict(sample) for sample in self.telemetry.sample_host()]}

    def host_metrics_history(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._metrics_history(payload)

    def get_telemetry_settings(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "history_retention_hours": self.telemetry_history.retention_hours,
            "history_persistence_enabled": self.telemetry_history.enabled,
        }

    def set_telemetry_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        hours = payload.get("history_retention_hours")
        if isinstance(hours, bool) or not isinstance(hours, int):
            raise RpcRemoteError("invalid_request", "A retenção deve ser um número inteiro de horas.")
        try:
            configured = self.telemetry_history.set_retention_hours(hours)
        except ValueError as exc:
            raise RpcRemoteError("invalid_request", str(exc)) from exc
        except RuntimeError as exc:
            raise RpcRemoteError("capability_unavailable", "O histórico persistente está desativado neste modo do appliance.") from exc
        return self.get_telemetry_settings({}) | {"history_retention_hours": configured}

    def list_reims_releases(self, _payload: dict[str, Any]) -> dict[str, Any]:
        from core.reims_build import prebuilt_current_release
        from core.reims_release import DEFAULT_REIMS_DIR, REIMS_REPO, ReleaseError, current_release, list_releases

        try:
            releases = list_releases()
        except ReleaseError as exc:
            raise RpcRemoteError("release_catalog_unavailable", "Não foi possível consultar as releases do runtime Reims.") from exc
        return ReimsReleaseList(
            repository=REIMS_REPO,
            current_release=current_release(DEFAULT_REIMS_DIR),
            active_release=prebuilt_current_release(),
            items=[{
                "tag": item.tag,
                "name": item.name or item.tag,
                "published_at": item.published_at or None,
                "asset_size_bytes": item.asset_size,
                "prerelease": item.prerelease,
            } for item in releases],
        ).model_dump(mode="json")

    def submit_reims_release_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.job_runner is None:
            raise RpcRemoteError("capability_unavailable", "O serviço de tarefas não está configurado.")
        try:
            request = ReimsReleaseUpdateRequest.model_validate(payload)
        except ValueError as exc:
            raise RpcRemoteError("invalid_request", "A tag da release ou a chave da operação é inválida.") from exc
        try:
            job, _created = self.job_runner.submit(
                idempotency_key=f"reims-release:{request.tag}:{request.idempotency_key}",
                kind="reims.release.update",
                payload={"tag": request.tag},
            )
            return JobAccepted(job_id=job["id"], events_url=f"/api/v1/jobs/{job['id']}").model_dump(mode="json")
        except Exception as exc:
            from core.web_job_runner import QueueFullError

            if isinstance(exc, QueueFullError):
                raise RpcRemoteError("job_queue_full", "A fila de tarefas está cheia; tente novamente mais tarde.") from exc
            raise

    def _metrics_history(self, payload: dict[str, Any], labels: dict[str, str] | None = None) -> dict[str, Any]:
        name = payload.get("name")
        if not isinstance(name, str) or not name or len(name) > TelemetryHistoryStore.MAX_TEXT:
            raise RpcRemoteError("invalid_request", "Informe o nome de uma métrica válido.")
        resolution = payload.get("resolution_seconds", 10)
        if isinstance(resolution, bool) or not isinstance(resolution, int):
            raise RpcRemoteError("invalid_request", "A resolução deve ser 10 ou 60 segundos.")
        since = self._history_datetime(payload.get("since"))
        until = self._history_datetime(payload.get("until"))
        limit = payload.get("limit", 5000)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise RpcRemoteError("invalid_request", "O limite deve ser um inteiro entre 1 e 5000.")
        try:
            points = self.telemetry_history.query(
                name, labels=labels if labels is not None else payload.get("labels", {}),
                resolution=resolution, since=since, until=until, limit=limit,
            )
        except (TypeError, ValueError) as exc:
            raise RpcRemoteError("invalid_request", str(exc)) from exc
        return {"name": name, "resolution_seconds": resolution, "points": points}

    @staticmethod
    def _history_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 64:
            raise RpcRemoteError("invalid_request", "Timestamp inválido.")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timestamp deve conter fuso horário")
            return parsed
        except ValueError as exc:
            raise RpcRemoteError("invalid_request", "Timestamp inválido.") from exc

    def host_gpus(self, _payload: dict[str, Any]) -> dict[str, Any]:
        devices = [device.as_dict() if hasattr(device, "as_dict") else device for device in self.gpu_inventory()]
        inventory_error = getattr(self.gpu_inventory, "last_error", None)
        if inventory_error:
            devices = [
                {**device, "selection_supported": False, "limitation": inventory_error}
                if isinstance(device, dict) else device
                for device in devices
            ]
        return {"items": devices, "stale": bool(inventory_error), "message": inventory_error}

    def host_usb_inventory(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """Expose only safe USB labels and VID:PID; never return serial/sysfs/bus data."""
        try:
            devices = self.usb_inventory()
        except Exception:
            return {"items": [], "stale": True, "message": "Não foi possível consultar o inventário USB do host."}
        grouped: dict[str, list[str]] = {}
        for device in devices:
            vendor = str(getattr(device, "vendor_id", "")).lower()
            product = str(getattr(device, "product_id", "")).lower()
            if not re.fullmatch(r"[0-9a-f]{4}", vendor) or not re.fullmatch(r"[0-9a-f]{4}", product):
                continue
            device_id = f"{vendor}:{product}"
            label = str(getattr(device, "product", "") or getattr(device, "manufacturer", "") or "Dispositivo USB")
            label = "".join(char for char in label if char.isprintable()).strip()[:120] or "Dispositivo USB"
            grouped.setdefault(device_id, []).append(label)
        items = []
        for device_id, labels in sorted(grouped.items()):
            unique = len(labels) == 1
            items.append({
                "device_id": device_id,
                "label": labels[0],
                "device_count": len(labels),
                "auto_attach_supported": unique,
                "limitation": None if unique else "Há mais de um dispositivo com este VID:PID; o QEMU não consegue distinguir qual deles será anexado.",
            })
        return {"items": items, "stale": False, "message": None}

    def _validated_usb_auto_attach(self, requested: list[str], *, existing: VMConfig | None = None) -> list[str]:
        """Allow only unique current host devices; unchanged legacy values are preserved."""
        normalized = [value.lower() for value in requested]
        prior = {str(value).lower() for value in (existing.usb_auto_attach if existing else [])}
        additions = set(normalized) - prior
        if not additions:
            return normalized
        inventory = self.host_usb_inventory({})
        if inventory["stale"]:
            raise RpcRemoteError("usb_selection_unavailable", "O inventário USB está indisponível; atualize antes de escolher dispositivos.")
        available = {
            item["device_id"] for item in inventory["items"]
            if item["auto_attach_supported"]
        }
        if additions - available:
            raise RpcRemoteError("usb_selection_unavailable", "Selecione apenas dispositivos USB únicos encontrados no inventário atual do host.")
        return normalized

    def storage_inventory(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """Report managed media, references, and bounded image-header sizes."""
        self._refresh_catalog()
        config_root = self.store.config_dir.resolve(strict=False)
        managed_root_path = self.store.config_dir / "vms"
        managed_root = managed_root_path.resolve(strict=False)
        managed_root_valid = not managed_root_path.is_symlink() and managed_root.is_relative_to(config_root)
        media_root_path = self.store.config_dir / "media"
        media_root = media_root_path.resolve(strict=False)
        media_root_valid = not media_root_path.is_symlink() and media_root.is_relative_to(config_root)
        image_root_path = self.store.config_dir / "images"
        image_root = image_root_path.resolve(strict=False)
        image_root_valid = not image_root_path.is_symlink() and image_root.is_relative_to(config_root)
        managed_roots = [
            root for root, valid in ((managed_root, managed_root_valid), (media_root, media_root_valid), (image_root, image_root_valid))
            if valid
        ]
        groups: dict[str, dict[str, Any]] = {}
        for vm in self.store.list_vms():
            configured_disks = ([(vm.disk_path, True)] if vm.disk_path else []) + [(disk.path, False) for disk in (vm.additional_disks or [])]
            for disk_value, is_primary_disk in configured_disks:
                path = Path(disk_value).expanduser()
                try:
                    resolved = path.resolve(strict=False)
                    managed = any(resolved.is_relative_to(root) for root in managed_roots)
                except (OSError, RuntimeError):
                    resolved = path.absolute()
                    managed = False
                key = str(resolved)
                item = groups.setdefault(key, {
                    "path": resolved,
                    "id": hashlib.sha256(os.fsencode(key)).hexdigest()[:32],
                    "name": path.name[:255] or "Disco sem nome",
                    "location": "managed" if managed else "external",
                    "availability": "external" if not managed else "missing",
                    "virtual_sizes": set(),
                    "references": set(),
                })
                item["references"].add(vm.id)
                if is_primary_disk and isinstance(vm.disk_size_gb, int) and not isinstance(vm.disk_size_gb, bool) and vm.disk_size_gb >= 0:
                    item["virtual_sizes"].add(vm.disk_size_gb * (1024 ** 3))
                else:
                    try:
                        info = path.lstat()
                        if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                            virtual_size = _qcow2_virtual_size(path, info) if path.suffix.casefold() == ".qcow2" else info.st_size
                            if virtual_size is not None:
                                item["virtual_sizes"].add(virtual_size)
                    except OSError:
                        pass

        # Include unreferenced VM disks, uploaded media, and cached Apple
        # Recovery images so operators can see imported or abandoned files.
        # Discovery is bounded to these managed roots and one child directory;
        # symlinks and unknown types are never followed or probed.
        candidates: list[Path] = []
        max_candidates = 4096
        for root, root_valid in (
            (managed_root, managed_root_valid),
            (media_root, media_root_valid),
            (image_root, image_root_valid),
        ):
            if not root_valid:
                continue
            try:
                for child in root.iterdir():
                    if len(candidates) >= max_candidates:
                        break
                    if child.is_symlink():
                        continue
                    if child.is_dir():
                        try:
                            for nested in child.iterdir():
                                if len(candidates) >= max_candidates:
                                    break
                                candidates.append(nested)
                        except OSError:
                            continue
                    else:
                        candidates.append(child)
            except OSError:
                continue
        known_media_suffixes = {".qcow2", ".img", ".raw", ".iso", ".dmg"}
        for candidate in candidates:
            if candidate.suffix.casefold() not in known_media_suffixes or candidate.is_symlink():
                continue
            try:
                metadata = candidate.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                resolved = candidate.resolve(strict=True)
                in_managed_root = managed_root_valid and resolved.is_relative_to(managed_root)
                in_media_root = media_root_valid and resolved.is_relative_to(media_root)
                in_image_root = image_root_valid and resolved.is_relative_to(image_root)
                if not (in_managed_root or in_media_root or in_image_root):
                    continue
            except (OSError, RuntimeError):
                continue
            key = str(resolved)
            if key in groups:
                continue
            groups[key] = {
                "path": resolved,
                "id": hashlib.sha256(os.fsencode(key)).hexdigest()[:32],
                "name": candidate.name[:255] or "Mídia sem nome",
                "location": "managed",
                "availability": "available",
                "virtual_sizes": {
                    (_qcow2_virtual_size(candidate, metadata) if candidate.suffix.casefold() == ".qcow2" else metadata.st_size)
                } - {None} if candidate.suffix.casefold() in {".qcow2", ".img", ".raw"} else set(),
                "references": set(),
            }

        items: list[dict[str, Any]] = []
        for entry in groups.values():
            resolved = entry.pop("path")
            sizes = entry.pop("virtual_sizes")
            entry["virtual_size_bytes"] = next(iter(sizes)) if len(sizes) == 1 else None
            entry["references"] = sorted(entry["references"])
            entry["allocated_bytes"] = None
            if entry["location"] == "managed":
                try:
                    original = resolved
                    for vm_id in entry["references"]:
                        referenced_vm = self.store.get_vm(vm_id)
                        referenced_paths = ([referenced_vm.disk_path] if referenced_vm.disk_path else []) + [disk.path for disk in (referenced_vm.additional_disks or [])]
                        matching_path = next((Path(value).expanduser() for value in referenced_paths if Path(value).expanduser().resolve(strict=False) == resolved), None)
                        if matching_path is not None:
                            original = matching_path
                            break
                    metadata = original.lstat()
                    if stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and resolved == original.resolve(strict=True):
                        entry["availability"] = "available"
                        entry["allocated_bytes"] = getattr(metadata, "st_blocks", 0) * 512 or metadata.st_size
                except (OSError, RuntimeError, AttributeError):
                    entry["availability"] = "missing"
            items.append(entry)
        try:
            free_bytes = shutil.disk_usage(self.store.config_dir).free
        except OSError:
            free_bytes = None
        return {
            "items": sorted(items, key=lambda item: item["name"].casefold()),
            "free_bytes": free_bytes,
            "managed_volume": "Volume das VMs gerenciadas",
        }

    def runtime_capabilities(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """Report only implementation and runtime facts known to this agent."""
        from core.runtime_inventory import inspect_runtime

        runtime = inspect_runtime()
        reims_options = set(runtime.get("reims_set_options", []))
        reims_schema_features = set(runtime.get("reims_schema_features", []))
        qemu_arguments_available = (
            runtime.get("status") == "available"
            and {"-rtc", "-msg"}.issubset(set(runtime.get("qemu_options", [])))
        )
        audio_backends = runtime.get("qemu_audio_backends", [])
        audio_backend_available = isinstance(audio_backends, list) and bool(audio_backends)
        reims_release_tag = runtime.get("reims_release_tag")
        release_match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9.-]+)?", str(reims_release_tag or ""))
        serial_log_available = bool(
            runtime.get("status") == "available"
            and release_match
            and tuple(map(int, release_match.groups()[:3])) >= (1, 3, 0)
        )
        virtio_audio_release = bool(release_match and tuple(map(int, release_match.groups()[:3])) >= (1, 4, 0))
        virtio_audio_device = "virtio-sound-pci" in (runtime.get("qemu_devices") or [])
        virtio_audio_options = {"--audio", "--audio-backend"}.issubset(reims_options)
        virtio_audio_available = bool(
            runtime.get("status") == "available"
            and virtio_audio_release
            and virtio_audio_device
            and virtio_audio_options
        )
        if virtio_audio_available:
            virtio_audio_limitation = None
        elif reims_release_tag and not virtio_audio_release:
            virtio_audio_limitation = (
                f"A release Reims {reims_release_tag} não ativa VirtIO Sound; é necessário v1.4.0 ou superior."
            )
        elif not virtio_audio_device:
            virtio_audio_limitation = "O QEMU selecionado não anuncia virtio-sound-pci."
        elif not reims_release_tag:
            virtio_audio_limitation = (
                "A release Reims instalada não foi identificada; não é possível confirmar que o launcher ativa VirtIO Sound."
            )
        else:
            virtio_audio_limitation = "O Reims selecionado não anuncia controle estruturado de áudio."
        network_available = "virtio-net-pci" in (runtime.get("qemu_devices") or [])
        network_available = network_available and {"--network", "--mac"}.issubset(reims_options)
        gpu_identity_available = {"gpu_identity", "gpu_inventory_json"}.issubset(reims_schema_features)
        gpu_json_available = "gpu_inventory_json" in reims_schema_features
        gpu_items = [device.as_dict() if hasattr(device, "as_dict") else device for device in self.gpu_inventory()]
        selectable_gpus = [item for item in gpu_items if isinstance(item, dict) and item.get("selection_supported") is True]
        inventory_error = getattr(self.gpu_inventory, "last_error", None)
        if selectable_gpus:
            gpu_available = True
            gpu_limitation = None
        else:
            gpu_available = False
            gpu_limitation = inventory_error or next(
                (item.get("limitation") for item in gpu_items if isinstance(item, dict) and item.get("limitation")),
                "Nenhuma GPU física com seleção exata foi confirmada neste host.",
            )

        provisioning_available = self.job_runner is not None and self.provisioning_service is not None
        lifecycle_available = self.job_runner is not None and self.vm_unit_manager is not None
        items = [
            Capability(
                id="vm.cpu.vcpus", label="Quantidade de vCPUs", supported=True,
                available="--cpus" in reims_options, requires_stopped_vm=True,
                description="O valor estruturado cpus é projetado e aplicado no próximo boot.",
                limitation=None if "--cpus" in reims_options else "O comando Reims não anuncia configuração de vCPUs.",
            ),
            Capability(
                id="vm.cpu.model", label="Modelo de CPU", supported=True,
                available=bool(runtime.get("qemu_cpu_models")) and "--cpus" in reims_options,
                requires_stopped_vm=True,
                description="O modelo só pode ser escolhido entre os modelos listados pelo QEMU selecionado.",
                limitation=None if runtime.get("qemu_cpu_models") and "--cpus" in reims_options else "Inventário QEMU/Reims insuficiente para validar o modelo de CPU.",
            ),
            Capability(
                id="vm.cpu.topology", label="Topologia de CPU",
                supported="cpu_topology" in reims_schema_features,
                available="cpu_topology" in reims_schema_features, requires_stopped_vm=True,
                description="Sockets, cores e threads são configurados somente quando o schema Reims anuncia cpu_topology; omitido mantém 1×N×1.",
                limitation=None if "cpu_topology" in reims_schema_features else "O runtime Reims não anuncia cpu_topology; a configuração atual será preservada e o painel mantém o default 1×N×1.",
            ),
            Capability(
                id="vm.cpu.flags", label="Flags de CPU e virtualização aninhada", supported=False,
                available=False, requires_stopped_vm=True,
                description="O painel não injeta flags CPU arbitrárias nem habilita nested virtualization por VM.",
                limitation="A aplicação segura exige campos estruturados do Reims e validação da combinação QEMU/host/macOS.",
            ),
            Capability(
                id="vm.memory", label="Memória da VM", supported=True,
                available="--memory" in reims_options, requires_stopped_vm=True,
                description="A memória configurada é aplicada no próximo boot e participa da reserva de recursos do host.",
                limitation=None if "--memory" in reims_options else "O comando Reims não anuncia configuração de memória.",
            ),
            Capability(
                id="vm.memory.numa", label="NUMA e política de memória", supported=False,
                available=False, requires_stopped_vm=True,
                description="A RAM total pode ser editada; topologia NUMA, huge pages e afinidade de memória não.",
                limitation="O launcher Reims selecionado não oferece esses campos estruturados.",
            ),
            Capability(
                id="vm.disks.new_size", label="Tamanho de disco novo", supported=True,
                available="--disk-size" in runtime.get("reims_create_options", []), requires_stopped_vm=False,
                description="O tamanho é editável durante o provisionamento de uma nova VM.",
                limitation=None if "--disk-size" in runtime.get("reims_create_options", []) else "O comando Reims não anuncia criação de disco com tamanho configurável.",
            ),
            Capability(
                id="host.telemetry", label="Telemetria do host", supported=True, available=True,
                requires_stopped_vm=False,
                description="A coleta do host está integrada; cada métrica informa sua própria disponibilidade e origem.",
            ),
            Capability(
                id="gpu.exact_selection", label="Seleção exata de GPU", supported=True,
                available=gpu_available, requires_stopped_vm=True,
                description="A seleção só fica disponível para placas cujo BDF foi associado sem ambiguidade ao runtime.",
                limitation=gpu_limitation,
            ),
            Capability(
                id="gpu.identity", label="Identidade estruturada de GPU",
                supported=gpu_identity_available, available=gpu_identity_available,
                requires_stopped_vm=True,
                description="O Reims recebe device_uuid, endereço PCI e driver_file, e informa separadamente identidade configurada e efetiva.",
                limitation=None if gpu_identity_available else "O schema Reims não anuncia gpu_identity; seleção e valores legados usam o caminho compatível.",
            ),
            Capability(
                id="gpu.inventory_json", label="Inventário JSON de GPUs",
                supported=gpu_json_available, available=gpu_json_available,
                requires_stopped_vm=False,
                description="O inventário usa `reims-ctl gpus --json` e valida UUID, BDF e driver antes de liberar seleção.",
                limitation=None if gpu_json_available else "O schema Reims não anuncia gpu_inventory_json; o formato legado continua disponível.",
            ),
            Capability(
                id="vm.provisioning", label="Provisionamento de VM", supported=True,
                available=provisioning_available, requires_stopped_vm=False,
                limitation=None if provisioning_available else "O provisionador assíncrono não está configurado neste agente.",
            ),
            Capability(
                id="vm.lifecycle", label="Controles de energia da VM", supported=True,
                available=lifecycle_available, requires_stopped_vm=False,
                description="Iniciar e reiniciar usam unidades systemd isoladas; desligar usa ACPI/QMP; parada forçada usa apenas QMP.",
                limitation=None if lifecycle_available else "O supervisor por VM e o serviço durável de tarefas precisam estar ativos.",
            ),
            Capability(
                id="vm.pause_resume", label="Pausar e retomar VM", supported=False,
                available=False, requires_stopped_vm=False,
                description="O binário QEMU empacotado aceitou stop/cont por QMP; o painel mantém os controles bloqueados até validar o caminho pelo launcher Reims com uma VM real.",
                limitation="O smoke test QMP não validou ainda o ciclo Reims/macOS, a reconciliação do estado pausado nem a recuperação do supervisor.",
            ),
            Capability(
                id="vm.opencore.picker", label="Regenerar OpenCore e alterar picker", supported=True,
                available=bool(self.job_runner is not None and runtime.get("reims_update_opencore_picker")),
                requires_stopped_vm=True,
                description="Executa somente reims-ctl update-opencore --boot-selection; mantém a identidade SMBIOS lida da EFI e exige confirmação explícita.",
                limitation=None if self.job_runner is not None and runtime.get("reims_update_opencore_picker") else (
                    "O agente precisa de tarefas persistentes e o Reims selecionado deve anunciar update-opencore --boot-selection."
                ),
            ),
            Capability(
                id="vm.smbios.diagnostics", label="Diagnóstico de identidade SMBIOS", supported=True,
                available=bool(self.job_runner is not None and shutil.which("guestfish")),
                requires_stopped_vm=True,
                description="Compara a identidade SMBIOS cadastrada com o OpenCore da VM sem exibir valores de série, MLB, UUID ou ROM.",
                limitation=None if self.job_runner is not None and shutil.which("guestfish") else (
                    "O diagnóstico exige o serviço persistente de tarefas e o utilitário guestfish instalado."
                ),
            ),
            Capability(
                id="vm.smbios.edit", label="Editar identidade SMBIOS", supported=False,
                available=False, requires_stopped_vm=True,
                description="A regeneração segura do OpenCore conserva a identidade da imagem atual; não altera serial, MLB, UUID ou ROM.",
                limitation="O Reims selecionado ainda não oferece atualização estruturada de SMBIOS. A edição deve permanecer bloqueada até o runtime suportar aplicação e verificação da EFI.",
            ),
            Capability(
                id="vm.qemu_extra_args", label="Argumentos QEMU personalizados", supported=True,
                available=qemu_arguments_available, requires_stopped_vm=True,
                description="Subset seguro aplicado pelo Reims no próximo boot: -rtc base=utc e -msg timestamp=on.",
                limitation=None if qemu_arguments_available else "Não foi possível confirmar que o Reims e o QEMU selecionados aceitam o conjunto seguro de opções.",
            ),
            Capability(
                id="vm.network_disable", label="Desativar rede da VM", supported=True,
                available=network_available, requires_stopped_vm=True,
                description="A opção estruturada [network].enabled é aplicada pelo Reims no próximo boot.",
                limitation=None if network_available else "O QEMU selecionado não informou virtio-net-pci no inventário.",
            ),
            Capability(
                id="vm.network.nat", label="Rede user/NAT", supported=True,
                available=network_available, requires_stopped_vm=True,
                description="O runtime atual oferece rede user-mode/NAT; ela não exige criar bridge no host.",
                limitation=None if network_available else "QEMU/Reims não confirmaram o dispositivo de rede deste runtime.",
            ),
            Capability(
                id="vm.network.bridge", label="Rede bridge/tap", supported=False,
                available=False, requires_stopped_vm=True,
                description="A interface do host ainda não é conectada diretamente à VM.",
                limitation="O schema e o launcher Reims atuais só oferecem user-mode/NAT. Bridge/tap requer suporte estruturado no Reims e um helper restrito para preparar a interface com segurança.",
            ),
            Capability(
                id="vm.network.port_forwarding", label="Redirecionamento SSH local", supported="--ssh-port" in reims_options,
                available=network_available and "--ssh-port" in reims_options, requires_stopped_vm=True,
                description="Uma porta TCP exclusiva em 127.0.0.1 do host encaminha para SSH (porta 22) do guest pela rede NAT.",
                limitation=None if network_available and "--ssh-port" in reims_options else "O Reims/QEMU selecionado não confirma o encaminhamento SSH local; regras arbitrárias e bridge/tap continuam indisponíveis.",
            ),
            Capability(
                id="vm.network_mac", label="Endereço MAC da VM", supported=True,
                available=network_available, requires_stopped_vm=True,
                description="MAC local por VM, validado e persistido em [network].mac.",
                limitation=None if network_available else "O QEMU selecionado não informou o dispositivo de rede esperado pelo Reims.",
            ),
            Capability(
                id="vm.audio.enabled", label="Áudio habilitado", supported=True,
                available="--audio" in reims_options, requires_stopped_vm=True,
                description="A opção estruturada [audio].enabled é aplicada no próximo boot.",
                limitation=None if "--audio" in reims_options else "O comando Reims selecionado não anuncia controle de áudio.",
            ),
            Capability(
                id="vm.audio.backend", label="Backend de áudio do host", supported=True,
                available=audio_backend_available and "--audio-backend" in reims_options, requires_stopped_vm=True,
                description="Seleção limitada à lista de backends informada pelo QEMU selecionado.",
                limitation=None if audio_backend_available and "--audio-backend" in reims_options else "QEMU e Reims precisam anunciar suporte ao backend de áudio.",
            ),
            Capability(
                id="vm.audio.virtio_device", label="Dispositivo de áudio VirtIO", supported=True,
                available=virtio_audio_available, requires_stopped_vm=True,
                description=(
                    "Reims v1.4.0 ou superior cria VirtIO Sound automaticamente quando o áudio está habilitado; "
                    "o backend do host continua configurável abaixo. Em x86-64 também há HDA para o som de boot do OpenCore. "
                    "A compatibilidade do áudio convidado ainda depende de validação no macOS."
                ),
                limitation=virtio_audio_limitation,
            ),
            Capability(
                id="vm.disks.multiple", label="Múltiplos discos editáveis",
                supported="additional_disks" in reims_schema_features,
                available="additional_disks" in reims_schema_features, requires_stopped_vm=True,
                description="Anexa imagens existentes, únicas e gerenciadas ao próximo boot quando o runtime anuncia esse schema.",
                limitation=None if "additional_disks" in reims_schema_features else "O Reims instalado não anuncia `additional_disks`; atualize o runtime compatível para editar discos adicionais.",
            ),
            Capability(
                id="vm.disks.controllers", label="Controladores de discos adicionais",
                supported="additional_disks" in reims_schema_features,
                available="additional_disks" in reims_schema_features, requires_stopped_vm=True,
                description="AHCI e NVMe dedicados; cada disco tem ordem estável de apresentação, independente do boot principal.",
                limitation=None if "additional_disks" in reims_schema_features else "Controladores adicionais aguardam suporte anunciado pelo schema do Reims.",
            ),
            Capability(
                id="vm.disks.resize", label="Redimensionar disco existente", supported=False,
                available=False, requires_stopped_vm=True,
                limitation="Redimensionar exige operação de imagem e revisão da partição do macOS; o editor mantém o tamanho existente somente leitura.",
            ),
            Capability(
                id="vm.display.multiple", label="Múltiplos monitores",
                supported="--displays" in reims_options,
                available=runtime.get("status") == "available" and "--displays" in reims_options,
                requires_stopped_vm=True,
                description="Configura de 1 a 8 saídas Reims; cada display abre em uma janela local e a compatibilidade do macOS guest ainda depende da versão.",
                limitation=None if runtime.get("status") == "available" and "--displays" in reims_options else "O Reims/QEMU selecionado não confirmou a opção estruturada --displays.",
            ),
            Capability(
                id="vm.pci.passthrough", label="Passthrough PCI/VFIO", supported=False,
                available=False, requires_stopped_vm=True,
                description="A seleção de GPU controla somente a integração suportada pelo Reims vGPU; o campo VFIO legado não inicia passthrough.",
                limitation="O launcher Reims selecionado não oferece passthrough PCI estruturado e validado.",
            ),
            Capability(
                id="vm.serial.console", label="Console serial por VM", supported=True,
                available=serial_log_available, requires_stopped_vm=False,
                description="Lê somente o arquivo serial.log da VM criado pelo Reims; a resposta é limitada e credenciais/identidade SMBIOS são redigidas.",
                limitation=None if serial_log_available else "É necessário identificar o runtime Reims v1.3.0 ou superior, que grava a saída serial em logs/serial.log.",
            ),
            Capability(
                id="vm.power_sync.host", label="Sincronização da energia com o host", supported=False,
                available=False, requires_stopped_vm=True,
                description="O comportamento gerenciado permanece `vm-only`; desligar ou reiniciar o guest não afeta o host.",
                limitation="A política kiosk exclusiva ainda não está implementada com bloqueios atômicos para outras VMs e tarefas de armazenamento.",
            ),
        ]
        serialized = [item.model_dump(mode="json") for item in items]
        fingerprint = hashlib.sha256(
            json.dumps({"runtime": runtime, "items": serialized}, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        ).hexdigest()
        return CapabilitiesResponse(
            runtime_fingerprint=fingerprint,
            generated_at=datetime.now(timezone.utc),
            items=items,
            runtime=runtime,
        ).model_dump(mode="json")

    def list_vms(self, _payload: dict[str, Any]) -> dict[str, Any]:
        self._refresh_catalog()
        items = [self._config_envelope(vm).model_dump(mode="json") for vm in self.store.list_vms()]
        return VmListResponse(items=items, revision=self.store.revision).model_dump(mode="json")

    def list_reims_import_candidates(self, _payload: dict[str, Any]) -> dict[str, Any]:
        self._refresh_catalog()
        from core.reims_ctl import registry_file, vm_runtime_snapshot
        from core.reims_import import ReimsImportError, list_import_candidates

        try:
            runtime = vm_runtime_snapshot()
            items = list_import_candidates(
                registry_file(), {vm.id for vm in self.store.list_vms()},
                runtime, runtime_observed=True,
            )
        except (ReimsImportError, OSError) as exc:
            raise RpcRemoteError("reims_import_unavailable", "Não foi possível revisar o registro e os arquivos do Reims.") from exc
        return ReimsImportCandidateList(items=items).model_dump(mode="json")

    def import_reims_vm(self, payload: dict[str, Any]) -> dict[str, Any]:
        from core.downloader import MACOS_PRODUCTS
        from core.reims_ctl import registry_file, vm_runtime_snapshot
        from core.reims_import import ReimsImportError, build_vm_config, list_import_candidates

        try:
            request = ReimsImportRequest.model_validate({
                "id": payload.get("id"),
                "macos_version": payload.get("macos_version"),
            })
        except Exception as exc:
            raise RpcRemoteError("invalid_request", "Informe a VM Reims e a versão macOS para importar.") from exc
        expected = self._expected_revision(payload)
        if request.macos_version not in MACOS_PRODUCTS:
            raise RpcRemoteError("invalid_vm_config", "Versão do macOS não suportada pelo provisionador.")
        self._refresh_catalog()
        if self.store.get_vm(request.id) is not None:
            raise RpcRemoteError("vm_already_exists", "Esta VM já está no catálogo do vHackintosh.")
        try:
            runtime = vm_runtime_snapshot()
            candidates = list_import_candidates(
                registry_file(), set(), runtime, runtime_observed=True,
            )
            candidate = next((item for item in candidates if item["id"] == request.id), None)
            if candidate is None:
                raise ReimsImportError("A VM não consta mais no registro Reims; atualize a prévia.")
            if candidate.get("runtime_state") == "running":
                raise ReimsImportError("A VM legada está em execução; pare-a pelo Reims e confirme novamente antes de importar.")
            if candidate.get("runtime_state") == "unknown":
                raise ReimsImportError("Não foi possível confirmar que a VM legada está parada; a importação foi bloqueada.")
            if not candidate["importable"]:
                raise ReimsImportError(candidate.get("limitation") or "A VM não passou pela revisão de importação.")
            vm = build_vm_config(candidate, request.macos_version)
            self.store.add_vm(vm, expected_revision=expected)
        except ConcurrentCatalogUpdate as exc:
            raise RpcRemoteError("catalog_conflict", "O catálogo mudou; recarregue antes de importar.") from exc
        except ReimsImportError as exc:
            raise RpcRemoteError("reims_import_rejected", str(exc)) from exc
        except ValueError as exc:
            raise RpcRemoteError("reims_import_rejected", "A configuração importada conflita com o catálogo atual.") from exc
        self._refresh_catalog()
        imported = self.store.get_vm(request.id)
        assert imported is not None
        return self._config_envelope(imported).model_dump(mode="json")

    def get_auto_start_vm(self, _payload: dict[str, Any]) -> dict[str, Any] | None:
        """Return the single boot target from the agent-owned catalog."""
        self._refresh_catalog()
        vm = self.store.get_auto_start_vm()
        if vm is None:
            return None
        return {"id": vm.id, "name": vm.name, "revision": self.store.revision}

    def set_auto_start_vm(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Update only the boot target without round-tripping unrelated fields."""
        vm_id = self._vm_id(payload)
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise RpcRemoteError("invalid_request", "O campo enabled precisa ser booleano.")
        expected = self._expected_revision(payload)
        self._refresh_catalog()
        if self.store.get_vm(vm_id) is None:
            raise RpcRemoteError("vm_not_found", "VM não encontrada.")
        try:
            self.store.set_auto_start(vm_id, enabled, expected_revision=expected)
        except ConcurrentCatalogUpdate as exc:
            raise RpcRemoteError("catalog_conflict", "O catálogo mudou; recarregue antes de alterar o auto-start.") from exc
        self._refresh_catalog()
        vm = self.store.get_vm(vm_id)
        return {"id": vm_id, "enabled": bool(vm and vm.auto_start), "revision": self.store.revision}

    def get_vm_config(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        vm_id = self._vm_id(payload)
        self._refresh_catalog()
        vm = self.store.get_vm(vm_id)
        return self._config_envelope(vm).model_dump(mode="json") if vm else None

    def create_vm_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate and persist a new stopped VM configuration."""
        try:
            expected = self._expected_revision(payload)
            draft = VmConfigCreate.model_validate(payload.get("config"))
            self._validate_supported_settings(draft)
            cpu_topology = self._validated_cpu_topology(draft)
            display_count = self._validated_display_count(draft)
            self._validate_cpu_model(draft)
            self._validate_host_capacity(draft.vcpus, draft.ram_gb)
            self._refresh_catalog()
            if draft.id in {vm.id for vm in self.store.list_vms()}:
                raise RpcRemoteError("vm_already_exists", "Já existe uma VM com este identificador.")
            usb_auto_attach = self._validated_usb_auto_attach(draft.usb_auto_attach)
            self._validate_network_mac(draft, existing=None)
            self._validate_network_ssh_port(draft, existing=None)
            if draft.additional_disks:
                raise RpcRemoteError("additional_disks_provisioning_unsupported", "Crie a VM primeiro; discos existentes podem ser anexados depois, na edição da VM.")
            vm = VMConfig(
                id=draft.id,
                name=draft.name,
                macos_version=draft.macos_version,
                vcpus=draft.vcpus,
                cpu_model=draft.cpu_model,
                cpu_topology=cpu_topology,
                ram_gb=draft.ram_gb,
                disk_size_gb=draft.disk_size_gb,
                auto_start=draft.auto_start,
                smbios=self._smbios_factory(draft.macos_version),
                audio_device=draft.audio_device,
                audio_enabled=draft.audio_enabled,
                audio_backend=draft.audio_backend,
                selected_gpu_device_uuid=None,
                selected_gpu_pci_address=None,
                selected_gpu_driver_file=None,
                network_enabled=draft.network_enabled,
                network_mac=draft.network_mac,
                network_ssh_port=draft.network_ssh_port,
                fullscreen=draft.fullscreen,
                display_resolution=draft.display_resolution,
                display_count=display_count,
                display_hidpi=draft.display_hidpi,
                display_refresh_hz=draft.display_refresh_hz,
                opencore_show_picker=draft.opencore_show_picker,
                created_at=datetime.now(timezone.utc).isoformat(),
                power_sync_mode=draft.power_sync_mode,
                usb_auto_attach=usb_auto_attach,
                qemu_extra_args=list(draft.qemu_extra_args),
            )
            self.store.add_vm(vm, expected_revision=expected)
            self._refresh_catalog()
            saved = self.store.get_vm(vm.id)
            assert saved is not None
            return self._config_envelope(saved).model_dump(mode="json")
        except ConcurrentCatalogUpdate as exc:
            raise RpcRemoteError("catalog_conflict", "O catálogo mudou; recarregue e tente novamente.") from exc
        except ValueError as exc:
            raise RpcRemoteError("invalid_vm_config", str(exc)) from exc

    def update_vm_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Update editable fields without replacing server-owned/unknown data."""
        vm_id = self._vm_id(payload)
        try:
            expected = self._expected_revision(payload)
            draft = VmConfigUpdate.model_validate(payload.get("config"))
            self._validate_host_capacity(draft.vcpus, draft.ram_gb)
            self._refresh_catalog()
            vm = self.store.get_vm(vm_id)
            if vm is None:
                raise RpcRemoteError("vm_not_found", "VM não encontrada.")
            self._validate_supported_settings(draft, existing=vm)
            cpu_topology = self._validated_cpu_topology(draft, existing=vm)
            display_count = self._validated_display_count(draft, existing=vm)
            self._validate_cpu_model(draft, existing=vm)
            if vm_id in self._get_running_vms():
                raise RpcRemoteError("vm_running", "A configuração não pode ser alterada enquanto a VM estiver em execução.")
            selected_gpu = self._validated_gpu_selection(draft, existing=vm)
            usb_auto_attach = self._validated_usb_auto_attach(draft.usb_auto_attach, existing=vm)
            self._validate_network_mac(draft, existing=vm)
            self._validate_network_ssh_port(draft, existing=vm)
            additional_disks = self._resolve_additional_disks(draft.additional_disks, existing=vm)
            if vm.disk_path and Path(vm.disk_path).is_file() and draft.disk_size_gb != vm.disk_size_gb:
                raise RpcRemoteError("disk_resize_unsupported", "O disco já provisionado não pode ser redimensionado por este painel.")
            # Preserve settings that the API does not own, including unknown
            # legacy fields carried by VMConfig.from_dict.
            vm.name = draft.name
            vm.macos_version = draft.macos_version
            vm.vcpus = draft.vcpus
            vm.cpu_model = draft.cpu_model
            vm.cpu_topology = cpu_topology
            vm.ram_gb = draft.ram_gb
            vm.disk_size_gb = draft.disk_size_gb
            vm.additional_disks = additional_disks
            vm.auto_start = draft.auto_start
            vm.audio_device = draft.audio_device
            vm.audio_enabled = draft.audio_enabled
            vm.audio_backend = draft.audio_backend
            vm.network_enabled = draft.network_enabled
            vm.network_mac = draft.network_mac
            vm.network_ssh_port = (
                draft.network_ssh_port if "network_ssh_port" in draft.model_fields_set else vm.network_ssh_port
            )
            vm.fullscreen = draft.fullscreen
            vm.display_resolution = draft.display_resolution
            vm.display_count = display_count
            vm.display_hidpi = draft.display_hidpi
            vm.display_refresh_hz = draft.display_refresh_hz
            vm.opencore_show_picker = draft.opencore_show_picker
            vm.power_sync_mode = draft.power_sync_mode
            vm.usb_auto_attach = usb_auto_attach
            vm.qemu_extra_args = list(draft.qemu_extra_args)
            if selected_gpu is not None:
                vm.selected_gpu = selected_gpu["pci_bdf"]
                vm.selected_gpu_name = selected_gpu["name"]
                vm.selected_gpu_uuid = selected_gpu["vulkan_uuid"]
                vm.selected_gpu_device_uuid = selected_gpu.get("reims_device_uuid")
                vm.selected_gpu_pci_address = selected_gpu.get("reims_pci_address")
                vm.selected_gpu_driver_file = selected_gpu.get("driver_file") if selected_gpu.get("reims_device_uuid") else None
            # Reims consumes the restricted token sequence from vm.toml.
            if vm_id in self._get_running_vms():
                raise RpcRemoteError("vm_running", "A configuração não pode ser alterada enquanto a VM estiver em execução.")
            self.store.update_vm(vm, expected_revision=expected)
            self._refresh_catalog()
            saved = self.store.get_vm(vm_id)
            assert saved is not None
            return self._config_envelope(saved).model_dump(mode="json")
        except ConcurrentCatalogUpdate as exc:
            raise RpcRemoteError("catalog_conflict", "O catálogo mudou; recarregue e tente novamente.") from exc
        except ValueError as exc:
            raise RpcRemoteError("invalid_vm_config", str(exc)) from exc

    def patch_vm_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply a small allowlisted set of legacy TUI fields through the catalog owner."""
        vm_id = self._vm_id(payload)
        expected = self._expected_revision(payload)
        patch = payload.get("fields")
        allowed = {"usb_auto_attach", "install_phase", "install_reboots", "install_evidence", "smbios"}
        if not isinstance(patch, dict) or not patch or set(patch) - allowed:
            raise RpcRemoteError("invalid_vm_patch", "A alteração contém campos não permitidos.")
        self._refresh_catalog()
        vm = self.store.get_vm(vm_id)
        if vm is None:
            raise RpcRemoteError("vm_not_found", "VM não encontrada.")
        if vm_id in self._get_running_vms():
            raise RpcRemoteError("vm_running", "A configuração não pode ser alterada enquanto a VM estiver em execução.")

        values: dict[str, Any] = {}
        for field, value in patch.items():
            if field == "usb_auto_attach":
                if (not isinstance(value, list) or len(value) > 32
                        or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", item) for item in value)):
                    raise RpcRemoteError("invalid_vm_patch", "A lista de dispositivos USB é inválida.")
                values[field] = sorted(set(item.lower() for item in value))
            elif field == "install_phase":
                if value not in ("pending", "installing", "installed"):
                    raise RpcRemoteError("invalid_vm_patch", "A fase de instalação é inválida.")
                values[field] = value
            elif field == "install_evidence":
                if value not in ("none", "partial", "complete"):
                    raise RpcRemoteError("invalid_vm_patch", "A evidência de instalação é inválida.")
                values[field] = value
            elif field == "install_reboots":
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise RpcRemoteError("invalid_vm_patch", "A contagem de reinicializações é inválida.")
                values[field] = value
            elif field == "smbios":
                if not isinstance(value, dict):
                    raise RpcRemoteError("invalid_vm_patch", "A identidade SMBIOS é inválida.")
                try:
                    smbios = ApiSmbiosConfig.model_validate(value)
                except Exception as exc:
                    raise RpcRemoteError("invalid_vm_patch", "A identidade SMBIOS é inválida.") from exc
                values[field] = SMBIOSConfig(**smbios.model_dump())

        for field, value in values.items():
            setattr(vm, field, value)
        try:
            self.store.update_vm(vm, expected_revision=expected)
        except ConcurrentCatalogUpdate as exc:
            raise RpcRemoteError("catalog_conflict", "O catálogo mudou; recarregue antes de salvar.") from exc
        except ValueError as exc:
            raise RpcRemoteError("invalid_vm_patch", str(exc)) from exc
        self._refresh_catalog()
        saved = self.store.get_vm(vm_id)
        assert saved is not None
        return self._config_envelope(saved).model_dump(mode="json")

    def delete_vm_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delete a stopped VM through the agent, optionally removing its managed disk."""
        vm_id = self._vm_id(payload)
        expected = self._expected_revision(payload)
        delete_disk = payload.get("delete_disk", False)
        if not isinstance(delete_disk, bool):
            raise RpcRemoteError("invalid_vm_delete", "delete_disk precisa ser booleano.")
        self._refresh_catalog()
        vm = self.store.get_vm(vm_id)
        if vm is None:
            raise RpcRemoteError("vm_not_found", "VM não encontrada.")
        if vm_id in self._get_running_vms():
            raise RpcRemoteError("vm_running", "Pare a VM antes de removê-la do catálogo.")
        if delete_disk and vm.disk_path:
            managed_root = (self.store.config_dir / "vms" / vm_id).resolve()
            disk_path = Path(vm.disk_path).resolve(strict=False)
            if not disk_path.is_relative_to(managed_root) or disk_path == managed_root:
                raise RpcRemoteError("disk_delete_forbidden", "O disco está fora do diretório gerenciado desta VM; ele foi preservado.")
            target_identity = None
            try:
                target_stat = disk_path.stat()
                if stat.S_ISREG(target_stat.st_mode) and not disk_path.is_symlink():
                    target_identity = (target_stat.st_dev, target_stat.st_ino)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RpcRemoteError("disk_reference_check_failed", "Não foi possível verificar referências ao disco; ele foi preservado.") from exc
            for other in self.store.list_vms():
                if other.id == vm_id or not other.disk_path:
                    continue
                other_path = Path(other.disk_path).expanduser().resolve(strict=False)
                if other_path == disk_path:
                    raise RpcRemoteError(
                        "disk_shared",
                        f"O disco também está associado à VM {other.name}; ele foi preservado.",
                    )
                if not other_path.exists():
                    continue
                try:
                    other_identities = self._disk_chain_identities(other_path)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    raise RpcRemoteError(
                        "disk_reference_check_failed",
                        f"Não foi possível verificar as referências da VM {other.name}; nenhum disco foi removido.",
                    ) from exc
                if target_identity is not None and target_identity in other_identities:
                    raise RpcRemoteError(
                        "disk_shared",
                        f"O disco é usado pela cadeia de armazenamento da VM {other.name}; ele foi preservado.",
                    )
        try:
            deleted = self.store.delete_vm(vm_id, delete_disk=delete_disk, expected_revision=expected)
        except ConcurrentCatalogUpdate as exc:
            raise RpcRemoteError("catalog_conflict", "O catálogo mudou; recarregue antes de excluir.") from exc
        except SharedVmDiskError as exc:
            raise RpcRemoteError("disk_shared", str(exc)) from exc
        if not deleted:
            raise RpcRemoteError("vm_not_found", "VM não encontrada.")
        self._refresh_catalog()
        return {"id": vm_id, "deleted": True, "revision": self.store.revision}

    @staticmethod
    def _disk_chain_identities(path: Path) -> set[tuple[int, int]]:
        """Inspect an image and its backing chain with bounded, shell-free qemu-img."""
        result = subprocess.run(
            ["qemu-img", "info", "--backing-chain", "--output=json", "-U", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=True,
            shell=False,
        )
        chain = json.loads(result.stdout)
        if not isinstance(chain, list) or not chain or len(chain) > 64:
            raise ValueError("invalid or excessively long backing chain")
        identities: set[tuple[int, int]] = set()
        for entry in chain:
            if not isinstance(entry, dict) or not isinstance(entry.get("filename"), str):
                raise ValueError("backing chain omitted an image path")
            metadata = Path(entry["filename"]).stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("backing chain contains a non-regular file")
            identities.add((metadata.st_dev, metadata.st_ino))
        return identities

    @staticmethod
    def _expected_revision(payload: dict[str, Any]) -> str:
        revision = payload.get("expected_revision")
        if not isinstance(revision, str) or not revision:
            raise RpcRemoteError("catalog_revision_required", "Envie If-Match com a revisão atual do catálogo.")
        return revision

    @classmethod
    def _validate_supported_settings(cls, draft: Any, *, existing: VMConfig | None = None) -> None:
        from core.downloader import MACOS_PRODUCTS

        if draft.macos_version not in MACOS_PRODUCTS:
            raise RpcRemoteError("invalid_vm_config", "Versão do macOS não suportada pelo provisionador.")
        if existing is not None and draft.opencore_show_picker != existing.opencore_show_picker:
            raise RpcRemoteError("opencore_regeneration_required", "A opção de picker só pode ser alterada durante o provisionamento; regeneração do OpenCore ainda não está disponível.")
        if existing is None and (draft.selected_gpu or draft.selected_gpu_uuid or draft.selected_gpu_device_uuid):
            raise RpcRemoteError("gpu_selection_unavailable", "Seleção de GPU exige validação do inventário durante o provisionamento.")
        cls._validate_audio_device(draft)
        cls._validate_audio_backend(draft, existing=existing)
        cls._validate_audio_enabled(draft, existing=existing)

    @staticmethod
    def _validate_audio_device(draft: Any) -> None:
        if getattr(draft, "audio_device", "usb-audio") != "virtio-sound":
            return
        try:
            from core.runtime_inventory import inspect_runtime

            runtime = inspect_runtime()
        except Exception:
            runtime = {}
        release = runtime.get("reims_release_tag")
        match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9.-]+)?", str(release or ""))
        supported_release = bool(match and tuple(map(int, match.groups()[:3])) >= (1, 4, 0))
        if not (
            runtime.get("status") == "available"
            and supported_release
            and "virtio-sound-pci" in (runtime.get("qemu_devices") or [])
            and {"--audio", "--audio-backend"}.issubset(set(runtime.get("reims_set_options", [])))
        ):
            raise RpcRemoteError(
                "audio_device_unavailable",
                "VirtIO Sound exige a release Reims v1.4.0 ou superior e o dispositivo virtio-sound-pci no QEMU selecionado.",
            )

    @staticmethod
    def _validate_audio_enabled(draft: Any, *, existing: VMConfig | None = None) -> None:
        enabled = getattr(draft, "audio_enabled", True)
        if enabled or (existing is not None and enabled == existing.audio_enabled):
            return
        try:
            from core.runtime_inventory import inspect_runtime
            options = inspect_runtime().get("reims_set_options", [])
        except Exception:
            options = []
        if "--audio" not in options:
            raise RpcRemoteError(
                "audio_control_unavailable",
                "O comando Reims selecionado não confirmou a opção para desabilitar áudio.",
            )

    @staticmethod
    def _validate_audio_backend(draft: Any, *, existing: VMConfig | None = None) -> None:
        backend = getattr(draft, "audio_backend", "auto")
        previous = existing.audio_backend if existing is not None else None
        if backend == "auto" or backend == previous:
            return
        try:
            from core.runtime_inventory import inspect_runtime
            available = inspect_runtime().get("qemu_audio_backends", [])
        except Exception:
            available = []
        if not isinstance(available, list) or backend not in available:
            raise RpcRemoteError(
                "audio_backend_unavailable",
                "Esse backend não foi confirmado pelo QEMU selecionado. Atualize o inventário e escolha uma opção listada.",
            )

    def _validate_network_mac(self, draft: Any, *, existing: VMConfig | None) -> None:
        mac = getattr(draft, "network_mac", None)
        if not mac or (existing is not None and mac == existing.network_mac):
            return
        for vm in self.store.list_vms():
            if existing is not None and vm.id == existing.id:
                continue
            other_mac = vm.network_mac
            if not other_mac:
                projection = self._load_reims_projection(vm)
                network = projection.get("network") if isinstance(projection, dict) else None
                other_mac = network.get("mac") if isinstance(network, dict) else None
            if isinstance(other_mac, str) and other_mac.lower() == mac.lower():
                raise RpcRemoteError("network_mac_conflict", "Esse endereço MAC já está configurado em outra VM.")

    def _validate_network_ssh_port(self, draft: Any, *, existing: VMConfig | None) -> None:
        port = getattr(draft, "network_ssh_port", None)
        if port in (None, 0):
            return
        if not getattr(draft, "network_enabled", True):
            raise RpcRemoteError("ssh_forward_requires_network", "Habilite a rede da VM para encaminhar a porta SSH.")
        try:
            from core.runtime_inventory import inspect_runtime

            runtime = inspect_runtime()
        except Exception:
            runtime = {}
        options = set(runtime.get("reims_set_options", [])) if isinstance(runtime, dict) else set()
        if runtime.get("status") != "available" or "--ssh-port" not in options:
            raise RpcRemoteError("ssh_forward_unavailable", "O runtime Reims selecionado não confirmou encaminhamento SSH local por porta.")
        self._refresh_catalog()
        for candidate in self.store.list_vms():
            if existing is not None and candidate.id == existing.id:
                continue
            other_port = candidate.network_ssh_port
            if other_port is None:
                projection = self._load_reims_projection(candidate)
                network = projection.get("network") if isinstance(projection, dict) else None
                other_port = network.get("ssh_port") if isinstance(network, dict) else None
            if other_port == port:
                raise RpcRemoteError(
                    "ssh_forward_port_conflict",
                    f"A porta local {port} já está configurada para outra VM. Escolha uma porta diferente.",
                )

    def _resolve_additional_disks(self, requested: list[Any], *, existing: VMConfig | None) -> list[Any]:
        """Resolve only unique, existing managed media; never accept a user-supplied path."""
        from core.config import AdditionalDiskConfig

        if existing is not None and existing.additional_disks is None and not requested:
            # The panel never owned this extension for this record. Preserve
            # any newer Reims projection instead of treating omission as an
            # explicit request to erase it.
            return None
        previous = list(existing.additional_disks or []) if existing is not None else []
        requested_ids = [item.media_id for item in requested]
        unchanged = requested_ids == [
            disk.media_id or hashlib.sha256(os.fsencode(str(Path(disk.path).expanduser().resolve(strict=False)))).hexdigest()[:32]
            for disk in previous
        ] and [item.controller for item in requested] == [disk.controller for disk in previous] and [item.order for item in requested] == [disk.order for disk in previous]
        if requested and not unchanged:
            try:
                from core.runtime_inventory import inspect_runtime
                schema_features = set(inspect_runtime().get("reims_schema_features", []))
            except Exception:
                schema_features = set()
            if "additional_disks" not in schema_features:
                raise RpcRemoteError(
                    "additional_disks_unavailable",
                    "O Reims instalado não anuncia o schema estruturado de discos adicionais.",
                )

        root = Path(self.store.config_dir).resolve(strict=True)
        roots = []
        for name in ("vms", "media", "images"):
            candidate = Path(self.store.config_dir) / name
            try:
                if candidate.is_symlink():
                    continue
                resolved_root = candidate.resolve(strict=True)
                if resolved_root.is_dir() and resolved_root.is_relative_to(root):
                    roots.append(resolved_root)
            except (OSError, RuntimeError):
                continue
        paths: dict[str, Path] = {}
        scanned = 0
        for media_root in roots:
            try:
                children = list(media_root.iterdir())
            except OSError:
                continue
            for child in children:
                if scanned >= 4096:
                    break
                scanned += 1
                if child.is_symlink():
                    continue
                try:
                    nested = list(child.iterdir()) if child.is_dir() else [child]
                except OSError:
                    continue
                for path in nested:
                    if scanned >= 4096:
                        break
                    scanned += 1
                    if path.suffix.casefold() not in {".qcow2", ".img", ".raw"} or path.is_symlink():
                        continue
                    try:
                        info = path.lstat()
                        resolved = path.resolve(strict=True)
                        if not stat.S_ISREG(info.st_mode) or not any(resolved.is_relative_to(item) for item in roots):
                            continue
                    except (OSError, RuntimeError):
                        continue
                    media_id = hashlib.sha256(os.fsencode(str(resolved))).hexdigest()[:32]
                    paths[media_id] = resolved

        available = {item["id"]: item for item in self.storage_inventory({}).get("items", []) if isinstance(item, dict)}
        result = []
        for item in requested:
            path = paths.get(item.media_id)
            inventory_item = available.get(item.media_id)
            if (
                path is None or inventory_item is None
                or inventory_item.get("location") != "managed"
                or inventory_item.get("availability") != "available"
            ):
                raise RpcRemoteError("additional_disk_unavailable", "Selecione uma imagem QCOW2/RAW existente e disponível na biblioteca gerenciada.")
            other_references = set(inventory_item.get("references", [])) - ({existing.id} if existing else set())
            if other_references:
                raise RpcRemoteError("additional_disk_in_use", "Essa mídia já está conectada a outra VM; discos graváveis não podem ser compartilhados.")
            if existing is not None and existing.disk_path:
                primary = Path(existing.disk_path).expanduser().resolve(strict=False)
                if path == primary:
                    raise RpcRemoteError("additional_disk_duplicate", "O disco principal da VM não pode ser anexado novamente como disco adicional.")
            result.append(AdditionalDiskConfig(
                path=str(path), format="qcow2" if path.suffix.casefold() == ".qcow2" else "raw",
                controller=item.controller, order=item.order,
            ))
        if not requested and previous and not unchanged:
            try:
                from core.runtime_inventory import inspect_runtime
                schema_features = set(inspect_runtime().get("reims_schema_features", []))
            except Exception:
                schema_features = set()
            if "additional_disks" not in schema_features:
                raise RpcRemoteError("additional_disks_unavailable", "O runtime atual não permite remover discos adicionais com segurança.")
        return result

    @staticmethod
    def _validate_cpu_model(draft: Any, *, existing: VMConfig | None = None) -> None:
        """Only persist CPU models discovered on the selected QEMU binary.

        The measured appliance default is accepted when probes are unavailable;
        unchanged legacy values are preserved without rewriting them.
        """
        if existing is not None and draft.cpu_model == existing.cpu_model:
            return
        if draft.cpu_model == "Skylake-Client-noTSX-IBRS":
            return
        try:
            from core.runtime_inventory import inspect_runtime

            models = inspect_runtime().get("qemu_cpu_models", [])
        except Exception:
            models = []
        if not isinstance(models, list) or draft.cpu_model not in models:
            raise RpcRemoteError(
                "cpu_model_unavailable",
                "O modelo de CPU não foi confirmado pelo QEMU selecionado. Atualize as capacidades e escolha um modelo listado.",
            )

    def _validated_display_count(self, draft: Any, *, existing: VMConfig | None = None) -> int | None:
        """Change display count only when the selected Reims command advertises it."""
        supplied = "display_count" in getattr(draft, "model_fields_set", set())
        if existing is not None and not supplied:
            return existing.display_count
        count = getattr(draft, "display_count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 8:
            raise RpcRemoteError("invalid_display_count", "A quantidade de displays deve ser um número inteiro entre 1 e 8.")

        previous = existing.display_count if existing is not None else None
        if existing is not None and previous is None:
            projection = self._load_reims_projection(existing)
            display = projection.get("display") if isinstance(projection, dict) else None
            projected = display.get("count") if isinstance(display, dict) else None
            if isinstance(projected, int) and not isinstance(projected, bool) and 1 <= projected <= 8:
                previous = projected
            else:
                previous = 1
        if count == previous:
            return existing.display_count if existing is not None else count
        if count == 1 and previous in (None, 1):
            return count

        try:
            from core.runtime_inventory import inspect_runtime

            runtime = inspect_runtime()
        except Exception:
            runtime = {}
        options = runtime.get("reims_set_options", []) if isinstance(runtime, dict) else []
        if runtime.get("status") != "available" or not isinstance(options, list) or "--displays" not in options:
            raise RpcRemoteError(
                "display_count_unavailable",
                "O Reims selecionado não confirma a edição da quantidade de displays; a configuração existente foi preservada.",
            )
        return count

    @staticmethod
    def _validated_cpu_topology(draft: Any, *, existing: VMConfig | None = None):
        """Accept CPU topology only when the selected Reims schema declares it."""
        from core.config import CpuTopologyConfig

        requested = getattr(draft, "cpu_topology", None)
        supplied = "cpu_topology" in getattr(draft, "model_fields_set", set())
        if existing is not None and not supplied:
            if existing.cpu_topology is not None:
                try:
                    existing.cpu_topology.validate(vcpus=draft.vcpus)
                except ValueError as exc:
                    raise RpcRemoteError(
                        "cpu_topology_unavailable",
                        "A topologia salva não pode ser alterada enquanto o runtime não anuncia cpu_topology.",
                    ) from exc
            return existing.cpu_topology

        old = existing.cpu_topology if existing is not None else None
        if requested is None:
            if old is None:
                return None
            # Explicit reset is represented as the current safe default so a
            # later projection can remove the old override instead of treating
            # this as an unowned legacy field.
            requested = CpuTopologyConfig(1, draft.vcpus, 1)
        elif hasattr(requested, "model_dump"):
            requested = CpuTopologyConfig(**requested.model_dump(mode="python"))
        elif isinstance(requested, dict):
            requested = CpuTopologyConfig(**requested)
        if not isinstance(requested, CpuTopologyConfig):
            raise RpcRemoteError("invalid_cpu_topology", "A topologia precisa conter sockets, cores e threads inteiros.")
        try:
            requested.validate(vcpus=draft.vcpus)
        except ValueError as exc:
            raise RpcRemoteError("invalid_cpu_topology", str(exc)) from exc

        def factors(value):
            return None if value is None else (value.sockets, value.cores, value.threads)

        if factors(requested) == factors(old):
            return old
        default = CpuTopologyConfig(1, draft.vcpus, 1)
        if old is None and factors(requested) == factors(default):
            return None
        try:
            from core.runtime_inventory import inspect_runtime

            features = inspect_runtime().get("reims_schema_features", [])
        except Exception:
            features = []
        if not isinstance(features, list) or "cpu_topology" not in features:
            raise RpcRemoteError(
                "cpu_topology_unavailable",
                "O Reims selecionado não anuncia cpu_topology; a topologia existente foi preservada.",
            )
        return requested

    def _validated_gpu_selection(self, draft: Any, *, existing: VMConfig) -> dict[str, str] | None:
        """Resolve an edited BDF only from the current, unambiguous Reims/Vulkan inventory."""
        changed = (
            draft.selected_gpu != existing.selected_gpu
            or draft.selected_gpu_uuid != existing.selected_gpu_uuid
            or draft.selected_gpu_device_uuid != existing.selected_gpu_device_uuid
        )
        if not changed:
            return None
        if not draft.selected_gpu:
            raise RpcRemoteError(
                "gpu_selection_unavailable",
                "A GPU configurada não pode ser removida até o runtime oferecer uma limpeza confirmada da seção [gpu].",
            )

        devices = self.gpu_inventory()
        inventory_error = getattr(self.gpu_inventory, "last_error", None)
        if inventory_error:
            raise RpcRemoteError(
                "gpu_selection_unavailable",
                "O inventário de GPU está desatualizado; atualize a leitura antes de selecionar uma placa.",
            )
        for device in devices:
            item = device.as_dict() if hasattr(device, "as_dict") else device
            if not isinstance(item, dict) or item.get("pci_bdf") != draft.selected_gpu:
                continue
            if not item.get("selection_supported") or not item.get("vulkan_uuid"):
                break
            if item["vulkan_uuid"] != draft.selected_gpu_uuid:
                raise RpcRemoteError(
                    "gpu_selection_unavailable",
                    "O UUID enviado não corresponde ao inventário atual da GPU.",
                )
            try:
                from core.runtime_inventory import inspect_runtime

                schema_features = set(inspect_runtime().get("reims_schema_features", []))
            except Exception:
                schema_features = set()
            if "gpu_identity" in schema_features:
                if not item.get("reims_device_uuid") or not item.get("reims_pci_address"):
                    break
                if draft.selected_gpu_device_uuid != item["reims_device_uuid"]:
                    raise RpcRemoteError(
                        "gpu_selection_unavailable",
                        "O UUID Reims enviado não corresponde à identidade atual da placa.",
                    )
            elif draft.selected_gpu_device_uuid is not None:
                raise RpcRemoteError(
                    "gpu_selection_unavailable",
                    "O runtime Reims não anuncia seleção por identidade estruturada.",
                )
            return {
                "pci_bdf": str(item["pci_bdf"]),
                "name": str(item.get("name") or item["pci_bdf"]),
                "vulkan_uuid": str(item["vulkan_uuid"]),
                "reims_device_uuid": item.get("reims_device_uuid"),
                "reims_pci_address": item.get("reims_pci_address"),
                "driver_file": item.get("driver_file"),
            }
        raise RpcRemoteError(
            "gpu_selection_unavailable",
            "A GPU selecionada não possui mapeamento único confirmado pelo inventário atual do Reims.",
        )

    @staticmethod
    def _validate_host_capacity(vcpus: int, ram_gb: int) -> None:
        logical_cpus = max(1, psutil.cpu_count(logical=True) or 1)
        total_ram_gb = max(1, int(psutil.virtual_memory().total / 1024**3))
        if vcpus > logical_cpus * 2:
            raise RpcRemoteError("invalid_vm_config", f"vCPUs excedem o limite do appliance ({logical_cpus * 2}).")
        if ram_gb > total_ram_gb:
            raise RpcRemoteError("invalid_vm_config", f"RAM excede a memória física do appliance ({total_ram_gb} GB).")

    def get_vm_runtime(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        vm_id = self._vm_id(payload)
        self._refresh_catalog()
        vm = self.store.get_vm(vm_id)
        if vm is None:
            return None
        running, problem = self._read_running_vms()
        unit_status, unit_problem = self._read_vm_unit(vm_id)
        try:
            from core.reims_ctl import vm_gpu_identity_snapshot

            gpu_snapshot = vm_gpu_identity_snapshot()
        except Exception:
            gpu_snapshot = None
        return self._runtime_for_vm(vm, running, problem, unit_status, unit_problem, gpu_snapshot)

    def get_vm_serial_console(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Read only the bounded Reims serial log for a catalogued VM.

        Reims owns this file and routes QEMU's `-serial` output there when
        `[logs].enabled` is true. Never query QMP/HMP or accept a caller path.
        """
        vm_id = self._vm_id(payload)
        self._refresh_catalog()
        vm = self.store.get_vm(vm_id)
        if vm is None:
            return None

        from core import reims_ctl

        def unavailable(message: str) -> dict[str, Any]:
            return VmSerialConsole(
                vm_id=vm_id, available=False, content="", message=message,
            ).model_dump(mode="json")

        directory = reims_ctl.vm_dir(vm)
        try:
            dir_stat = directory.lstat()
            if not stat.S_ISDIR(dir_stat.st_mode) or directory.resolve(strict=True) != Path(os.path.abspath(directory)):
                return unavailable("Diretório registrado da VM indisponível ou inseguro.")
            config_path = directory / "vm.toml"
            if config_path.is_symlink() or not config_path.is_file():
                return unavailable("A configuração Reims não está disponível para esta VM.")
            with config_path.open("rb") as handle:
                projection = tomllib.load(handle)
            logs = projection.get("logs")
            if not isinstance(logs, dict) or logs.get("enabled") is not True:
                return unavailable("A captura serial do Reims não está habilitada nesta configuração.")

            logs_fd = os.open(
                directory / "logs",
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                serial_fd = os.open(
                    "serial.log",
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=logs_fd,
                )
            finally:
                os.close(logs_fd)
            try:
                info = os.fstat(serial_fd)
                if not stat.S_ISREG(info.st_mode):
                    return unavailable("O arquivo serial do Reims não é um arquivo regular.")
                limit = 64 * 1024
                offset = max(0, info.st_size - limit)
                content = os.pread(serial_fd, limit, offset).decode("utf-8", errors="replace")
                updated_at = datetime.fromtimestamp(info.st_mtime, timezone.utc)
            finally:
                os.close(serial_fd)
        except FileNotFoundError:
            return unavailable("Ainda não há saída serial para esta VM; o arquivo aparece após uma inicialização com logs habilitados.")
        except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError):
            return unavailable("Não foi possível ler com segurança a saída serial desta VM.")

        # Serial is guest-controlled text. Remove terminal control sequences
        # before presenting it in the browser and redact common credentials as
        # well as the host's stored SMBIOS identifiers.
        content = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", content)
        content = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", content)
        content = "".join(char for char in content if char in "\n\r\t" or (char.isprintable() and ord(char) != 0x7f))
        content = re.sub(r"(?i)\b(password|token|secret|authorization|api[_-]?key)\b\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", content)
        content = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [redacted]", content)
        for private_value in (vm.smbios.serial_number, vm.smbios.board_serial, vm.smbios.smuuid, vm.smbios.rom):
            if isinstance(private_value, str) and len(private_value) >= 4:
                content = content.replace(private_value, "[SMBIOS redacted]")
        return VmSerialConsole(
            vm_id=vm_id,
            available=True,
            content=content[-(64 * 1024):],
            updated_at=updated_at,
            truncated=offset > 0,
            message=None,
        ).model_dump(mode="json")

    def list_vm_runtimes(self, _payload: dict[str, Any]) -> dict[str, Any]:
        """Return one consistent runtime snapshot for the VM catalog."""
        self._refresh_catalog()
        running, problem = self._read_running_vms()
        try:
            from core.reims_ctl import vm_gpu_identity_snapshot

            gpu_snapshot = vm_gpu_identity_snapshot()
        except Exception:
            gpu_snapshot = None
        items = []
        for vm in self.store.list_vms():
            unit_status, unit_problem = self._read_vm_unit(vm.id)
            items.append(self._runtime_for_vm(vm, running, problem, unit_status, unit_problem, gpu_snapshot))
        return {"items": items, "observed_at": datetime.now(timezone.utc).isoformat()}

    def _read_vm_unit(self, vm_id: str) -> tuple[Any | None, str | None]:
        if self.vm_unit_manager is None:
            return None, None
        try:
            return self.vm_unit_manager.query(vm_id), None
        except Exception as exc:
            # systemd status is an additional signal. It never masks the
            # independent Reims/QMP evidence, but it must prevent a false
            # STOPPED claim when the process inventory is also unavailable.
            return None, f"Não foi possível confirmar a unidade de VM ({type(exc).__name__})."

    def _runtime_for_vm(
        self, vm: VMConfig, running: dict[str, int] | None,
        problem: str | None = None, unit_status: Any | None = None,
        unit_problem: str | None = None, gpu_snapshot: dict[str, dict] | None = None,
    ) -> dict[str, Any]:
        vm_id = vm.id
        pid = running.get(vm_id) if running is not None else None
        observed = datetime.now(timezone.utc)
        qmp_available = False
        state = VmRunState.UNKNOWN if problem or unit_problem else VmRunState.STOPPED
        started_at = None
        message = problem or unit_problem
        supervisor = None
        if unit_status is not None:
            supervisor_started_at = None
            if unit_status.process_create_time is not None:
                supervisor_started_at = datetime.fromtimestamp(unit_status.process_create_time, timezone.utc)
            supervisor = {
                "unit": unit_status.unit,
                "load_state": unit_status.load_state,
                "active_state": unit_status.active_state,
                "sub_state": unit_status.sub_state,
                "main_pid": unit_status.main_pid,
                "main_process_started_at": supervisor_started_at,
                "result": unit_status.result,
            }
            if pid is None and running is not None:
                if unit_status.active_state in {"activating", "active", "reloading"}:
                    state = VmRunState.STARTING
                    message = "O supervisor da VM está ativo; aguardando a confirmação do processo QEMU."
                elif unit_status.active_state == "deactivating":
                    state = VmRunState.STOPPING
                    message = "O supervisor da VM está encerrando."
                elif unit_status.active_state == "failed":
                    state = VmRunState.DEGRADED
                    message = "A unidade do supervisor falhou; confira os detalhes do serviço no host."
        if pid:
            state = VmRunState.RUNNING
            try:
                started_at = datetime.fromtimestamp(psutil.Process(pid).create_time(), timezone.utc)
            except (psutil.Error, OSError, ValueError):
                pass
            qmp = self._open_qmp(vm)
            if qmp is not None:
                try:
                    status = qmp.command("query-status")
                    qmp_available = isinstance(status, dict)
                    if qmp_available and status.get("running") is False:
                        state = VmRunState.STOPPING
                except Exception:
                    message = "QEMU está em execução; o monitor QMP não respondeu."
                finally:
                    qmp.close()
            else:
                message = "QEMU está em execução; o controle QMP ainda não está disponível."
            if unit_status is not None and unit_status.active_state in {"inactive", "failed"}:
                # Process identity is the primary fact about guest execution;
                # a stale/missing systemd unit must not relabel a live QEMU as
                # stopped (or hide it from the catalog). The supervisor field
                # still exposes the ownership problem separately.
                state = VmRunState.RUNNING
                message = "QEMU está em execução, mas a unidade de supervisão não está ativa."
        gpu_assignment = None
        if gpu_snapshot is not None:
            entry = gpu_snapshot.get(vm.id)
            reported = entry if isinstance(entry, dict) else None
            configured = reported.get("configured") if reported else None
            effective = reported.get("effective") if reported else None
            configured_device_uuid = vm.selected_gpu_device_uuid
            configured_pci = vm.selected_gpu_pci_address or vm.selected_gpu
            configured_driver = vm.selected_gpu_driver_file
            configured_mismatch = False
            if isinstance(configured, dict):
                reported_identity = (
                    configured.get("device_uuid"), configured.get("pci_address"), configured.get("driver_file"),
                )
                saved_identity = (configured_device_uuid, configured_pci, configured_driver)
                if all(value is None for value in saved_identity):
                    configured_device_uuid, configured_pci, configured_driver = reported_identity
                elif any(value is not None for value in saved_identity) and saved_identity != reported_identity:
                    configured_mismatch = True
            verification = "unavailable"
            message = "O Reims não informou uma GPU efetiva para esta VM."
            effective_bdf = None
            effective_uuid = None
            if configured_mismatch:
                verification = "mismatch"
                message = "A identidade configurada no catálogo diverge do Reims; a atribuição efetiva foi ocultada."
            elif isinstance(effective, dict):
                effective_bdf = effective.get("pci_address")
                effective_uuid = effective.get("device_uuid")
                if configured_device_uuid and (
                    configured_device_uuid != effective_uuid or configured_pci != effective_bdf
                ):
                    verification = "mismatch"
                    message = "A GPU efetiva diverge da identidade configurada; a atribuição foi ocultada."
                    effective_bdf = None
                    effective_uuid = None
                else:
                    verification = "confirmed"
                    message = None
            gpu_assignment = {
                "configured_bdf": configured_pci,
                "configured_uuid": vm.selected_gpu_uuid,
                "configured_device_uuid": configured_device_uuid,
                "configured_pci_address": configured_pci,
                "configured_driver_file": configured_driver,
                "effective_bdf": effective_bdf,
                "effective_uuid": effective_uuid,
                "verification": verification,
                "message": message,
            }
        return VmRuntime(
            id=vm_id,
            state=state,
            pid=pid,
            process_started_at=started_at,
            qmp_available=qmp_available,
            install_phase=InstallPhase(vm.install_phase),
            supervisor=supervisor,
            gpu=gpu_assignment,
            observed_at=observed,
            message=message,
        ).model_dump(mode="json")

    def get_vm_metrics(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        vm_id = self._vm_id(payload)
        self._refresh_catalog()
        vm = self.store.get_vm(vm_id)
        if vm is None:
            return None
        pid = self._get_running_vms().get(vm_id)
        if not pid:
            return {"vm_id": vm_id, "samples": [], "message": "A VM está parada."}
        identity = VMProcessIdentity(vm_id, pid, max(1, vm.vcpus))
        samples = list(self.telemetry.sample_vm(identity))
        qmp = self._open_qmp(vm)
        if qmp is not None:
            try:
                samples.extend(self.telemetry.sample_qmp(vm_id, qmp))
            finally:
                qmp.close()
        return {"vm_id": vm_id, "samples": [_sample_dict(sample) for sample in samples]}

    def get_vm_metrics_history(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        vm_id = self._vm_id(payload)
        self._refresh_catalog()
        if self.store.get_vm(vm_id) is None:
            return None
        result = self._metrics_history(payload, {"vm_id": vm_id})
        result["vm_id"] = vm_id
        return result

    def vm_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Queue explicit VM lifecycle actions through the managed runtime."""
        vm_id = self._vm_id(payload)
        if self.job_runner is None or self.job_store is None:
            raise RpcRemoteError("capability_unavailable", "O serviço de tarefas de VM não está configurado.")
        try:
            action = VmActionRequest.model_validate(payload.get("action"))
        except Exception as exc:
            raise RpcRemoteError("invalid_vm_action", "A ação de VM é inválida.") from exc
        self._refresh_catalog()
        vm = self.store.get_vm(vm_id)
        if vm is None:
            raise RpcRemoteError("vm_not_found", "VM não encontrada.")
        if action.expected_revision is not None and action.expected_revision != self.store.revision:
            raise RpcRemoteError("catalog_conflict", "O catálogo mudou; recarregue antes de controlar a VM.")
        if action.operation in {Operation.START, Operation.RESTART, Operation.FORCE_STOP} and self.vm_unit_manager is None:
            raise RpcRemoteError("capability_unavailable", "O supervisor por VM não está ativo neste agente.")
        if action.operation in {Operation.PAUSE, Operation.RESUME}:
            raise RpcRemoteError("capability_unavailable", "Pausar e retomar ainda não são compatíveis com o runtime Reims.")
        running = self._get_running_vms()
        if action.operation in {Operation.SHUTDOWN, Operation.FORCE_STOP, Operation.RESTART} and vm_id not in running:
            raise RpcRemoteError("vm_not_running", "A VM não está em execução.")
        if action.operation is Operation.START:
            unit_status, unit_problem = self._read_vm_unit(vm_id)
            if vm_id in running or (unit_status is not None and unit_status.active_state in {"active", "activating", "deactivating"}):
                raise RpcRemoteError("vm_running", "A VM já está em execução ou iniciando.")
            if unit_problem:
                raise RpcRemoteError("runtime_state_unavailable", unit_problem)
        if action.operation is Operation.FORCE_STOP and action.confirmation != vm_id:
            raise RpcRemoteError("confirmation_required", "Confirme a parada informando o identificador exato da VM.")
        kind = {
            Operation.SHUTDOWN: "vm.shutdown",
            Operation.START: "vm.start",
            Operation.FORCE_STOP: "vm.force_stop",
            Operation.RESTART: "vm.restart",
        }.get(action.operation)
        if kind is None:
            raise RpcRemoteError("capability_unavailable", "A ação solicitada ainda não está disponível.")
        try:
            job, _created = self.job_runner.submit(
                idempotency_key=action.idempotency_key,
                kind=kind,
                payload={"vm_id": vm_id, "operation": action.operation.value},
                target_vm_id=vm_id,
                vm_revision=self.store.revision,
            )
        except ValueError as exc:
            raise RpcRemoteError("invalid_vm_action", "A chave de idempotência já foi usada com outros dados.") from exc
        except Exception as exc:
            from core.web_job_runner import QueueFullError
            if isinstance(exc, QueueFullError):
                raise RpcRemoteError("job_queue_full", "A fila de tarefas está cheia; tente novamente mais tarde.") from exc
            raise
        return JobAccepted(job_id=job["id"], events_url=f"/api/v1/jobs/{job['id']}").model_dump(mode="json")

    def _run_vm_start_job(self, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        vm_id = self._vm_id(payload)
        if self.vm_unit_manager is None:
            raise RuntimeError("O supervisor por VM não está ativo.")
        context.phase("starting_vm")
        context.checkpoint("vm_start_prepared", {"vm_id": vm_id}, completed=True)
        self.vm_unit_manager.start(vm_id)
        return self._wait_for_vm_qemu(vm_id)

    def _run_vm_force_stop_job(self, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        """Request QEMU quit over QMP; never signal a process or stop its unit."""
        vm_id = self._vm_id(payload)
        self._refresh_catalog()
        vm = self.store.get_vm(vm_id)
        if vm is None:
            raise RuntimeError("VM não encontrada antes da parada forçada.")
        pid = self._get_running_vms().get(vm_id)
        if pid is None:
            return {"vm_id": vm_id, "state": "stopped", "message": "A VM já estava parada."}
        try:
            started_at = psutil.Process(pid).create_time()
        except psutil.Error as exc:
            raise RuntimeError("Não foi possível confirmar a identidade do processo da VM.") from exc
        qmp = self._open_qmp(vm)
        if qmp is None:
            raise RuntimeError("O monitor QMP não respondeu; a VM continua em execução.")
        try:
            status = qmp.command("query-status")
            if not isinstance(status, dict) or status.get("running") is not True:
                raise RuntimeError("O QMP não confirmou que a VM está rodando; nenhum comando foi enviado.")
            context.checkpoint("force_stop_prepared", {"pid": pid}, completed=True)
            context.phase("force_stop_requested")
            qmp.command("quit")
        finally:
            qmp.close()

        deadline = time.monotonic() + 30
        last_checkpoint = time.monotonic()
        while time.monotonic() < deadline:
            context.check_cancelled()
            current = self._get_running_vms().get(vm_id)
            if current is None:
                return {"vm_id": vm_id, "state": "stopped", "message": "O QEMU encerrou após o comando QMP quit."}
            if current != pid:
                raise RuntimeError("O processo da VM mudou durante a parada; confirme o estado antes de tentar novamente.")
            try:
                current_started_at = psutil.Process(current).create_time()
            except psutil.NoSuchProcess:
                return {"vm_id": vm_id, "state": "stopped", "message": "O processo QEMU encerrou."}
            except psutil.Error as exc:
                raise RuntimeError("Não foi possível confirmar o processo da VM durante a parada.") from exc
            if current_started_at != started_at:
                raise RuntimeError("O identificador do processo foi reutilizado; confirme o estado da VM.")
            now = time.monotonic()
            if now - last_checkpoint >= 5:
                context.checkpoint("waiting_for_qemu_exit", {"pid": pid})
                last_checkpoint = now
            time.sleep(0.25)
        raise TimeoutError("O QEMU não encerrou em 30 segundos; nenhum sinal de processo foi enviado.")

    def _run_vm_restart_job(self, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        """Restart only after a confirmed graceful ACPI shutdown."""
        vm_id = self._vm_id(payload)
        if self.vm_unit_manager is None:
            raise RuntimeError("O supervisor por VM não está ativo.")
        context.phase("shutting_down_vm")
        stopped = self._run_vm_shutdown_job(payload, context)
        if stopped.get("state") != "stopped":
            raise RuntimeError("O desligamento ACPI não confirmou a parada; a VM não será reiniciada.")
        context.check_cancelled()
        context.phase("restarting_vm")
        self.vm_unit_manager.start(vm_id)
        return self._wait_for_vm_qemu(vm_id)

    def _wait_for_vm_qemu(self, vm_id: str) -> dict[str, Any]:
        """Wait for QEMU and its supervisor after systemd starts a Type=simple unit."""
        deadline = time.monotonic() + VM_START_READY_TIMEOUT_SECONDS
        while True:
            running = self._get_running_vms()
            try:
                status = self.vm_unit_manager.query(vm_id)
            except Exception as exc:
                if vm_id in running:
                    raise RuntimeError(
                        "O QEMU foi localizado, mas não foi possível confirmar a unidade de supervisão; "
                        "não tente iniciar outra instância até conferir o estado da VM."
                    ) from exc
                raise RuntimeError("Não foi possível confirmar o supervisor durante a inicialização da VM.") from exc

            if vm_id in running:
                if status.active_state in {"active", "activating", "reloading"}:
                    return {"vm_id": vm_id, "state": "running"}
                raise RuntimeError(
                    "O processo QEMU está ativo, mas a unidade de supervisão não está saudável; "
                    "não tente iniciar outra instância até confirmar a parada."
                )

            if status.active_state == "failed":
                suffix = f" (resultado systemd: {status.result})" if status.result else ""
                diagnosis = None
                read_diagnosis = getattr(self.vm_unit_manager, "admission_failure_message", None)
                if callable(read_diagnosis):
                    try:
                        diagnosis = read_diagnosis(vm_id)
                    except Exception:
                        diagnosis = None
                if diagnosis:
                    raise RuntimeError(
                        diagnosis + " A unidade falhou antes de confirmar QEMU; a tarefa não será marcada como iniciada."
                    )
                raise RuntimeError(
                    "O supervisor falhou antes de confirmar o processo QEMU" + suffix + ". "
                    "A tarefa não será marcada como iniciada."
                )
            if status.active_state in {"inactive", "deactivating"}:
                raise RuntimeError(
                    "O supervisor encerrou antes de confirmar o processo QEMU; "
                    "a tarefa não será marcada como iniciada."
                )

            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError(
                    f"O supervisor permaneceu ativo por {VM_START_READY_TIMEOUT_SECONDS} segundos "
                    "sem confirmar o processo QEMU."
                )
            time.sleep(0.25)

    def _run_vm_shutdown_job(self, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        """Send ACPI shutdown over the managed QMP client and never force-stop."""
        vm_id = self._vm_id(payload)
        self._refresh_catalog()
        vm = self.store.get_vm(vm_id)
        if vm is None:
            raise RuntimeError("VM não encontrada antes do desligamento.")
        running = self._get_running_vms()
        pid = running.get(vm_id)
        if pid is None:
            return {"vm_id": vm_id, "state": "stopped", "message": "A VM já estava parada."}
        try:
            started_at = psutil.Process(pid).create_time()
        except psutil.NoSuchProcess:
            refreshed = self._get_running_vms()
            if vm_id not in refreshed:
                return {"vm_id": vm_id, "state": "stopped", "message": "A VM já estava parada."}
            raise RuntimeError("Não foi possível confirmar a identidade do processo da VM.")
        except psutil.Error as exc:
            raise RuntimeError("Não foi possível confirmar a identidade do processo da VM.") from exc

        context.phase("validating_qmp")
        qmp = self._open_qmp(vm)
        if qmp is None:
            raise RuntimeError("O monitor QMP não respondeu; a VM continua em execução.")
        try:
            status = qmp.command("query-status")
            if not isinstance(status, dict) or status.get("running") is not True:
                raise RuntimeError("O QMP não confirmou que a VM está rodando; nenhum desligamento foi enviado.")
            context.checkpoint("shutdown_request_prepared", {"pid": pid}, completed=True)
            qmp.command("system_powerdown")
            context.checkpoint("acpi_shutdown_sent", {"pid": pid}, completed=True)
        finally:
            qmp.close()

        shutdown_requested_at = time.monotonic()
        deadline = shutdown_requested_at + VM_SHUTDOWN_TIMEOUT_SECONDS
        last_wait_checkpoint = shutdown_requested_at
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now - last_wait_checkpoint >= 15:
                context.checkpoint(
                    "shutdown_wait",
                    {"pid": pid, "elapsed_seconds": int(now - shutdown_requested_at)},
                )
                last_wait_checkpoint = now
            running, problem = self._read_running_vms()
            if running is None:
                raise RuntimeError(problem or "Não foi possível confirmar o estado da VM após o pedido de desligamento.")
            current_pid = running.get(vm_id)
            if current_pid is None:
                return {"vm_id": vm_id, "state": "stopped", "message": "O macOS desligou após o pedido ACPI."}
            if current_pid != pid:
                raise RuntimeError("O processo da VM mudou durante o desligamento; confirme o estado antes de tentar novamente.")
            try:
                current_started_at = psutil.Process(current_pid).create_time()
            except psutil.NoSuchProcess:
                return {"vm_id": vm_id, "state": "stopped", "message": "O processo da VM encerrou após o pedido ACPI."}
            except psutil.Error as exc:
                raise RuntimeError("Não foi possível confirmar o processo da VM durante o desligamento.") from exc
            if current_started_at != started_at:
                raise RuntimeError("O identificador do processo foi reutilizado; o estado da VM requer nova verificação.")
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        raise TimeoutError(
            f"O pedido ACPI expirou após {VM_SHUTDOWN_TIMEOUT_SECONDS} segundos; nenhuma parada forçada foi executada."
        )

    def start_jobs(self) -> None:
        if self.job_runner is not None:
            self.job_runner.start()
        if self.media_job_runner is not None:
            self.media_job_runner.start()

    def close_jobs(self) -> None:
        if self.media_job_runner is not None:
            self.media_job_runner.close()
        if self.job_runner is not None:
            self.job_runner.close()

    def submit_provision_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.job_runner is None:
            raise RpcRemoteError("capability_unavailable", "O provisionador assíncrono não está configurado.")
        try:
            request = VmProvisionRequest.model_validate(payload)
            cpu_topology = self._validated_cpu_topology(request)
            display_count = self._validated_display_count(request)
            self._validate_cpu_model(request)
            self._validate_audio_device(request)
            self._validate_audio_backend(request)
            self._validate_audio_enabled(request)
            self._validate_network_mac(request, existing=None)
            self._validate_network_ssh_port(request, existing=None)
            body = request.model_dump(mode="json", exclude={"idempotency_key"})
            body["cpu_topology"] = None if cpu_topology is None else {
                "sockets": cpu_topology.sockets,
                "cores": cpu_topology.cores,
                "threads": cpu_topology.threads,
            }
            body["display_count"] = display_count
            body["usb_auto_attach"] = self._validated_usb_auto_attach(request.usb_auto_attach)
            selected_gpu = None
            selected_gpu_name = None
            selected_gpu_uuid = None
            selected_gpu_device_uuid = None
            selected_gpu_pci_address = None
            selected_gpu_driver_file = None
            if request.selected_gpu_bdf is not None:
                devices = self.gpu_inventory()
                inventory_error = getattr(self.gpu_inventory, "last_error", None)
                if inventory_error:
                    raise RpcRemoteError(
                        "gpu_selection_unavailable",
                        "O inventário de GPU está desatualizado; atualize a leitura antes de selecionar uma placa.",
                    )
                item = next((device for device in devices if getattr(device, "pci_bdf", None) == request.selected_gpu_bdf), None)
                item_data = None
                if item is None and isinstance(devices, list):
                    item_data = next((device for device in devices if isinstance(device, dict) and device.get("pci_bdf") == request.selected_gpu_bdf), None)
                else:
                    item_data = item.as_dict() if item is not None and hasattr(item, "as_dict") else item
                if not isinstance(item_data, dict) or not item_data.get("selection_supported") or not item_data.get("vulkan_uuid"):
                    raise RpcRemoteError("gpu_selection_unavailable", "A GPU selecionada não possui mapeamento único confirmado pelo Reims.")
                if request.selected_gpu_uuid != item_data["vulkan_uuid"]:
                    raise RpcRemoteError("gpu_selection_unavailable", "O UUID enviado não corresponde ao inventário atual da GPU.")
                try:
                    from core.runtime_inventory import inspect_runtime

                    schema_features = set(inspect_runtime().get("reims_schema_features", []))
                except Exception:
                    schema_features = set()
                if "gpu_identity" in schema_features:
                    if not item_data.get("reims_device_uuid") or not item_data.get("reims_pci_address"):
                        raise RpcRemoteError("gpu_selection_unavailable", "O Reims não confirmou a identidade exata desta GPU.")
                    if request.selected_gpu_device_uuid != item_data["reims_device_uuid"]:
                        raise RpcRemoteError("gpu_selection_unavailable", "O UUID Reims enviado não corresponde ao inventário atual da GPU.")
                    selected_gpu_device_uuid = item_data["reims_device_uuid"]
                    selected_gpu_pci_address = item_data["reims_pci_address"]
                    selected_gpu_driver_file = item_data.get("driver_file")
                elif request.selected_gpu_device_uuid is not None:
                    raise RpcRemoteError("gpu_selection_unavailable", "O runtime Reims não anuncia seleção por identidade estruturada.")
                selected_gpu = item_data["pci_bdf"]
                selected_gpu_name = item_data["name"]
                selected_gpu_uuid = item_data["vulkan_uuid"]
            elif request.selected_gpu_uuid is not None or request.selected_gpu_device_uuid is not None:
                raise RpcRemoteError("gpu_selection_unavailable", "Envie uma placa selecionada para usar o UUID.")
            body.pop("id", None)
            body.pop("selected_gpu_bdf", None)
            body.pop("selected_gpu_uuid", None)
            body.pop("selected_gpu_device_uuid", None)
            body.update({
                "vm_id": request.id,
                "selected_gpu": selected_gpu,
                "selected_gpu_name": selected_gpu_name,
                "selected_gpu_uuid": selected_gpu_uuid,
                "selected_gpu_device_uuid": selected_gpu_device_uuid,
                "selected_gpu_pci_address": selected_gpu_pci_address,
                "selected_gpu_driver_file": selected_gpu_driver_file,
            })
            job, _created = self.job_runner.submit(
                idempotency_key=request.idempotency_key,
                kind="vm.provision",
                payload=body,
                target_vm_id=request.id,
            )
            return JobAccepted(job_id=job["id"], events_url=f"/api/v1/jobs/{job['id']}").model_dump(mode="json")
        except ValueError as exc:
            raise RpcRemoteError("invalid_vm_config", str(exc)) from exc
        except Exception as exc:
            from core.web_job_runner import QueueFullError

            if isinstance(exc, QueueFullError):
                raise RpcRemoteError("job_queue_full", "A fila de provisionamento está cheia; tente novamente mais tarde.") from exc
            raise

    def submit_media_import_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.media_job_runner is None:
            raise RpcRemoteError("capability_unavailable", "O serviço de tarefas não está configurado.")
        transfer_id = payload.get("transfer_id")
        if not isinstance(transfer_id, str) or not re.fullmatch(r"[0-9a-f]{32}", transfer_id):
            raise RpcRemoteError("invalid_transfer", "Identificador da transferência inválido.")
        try:
            job, _created = self.media_job_runner.submit(
                idempotency_key=f"media-import:{transfer_id}",
                kind="media.import",
                payload={"transfer_id": transfer_id},
            )
            return JobAccepted(job_id=job["id"], events_url=f"/api/v1/jobs/{job['id']}").model_dump(mode="json")
        except Exception as exc:
            from core.web_job_runner import QueueFullError

            if isinstance(exc, QueueFullError):
                raise RpcRemoteError("job_queue_full", "A fila de tarefas está cheia; tente novamente mais tarde.") from exc
            raise

    def submit_media_convert_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.media_job_runner is None:
            raise RpcRemoteError("capability_unavailable", "O serviço de tarefas não está configurado.")
        media_id = payload.get("media_id")
        output_format = payload.get("output_format")
        request_key = payload.get("idempotency_key")
        if not isinstance(media_id, str) or not re.fullmatch(r"[0-9a-f]{32}", media_id):
            raise RpcRemoteError("invalid_media", "Identificador da mídia inválido.")
        if not isinstance(output_format, str) or output_format not in {"raw", "qcow2"}:
            raise RpcRemoteError("invalid_media_format", "Formato de saída inválido.")
        if not isinstance(request_key, str) or not re.fullmatch(r"[0-9a-f-]{36}", request_key):
            raise RpcRemoteError("invalid_idempotency_key", "Chave da operação inválida.")
        try:
            job, _created = self.media_job_runner.submit(
                idempotency_key=f"media-convert:{media_id}:{request_key}",
                kind="media.convert",
                payload={"media_id": media_id, "output_format": output_format},
            )
            return JobAccepted(job_id=job["id"], events_url=f"/api/v1/jobs/{job['id']}").model_dump(mode="json")
        except Exception as exc:
            from core.web_job_runner import QueueFullError

            if isinstance(exc, QueueFullError):
                raise RpcRemoteError("job_queue_full", "A fila de tarefas está cheia; tente novamente mais tarde.") from exc
            raise

    def submit_apple_recovery_download_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.media_job_runner is None:
            raise RpcRemoteError("capability_unavailable", "O serviço de tarefas não está configurado.")
        version = payload.get("macos_version")
        request_key = payload.get("idempotency_key")
        if not isinstance(version, str) or version not in {"tahoe", "sequoia", "ventura"}:
            raise RpcRemoteError("invalid_macos_version", "Versão do macOS não suportada pelo downloader.")
        if not isinstance(request_key, str) or not re.fullmatch(r"[0-9a-f-]{36}", request_key):
            raise RpcRemoteError("invalid_idempotency_key", "Chave da operação inválida.")
        try:
            job, _created = self.media_job_runner.submit(
                idempotency_key=f"apple-recovery:{version}:{request_key}",
                kind="media.apple.download",
                payload={"macos_version": version},
            )
            return JobAccepted(job_id=job["id"], events_url=f"/api/v1/jobs/{job['id']}").model_dump(mode="json")
        except Exception as exc:
            from core.web_job_runner import QueueFullError

            if isinstance(exc, QueueFullError):
                raise RpcRemoteError("job_queue_full", "A fila de tarefas está cheia; tente novamente mais tarde.") from exc
            raise

    def submit_vm_backup_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.job_runner is None:
            raise RpcRemoteError("capability_unavailable", "O serviço de tarefas não está configurado.")
        vm_id = payload.get("vm_id")
        idempotency_key = payload.get("idempotency_key")
        if not isinstance(vm_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", vm_id):
            raise RpcRemoteError("invalid_vm_id", "Identificador da VM inválido.")
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[0-9a-f-]{36}", idempotency_key):
            raise RpcRemoteError("invalid_idempotency_key", "Chave da operação inválida.")
        try:
            job, _created = self.job_runner.submit(
                idempotency_key=f"vm-backup:{vm_id}:{idempotency_key}",
                kind="vm.backup",
                payload={"vm_id": vm_id},
                target_vm_id=vm_id,
            )
            return JobAccepted(job_id=job["id"], events_url=f"/api/v1/jobs/{job['id']}").model_dump(mode="json")
        except Exception as exc:
            from core.web_job_runner import QueueFullError

            if isinstance(exc, QueueFullError):
                raise RpcRemoteError("job_queue_full", "A fila de tarefas está cheia; tente novamente mais tarde.") from exc
            raise

    def submit_vm_opencore_picker(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Queue one explicit, versioned OpenCore regeneration for a stopped managed VM."""
        if self.job_runner is None:
            raise RpcRemoteError("capability_unavailable", "O serviço de tarefas não está configurado.")
        vm_id = payload.get("vm_id")
        desired = payload.get("boot_selection")
        confirmation = payload.get("confirmation")
        idempotency_key = payload.get("idempotency_key")
        try:
            expected = self._expected_revision(payload).strip('"')
        except RpcRemoteError:
            raise
        if not isinstance(vm_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", vm_id):
            raise RpcRemoteError("invalid_vm_id", "Identificador da VM inválido.")
        if confirmation != vm_id:
            raise RpcRemoteError("confirmation_required", "Confirme informando o identificador exato da VM.")
        if not isinstance(desired, bool):
            raise RpcRemoteError("invalid_request", "A opção do seletor OpenCore precisa ser booleana.")
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[0-9a-f-]{36}", idempotency_key):
            raise RpcRemoteError("invalid_idempotency_key", "Chave da operação inválida.")
        try:
            from core.runtime_inventory import inspect_runtime
            runtime = inspect_runtime()
        except Exception:
            runtime = {}
        if not runtime.get("reims_update_opencore_picker"):
            raise RpcRemoteError("opencore_update_unavailable", "O Reims instalado não anuncia atualização do picker OpenCore.")
        self._refresh_catalog()
        if self.store.revision != expected:
            raise RpcRemoteError("catalog_conflict", "O catálogo mudou; recarregue antes de regenerar o OpenCore.")
        vm = self.store.get_vm(vm_id)
        if vm is None:
            raise RpcRemoteError("vm_not_found", "VM não encontrada.")
        if vm.opencore_show_picker == desired:
            raise RpcRemoteError("opencore_picker_unchanged", "A VM já está configurada com essa opção de picker.")
        if vm_id in self._get_running_vms():
            raise RpcRemoteError("vm_running", "Pare a VM antes de regenerar o OpenCore.")
        try:
            job, _created = self.job_runner.submit(
                idempotency_key=f"vm-opencore-picker:{vm_id}:{idempotency_key}",
                kind="vm.opencore.picker",
                payload={"vm_id": vm_id, "boot_selection": desired, "expected_revision": expected},
                target_vm_id=vm_id,
                vm_revision=expected,
            )
        except ValueError as exc:
            raise RpcRemoteError("invalid_request", "A chave de idempotência já foi usada com outros dados.") from exc
        except Exception as exc:
            from core.web_job_runner import QueueFullError
            if isinstance(exc, QueueFullError):
                raise RpcRemoteError("job_queue_full", "A fila de tarefas está cheia; tente novamente mais tarde.") from exc
            raise
        return JobAccepted(job_id=job["id"], events_url=f"/api/v1/jobs/{job['id']}").model_dump(mode="json")

    def _run_vm_opencore_picker_job(self, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        """Run only Reims' allowlisted OpenCore regeneration command; never expose its output."""
        from core.config import ConcurrentCatalogUpdate, ExclusiveVMLock
        from core import reims_ctl
        from core.web_job_runner import JobCancelledError

        vm_id = self._vm_id(payload)
        desired = payload.get("boot_selection")
        expected = payload.get("expected_revision")
        if not isinstance(desired, bool) or not isinstance(expected, str) or not expected:
            raise ValueError("A solicitação de atualização do OpenCore é inválida.")
        context.check_cancelled()
        self._refresh_catalog()
        if self.store.revision != expected:
            raise RuntimeError("O catálogo mudou antes da regeneração; recarregue a VM e tente novamente.")
        vm = self.store.get_vm(vm_id)
        if vm is None:
            raise RuntimeError("VM não encontrada.")
        record = reims_ctl.vm_record(vm_id)
        if not isinstance(record, dict) or record.get("state") != "stopped" or record.get("pid") is not None:
            raise RuntimeError("O Reims não confirmou que a VM está parada e registrada.")
        directory_value = record.get("dir")
        if not isinstance(directory_value, str) or not directory_value:
            raise RuntimeError("O registro Reims não informou o diretório da VM.")
        vm_directory = Path(directory_value).expanduser()
        managed_root = (self.store.config_dir / "vms").resolve(strict=False)
        try:
            resolved_directory = vm_directory.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("O diretório registrado da VM não está acessível.") from exc
        if resolved_directory != managed_root / vm_id or not resolved_directory.is_relative_to(managed_root):
            raise RuntimeError("A regeneração só está disponível para OpenCore dentro do volume gerenciado do appliance.")
        projection_path = resolved_directory / "vm.toml"
        try:
            if projection_path.is_symlink() or not projection_path.is_file():
                raise ValueError("Projeção ausente ou insegura")
            with projection_path.open("rb") as handle:
                projection = tomllib.load(handle)
            q35 = projection.get("q35")
            opencore_name = q35.get("opencore", "OpenCore.qcow2") if isinstance(q35, dict) else "OpenCore.qcow2"
            relative_image = Path(opencore_name)
            if relative_image.is_absolute() or ".." in relative_image.parts:
                raise ValueError("Caminho fora do diretório")
            image = resolved_directory / relative_image
            resolved_image = image.resolve(strict=True)
            if (
                image.is_symlink()
                or resolved_image != image
                or not resolved_image.is_relative_to(resolved_directory)
                or not resolved_image.is_file()
                or resolved_image.stat().st_size <= 0
            ):
                raise ValueError("Imagem ausente, link ou vazia")
        except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError) as exc:
            raise RuntimeError("O OpenCore não é um arquivo gerenciado e verificável pelo painel.") from exc
        binary = reims_ctl.reims_ctl_bin()
        if not binary:
            raise RuntimeError("O binário reims-ctl não está disponível.")
        try:
            from core.runtime_inventory import inspect_runtime
            if not inspect_runtime(use_cache=False).get("reims_update_opencore_picker"):
                raise RuntimeError("O Reims instalado não oferece a atualização segura do picker OpenCore.")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("Não foi possível confirmar o suporte do Reims para regenerar o OpenCore.") from exc

        lock = ExclusiveVMLock()
        if not lock.acquire(
            vm.name, vm.ram_gb, vm.id, disk_path=vm.disk_path,
            disk_paths=[disk.path for disk in (vm.additional_disks or [])],
            usb_devices=vm.usb_auto_attach,
        ):
            raise RuntimeError("A VM não pôde reservar seus recursos para regenerar o OpenCore.")
        try:
            context.check_cancelled()
            # Recheck immediately before the side effect; command output includes
            # SMBIOS values, so capture and intentionally discard both streams.
            self._refresh_catalog()
            if self.store.revision != expected or vm_id in self._get_running_vms():
                raise RuntimeError("A VM ou o catálogo mudou antes da regeneração; nenhuma imagem foi alterada.")
            context.phase("Regenerando OpenCore com a identidade atual", 10)
            context.checkpoint("opencore_regeneration_started", {"vm_id": vm_id, "boot_selection": desired})
            try:
                result = subprocess.run(
                    [binary, "update-opencore", vm_id, "--boot-selection", str(desired).lower()],
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError("Não foi possível confirmar o resultado da regeneração; confira o estado da imagem OpenCore antes de tentar novamente.") from exc
            if result.returncode != 0:
                raise RuntimeError("O Reims recusou ou não conseguiu regenerar o OpenCore; consulte o diagnóstico da tarefa.")
            context.phase("Atualizando o catálogo da VM", 90)
            # Merge only this field with the freshest catalog so unrelated edits
            # made while the long-running image operation ran are preserved.
            for _attempt in range(3):
                self._refresh_catalog()
                updated = self.store.get_vm(vm_id)
                if updated is None:
                    raise RuntimeError("O OpenCore foi regenerado, mas a VM desapareceu do catálogo.")
                updated.opencore_show_picker = desired
                try:
                    self.store.update_vm(updated, expected_revision=self.store.revision)
                    break
                except ConcurrentCatalogUpdate:
                    continue
            else:
                raise RuntimeError("O OpenCore foi regenerado, mas o catálogo mudou repetidamente; reconcilie a configuração da VM.")
            context.phase("OpenCore atualizado", 100)
            return {"vm_id": vm_id, "boot_selection": desired, "message": "OpenCore regenerado com a identidade SMBIOS preservada."}
        finally:
            lock.release()

    def submit_vm_smbios_diagnostic(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Queue a read-only, redacted SMBIOS consistency check."""
        if self.job_runner is None:
            raise RpcRemoteError("capability_unavailable", "O serviço de tarefas não está configurado.")
        vm_id = self._vm_id(payload)
        idempotency_key = payload.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[0-9a-f-]{36}", idempotency_key):
            raise RpcRemoteError("invalid_idempotency_key", "Chave da operação inválida.")
        try:
            expected = self._expected_revision(payload).strip('"')
        except RpcRemoteError:
            raise
        if not shutil.which("guestfish"):
            raise RpcRemoteError("capability_unavailable", "O utilitário de inspeção SMBIOS não está instalado.")
        self._refresh_catalog()
        if self.store.revision != expected:
            raise RpcRemoteError("catalog_conflict", "O catálogo mudou; recarregue antes do diagnóstico.")
        vm = self.store.get_vm(vm_id)
        if vm is None:
            raise RpcRemoteError("vm_not_found", "VM não encontrada.")
        if vm_id in self._get_running_vms():
            raise RpcRemoteError("vm_running", "Pare a VM antes de inspecionar a imagem OpenCore.")
        try:
            job, _created = self.job_runner.submit(
                idempotency_key=f"vm-smbios-diagnose:{vm_id}:{idempotency_key}",
                kind="vm.smbios.diagnose",
                payload={"vm_id": vm_id, "expected_revision": expected},
                target_vm_id=vm_id,
                vm_revision=expected,
            )
        except ValueError as exc:
            raise RpcRemoteError("invalid_request", "A chave de idempotência já foi usada com outros dados.") from exc
        except Exception as exc:
            from core.web_job_runner import QueueFullError
            if isinstance(exc, QueueFullError):
                raise RpcRemoteError("job_queue_full", "A fila de tarefas está cheia; tente novamente mais tarde.") from exc
            raise
        return JobAccepted(job_id=job["id"], events_url=f"/api/v1/jobs/{job['id']}").model_dump(mode="json")

    def _run_vm_smbios_diagnostic_job(self, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        """Inspect a managed, stopped OpenCore image without returning identity data."""
        from core import reims_ctl
        from core.config import ExclusiveVMLock

        vm_id = self._vm_id(payload)
        expected = payload.get("expected_revision")
        if not isinstance(expected, str) or not expected:
            raise ValueError("A revisão do catálogo é obrigatória para diagnosticar SMBIOS.")
        context.check_cancelled()
        self._refresh_catalog()
        if self.store.revision != expected:
            raise RuntimeError("O catálogo mudou; recarregue antes do diagnóstico SMBIOS.")
        vm = self.store.get_vm(vm_id)
        if vm is None:
            raise RuntimeError("VM não encontrada.")
        record = reims_ctl.vm_record(vm_id)
        if not isinstance(record, dict) or record.get("state") != "stopped" or record.get("pid") is not None:
            raise RuntimeError("O Reims não confirmou que a VM está parada e registrada.")
        directory_value = record.get("dir")
        if not isinstance(directory_value, str) or not directory_value:
            raise RuntimeError("O registro Reims não informou o diretório da VM.")
        managed_root = (self.store.config_dir / "vms").resolve(strict=False)
        try:
            directory = Path(directory_value).expanduser().resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("O diretório registrado da VM não está acessível.") from exc
        if directory != managed_root / vm_id or not directory.is_relative_to(managed_root):
            raise RuntimeError("O diagnóstico só está disponível para VMs dentro do armazenamento gerenciado do appliance.")
        projection_path = directory / "vm.toml"
        try:
            if projection_path.is_symlink() or not projection_path.is_file():
                raise ValueError("Projeção ausente ou insegura")
            with projection_path.open("rb") as handle:
                projection = tomllib.load(handle)
            q35 = projection.get("q35")
            opencore_name = q35.get("opencore", "OpenCore.qcow2") if isinstance(q35, dict) else "OpenCore.qcow2"
            relative_image = Path(opencore_name)
            if relative_image.is_absolute() or ".." in relative_image.parts:
                raise ValueError("Caminho fora do diretório")
            image = directory / relative_image
            resolved_image = image.resolve(strict=True)
            if image.is_symlink() or resolved_image != image or not resolved_image.is_relative_to(directory) or not resolved_image.is_file():
                raise ValueError("Imagem ausente, link ou fora do diretório")
        except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError) as exc:
            raise RuntimeError("O OpenCore não é um arquivo gerenciado e verificável pelo painel.") from exc
        lock = ExclusiveVMLock()
        if not lock.acquire(
            vm.name, vm.ram_gb, vm.id, disk_path=vm.disk_path,
            disk_paths=[disk.path for disk in (vm.additional_disks or [])],
            usb_devices=vm.usb_auto_attach,
        ):
            raise RuntimeError("A VM não pôde reservar seus recursos para o diagnóstico.")
        try:
            context.check_cancelled()
            self._refresh_catalog()
            if self.store.revision != expected or vm_id in self._get_running_vms():
                raise RuntimeError("A VM ou o catálogo mudou antes do diagnóstico.")
            if image.is_symlink() or image.resolve(strict=True) != resolved_image:
                raise RuntimeError("A imagem OpenCore mudou antes do diagnóstico; recarregue os dados da VM.")
            context.phase("Inspecionando OpenCore em modo somente leitura", 20)
            from core.smbios import GenSMBIOS
            result = GenSMBIOS.inspect_vm(vm, resolved_image)
            if result.get("ok") is True:
                status = "match"
                message = "A identidade SMBIOS do OpenCore corresponde ao cadastro local. Isso não confirma ativação ou login nos serviços Apple."
            elif result.get("reason") == "identity fields missing or differ; Apple activation remains unverified":
                status = "mismatch"
                message = "A identidade SMBIOS do OpenCore diverge do cadastro local. A ativação Apple não foi verificada."
            else:
                status = "unavailable"
                reason = result.get("reason")
                if reason == "guestfish unavailable; EFI could not be inspected":
                    message = "O utilitário guestfish não está disponível para ler a EFI."
                elif reason == "OpenCore EFI image missing":
                    message = "A imagem OpenCore não foi encontrada."
                else:
                    message = "Não foi possível ler a configuração EFI do OpenCore."
            context.checkpoint("smbios_diagnostic_result", {"status": status, "message": message})
            context.phase("Diagnóstico concluído", 100)
            return {"vm_id": vm_id, "status": status, "message": message}
        finally:
            lock.release()

    def list_vm_backups(self, payload: dict[str, Any]) -> dict[str, Any]:
        from core.vm_backup import VMBackupService, VMBackupUnsafe

        vm_id = payload.get("vm_id")
        if not isinstance(vm_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", vm_id):
            raise RpcRemoteError("invalid_vm_id", "Identificador da VM inválido.")
        try:
            service = VMBackupService(
                self.store.catalog_path,
                self.backup_root,
                vm_storage_root=self.store.config_dir / "vms",
            )
            return {"vm_id": vm_id, "items": service.list_backups(vm_id)}
        except VMBackupUnsafe as exc:
            raise RpcRemoteError("backup_inventory_unavailable", str(exc)) from exc

    def submit_vm_backup_verify_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.job_runner is None:
            raise RpcRemoteError("capability_unavailable", "O serviço de tarefas não está configurado.")
        vm_id = payload.get("vm_id")
        backup_id = payload.get("backup_id")
        idempotency_key = payload.get("idempotency_key")
        if not isinstance(vm_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", vm_id):
            raise RpcRemoteError("invalid_vm_id", "Identificador de VM inválido.")
        if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9a-f]{32}\.vhb", backup_id):
            raise RpcRemoteError("invalid_backup_id", "Identificador do backup inválido.")
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[0-9a-f-]{36}", idempotency_key):
            raise RpcRemoteError("invalid_idempotency_key", "Chave da operação inválida.")
        try:
            job, _created = self.job_runner.submit(
                idempotency_key=f"vm-backup-verify:{vm_id}:{backup_id}:{idempotency_key}",
                kind="vm.backup.verify",
                payload={"vm_id": vm_id, "backup_id": backup_id},
                target_vm_id=vm_id,
            )
            return JobAccepted(job_id=job["id"], events_url=f"/api/v1/jobs/{job['id']}").model_dump(mode="json")
        except Exception as exc:
            from core.web_job_runner import QueueFullError

            if isinstance(exc, QueueFullError):
                raise RpcRemoteError("job_queue_full", "A fila de tarefas está cheia; tente novamente mais tarde.") from exc
            raise

    def submit_vm_backup_restore_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.job_runner is None:
            raise RpcRemoteError("capability_unavailable", "O serviço de tarefas não está configurado.")
        vm_id = payload.get("vm_id")
        backup_id = payload.get("backup_id")
        confirmation = payload.get("confirmation")
        idempotency_key = payload.get("idempotency_key")
        if not isinstance(vm_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", vm_id):
            raise RpcRemoteError("invalid_vm_id", "Identificador de VM inválido.")
        if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9a-f]{32}\.vhb", backup_id):
            raise RpcRemoteError("invalid_backup_id", "Identificador do backup inválido.")
        if confirmation != f"{vm_id}:{backup_id}":
            raise RpcRemoteError("confirmation_required", "Confirme a restauração para esta VM e este backup.")
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[0-9a-f-]{36}", idempotency_key):
            raise RpcRemoteError("invalid_idempotency_key", "Chave da operação inválida.")
        try:
            job, _created = self.job_runner.submit(
                idempotency_key=f"vm-backup-restore:{vm_id}:{backup_id}:{idempotency_key}",
                kind="vm.backup.restore",
                payload={"vm_id": vm_id, "backup_id": backup_id},
                target_vm_id=vm_id,
            )
            return JobAccepted(job_id=job["id"], events_url=f"/api/v1/jobs/{job['id']}").model_dump(mode="json")
        except Exception as exc:
            from core.web_job_runner import QueueFullError

            if isinstance(exc, QueueFullError):
                raise RpcRemoteError("job_queue_full", "A fila de tarefas está cheia; tente novamente mais tarde.") from exc
            raise

    def submit_vm_clone_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.job_runner is None:
            raise RpcRemoteError("capability_unavailable", "O serviço de tarefas não está configurado.")
        source_id = payload.get("vm_id")
        clone_id = payload.get("clone_id")
        name = payload.get("name")
        confirmation = payload.get("confirmation")
        idempotency_key = payload.get("idempotency_key")
        if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", source_id):
            raise RpcRemoteError("invalid_vm_id", "Identificador de VM de origem inválido.")
        if not isinstance(clone_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", clone_id):
            raise RpcRemoteError("invalid_vm_id", "Identificador do clone inválido.")
        if source_id == clone_id or confirmation != f"{source_id}:{clone_id}":
            raise RpcRemoteError("confirmation_required", "Confirme a clonagem para esta VM e para o novo identificador.")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise RpcRemoteError("invalid_vm_name", "O nome do clone deve ter entre 1 e 80 caracteres.")
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[0-9a-f-]{36}", idempotency_key):
            raise RpcRemoteError("invalid_idempotency_key", "Chave da operação inválida.")
        try:
            job, _created = self.job_runner.submit(
                idempotency_key=f"vm-clone:{source_id}:{clone_id}:{idempotency_key}",
                kind="vm.clone",
                payload={"source_vm_id": source_id, "clone_id": clone_id, "name": name.strip()},
                target_vm_id=source_id,
            )
            return JobAccepted(job_id=job["id"], events_url=f"/api/v1/jobs/{job['id']}").model_dump(mode="json")
        except Exception as exc:
            from core.web_job_runner import QueueFullError

            if isinstance(exc, QueueFullError):
                raise RpcRemoteError("job_queue_full", "A fila de tarefas está cheia; tente novamente mais tarde.") from exc
            raise

    def _run_vm_backup_verify_job(self, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        from core.vm_backup import VMBackupService, VMBackupUnsafe

        vm_id = payload.get("vm_id")
        backup_id = payload.get("backup_id")
        if not isinstance(vm_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", vm_id):
            raise ValueError("Identificador da VM inválido.")
        if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9a-f]{32}\.vhb", backup_id):
            raise ValueError("Identificador do backup inválido.")

        def on_progress(copied: int, total: int) -> None:
            context.phase("Verificando hashes e discos do backup", min(99.0, copied * 100 / total) if total else 99.0)

        service = VMBackupService(
            self.store.catalog_path,
            self.backup_root,
            vm_storage_root=self.store.config_dir / "vms",
        )
        context.phase("Validando manifesto do backup", 0)
        try:
            result = service.verify_backup(
                vm_id, backup_id,
                check_cancelled=context.check_cancelled,
                on_progress=on_progress,
            )
        except VMBackupUnsafe as exc:
            raise RuntimeError(str(exc)) from exc
        context.phase("Integridade do backup confirmada", 100)
        return result

    def _run_vm_backup_restore_job(self, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        from core.vm_backup import VMBackupConflict, VMBackupRunning, VMBackupService, VMBackupUnsafe
        from core.web_job_runner import JobCancelledError

        vm_id = payload.get("vm_id")
        backup_id = payload.get("backup_id")
        if not isinstance(vm_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", vm_id):
            raise ValueError("Identificador de VM inválido.")
        if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9a-f]{32}\.vhb", backup_id):
            raise ValueError("Identificador do backup inválido.")

        def on_progress(copied: int, total: int) -> None:
            context.phase("Preparando arquivos verificados para restauração", min(99.0, copied * 100 / total) if total else 99.0)

        service = VMBackupService(
            self.store.catalog_path,
            self.backup_root,
            vm_storage_root=self.store.config_dir / "vms",
        )
        context.phase("Validando backup e VM parada", 0)
        try:
            result = service.restore(
                vm_id, backup_id,
                check_cancelled=context.check_cancelled,
                on_progress=on_progress,
            )
        except JobCancelledError:
            raise
        except VMBackupRunning as exc:
            raise RuntimeError("A VM precisa estar parada antes de restaurar o backup.") from exc
        except (VMBackupConflict, VMBackupUnsafe) as exc:
            raise RuntimeError(str(exc)) from exc
        except OSError:
            raise RuntimeError("Não foi possível publicar a restauração; os dados anteriores foram preservados.") from None
        context.phase("Restauração concluída", 100)
        return result

    def _run_vm_clone_job(self, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        from core.vm_backup import VMBackupConflict, VMBackupRunning, VMBackupUnsafe
        from core.vm_clone import VMCloneService
        from core.web_job_runner import JobCancelledError

        source_id = payload.get("source_vm_id")
        clone_id = payload.get("clone_id")
        name = payload.get("name")
        if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", source_id):
            raise ValueError("Identificador da VM de origem inválido.")
        if not isinstance(clone_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", clone_id):
            raise ValueError("Identificador do clone inválido.")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("Nome do clone inválido.")

        def on_progress(copied: int, total: int) -> None:
            context.phase("Copiando VM e preparando identidade independente", min(99.0, copied * 100 / total) if total else 99.0)

        context.phase("Validando VM de origem parada e espaço", 0)
        service = VMCloneService(
            self.store.catalog_path,
            vm_storage_root=self.store.config_dir / "vms",
        )
        try:
            result = service.clone(
                source_id, clone_id, name,
                check_cancelled=context.check_cancelled,
                on_progress=on_progress,
            )
        except JobCancelledError:
            raise
        except VMBackupRunning as exc:
            raise RuntimeError("A VM de origem precisa estar parada para criar o clone.") from exc
        except (VMBackupConflict, VMBackupUnsafe) as exc:
            raise RuntimeError(str(exc)) from exc
        except OSError:
            raise RuntimeError("Não foi possível preparar o clone; a VM de origem foi preservada.") from None
        context.phase("Clone independente concluído", 100)
        return result

    def _run_vm_backup_job(self, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        from core.vm_backup import VMBackupConflict, VMBackupRunning, VMBackupService, VMBackupUnsafe
        from core.web_job_runner import JobCancelledError

        vm_id = payload.get("vm_id")
        if not isinstance(vm_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", vm_id):
            raise ValueError("Identificador da VM inválido.")

        def on_progress(copied: int, total: int) -> None:
            percent = min(99.0, copied * 100 / total) if total else 99.0
            context.phase("Copiando e validando backup offline", percent)

        context.phase("Validando VM parada", 0)
        service = VMBackupService(
            self.store.catalog_path,
            self.backup_root,
            vm_storage_root=self.store.config_dir / "vms",
        )
        try:
            destination = service.create(
                vm_id,
                check_cancelled=context.check_cancelled,
                on_progress=on_progress,
            )
        except JobCancelledError:
            raise
        except VMBackupRunning as exc:
            raise RuntimeError("A VM precisa estar parada antes de criar o backup.") from exc
        except (VMBackupConflict, VMBackupUnsafe) as exc:
            raise RuntimeError(str(exc)) from exc
        except OSError:
            raise RuntimeError("Não foi possível concluir o backup; verifique espaço e integridade do armazenamento.") from None
        context.phase("Backup offline concluído", 100)
        return {"vm_id": vm_id, "backup_id": destination.name, "file_count": 7}

    def _run_media_import_job(self, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        from core.web_media import ManagedMediaImporter

        transfer_id = payload.get("transfer_id")
        if not isinstance(transfer_id, str) or not re.fullmatch(r"[0-9a-f]{32}", transfer_id):
            raise ValueError("Identificador da transferência inválido.")

        def on_progress(copied: int, total: int) -> None:
            context.phase("Importando mídia", min(99.0, copied * 100 / total))

        def check_cancelled() -> None:
            context.check_cancelled()

        context.phase("Validando mídia", 0)
        importer = ManagedMediaImporter(
            self.transfer_root,
            self.store.config_dir / "media",
            on_progress=on_progress,
            check_cancelled=check_cancelled,
        )
        try:
            result = importer.import_transfer(transfer_id)
        except OSError:
            raise RuntimeError("Não foi possível copiar a mídia para a biblioteca gerenciada.") from None
        context.phase("Mídia importada", 100)
        return {
            "transfer_id": result.id,
            "name": result.name,
            "format": result.format,
            "size_bytes": result.size_bytes,
        }

    def _run_media_convert_job(self, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        from core.web_media_convert import InvalidMediaConversion, ManagedMediaConverter

        media_id = payload.get("media_id")
        output_format = payload.get("output_format")
        if not isinstance(media_id, str) or not re.fullmatch(r"[0-9a-f]{32}", media_id):
            raise ValueError("Identificador da mídia inválido.")
        if not isinstance(output_format, str) or output_format not in {"raw", "qcow2"}:
            raise ValueError("Formato de saída inválido.")

        media_root = self.store.config_dir / "media"
        root = media_root.resolve(strict=False)
        if media_root.is_symlink() or not root.is_relative_to(self.store.config_dir.resolve(strict=False)):
            raise InvalidMediaConversion("A biblioteca gerenciada não está disponível.")
        source = None
        for candidate in root.iterdir() if root.is_dir() else ():
            try:
                info = candidate.lstat()
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not stat.S_ISREG(info.st_mode) or candidate.is_symlink() or not resolved.is_relative_to(root):
                continue
            candidate_id = hashlib.sha256(os.fsencode(str(resolved))).hexdigest()[:32]
            if candidate_id == media_id:
                source = candidate
                break
        if source is None:
            raise InvalidMediaConversion("A mídia não existe na biblioteca gerenciada.")
        for vm in self.store.list_vms():
            if not vm.disk_path:
                continue
            try:
                if Path(vm.disk_path).expanduser().resolve(strict=True) == source.resolve(strict=True):
                    raise InvalidMediaConversion("Mídias referenciadas por uma VM não podem ser convertidas.")
            except FileNotFoundError:
                continue
        suffix = source.suffix.casefold()
        source_format = "qcow2" if suffix == ".qcow2" else "raw" if suffix in {".raw", ".img"} else None
        if source_format is None:
            raise InvalidMediaConversion("Somente mídias RAW e QCOW2 podem ser convertidas.")
        target_suffix = ".qcow2" if output_format == "qcow2" else ".raw"
        destination_name = f"{source.stem}{target_suffix}"

        def on_progress(percent: float) -> None:
            context.phase("Convertendo imagem de disco", min(99.0, percent))

        context.phase("Validando origem", 0)
        converter = ManagedMediaConverter(
            root, check_cancelled=context.check_cancelled, on_progress=on_progress,
        )
        result = converter.convert(source.name, destination_name, source_format, output_format)
        context.phase("Conversão concluída", 100)
        return {"name": result.name, "source_format": result.source_format,
                "format": result.format, "size_bytes": result.size_bytes}

    def _run_apple_recovery_download_job(self, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        version = payload.get("macos_version")
        if not isinstance(version, str) or version not in {"tahoe", "sequoia", "ventura"}:
            raise ValueError("Versão do macOS não suportada pelo downloader.")
        from core.downloader import MACOS_PRODUCTS, MacOSDownloader

        context.phase(f"Consultando catálogo Apple para {MACOS_PRODUCTS[version]['name']}", 0)
        context.check_cancelled()
        image = MacOSDownloader.prepare_installer(version, job_context=context)
        if image is None or not Path(image).is_file() or Path(image).stat().st_size <= 0:
            raise RuntimeError("A Apple não forneceu uma imagem válida ou a conversão do instalador falhou.")
        context.phase("Recovery Apple pronto", 100)
        return {"macos_version": version, "name": Path(image).name, "size_bytes": Path(image).stat().st_size}

    def _run_provision_job(self, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        if self.provisioning_service is None:
            raise RuntimeError("VMProvisioningService não está configurado")
        from core.vm_provisioning import InstallerPolicy, VMProvisioningRequest

        request_payload = dict(payload)
        request_payload["installer_policy"] = InstallerPolicy(request_payload["installer_policy"])
        raw_topology = request_payload.get("cpu_topology")
        if raw_topology is not None:
            from core.config import CpuTopologyConfig

            request_payload["cpu_topology"] = CpuTopologyConfig(**raw_topology)
        request = VMProvisioningRequest(
            **request_payload,
        )

        def on_event(event: Any) -> None:
            context.phase(event.stage, event.progress)
            context.checkpoint(
                event.stage,
                {"status": event.status, "message": event.message, "vm_id": event.vm_id},
                completed=event.status == "completed",
            )
            if event.message:
                context.log(event.message)

        class CancelProbe:
            @staticmethod
            def is_set() -> bool:
                context.check_cancelled()
                return False

        import inspect

        provision = self.provisioning_service.provision
        parameters = inspect.signature(provision).parameters.values()
        accepts_context = any(
            parameter.name == "job_context" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        kwargs = {"on_event": on_event, "cancel_event": CancelProbe()}
        if accepts_context:
            kwargs["job_context"] = context
        result = provision(request, **kwargs)
        return {"vm_id": result.vm.id, "name": result.vm.name, "disk_path": result.vm.disk_path}

    def _run_reims_release_update_job(self, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        from core.reims_release import update_from_release

        tag = payload.get("tag")
        if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", tag):
            raise ValueError("Tag da release inválida.")
        context.check_cancelled()
        context.phase("Bloqueando novas partidas e verificando VMs ativas", 0)
        # From this point the updater may mutate /opt/reims-vgpu and cannot be
        # interrupted safely. The core updater holds the appliance maintenance
        # window for the full download, verification, and publication.
        context.checkpoint("reims_release_update_started", {"tag": tag})
        context.phase(f"Baixando e validando Reims {tag}", 5)
        result = update_from_release(tag=tag, log=context.log)
        if not result.ok:
            raise RuntimeError(result.message or "Não foi possível atualizar o runtime Reims.")
        context.phase(f"Reims {result.tag} disponível; será ativado no próximo boot", 100)
        return {
            "tag": result.tag,
            "previous": result.previous,
            "changed": result.changed,
            "message": result.message,
            "activation": "next-vm-start",
        }

    def get_job(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        job_id = self._job_id(payload)
        if self.job_store is None:
            raise RpcRemoteError("capability_unavailable", "O armazenamento de tarefas não está configurado.")
        job = self.job_store.get_job(job_id)
        return self._job_detail(job) if job else None

    def list_jobs(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.job_store is None:
            raise RpcRemoteError("capability_unavailable", "O armazenamento de tarefas não está configurado.")
        limit = payload.get("limit", 25)
        offset = payload.get("offset", 0)
        states_value = payload.get("states")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise RpcRemoteError("invalid_job_query", "O limite deve estar entre 1 e 100.")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000:
            raise RpcRemoteError("invalid_job_query", "O deslocamento deve estar entre 0 e 10000.")
        states: set[str] | None = None
        if states_value is not None:
            valid = {"queued", "running", "succeeded", "failed", "cancelling", "cancelled", "interrupted"}
            if (not isinstance(states_value, list) or not states_value
                    or any(not isinstance(value, str) or value not in valid for value in states_value)):
                raise RpcRemoteError("invalid_job_query", "O filtro de estado contém valores inválidos.")
            states = set(states_value)
        try:
            rows = self.job_store.list_jobs(states=states, limit=limit, offset=offset)
            total = self.job_store.count_jobs(states=states)
        except ValueError as exc:
            raise RpcRemoteError("invalid_job_query", "Os filtros da lista de tarefas são inválidos.") from exc
        return JobListResponse(
            items=[self._job_summary(row) for row in rows],
            limit=limit, offset=offset, total=total,
        ).model_dump(mode="json")

    def cancel_job(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        job_id = self._job_id(payload)
        if self.job_store is None:
            raise RpcRemoteError("capability_unavailable", "O cancelamento de tarefas não está configurado.")
        job = self.job_store.get_job(job_id)
        if job is None:
            return None
        runner = self._runner_for_kind(job["kind"])
        if runner is None:
            raise RpcRemoteError("capability_unavailable", "O cancelamento de tarefas não está configurado.")
        if self._job_side_effect_started(job):
            raise RpcRemoteError("job_not_cancellable", "A ação da VM já foi enviada ao runtime; cancelar a tarefa não reverteria a operação.")
        try:
            runner.cancel(job_id)
        except ValueError as exc:
            raise RpcRemoteError("job_not_cancellable", "A tarefa já terminou e não pode ser cancelada.") from exc
        return self.get_job({"job_id": job_id})

    def retry_job(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        job_id = self._job_id(payload)
        if self.job_store is None:
            raise RpcRemoteError("capability_unavailable", "A repetição de tarefas não está configurada.")
        job = self.job_store.get_job(job_id)
        if job is None:
            return None
        runner = self._runner_for_kind(job["kind"])
        if runner is None:
            raise RpcRemoteError("capability_unavailable", "A repetição de tarefas não está configurada.")
        if job["kind"] == "vm.shutdown" and self._shutdown_request_started(job_id):
            raise RpcRemoteError("job_not_retryable", "O pedido ACPI já foi enviado; verifique o estado da VM antes de iniciar outra ação.")
        from core.web_job_runner import QueueFullError

        try:
            runner.retry(job_id)
        except QueueFullError:
            # retry() has already persisted the queued state before reporting
            # a full in-memory queue; workers will pick up this durable job.
            pass
        except KeyError as exc:
            raise RpcRemoteError("job_not_found", "Tarefa não encontrada.") from exc
        except ValueError as exc:
            raise RpcRemoteError("job_not_retryable", "A tarefa não pode ser repetida neste estado ou não possui solicitação persistida.") from exc
        return self.get_job({"job_id": job_id})

    def _job_detail(self, job: dict[str, Any]) -> dict[str, Any]:
        checkpoints = [
            {"name": item["name"], "payload": self._public_job_value(item["payload"]),
             "completed": item["completed"], "updated_at": item["updated_at"]}
            for item in self.job_store.get_checkpoints(job["id"])
            if not item["name"].startswith("__") and not self._private_job_field(item["name"])
        ]
        return {
            "id": job["id"], "kind": job["kind"], "state": job["state"],
            "stage": job["phase"], "progress_percent": job["progress"],
            "created_at": job["created_at"], "updated_at": job["updated_at"],
            "result": self._public_job_value(job["result"] or {}),
            "error": ({"code": job["error_code"] or "job_failed", "message": self._redact_job_text(job["error_message"])} if job["error_message"] else None),
            "cancellable": job["state"] in {"queued", "running", "cancelling"} and not self._job_side_effect_started(job),
            "retryable": self._is_job_retryable(job),
            "checkpoints": checkpoints,
            "logs": [{**item, "message": self._redact_job_text(item["message"])}
                     for item in self.job_store.get_logs(job["id"], limit=100)],
        }

    @staticmethod
    def _private_job_field(value: str) -> bool:
        lowered = value.lower()
        return any(term in lowered for term in (
            "request", "payload", "secret", "token", "password", "credential",
            "argument", "private", "path", "api_key", "apikey",
        ))

    @classmethod
    def _public_job_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): cls._public_job_value(item)
                for key, item in value.items()
                if not cls._private_job_field(str(key))
            }
        if isinstance(value, list):
            return [cls._public_job_value(item) for item in value[:100]]
        if isinstance(value, str):
            return cls._redact_job_text(value[:2000])
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return "[omitted]"

    @staticmethod
    def _redact_job_text(value: str) -> str:
        value = re.sub(r"(?i)\b(password|token|secret|authorization|api[_-]?key)\b\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", value)
        value = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [redacted]", value)
        return value[:2000]

    def _is_job_retryable(self, job: dict[str, Any]) -> bool:
        runner = self._runner_for_kind(job["kind"])
        if (runner is None or self.job_store is None
                or job["state"] not in {"failed", "interrupted"}
                or job["kind"] not in runner._handlers):
            return False
        if job["kind"] == "vm.shutdown" and self._shutdown_request_started(job["id"]):
            return False
        return any(
            item["name"] == "__runner_request_v1"
            for item in self.job_store.get_checkpoints(job["id"])
        )

    def _job_summary(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": job["id"], "kind": job["kind"], "state": job["state"],
            "stage": job["phase"], "progress_percent": job["progress"],
            "target_vm_id": job["target_vm_id"], "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "cancellable": job["state"] in {"queued", "running", "cancelling"} and not self._job_side_effect_started(job),
            "retryable": self._is_job_retryable(job),
        }

    def _shutdown_request_started(self, job_id: str) -> bool:
        if self.job_store is None:
            return False
        return any(
            item["name"] in {"shutdown_request_prepared", "acpi_shutdown_sent"}
            for item in self.job_store.get_checkpoints(job_id)
        )

    def _job_side_effect_started(self, job: dict[str, Any]) -> bool:
        if self.job_store is None:
            return False
        if job["kind"] == "vm.shutdown" and self._shutdown_request_started(job["id"]):
            return True
        irreversible_checkpoints = {
            "vm.start": {"vm_start_prepared"},
            "vm.force_stop": {"force_stop_prepared"},
            "vm.restart": {"shutdown_request_prepared", "acpi_shutdown_sent", "vm_unit_restarted"},
            "vm.opencore.picker": {"opencore_regeneration_started"},
            "reims.release.update": {"reims_release_update_started"},
        }.get(job["kind"], set())
        return any(item["name"] in irreversible_checkpoints for item in self.job_store.get_checkpoints(job["id"]))

    def _runner_for_kind(self, kind: str) -> Any | None:
        return self.media_job_runner if kind in {"media.import", "media.convert", "media.apple.download"} else self.job_runner

    @staticmethod
    def _job_id(payload: dict[str, Any]) -> str:
        value = payload.get("job_id")
        if not isinstance(value, str) or not value or len(value) > 128 or "/" in value or "\\" in value:
            raise RpcRemoteError("invalid_job_id", "Identificador de tarefa inválido.")
        return value

    def _refresh_catalog(self) -> None:
        try:
            self.store.refresh()
        except (OSError, RuntimeError) as exc:
            raise RpcRemoteError("catalog_unavailable", "O catálogo de VMs não pôde ser lido.") from exc

    def _config_envelope(self, vm: VMConfig) -> VmConfigEnvelope:
        projection = self._load_reims_projection(vm)
        config_data = vm.to_dict()
        additional_disks = vm.additional_disks or []
        config_data["additional_disks"] = [
            {
                "media_id": hashlib.sha256(os.fsencode(str(Path(disk.path).expanduser().resolve(strict=False)))).hexdigest()[:32],
                "path": disk.path,
                "format": disk.format,
                "controller": disk.controller,
                "order": disk.order,
            }
            for disk in additional_disks
        ]
        config_data["audio_backend"] = vm.audio_backend or "auto"
        if config_data.get("display_count") is None:
            display = projection.get("display") if isinstance(projection, dict) else None
            count = display.get("count") if isinstance(display, dict) else None
            config_data["display_count"] = count if isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= 8 else 1
        if config_data.get("network_ssh_port") is None and isinstance(projection, dict):
            network = projection.get("network")
            port = network.get("ssh_port") if isinstance(network, dict) else None
            if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
                config_data["network_ssh_port"] = port
        if not config_data.get("network_mac") and isinstance(projection, dict):
            network = projection.get("network")
            if isinstance(network, dict) and isinstance(network.get("mac"), str):
                config_data["network_mac"] = network["mac"]
        config = ApiVmConfig(**config_data, revision=self.store.revision)
        differences = self._projection_differences(vm, projection) if projection is not None else []
        pending_fields = [item.field for item in differences]
        return VmConfigEnvelope(
            config=config,
            revision=self.store.revision,
            runtime_config_differs=bool(pending_fields),
            pending_restart_fields=pending_fields,
            projection_status="available" if projection is not None else "unknown",
            config_differences=differences,
        )

    def _load_reims_projection(self, vm: VMConfig) -> dict[str, Any] | None:
        """Read a private, regular VM projection without following symlinks."""
        vm_dir = Path(self.store.config_dir) / "vms" / vm.id
        toml_path = vm_dir / "vm.toml"
        try:
            directory_info = vm_dir.lstat()
            file_info = toml_path.lstat()
            if not stat.S_ISDIR(directory_info.st_mode) or not stat.S_ISREG(file_info.st_mode):
                return None
            fd = os.open(toml_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (file_info.st_dev, file_info.st_ino):
                    return None
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    projection = tomllib.load(handle)
            finally:
                os.close(fd)
        except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError):
            return None
        return projection if isinstance(projection, dict) else None

    @staticmethod
    def _projection_differences(vm: VMConfig, projection: dict[str, Any]) -> list[Any]:
        from web.api.contracts import VmConfigDifference

        differences: list[VmConfigDifference] = []

        def show(value: Any) -> str:
            if isinstance(value, (list, dict)):
                return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if value is None:
                return "Automático"
            return str(value)

        def differs(field: str, saved: Any, projected: Any) -> None:
            if projected is not None and saved != projected and field not in {item.field for item in differences}:
                differences.append(VmConfigDifference(
                    field=field, saved_value=show(saved), last_projection_value=show(projected),
                ))

        differs("vcpus", vm.vcpus, projection.get("cpus"))
        projected_disk = projection.get("disk")
        if vm.disk_path and isinstance(projected_disk, str):
            differs("disk_path", str(Path(vm.disk_path).expanduser()), str(Path(projected_disk).expanduser()))
        q35 = projection.get("q35")
        if isinstance(q35, dict):
            differs("cpu_model", vm.cpu_model, q35.get("cpu_model"))
            projected_topology = q35.get("cpu_topology")
            saved_topology = {
                "sockets": vm.cpu_topology.sockets if vm.cpu_topology else 1,
                "cores": vm.cpu_topology.cores if vm.cpu_topology else vm.vcpus,
                "threads": vm.cpu_topology.threads if vm.cpu_topology else 1,
            }
            saved_is_custom = saved_topology != {"sockets": 1, "cores": vm.vcpus, "threads": 1}
            if isinstance(projected_topology, dict):
                differs("cpu_topology", saved_topology, projected_topology)
            elif saved_is_custom:
                projected_vcpus = projection.get("cpus")
                if not isinstance(projected_vcpus, int) or isinstance(projected_vcpus, bool):
                    projected_vcpus = vm.vcpus
                projected_topology = {"sockets": 1, "cores": projected_vcpus, "threads": 1}
                differs("cpu_topology", saved_topology, projected_topology)
        memory_mib = projection.get("memory_mib")
        if isinstance(memory_mib, int) and not isinstance(memory_mib, bool):
            differs("ram_gb", vm.ram_gb, memory_mib / 1024)
        display = projection.get("display")
        if isinstance(display, dict):
            differs("display_resolution", vm.display_resolution, display.get("resolution"))
            if vm.display_count is not None:
                differs("display_count", vm.display_count, display.get("count", 1))
            differs("display_hidpi", vm.display_hidpi, display.get("hidpi"))
            differs("fullscreen", vm.fullscreen, display.get("borderless"))
            differs("display_refresh_hz", vm.display_refresh_hz, display.get("refresh"))
        projected_args = projection.get("extra_args")
        if isinstance(projected_args, list) and all(isinstance(token, str) for token in projected_args):
            if projected_args[:2] == ["-action", "reboot=shutdown"]:
                projected_args = projected_args[2:]
            differs("qemu_extra_args", vm.qemu_extra_args, projected_args)
        audio = projection.get("audio")
        if isinstance(audio, dict):
            differs("audio_enabled", vm.audio_enabled, audio.get("enabled", True))
            differs("audio_backend", vm.audio_backend or "auto", audio.get("backend") or "auto")
        projected_disks = projection.get("additional_disks")
        if isinstance(projected_disks, list):
            saved_disks = [
                {"name": Path(item.path).name, "format": item.format, "controller": item.controller, "order": item.order}
                for item in (vm.additional_disks or [])
            ]
            projected_summary = [
                {
                    "name": Path(item["path"]).name if isinstance(item, dict) and isinstance(item.get("path"), str) else "desconhecido",
                    "format": item.get("format") if isinstance(item, dict) else None,
                    "controller": item.get("controller") if isinstance(item, dict) else None,
                    "order": item.get("order") if isinstance(item, dict) else None,
                }
                for item in projected_disks
            ]
            differs("additional_disks", saved_disks, projected_summary)
        network = projection.get("network")
        if isinstance(network, dict):
            differs("network_enabled", vm.network_enabled, network.get("enabled", True))
            if vm.network_mac:
                differs("network_mac", vm.network_mac.lower(), str(network.get("mac", "")).lower() or None)
            if vm.network_ssh_port is not None:
                differs("network_ssh_port", vm.network_ssh_port or None, network.get("ssh_port"))
        # Reims persists a GPU as a Vulkan driver file (and sometimes a device
        # ID), while the panel stores the selected PCI BDF and Vulkan UUID.
        # Compare only when the current host can resolve that mapping exactly;
        # otherwise an unavailable/stale inventory must not create a false
        # mismatch or claim that the running QEMU uses the selected device.
        projected_gpu = projection.get("gpu")
        if vm.selected_gpu and isinstance(projected_gpu, dict):
            try:
                from core.runtime_inventory import inspect_runtime

                schema_features = set(inspect_runtime().get("reims_schema_features", []))
            except Exception:
                schema_features = set()
            if "gpu_identity" in schema_features:
                saved_identity = {
                    "device_uuid": vm.selected_gpu_device_uuid,
                    "pci_address": vm.selected_gpu_pci_address or vm.selected_gpu,
                    "driver_file": vm.selected_gpu_driver_file,
                }
                projected_identity = {
                    "device_uuid": projected_gpu.get("device_uuid"),
                    "pci_address": projected_gpu.get("pci_address"),
                    "driver_file": projected_gpu.get("driver_file"),
                }
                compared_fields = [key for key, value in saved_identity.items() if value is not None]
                if compared_fields and any(saved_identity[key] != projected_identity[key] for key in compared_fields):
                    differs(
                        "selected_gpu",
                        f"{vm.selected_gpu_name or vm.selected_gpu} ({vm.selected_gpu})",
                        json.dumps(projected_identity, ensure_ascii=False, separators=(",", ":")),
                    )
            else:
                try:
                    from core.reims_ctl import _gpu_config_para_slot

                    expected_gpu = _gpu_config_para_slot(vm.selected_gpu)
                except (ImportError, OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                    expected_gpu = None
                if isinstance(expected_gpu, dict) and expected_gpu:
                    expected_driver = expected_gpu.get("driver_file")
                    actual_driver = projected_gpu.get("driver_file")
                    expected_device = expected_gpu.get("device_id")
                    actual_device = projected_gpu.get("device_id")
                    if expected_driver != actual_driver or expected_device != actual_device:
                        differs(
                            "selected_gpu",
                            f"{vm.selected_gpu_name or vm.selected_gpu} ({vm.selected_gpu})",
                            f"driver_file={actual_driver or 'ausente'}, device_id={actual_device or 'automático'}",
                        )
        return differences

    def _pending_restart_fields(self, vm: VMConfig) -> list[str]:
        projection = self._load_reims_projection(vm)
        if projection is None:
            return []
        return [item.field for item in self._projection_differences(vm, projection)]

    @staticmethod
    def _vm_id(payload: dict[str, Any]) -> str:
        value = payload.get("vm_id")
        if not isinstance(value, str) or not value or len(value) > 128 or "/" in value or "\\" in value or "\x00" in value:
            raise RpcRemoteError("invalid_vm_id", "Identificador de VM inválido.")
        return value

    def _get_running_vms(self) -> dict[str, int]:
        running, problem = self._read_running_vms()
        if running is None:
            raise RpcRemoteError("runtime_state_unavailable", problem or "Não foi possível verificar as VMs em execução.")
        return running

    def _read_running_vms(self) -> tuple[dict[str, int] | None, str | None]:
        try:
            managed_vms = self.store.list_vms()
            if self._running_vms is not None:
                result = self._running_vms()
            else:
                from core.reims_ctl import vms_rodando
                result = vms_rodando()
            if not isinstance(result, dict) or any(not isinstance(key, str) or not isinstance(value, int) for key, value in result.items()):
                return None, "A lista de processos de VM retornou dados inválidos."
            if self._running_vms is None:
                recovered, complete = self._discover_qemu_processes(managed_vms)
                result.update(recovered)
                if not complete and not result:
                    return None, "Não foi possível confirmar quais VMs estão em execução."
            return result, None
        except Exception as exc:
            return None, "Não foi possível confirmar quais VMs estão em execução."

    @staticmethod
    def _discover_qemu_processes(vms: list[VMConfig]) -> tuple[dict[str, int], bool]:
        """Recover live Reims guests when its pidfile-based list misses them.

        A process is attributed only when its QMP Unix socket exactly matches
        the path registered for a VM. This never treats an arbitrary QEMU
        process on the host as one of our guests.
        """
        from core.reims_ctl import qmp_socket

        expected: dict[str, str] = {}
        complete = True
        for vm in vms:
            try:
                expected[str(qmp_socket(vm).resolve(strict=False))] = vm.id
            except OSError:
                complete = False
        matched: dict[str, set[int]] = {}
        try:
            processes = psutil.process_iter(["pid", "name", "cmdline"], ad_value=None)
            for process in processes:
                info = process.info
                name = info.get("name") or ""
                if not name.startswith("qemu-system-"):
                    continue
                command = info.get("cmdline")
                pid = info.get("pid")
                if not isinstance(command, list) or not isinstance(pid, int) or pid <= 0:
                    complete = False
                    continue
                try:
                    qmp_index = command.index("-qmp")
                    address = command[qmp_index + 1]
                except (ValueError, IndexError):
                    continue
                if not isinstance(address, str) or not address.startswith("unix:"):
                    continue
                socket_path = address[5:].split(",", 1)[0]
                try:
                    vm_id = expected.get(str(Path(socket_path).resolve(strict=False)))
                except OSError:
                    complete = False
                    continue
                if vm_id:
                    matched.setdefault(vm_id, set()).add(pid)
        except (psutil.Error, OSError):
            complete = False
        # Two live processes claiming the same managed QMP path are ambiguous;
        # preserve the caller's existing Reims result and do not guess.
        return ({vm_id: next(iter(pids)) for vm_id, pids in matched.items() if len(pids) == 1}, complete)

    def _open_qmp(self, vm: VMConfig) -> Any | None:
        if self._qmp_factory is not None:
            try:
                return self._qmp_factory(vm)
            except Exception:
                return None
        try:
            from core.qmp.relay import QmpControlClient, qmp_control_socket

            control_socket = qmp_control_socket(vm.id)
            if control_socket.exists():
                return QmpControlClient(vm.id, timeout=2.0)
            # The Reims QMP Unix server accepts one managed client in the
            # runtime currently used by the appliance. The per-VM supervisor
            # owns that connection for lifecycle events; do not flood its
            # listen backlog with competing HTTP-agent connections.
            if self.vm_unit_manager is not None:
                try:
                    unit_status = self.vm_unit_manager.query(vm.id)
                except Exception:
                    unit_status = None
                if unit_status is not None and unit_status.active_state in {"active", "activating", "reloading"}:
                    return None

            from core.passthrough import QmpClient
            from core.reims_ctl import qmp_socket

            client = QmpClient(qmp_socket(vm), timeout=1.0)
            client.connect()
            return client
        except Exception:
            return None


def main(argv: list[str] | None = None) -> int:
    """Run the privileged, allowlisted local agent for the HTTPS service."""
    parser = argparse.ArgumentParser(description="vHackintosh appliance local web agent")
    parser.add_argument("--socket", default=os.environ.get("VHACKINTOSH_AGENT_SOCKET", "/run/vhackintosh-agent/agent.sock"))
    parser.add_argument("--catalog", default=os.environ.get("VHACKINTOSH_VM_CATALOG", str(VMS_FILE)))
    parser.add_argument("--jobs-db", default=os.environ.get("VHACKINTOSH_JOBS_DB", "/var/lib/vhackintosh/web/jobs.sqlite"))
    parser.add_argument("--web-uid", type=int, default=os.environ.get("VHACKINTOSH_WEB_UID"))
    parser.add_argument("--web-group", default=os.environ.get("VHACKINTOSH_WEB_GROUP", "vhackintosh-web"))
    args = parser.parse_args(argv)
    web_uid = args.web_uid
    if web_uid is None:
        try:
            web_uid = pwd.getpwnam("vhackintosh-web").pw_uid
        except KeyError as exc:
            parser.error("defina VHACKINTOSH_WEB_UID ou instale o usuário vhackintosh-web")
    try:
        web_gid = grp.getgrnam(args.web_group).gr_gid
    except KeyError as exc:
        parser.error(f"grupo local RPC ausente: {args.web_group}")
    unit_manager = None
    if os.environ.get("VHACKINTOSH_VM_SUPERVISOR_STATUS") == "1":
        systemd_scope = os.environ.get("VHACKINTOSH_VM_SYSTEMD_SCOPE", "system")
        unit_manager = SystemdVmUnitManager(timeout=2.0, scope=systemd_scope)
    service = WebAgentService(args.catalog, jobs_db_path=args.jobs_db, vm_unit_manager=unit_manager)
    service.start_jobs()
    server = UnixRpcServer(
        args.socket,
        service.handlers(),
        allowed_uids={int(web_uid)},
        socket_gid=web_gid,
        socket_mode=0o660,
        max_clients=8,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        service.close_jobs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
