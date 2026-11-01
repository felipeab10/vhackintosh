"""Typed adapter between the HTTP API and the local Unix RPC transport."""

from __future__ import annotations

from typing import Any

from core.web_rpc import RpcError, UnixRpcClient

from .contracts import (
    HostOverview,
    CapabilitiesResponse,
    GpuInventoryResponse,
    HostUsbInventory,
    JobAccepted,
    JobListResponse,
    VmActionRequest,
    VmConfigEnvelope,
    VmConfigCreate,
    VmConfigUpdate,
    VmDeleteResponse,
    VmListResponse,
    VmRuntimeList,
    VmSerialConsole,
    StorageInventory,
    VmBackupListResponse,
    TelemetrySettingsView,
    ReimsReleaseList,
    ReimsReleaseUpdateRequest,
    VmProvisionRequest,
    VmBackupRequest,
    VmBackupRestoreRequest,
    VmCloneRequest,
    JobDetail,
    MediaConvertRequest,
    AppleRecoveryDownloadRequest,
    ReimsImportCandidateList,
    ReimsImportRequest,
    VmOpenCorePickerRequest,
    VmSmbiosDiagnosticRequest,
)


class RpcAgentClient:
    """Expose only the typed API operations over the versioned local RPC.

    The names below must be explicitly registered by the appliance agent.
    This adapter intentionally provides no arbitrary command/QMP method.
    """

    def __init__(self, rpc: UnixRpcClient):
        self.rpc = rpc

    def is_connected(self) -> bool:
        try:
            result = self.rpc.call("agent.health")
        except RpcError:
            return False
        return isinstance(result, dict) and result.get("status") == "ok"

    def host_overview(self) -> HostOverview:
        return HostOverview.model_validate(self.rpc.call("host.overview"))

    def runtime_capabilities(self) -> CapabilitiesResponse:
        return CapabilitiesResponse.model_validate(self.rpc.call("host.capabilities"))

    def list_vms(self) -> VmListResponse:
        return VmListResponse.model_validate(self.rpc.call("vms.list"))

    def list_reims_import_candidates(self) -> ReimsImportCandidateList:
        return ReimsImportCandidateList.model_validate(self.rpc.call("vms.import.candidates"))

    def import_reims_vm(self, request: ReimsImportRequest, expected_revision: str) -> VmConfigEnvelope:
        return VmConfigEnvelope.model_validate(self.rpc.call("vms.import", {
            **request.model_dump(mode="json"), "expected_revision": expected_revision,
        }))

    def get_vm_config(self, vm_id: str) -> VmConfigEnvelope | None:
        result = self.rpc.call("vms.config.get", {"vm_id": vm_id})
        return VmConfigEnvelope.model_validate(result) if result is not None else None

    def create_vm_config(self, config: VmConfigCreate, expected_revision: str) -> VmConfigEnvelope:
        result = self.rpc.call("vms.config.create", {
            "config": config.model_dump(mode="json"),
            "expected_revision": expected_revision,
        })
        return VmConfigEnvelope.model_validate(result)

    def update_vm_config(self, vm_id: str, config: VmConfigUpdate, expected_revision: str) -> VmConfigEnvelope:
        result = self.rpc.call("vms.config.update", {
            "vm_id": vm_id,
            "config": config.model_dump(mode="json"),
            "expected_revision": expected_revision,
        })
        return VmConfigEnvelope.model_validate(result)

    def patch_vm_config(
        self, vm_id: str, fields: dict[str, Any], expected_revision: str
    ) -> VmConfigEnvelope:
        result = self.rpc.call("vms.config.patch", {
            "vm_id": vm_id,
            "fields": fields,
            "expected_revision": expected_revision,
        })
        return VmConfigEnvelope.model_validate(result)

    def update_vm_opencore_picker(
        self, vm_id: str, request: VmOpenCorePickerRequest, expected_revision: str
    ) -> JobAccepted:
        return JobAccepted.model_validate(self.rpc.call("vms.opencore.picker", {
            "vm_id": vm_id,
            **request.model_dump(mode="json"),
            "expected_revision": expected_revision,
        }))

    def diagnose_vm_smbios(self, vm_id: str, request: VmSmbiosDiagnosticRequest, expected_revision: str) -> JobAccepted:
        return JobAccepted.model_validate(self.rpc.call("vms.smbios.diagnose", {
            "vm_id": vm_id,
            **request.model_dump(mode="json"),
            "expected_revision": expected_revision,
        }))

    def delete_vm_config(self, vm_id: str, expected_revision: str, delete_disk: bool = False) -> VmDeleteResponse:
        result = self.rpc.call("vms.config.delete", {
            "vm_id": vm_id,
            "expected_revision": expected_revision,
            "delete_disk": delete_disk,
        })
        return VmDeleteResponse.model_validate(result)

    def vm_action(self, vm_id: str, action: VmActionRequest) -> JobAccepted:
        return JobAccepted.model_validate(
            self.rpc.call(
                "vms.action",
                {"vm_id": vm_id, "action": action.model_dump(mode="json")},
            )
        )

    def provision_vm(self, request: VmProvisionRequest) -> JobAccepted:
        result = self.rpc.call("vms.provision", request.model_dump(mode="json"))
        return JobAccepted.model_validate(result)

    def get_job(self, job_id: str) -> JobDetail | None:
        result = self.rpc.call("jobs.get", {"job_id": job_id})
        return JobDetail.model_validate(result) if result is not None else None

    def list_jobs(self, limit: int = 25, offset: int = 0, states: list[str] | None = None) -> JobListResponse:
        result = self.rpc.call("jobs.list", {"limit": limit, "offset": offset, "states": states})
        return JobListResponse.model_validate(result)

    def cancel_job(self, job_id: str) -> JobDetail | None:
        result = self.rpc.call("jobs.cancel", {"job_id": job_id})
        return JobDetail.model_validate(result) if result is not None else None

    def retry_job(self, job_id: str) -> JobAccepted | None:
        result = self.rpc.call("jobs.retry", {"job_id": job_id})
        if result is None:
            return None
        # The agent returns the current detail so the caller can confirm the
        # target exists; the HTTP contract acknowledges the existing job ID.
        detail = JobDetail.model_validate(result)
        return JobAccepted(
            job_id=detail.id,
            events_url=f"/api/v1/jobs/{detail.id}/events",
        )

    def host_metrics(self) -> Any:
        return self.rpc.call("host.metrics")

    def host_metrics_history(
        self, name: str, labels: dict[str, str], resolution_seconds: int,
        since: str | None, until: str | None, limit: int,
    ) -> Any:
        return self.rpc.call("host.metrics.history", {
            "name": name, "labels": labels, "resolution_seconds": resolution_seconds,
            "since": since, "until": until, "limit": limit,
        })

    def get_telemetry_settings(self) -> TelemetrySettingsView:
        return TelemetrySettingsView.model_validate(self.rpc.call("telemetry.settings.get"))

    def set_telemetry_settings(self, history_retention_hours: int) -> TelemetrySettingsView:
        return TelemetrySettingsView.model_validate(self.rpc.call(
            "telemetry.settings.set", {"history_retention_hours": history_retention_hours},
        ))

    def list_reims_releases(self) -> ReimsReleaseList:
        return ReimsReleaseList.model_validate(self.rpc.call("reims.releases.list"))

    def update_reims_release(self, request: ReimsReleaseUpdateRequest) -> JobAccepted:
        return JobAccepted.model_validate(self.rpc.call(
            "reims.release.update", request.model_dump(mode="json"),
        ))

    def host_gpus(self) -> GpuInventoryResponse:
        result = self.rpc.call("host.gpus")
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise ValueError("Resposta inválida para inventário de GPU")
        return GpuInventoryResponse.model_validate(result)

    def host_usb_inventory(self) -> HostUsbInventory:
        result = self.rpc.call("host.usb")
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise ValueError("Resposta inválida para inventário USB")
        return HostUsbInventory.model_validate(result)

    def storage_inventory(self) -> StorageInventory:
        return StorageInventory.model_validate(self.rpc.call("storage.inventory"))

    def import_media_transfer(self, transfer_id: str) -> JobAccepted:
        return JobAccepted.model_validate(self.rpc.call("storage.media.import", {"transfer_id": transfer_id}))

    def convert_managed_media(self, media_id: str, request: MediaConvertRequest) -> JobAccepted:
        return JobAccepted.model_validate(self.rpc.call("storage.media.convert", {
            "media_id": media_id, **request.model_dump(mode="json"),
        }))

    def download_apple_recovery(self, request: AppleRecoveryDownloadRequest) -> JobAccepted:
        return JobAccepted.model_validate(self.rpc.call("storage.apple.download", request.model_dump(mode="json")))

    def create_vm_backup(self, vm_id: str, request: VmBackupRequest) -> JobAccepted:
        return JobAccepted.model_validate(self.rpc.call("vms.backup", {
            "vm_id": vm_id,
            "idempotency_key": request.idempotency_key,
        }))

    def verify_vm_backup(self, vm_id: str, backup_id: str, request: VmBackupRequest) -> JobAccepted:
        return JobAccepted.model_validate(self.rpc.call("vms.backup.verify", {
            "vm_id": vm_id,
            "backup_id": backup_id,
            "idempotency_key": request.idempotency_key,
        }))

    def restore_vm_backup(self, vm_id: str, backup_id: str, request: VmBackupRestoreRequest) -> JobAccepted:
        return JobAccepted.model_validate(self.rpc.call("vms.backup.restore", {
            "vm_id": vm_id,
            "backup_id": backup_id,
            "confirmation": request.confirmation,
            "idempotency_key": request.idempotency_key,
        }))

    def clone_vm(self, vm_id: str, request: VmCloneRequest) -> JobAccepted:
        return JobAccepted.model_validate(self.rpc.call("vms.clone", {
            "vm_id": vm_id,
            "clone_id": request.clone_id,
            "name": request.name,
            "confirmation": request.confirmation,
            "idempotency_key": request.idempotency_key,
        }))

    def list_vm_backups(self, vm_id: str) -> VmBackupListResponse:
        return VmBackupListResponse.model_validate(self.rpc.call("vms.backups.list", {"vm_id": vm_id}))

    def get_vm_runtime(self, vm_id: str) -> Any:
        return self.rpc.call("vms.runtime.get", {"vm_id": vm_id})

    def get_vm_serial_console(self, vm_id: str) -> VmSerialConsole:
        return VmSerialConsole.model_validate(self.rpc.call("vms.serial.get", {"vm_id": vm_id}))

    def list_vm_runtimes(self) -> VmRuntimeList:
        return VmRuntimeList.model_validate(self.rpc.call("vms.runtime.list"))

    def get_vm_metrics(self, vm_id: str) -> Any:
        return self.rpc.call("vms.metrics.get", {"vm_id": vm_id})

    def get_vm_metrics_history(
        self, vm_id: str, name: str, labels: dict[str, str], resolution_seconds: int,
        since: str | None, until: str | None, limit: int,
    ) -> Any:
        return self.rpc.call("vms.metrics.history", {
            "vm_id": vm_id, "name": name, "labels": labels,
            "resolution_seconds": resolution_seconds, "since": since,
            "until": until, "limit": limit,
        })
