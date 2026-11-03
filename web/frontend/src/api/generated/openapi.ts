/**
 * Generated from the live FastAPI OpenAPI schema. Do not edit by hand.
 * Regenerate with `npm run api:types` and check with `npm run api:types:check`.
 */
export interface components {
  schemas: {
    "AdditionalDiskConfig": {
  "media_id"?: (string) | (null);
  "path": string;
  "format": "qcow2" | "raw";
  "controller": "ahci" | "nvme";
  "order": number;
};
    "AdditionalDiskDraft": {
  "media_id": string;
  "controller": "ahci" | "nvme";
  "order": number;
};
    "ApiError": {
  "code": string;
  "message": string;
  "request_id"?: (string) | (null);
  "details"?: {
  [key: string]: unknown;
};
};
    "AppleRecoveryDownloadRequest": {
  "macos_version": "tahoe" | "sequoia" | "ventura";
  "idempotency_key": string;
};
    "AuditEvent": {
  "created_at": string;
  "actor": string;
  "action": string;
  "target": string;
  "outcome": number;
  "request_id": string;
  "operation_id": string;
};
    "AuditListResponse": {
  "items": Array<components["schemas"]["AuditEvent"]>;
  "limit": number;
};
    "BootstrapRequest": {
  "code": string;
  "username": string;
  "password": string;
};
    "CapabilitiesResponse": {
  "runtime_fingerprint": string;
  "generated_at": string;
  "items": Array<components["schemas"]["Capability"]>;
  "runtime"?: (components["schemas"]["RuntimeInventory"]) | (null);
};
    "Capability": {
  "id": string;
  "label": string;
  "supported": boolean;
  "available": boolean;
  "requires_stopped_vm"?: boolean;
  "description"?: (string) | (null);
  "limitation"?: (string) | (null);
};
    "ControlRelease": {
  "tag": string;
  "name": string;
  "published_at"?: (string) | (null);
  "asset_size_bytes": number;
  "prerelease"?: boolean;
};
    "ControlReleaseList": {
  "repository": string;
  "package_format": "arch" | "deb" | "rpm";
  "current_release": string;
  "items": Array<components["schemas"]["ControlRelease"]>;
};
    "ControlReleaseUpdateRequest": {
  "idempotency_key": string;
  "tag": string;
};
    "CpuTopology": {
  "sockets": number;
  "cores": number;
  "threads": number;
};
    "GpuAssignment": {
  "configured_bdf"?: (string) | (null);
  "configured_uuid"?: (string) | (null);
  "configured_device_uuid"?: (string) | (null);
  "configured_pci_address"?: (string) | (null);
  "configured_driver_file"?: (string) | (null);
  "effective_bdf"?: (string) | (null);
  "effective_uuid"?: (string) | (null);
  "verification": components["schemas"]["GpuVerification"];
  "message"?: (string) | (null);
};
    "GpuDevice": {
  "pci_bdf": string;
  "vendor_id": string;
  "device_id": string;
  "name": string;
  "kind": string;
  "render_node"?: (string) | (null);
  "vulkan_uuid"?: (string) | (null);
  "driver_file"?: (string) | (null);
  "reims_device_uuid"?: (string) | (null);
  "reims_pci_address"?: (string) | (null);
  "reims_shares_driver"?: (boolean) | (null);
  "vulkan_available": boolean;
  "used_by_host"?: (boolean) | (null);
  "assigned_vm_id"?: (string) | (null);
  "selection_supported": boolean;
  "limitation"?: (string) | (null);
};
    "GpuInventoryResponse": {
  "items": Array<components["schemas"]["GpuDevice"]>;
  "stale"?: boolean;
  "message"?: (string) | (null);
};
    "GpuVerification": "confirmed" | "unavailable" | "mismatch" | "ambiguous";
    "HTTPValidationError": {
  "detail"?: Array<components["schemas"]["ValidationError"]>;
};
    "Health": {
  "status": "ok" | "degraded" | "unavailable";
  "version": string;
  "agent_connected": boolean;
  "checked_at": string;
};
    "HostAlert": {
  "id": string;
  "code": "cpu.high" | "memory.high" | "temperature.high" | "filesystem.low";
  "title": string;
  "severity": "warning" | "critical";
  "unit": "%" | "°C";
  "value": number;
  "first_seen": string;
  "last_seen": string;
  "acknowledged_at"?: (string) | (null);
  "cleared_at"?: (string) | (null);
};
    "HostAlertList": {
  "items": Array<components["schemas"]["HostAlert"]>;
  "persistence_enabled": boolean;
  "limit": number;
};
    "HostOverview": {
  "hostname": string;
  "architecture": string;
  "cpu_model": string;
  "logical_cpus": number;
  "memory_total_bytes": number;
  "memory_available_bytes": number;
  "uptime_seconds": number;
  "addresses"?: Array<string>;
  "web_port"?: number;
  "tls_fingerprint"?: (string) | (null);
};
    "HostTelemetry": {
  "samples": Array<components["schemas"]["TelemetrySample"]>;
};
    "HostUsbDevice": {
  "device_id": string;
  "label": string;
  "device_count": number;
  "auto_attach_supported": boolean;
  "limitation"?: (string) | (null);
};
    "HostUsbInventory": {
  "items": Array<components["schemas"]["HostUsbDevice"]>;
  "stale"?: boolean;
  "message"?: (string) | (null);
};
    "InstallPhase": "pending" | "installing" | "installed";
    "InstallerPolicy": "cached" | "download" | "none";
    "JobAccepted": {
  "job_id": string;
  "state"?: components["schemas"]["JobState"];
  "events_url": string;
};
    "JobCheckpoint": {
  "name": string;
  "payload"?: {
  [key: string]: unknown;
};
  "completed": boolean;
  "updated_at": string;
};
    "JobDetail": {
  "id": string;
  "kind": string;
  "state": "queued" | "running" | "succeeded" | "failed" | "cancelling" | "cancelled" | "interrupted";
  "stage": string;
  "progress_percent"?: (number) | (null);
  "created_at": string;
  "updated_at": string;
  "result"?: {
  [key: string]: unknown;
};
  "error"?: (components["schemas"]["ApiError"]) | (null);
  "cancellable"?: boolean;
  "retryable"?: boolean;
  "checkpoints"?: Array<components["schemas"]["JobCheckpoint"]>;
  "logs"?: Array<components["schemas"]["JobLog"]>;
};
    "JobListResponse": {
  "items": Array<components["schemas"]["JobSummary"]>;
  "limit": number;
  "offset": number;
  "total": number;
};
    "JobLog": {
  "id": number;
  "created_at": string;
  "level": "debug" | "info" | "warning" | "error";
  "message": string;
};
    "JobState": "queued" | "running" | "succeeded" | "failed" | "cancelling" | "cancelled" | "interrupted";
    "JobSummary": {
  "id": string;
  "kind": string;
  "state": "queued" | "running" | "succeeded" | "failed" | "cancelling" | "cancelled" | "interrupted";
  "stage": string;
  "progress_percent"?: (number) | (null);
  "target_vm_id"?: (string) | (null);
  "created_at": string;
  "updated_at": string;
  "cancellable"?: boolean;
  "retryable"?: boolean;
};
    "LoginRequest": {
  "username": string;
  "password": string;
};
    "MediaConvertRequest": {
  "output_format": "raw" | "qcow2";
  "idempotency_key": string;
};
    "Operation": "start" | "shutdown" | "force_stop" | "restart" | "pause" | "resume";
    "PasswordChangeRequest": {
  "current_password": string;
  "new_password": string;
};
    "PrivateTransferListResponse": {
  "items": Array<components["schemas"]["PrivateTransferView"]>;
  "max_file_bytes": number;
  "max_total_bytes": number;
  "min_free_bytes": number;
};
    "PrivateTransferView": {
  "id": string;
  "filename": string;
  "size_bytes": number;
  "format": "iso" | "qcow2" | "dmg";
};
    "QemuDevicePropertyInventory": {
  "status": "reported" | "unknown";
  "source": string;
  "properties"?: Array<string>;
  "meaning": string;
};
    "ReimsImportCandidate": {
  "id": string;
  "name": string;
  "directory": string;
  "disk_path": string;
  "vcpus": number;
  "ram_gb": number;
  "disk_size_gb": number;
  "mac"?: (string) | (null);
  "display_resolution": string;
  "display_hidpi": boolean;
  "importable": boolean;
  "runtime_state"?: "running" | "stopped" | "unknown";
  "runtime_pid"?: (number) | (null);
  "limitation"?: (string) | (null);
};
    "ReimsImportCandidateList": {
  "items": Array<components["schemas"]["ReimsImportCandidate"]>;
};
    "ReimsImportRequest": {
  "id": string;
  "macos_version": "tahoe" | "sequoia" | "ventura";
};
    "ReimsRelease": {
  "tag": string;
  "name": string;
  "published_at"?: (string) | (null);
  "asset_size_bytes": number;
  "prerelease"?: boolean;
};
    "ReimsReleaseList": {
  "repository": string;
  "current_release"?: (string) | (null);
  "active_release"?: (string) | (null);
  "items": Array<components["schemas"]["ReimsRelease"]>;
};
    "ReimsReleaseUpdateRequest": {
  "idempotency_key": string;
  "tag": string;
};
    "RuntimeInventory": {
  "status": "available" | "partial" | "missing";
  "reims_version"?: (string) | (null);
  "reims_release_tag"?: (string) | (null);
  "reims_schema_version"?: (number) | (null);
  "reims_schema_features"?: Array<string>;
  "qemu_version"?: (string) | (null);
  "qemu_options"?: Array<string>;
  "qemu_command_options"?: Array<string>;
  "qemu_devices"?: Array<string>;
  "qemu_machine_types"?: Array<string>;
  "qemu_cpu_models"?: Array<string>;
  "qemu_audio_backends"?: Array<string>;
  "qemu_device_categories"?: {
  [key: string]: Array<string>;
};
  "qemu_device_properties"?: {
  [key: string]: components["schemas"]["QemuDevicePropertyInventory"];
};
  "reims_set_options"?: Array<string>;
  "reims_create_options"?: Array<string>;
  "reims_update_opencore_picker"?: boolean;
  "capability_inventory"?: {
  [key: string]: unknown;
};
  "binary_fingerprint": string;
};
    "SessionView": {
  "authenticated": boolean;
  "username"?: (string) | (null);
  "expires_at"?: (string) | (null);
  "bootstrap_required"?: boolean;
  "must_change_password"?: boolean;
  "default_credentials_available"?: boolean;
  "tls_fingerprint"?: (string) | (null);
  "reims_update_status"?: ("queued" | "unavailable") | (null);
  "reims_update_job_id"?: (string) | (null);
};
    "SmbiosConfig": {
  "model": string;
  "serial_number": string;
  "board_serial": string;
  "smuuid": string;
  "rom": string;
};
    "StorageInventory": {
  "items": Array<components["schemas"]["StorageItem"]>;
  "free_bytes"?: (number) | (null);
  "managed_volume": string;
};
    "StorageItem": {
  "id": string;
  "name": string;
  "location": "managed" | "external";
  "availability": "available" | "missing" | "external";
  "virtual_size_bytes"?: (number) | (null);
  "allocated_bytes"?: (number) | (null);
  "references"?: Array<string>;
};
    "TelemetryHistory": {
  "name": string;
  "resolution_seconds": 10 | 60;
  "points": Array<components["schemas"]["TelemetryHistoryPoint"]>;
  "vm_id"?: (string) | (null);
};
    "TelemetryHistoryPoint": {
  "timestamp": string;
  "resolution_seconds": 10 | 60;
  "average"?: (number) | (null);
  "minimum"?: (number) | (null);
  "maximum"?: (number) | (null);
  "sample_count": number;
  "unavailable_count": number;
  "availability": "available" | "partial" | "unavailable";
  "unit": string;
  "source": string;
  "labels"?: {
  [key: string]: string;
};
};
    "TelemetrySample": {
  "name": string;
  "value"?: (number) | (number) | (string) | (null);
  "timestamp": string;
  "unit": string;
  "source": string;
  "quality"?: string;
  "availability"?: "available" | "unavailable";
  "labels"?: {
  [key: string]: string;
};
};
    "TelemetrySettingsUpdate": {
  "history_retention_hours": 1 | 6 | 12 | 24;
};
    "TelemetrySettingsView": {
  "history_retention_hours": 1 | 6 | 12 | 24;
  "history_persistence_enabled": boolean;
};
    "ValidationError": {
  "loc": Array<(string) | (number)>;
  "msg": string;
  "type": string;
  "input"?: unknown;
  "ctx"?: {
};
};
    "VmActionRequest": {
  "operation": components["schemas"]["Operation"];
  "expected_revision"?: (string) | (null);
  "confirmation"?: (string) | (null);
  "idempotency_key": string;
};
    "VmBackupItem": {
  "backup_id": string;
  "created_at": string;
  "size_bytes": number;
  "file_count": number;
  "metadata_valid": boolean;
  "integrity_note": string;
};
    "VmBackupListResponse": {
  "vm_id": string;
  "items": Array<components["schemas"]["VmBackupItem"]>;
};
    "VmBackupRequest": {
  "idempotency_key": string;
};
    "VmBackupRestoreRequest": {
  "idempotency_key": string;
  "confirmation": string;
};
    "VmCloneRequest": {
  "idempotency_key": string;
  "clone_id": string;
  "name": string;
  "confirmation": string;
};
    "VmConfig": {
  "id": string;
  "name": string;
  "macos_version": string;
  "vcpus": number;
  "cpu_model"?: string;
  "cpu_topology"?: (components["schemas"]["CpuTopology"]) | (null);
  "ram_gb": number;
  "disk_size_gb": number;
  "disk_path"?: string;
  "additional_disks"?: Array<components["schemas"]["AdditionalDiskConfig"]>;
  "auto_start"?: boolean;
  "smbios": components["schemas"]["SmbiosConfig"];
  "gpu_mode"?: string;
  "vfio_pci_id"?: (string) | (null);
  "selected_gpu"?: (string) | (null);
  "selected_gpu_name"?: (string) | (null);
  "selected_gpu_uuid"?: (string) | (null);
  "selected_gpu_device_uuid"?: (string) | (null);
  "selected_gpu_pci_address"?: (string) | (null);
  "selected_gpu_driver_file"?: (string) | (null);
  "audio_device": string;
  "audio_enabled"?: boolean;
  "audio_backend"?: string;
  "network_enabled"?: boolean;
  "network_mac"?: (string) | (null);
  "network_ssh_port"?: (number) | (null);
  "fullscreen": boolean;
  "display_resolution"?: string;
  "display_count"?: number;
  "display_hidpi"?: boolean;
  "display_refresh_hz"?: (number) | (null);
  "opencore_show_picker": boolean;
  "created_at": string;
  "last_booted_at"?: (string) | (null);
  "install_phase": components["schemas"]["InstallPhase"];
  "install_reboots": number;
  "install_evidence": string;
  "usb_auto_attach"?: Array<string>;
  "qemu_extra_args"?: Array<string>;
  "extra_config"?: {
  [key: string]: unknown;
};
  "power_sync_mode"?: "vm-only" | "kiosk";
  "revision": string;
};
    "VmConfigCreate": {
  "name": string;
  "macos_version": string;
  "vcpus": number;
  "cpu_model"?: string;
  "cpu_topology"?: (components["schemas"]["CpuTopology"]) | (null);
  "ram_gb": number;
  "disk_size_gb": number;
  "additional_disks"?: Array<components["schemas"]["AdditionalDiskDraft"]>;
  "auto_start"?: boolean;
  "audio_device"?: "usb-audio" | "virtio-sound";
  "audio_enabled"?: boolean;
  "audio_backend"?: string;
  "fullscreen"?: boolean;
  "display_resolution"?: string;
  "display_count"?: number;
  "display_hidpi"?: boolean;
  "display_refresh_hz"?: (number) | (null);
  "opencore_show_picker"?: boolean;
  "gpu_mode"?: "reims-vgpu";
  "selected_gpu"?: (string) | (null);
  "selected_gpu_uuid"?: (string) | (null);
  "selected_gpu_device_uuid"?: (string) | (null);
  "network_enabled"?: boolean;
  "network_mac"?: (string) | (null);
  "network_ssh_port"?: (number) | (null);
  "power_sync_mode"?: "vm-only";
  "qemu_extra_args"?: Array<string>;
  "usb_auto_attach"?: Array<string>;
  "id": string;
};
    "VmConfigDifference": {
  "field": string;
  "saved_value": string;
  "last_projection_value": string;
};
    "VmConfigEnvelope": {
  "config": components["schemas"]["VmConfig"];
  "revision": string;
  "runtime_config_differs"?: boolean;
  "pending_restart_fields"?: Array<string>;
  "projection_status"?: "available" | "unknown";
  "config_differences"?: Array<components["schemas"]["VmConfigDifference"]>;
};
    "VmConfigUpdate": {
  "name": string;
  "macos_version": string;
  "vcpus": number;
  "cpu_model"?: string;
  "cpu_topology"?: (components["schemas"]["CpuTopology"]) | (null);
  "ram_gb": number;
  "disk_size_gb": number;
  "additional_disks"?: Array<components["schemas"]["AdditionalDiskDraft"]>;
  "auto_start"?: boolean;
  "audio_device"?: "usb-audio" | "virtio-sound";
  "audio_enabled"?: boolean;
  "audio_backend"?: string;
  "fullscreen"?: boolean;
  "display_resolution"?: string;
  "display_count"?: number;
  "display_hidpi"?: boolean;
  "display_refresh_hz"?: (number) | (null);
  "opencore_show_picker"?: boolean;
  "gpu_mode"?: "reims-vgpu";
  "selected_gpu"?: (string) | (null);
  "selected_gpu_uuid"?: (string) | (null);
  "selected_gpu_device_uuid"?: (string) | (null);
  "network_enabled"?: boolean;
  "network_mac"?: (string) | (null);
  "network_ssh_port"?: (number) | (null);
  "power_sync_mode"?: "vm-only";
  "qemu_extra_args"?: Array<string>;
  "usb_auto_attach"?: Array<string>;
};
    "VmDeleteRequest": {
  "confirmation": string;
  "delete_disk"?: boolean;
};
    "VmDeleteResponse": {
  "id": string;
  "deleted": true;
  "revision": string;
};
    "VmInstallConfirmRequest": {
  "confirmation": string;
};
    "VmListResponse": {
  "items": Array<components["schemas"]["VmConfigEnvelope"]>;
  "revision": string;
};
    "VmOpenCorePickerRequest": {
  "boot_selection": boolean;
  "confirmation": string;
  "idempotency_key": string;
};
    "VmProvisionRequest": {
  "idempotency_key": string;
  "id"?: (string) | (null);
  "name": string;
  "macos_version": string;
  "vcpus": number;
  "cpu_model"?: string;
  "cpu_topology"?: (components["schemas"]["CpuTopology"]) | (null);
  "ram_gb": number;
  "disk_size_gb"?: number;
  "auto_start"?: boolean;
  "selected_gpu_bdf"?: (string) | (null);
  "selected_gpu_uuid"?: (string) | (null);
  "selected_gpu_device_uuid"?: (string) | (null);
  "installer_policy": components["schemas"]["InstallerPolicy"];
  "fullscreen"?: boolean;
  "display_resolution"?: string;
  "display_count"?: number;
  "display_hidpi"?: boolean;
  "display_refresh_hz"?: (number) | (null);
  "opencore_show_picker"?: (boolean) | (null);
  "usb_auto_attach"?: Array<string>;
  "qemu_extra_args"?: Array<string>;
  "audio_backend"?: string;
  "audio_enabled"?: boolean;
  "audio_device"?: "usb-audio" | "virtio-sound";
  "network_enabled"?: boolean;
  "network_mac"?: (string) | (null);
  "network_ssh_port"?: number;
};
    "VmRunState": "stopped" | "starting" | "running" | "stopping" | "degraded" | "unknown";
    "VmRuntime": {
  "id": string;
  "state": components["schemas"]["VmRunState"];
  "pid"?: (number) | (null);
  "process_started_at"?: (string) | (null);
  "qmp_available": boolean;
  "install_phase": components["schemas"]["InstallPhase"];
  "supervisor"?: (components["schemas"]["VmSupervisorStatus"]) | (null);
  "gpu"?: (components["schemas"]["GpuAssignment"]) | (null);
  "observed_at": string;
  "message"?: (string) | (null);
};
    "VmRuntimeList": {
  "items": Array<components["schemas"]["VmRuntime"]>;
  "observed_at": string;
};
    "VmSerialConsole": {
  "vm_id": string;
  "available": boolean;
  "content": string;
  "updated_at"?: (string) | (null);
  "truncated"?: boolean;
  "message"?: (string) | (null);
};
    "VmSmbiosDiagnosticRequest": {
  "idempotency_key": string;
};
    "VmSupervisorStatus": {
  "unit": string;
  "load_state": string;
  "active_state": string;
  "sub_state": string;
  "main_pid"?: (number) | (null);
  "main_process_started_at"?: (string) | (null);
  "result": string;
};
    "VmTelemetry": {
  "vm_id": string;
  "samples": Array<components["schemas"]["TelemetrySample"]>;
  "message"?: (string) | (null);
};
    "WebActivationSubmitResponse": {
  "status": "pending" | "approved" | "rejected";
  "updated_at": string;
};
    "WebActivationView": {
  "appliance_id": string;
  "activation_code": string;
  "status": "not_submitted" | "pending" | "approved" | "rejected";
  "updated_at": string;
  "portal_url": string;
  "inventory_changed"?: boolean;
};
  };
}

export interface operations {
  "list_transfers_api_v1_transfers_get": {
  responses: {
    "200": {
      content: components["schemas"]["PrivateTransferListResponse"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "upload_transfer_api_v1_transfers_post": {
  header: {
    "X-Transfer-Filename": string;
    "X-Transfer-Format": "iso" | "qcow2" | "dmg";
  };
  responses: {
    "201": {
      content: components["schemas"]["PrivateTransferView"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "download_transfer_api_v1_transfers__transfer_id__content_get": {
  path: {
    "transfer_id": string;
  };
  responses: {
    "200": {
      content: unknown;
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "delete_transfer_api_v1_transfers__transfer_id__delete": {
  path: {
    "transfer_id": string;
  };
  responses: {
    "204": {
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "health_api_v1_health_get": {
  responses: {
    "200": {
      content: components["schemas"]["Health"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "session_view_api_v1_session_get": {
  responses: {
    "200": {
      content: components["schemas"]["SessionView"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "list_audit_api_v1_audit_get": {
  query: {
    "limit"?: number;
  };
  responses: {
    "200": {
      content: components["schemas"]["AuditListResponse"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "bootstrap_api_v1_session_bootstrap_post": {
  requestBody: components["schemas"]["BootstrapRequest"];
  responses: {
    "200": {
      content: components["schemas"]["SessionView"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "login_api_v1_session_login_post": {
  requestBody: components["schemas"]["LoginRequest"];
  responses: {
    "200": {
      content: components["schemas"]["SessionView"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "logout_api_v1_session_logout_post": {
  responses: {
    "200": {
      content: components["schemas"]["SessionView"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "change_password_api_v1_session_password_post": {
  requestBody: components["schemas"]["PasswordChangeRequest"];
  responses: {
    "200": {
      content: components["schemas"]["SessionView"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "host_overview_api_v1_host_get": {
  responses: {
    "200": {
      content: components["schemas"]["HostOverview"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "web_activation_view_api_v1_activation_get": {
  responses: {
    "200": {
      content: components["schemas"]["WebActivationView"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "submit_web_activation_api_v1_activation_submit_post": {
  responses: {
    "202": {
      content: components["schemas"]["WebActivationSubmitResponse"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "sync_web_activation_api_v1_activation_sync_post": {
  responses: {
    "200": {
      content: components["schemas"]["WebActivationSubmitResponse"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "runtime_capabilities_api_v1_capabilities_get": {
  responses: {
    "200": {
      content: components["schemas"]["CapabilitiesResponse"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "list_vms_api_v1_vms_get": {
  responses: {
    "200": {
      content: components["schemas"]["VmListResponse"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "create_vm_api_v1_vms_post": {
  requestBody: components["schemas"]["VmConfigCreate"];
  responses: {
    "201": {
      content: components["schemas"]["VmConfigEnvelope"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "list_reims_import_candidates_api_v1_vms_import_candidates_get": {
  responses: {
    "200": {
      content: components["schemas"]["ReimsImportCandidateList"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "import_reims_vm_api_v1_vms_import_post": {
  requestBody: components["schemas"]["ReimsImportRequest"];
  responses: {
    "201": {
      content: components["schemas"]["VmConfigEnvelope"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "list_vm_runtimes_api_v1_vms_runtime_get": {
  responses: {
    "200": {
      content: components["schemas"]["VmRuntimeList"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "get_vm_config_api_v1_vms__vm_id__config_get": {
  path: {
    "vm_id": string;
  };
  responses: {
    "200": {
      content: components["schemas"]["VmConfigEnvelope"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "update_vm_api_v1_vms__vm_id__config_put": {
  path: {
    "vm_id": string;
  };
  requestBody: components["schemas"]["VmConfigUpdate"];
  responses: {
    "200": {
      content: components["schemas"]["VmConfigEnvelope"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "confirm_vm_installation_api_v1_vms__vm_id__install_confirm_post": {
  path: {
    "vm_id": string;
  };
  requestBody: components["schemas"]["VmInstallConfirmRequest"];
  responses: {
    "200": {
      content: components["schemas"]["VmConfigEnvelope"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "update_vm_opencore_picker_api_v1_vms__vm_id__opencore_picker_post": {
  path: {
    "vm_id": string;
  };
  requestBody: components["schemas"]["VmOpenCorePickerRequest"];
  responses: {
    "202": {
      content: components["schemas"]["JobAccepted"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "diagnose_vm_smbios_api_v1_vms__vm_id__smbios_diagnostics_post": {
  path: {
    "vm_id": string;
  };
  requestBody: components["schemas"]["VmSmbiosDiagnosticRequest"];
  responses: {
    "202": {
      content: components["schemas"]["JobAccepted"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "delete_vm_api_v1_vms__vm_id__delete": {
  path: {
    "vm_id": string;
  };
  requestBody: components["schemas"]["VmDeleteRequest"];
  responses: {
    "200": {
      content: components["schemas"]["VmDeleteResponse"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "provision_vm_api_v1_vms_provision_post": {
  requestBody: components["schemas"]["VmProvisionRequest"];
  responses: {
    "202": {
      content: components["schemas"]["JobAccepted"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "list_jobs_api_v1_jobs_get": {
  query: {
    "limit"?: number;
    "offset"?: number;
    "state"?: (Array<string>) | (null);
  };
  responses: {
    "200": {
      content: components["schemas"]["JobListResponse"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "get_job_api_v1_jobs__job_id__get": {
  path: {
    "job_id": string;
  };
  responses: {
    "200": {
      content: components["schemas"]["JobDetail"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "cancel_job_api_v1_jobs__job_id__cancel_post": {
  path: {
    "job_id": string;
  };
  responses: {
    "200": {
      content: components["schemas"]["JobDetail"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "retry_job_api_v1_jobs__job_id__retry_post": {
  path: {
    "job_id": string;
  };
  responses: {
    "202": {
      content: components["schemas"]["JobAccepted"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "vm_action_api_v1_vms__vm_id__actions_post": {
  path: {
    "vm_id": string;
  };
  requestBody: components["schemas"]["VmActionRequest"];
  responses: {
    "202": {
      content: components["schemas"]["JobAccepted"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "host_metrics_api_v1_host_metrics_get": {
  responses: {
    "200": {
      content: components["schemas"]["HostTelemetry"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "host_alerts_list_api_v1_host_alerts_get": {
  query: {
    "include_cleared"?: boolean;
    "limit"?: number;
  };
  responses: {
    "200": {
      content: components["schemas"]["HostAlertList"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "acknowledge_host_alert_api_v1_host_alerts__alert_id__acknowledge_post": {
  path: {
    "alert_id": string;
  };
  responses: {
    "204": {
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "host_metrics_history_api_v1_host_metrics_history_get": {
  query: {
    "name": string;
    "resolution_seconds"?: number;
    "since"?: (string) | (null);
    "until"?: (string) | (null);
    "limit"?: number;
    "labels"?: (string) | (null);
  };
  responses: {
    "200": {
      content: components["schemas"]["TelemetryHistory"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "host_metrics_stream_api_v1_host_metrics_stream_get": {
  responses: {
    "200": {
      content: unknown;
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "host_gpus_api_v1_host_gpus_get": {
  responses: {
    "200": {
      content: components["schemas"]["GpuInventoryResponse"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "host_usb_inventory_api_v1_host_usb_get": {
  responses: {
    "200": {
      content: components["schemas"]["HostUsbInventory"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "storage_inventory_api_v1_storage_get": {
  responses: {
    "200": {
      content: components["schemas"]["StorageInventory"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "convert_managed_media_api_v1_storage__media_id__convert_post": {
  path: {
    "media_id": string;
  };
  requestBody: components["schemas"]["MediaConvertRequest"];
  responses: {
    "202": {
      content: components["schemas"]["JobAccepted"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "download_apple_recovery_api_v1_storage_apple_recovery_post": {
  requestBody: components["schemas"]["AppleRecoveryDownloadRequest"];
  responses: {
    "202": {
      content: components["schemas"]["JobAccepted"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "get_telemetry_settings_api_v1_settings_telemetry_get": {
  responses: {
    "200": {
      content: components["schemas"]["TelemetrySettingsView"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "update_telemetry_settings_api_v1_settings_telemetry_put": {
  requestBody: components["schemas"]["TelemetrySettingsUpdate"];
  responses: {
    "200": {
      content: components["schemas"]["TelemetrySettingsView"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "list_reims_runtime_releases_api_v1_settings_reims_releases_get": {
  responses: {
    "200": {
      content: components["schemas"]["ReimsReleaseList"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "update_reims_runtime_release_api_v1_settings_reims_releases_post": {
  requestBody: components["schemas"]["ReimsReleaseUpdateRequest"];
  responses: {
    "202": {
      content: components["schemas"]["JobAccepted"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "list_control_runtime_releases_api_v1_settings_control_releases_get": {
  responses: {
    "200": {
      content: components["schemas"]["ControlReleaseList"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "update_control_runtime_release_api_v1_settings_control_releases_post": {
  requestBody: components["schemas"]["ControlReleaseUpdateRequest"];
  responses: {
    "202": {
      content: components["schemas"]["JobAccepted"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "import_transfer_api_v1_transfers__transfer_id__import_post": {
  path: {
    "transfer_id": string;
  };
  responses: {
    "202": {
      content: components["schemas"]["JobAccepted"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "create_vm_backup_api_v1_vms__vm_id__backups_post": {
  path: {
    "vm_id": string;
  };
  requestBody: components["schemas"]["VmBackupRequest"];
  responses: {
    "202": {
      content: components["schemas"]["JobAccepted"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "list_vm_backups_api_v1_vms__vm_id__backups_get": {
  path: {
    "vm_id": string;
  };
  responses: {
    "200": {
      content: components["schemas"]["VmBackupListResponse"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "verify_vm_backup_api_v1_vms__vm_id__backups__backup_id__verify_post": {
  path: {
    "vm_id": string;
    "backup_id": string;
  };
  requestBody: components["schemas"]["VmBackupRequest"];
  responses: {
    "202": {
      content: components["schemas"]["JobAccepted"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "restore_vm_backup_api_v1_vms__vm_id__backups__backup_id__restore_post": {
  path: {
    "vm_id": string;
    "backup_id": string;
  };
  requestBody: components["schemas"]["VmBackupRestoreRequest"];
  responses: {
    "202": {
      content: components["schemas"]["JobAccepted"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "clone_vm_api_v1_vms__vm_id__clone_post": {
  path: {
    "vm_id": string;
  };
  requestBody: components["schemas"]["VmCloneRequest"];
  responses: {
    "202": {
      content: components["schemas"]["JobAccepted"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "vm_runtime_api_v1_vms__vm_id__runtime_get": {
  path: {
    "vm_id": string;
  };
  responses: {
    "200": {
      content: components["schemas"]["VmRuntime"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "vm_serial_console_api_v1_vms__vm_id__serial_get": {
  path: {
    "vm_id": string;
  };
  responses: {
    "200": {
      content: components["schemas"]["VmSerialConsole"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "vm_metrics_api_v1_vms__vm_id__metrics_get": {
  path: {
    "vm_id": string;
  };
  responses: {
    "200": {
      content: components["schemas"]["VmTelemetry"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "vm_metrics_stream_api_v1_vms__vm_id__metrics_stream_get": {
  path: {
    "vm_id": string;
  };
  responses: {
    "200": {
      content: unknown;
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
  "vm_metrics_history_api_v1_vms__vm_id__metrics_history_get": {
  path: {
    "vm_id": string;
  };
  query: {
    "name": string;
    "resolution_seconds"?: number;
    "since"?: (string) | (null);
    "until"?: (string) | (null);
    "limit"?: number;
    "labels"?: (string) | (null);
  };
  responses: {
    "200": {
      content: components["schemas"]["TelemetryHistory"];
    };
    "422": {
      content: components["schemas"]["ApiError"];
    };
    "500": {
      content: components["schemas"]["ApiError"];
    };
  };
};
}

export interface paths {
  "/api/v1/transfers": {
    get: operations["list_transfers_api_v1_transfers_get"];
    post: operations["upload_transfer_api_v1_transfers_post"];
  };
  "/api/v1/transfers/{transfer_id}/content": {
    get: operations["download_transfer_api_v1_transfers__transfer_id__content_get"];
  };
  "/api/v1/transfers/{transfer_id}": {
    delete: operations["delete_transfer_api_v1_transfers__transfer_id__delete"];
  };
  "/api/v1/health": {
    get: operations["health_api_v1_health_get"];
  };
  "/api/v1/session": {
    get: operations["session_view_api_v1_session_get"];
  };
  "/api/v1/audit": {
    get: operations["list_audit_api_v1_audit_get"];
  };
  "/api/v1/session/bootstrap": {
    post: operations["bootstrap_api_v1_session_bootstrap_post"];
  };
  "/api/v1/session/login": {
    post: operations["login_api_v1_session_login_post"];
  };
  "/api/v1/session/logout": {
    post: operations["logout_api_v1_session_logout_post"];
  };
  "/api/v1/session/password": {
    post: operations["change_password_api_v1_session_password_post"];
  };
  "/api/v1/host": {
    get: operations["host_overview_api_v1_host_get"];
  };
  "/api/v1/activation": {
    get: operations["web_activation_view_api_v1_activation_get"];
  };
  "/api/v1/activation/submit": {
    post: operations["submit_web_activation_api_v1_activation_submit_post"];
  };
  "/api/v1/activation/sync": {
    post: operations["sync_web_activation_api_v1_activation_sync_post"];
  };
  "/api/v1/capabilities": {
    get: operations["runtime_capabilities_api_v1_capabilities_get"];
  };
  "/api/v1/vms": {
    get: operations["list_vms_api_v1_vms_get"];
    post: operations["create_vm_api_v1_vms_post"];
  };
  "/api/v1/vms/import-candidates": {
    get: operations["list_reims_import_candidates_api_v1_vms_import_candidates_get"];
  };
  "/api/v1/vms/import": {
    post: operations["import_reims_vm_api_v1_vms_import_post"];
  };
  "/api/v1/vms/runtime": {
    get: operations["list_vm_runtimes_api_v1_vms_runtime_get"];
  };
  "/api/v1/vms/{vm_id}/config": {
    get: operations["get_vm_config_api_v1_vms__vm_id__config_get"];
    put: operations["update_vm_api_v1_vms__vm_id__config_put"];
  };
  "/api/v1/vms/{vm_id}/install/confirm": {
    post: operations["confirm_vm_installation_api_v1_vms__vm_id__install_confirm_post"];
  };
  "/api/v1/vms/{vm_id}/opencore/picker": {
    post: operations["update_vm_opencore_picker_api_v1_vms__vm_id__opencore_picker_post"];
  };
  "/api/v1/vms/{vm_id}/smbios/diagnostics": {
    post: operations["diagnose_vm_smbios_api_v1_vms__vm_id__smbios_diagnostics_post"];
  };
  "/api/v1/vms/{vm_id}": {
    delete: operations["delete_vm_api_v1_vms__vm_id__delete"];
  };
  "/api/v1/vms/provision": {
    post: operations["provision_vm_api_v1_vms_provision_post"];
  };
  "/api/v1/jobs": {
    get: operations["list_jobs_api_v1_jobs_get"];
  };
  "/api/v1/jobs/{job_id}": {
    get: operations["get_job_api_v1_jobs__job_id__get"];
  };
  "/api/v1/jobs/{job_id}/cancel": {
    post: operations["cancel_job_api_v1_jobs__job_id__cancel_post"];
  };
  "/api/v1/jobs/{job_id}/retry": {
    post: operations["retry_job_api_v1_jobs__job_id__retry_post"];
  };
  "/api/v1/vms/{vm_id}/actions": {
    post: operations["vm_action_api_v1_vms__vm_id__actions_post"];
  };
  "/api/v1/host/metrics": {
    get: operations["host_metrics_api_v1_host_metrics_get"];
  };
  "/api/v1/host/alerts": {
    get: operations["host_alerts_list_api_v1_host_alerts_get"];
  };
  "/api/v1/host/alerts/{alert_id}/acknowledge": {
    post: operations["acknowledge_host_alert_api_v1_host_alerts__alert_id__acknowledge_post"];
  };
  "/api/v1/host/metrics/history": {
    get: operations["host_metrics_history_api_v1_host_metrics_history_get"];
  };
  "/api/v1/host/metrics/stream": {
    get: operations["host_metrics_stream_api_v1_host_metrics_stream_get"];
  };
  "/api/v1/host/gpus": {
    get: operations["host_gpus_api_v1_host_gpus_get"];
  };
  "/api/v1/host/usb": {
    get: operations["host_usb_inventory_api_v1_host_usb_get"];
  };
  "/api/v1/storage": {
    get: operations["storage_inventory_api_v1_storage_get"];
  };
  "/api/v1/storage/{media_id}/convert": {
    post: operations["convert_managed_media_api_v1_storage__media_id__convert_post"];
  };
  "/api/v1/storage/apple-recovery": {
    post: operations["download_apple_recovery_api_v1_storage_apple_recovery_post"];
  };
  "/api/v1/settings/telemetry": {
    get: operations["get_telemetry_settings_api_v1_settings_telemetry_get"];
    put: operations["update_telemetry_settings_api_v1_settings_telemetry_put"];
  };
  "/api/v1/settings/reims/releases": {
    get: operations["list_reims_runtime_releases_api_v1_settings_reims_releases_get"];
    post: operations["update_reims_runtime_release_api_v1_settings_reims_releases_post"];
  };
  "/api/v1/settings/control/releases": {
    get: operations["list_control_runtime_releases_api_v1_settings_control_releases_get"];
    post: operations["update_control_runtime_release_api_v1_settings_control_releases_post"];
  };
  "/api/v1/transfers/{transfer_id}/import": {
    post: operations["import_transfer_api_v1_transfers__transfer_id__import_post"];
  };
  "/api/v1/vms/{vm_id}/backups": {
    post: operations["create_vm_backup_api_v1_vms__vm_id__backups_post"];
    get: operations["list_vm_backups_api_v1_vms__vm_id__backups_get"];
  };
  "/api/v1/vms/{vm_id}/backups/{backup_id}/verify": {
    post: operations["verify_vm_backup_api_v1_vms__vm_id__backups__backup_id__verify_post"];
  };
  "/api/v1/vms/{vm_id}/backups/{backup_id}/restore": {
    post: operations["restore_vm_backup_api_v1_vms__vm_id__backups__backup_id__restore_post"];
  };
  "/api/v1/vms/{vm_id}/clone": {
    post: operations["clone_vm_api_v1_vms__vm_id__clone_post"];
  };
  "/api/v1/vms/{vm_id}/runtime": {
    get: operations["vm_runtime_api_v1_vms__vm_id__runtime_get"];
  };
  "/api/v1/vms/{vm_id}/serial": {
    get: operations["vm_serial_console_api_v1_vms__vm_id__serial_get"];
  };
  "/api/v1/vms/{vm_id}/metrics": {
    get: operations["vm_metrics_api_v1_vms__vm_id__metrics_get"];
  };
  "/api/v1/vms/{vm_id}/metrics/stream": {
    get: operations["vm_metrics_stream_api_v1_vms__vm_id__metrics_stream_get"];
  };
  "/api/v1/vms/{vm_id}/metrics/history": {
    get: operations["vm_metrics_history_api_v1_vms__vm_id__metrics_history_get"];
  };
}
