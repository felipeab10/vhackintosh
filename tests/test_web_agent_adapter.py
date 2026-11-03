"""Typed HTTP-agent adapter maps only the registered local RPC operations."""

import unittest

from web.api.agent import RpcAgentClient
from web.api.contracts import AppleRecoveryDownloadRequest, ControlReleaseUpdateRequest, MediaConvertRequest, Operation, VmActionRequest, VmBackupRequest, VmConfigCreate, VmConfigUpdate, VmOpenCorePickerRequest, VmSmbiosDiagnosticRequest


class FakeRpc:
    def __init__(self):
        self.calls = []

    def call(self, method, payload=None):
        self.calls.append((method, payload or {}))
        if method == "agent.health":
            return {"status": "ok"}
        if method == "host.overview":
            return {
                "hostname": "appliance",
                "architecture": "x86_64",
                "cpu_model": "CPU",
                "logical_cpus": 8,
                "memory_total_bytes": 1024,
                "memory_available_bytes": 512,
                "uptime_seconds": 20,
            }
        if method == "host.capabilities":
            return {"runtime_fingerprint": "a" * 64, "generated_at": "2026-10-30T00:00:00Z", "items": []}
        if method == "storage.inventory":
            return {"items": [], "free_bytes": 1024, "managed_volume": "Volume das VMs gerenciadas"}
        if method == "control.releases.list":
            return {
                "repository": "felipeab10/vhackintosh", "package_format": "arch",
                "current_release": "control-v1.0.0-beta.13",
                "items": [{
                    "tag": "control-v1.0.0-beta.14", "name": "vHackintosh Control 1.0.0-beta.14",
                    "published_at": "2026-11-03T00:00:00Z", "asset_size_bytes": 1024,
                    "prerelease": True,
                }],
            }
        if method == "control.release.update":
            return {"job_id": "control-update-1", "events_url": "/api/v1/jobs/control-update-1"}
        if method == "storage.media.import":
            return {"job_id": "media-import-1", "events_url": "/api/v1/jobs/media-import-1"}
        if method in {"storage.media.convert", "storage.apple.download"}:
            return {"job_id": "media-operation-1", "events_url": "/api/v1/jobs/media-operation-1"}
        if method == "vms.backup":
            return {"job_id": "backup-1", "events_url": "/api/v1/jobs/backup-1"}
        if method == "vms.backup.verify":
            return {"job_id": "verify-1", "events_url": "/api/v1/jobs/verify-1"}
        if method == "vms.backups.list":
            return {"vm_id": "vm-1", "items": []}
        if method == "vms.config.get":
            return None
        if method == "vms.config.delete":
            return {"id": "vm-1", "deleted": True, "revision": "c" * 64}
        if method == "jobs.list":
            return {"items": [], "limit": 10, "offset": 20, "total": 0}
        if method in {"vms.config.create", "vms.config.update", "vms.config.patch"}:
            return {
                "config": {
                    "id": "vm-1", "name": "macOS", "macos_version": "tahoe", "vcpus": 4,
                    "ram_gb": 8, "disk_size_gb": 64, "smbios": {
                        "model": "iMac20,1", "serial_number": "", "board_serial": "", "smuuid": "", "rom": "",
                    }, "audio_device": "usb-audio", "fullscreen": True, "opencore_show_picker": False,
                    "created_at": "2026-10-01T00:00:00Z", "install_phase": "pending",
                    "install_reboots": 0, "install_evidence": "none", "revision": "a" * 64,
                },
                "revision": "a" * 64,
            }
        if method == "vms.action":
            return {"job_id": "job-1", "events_url": "/api/v1/jobs/job-1/events"}
        if method == "vms.opencore.picker":
            return {"job_id": "picker-job", "events_url": "/api/v1/jobs/picker-job"}
        if method == "vms.smbios.diagnose":
            return {"job_id": "smbios-job", "events_url": "/api/v1/jobs/smbios-job"}
        if method == "host.gpus":
            return {"items": [{
                "pci_bdf": "0000:03:00.0", "vendor_id": "1002", "device_id": "744c",
                "name": "AMD GPU", "kind": "amd", "vulkan_available": False,
                "selection_supported": False,
            }], "stale": False, "message": None}
        if method == "host.metrics":
            return {"samples": []}
        if method in {"host.metrics.history", "vms.metrics.history"}:
            return {"name": "cpu", "resolution_seconds": 10, "points": []}
        if method == "vms.metrics.get":
            return {"vm_id": "vm-1", "samples": []}
        raise AssertionError(f"unexpected RPC method {method}")


class RpcAgentAdapterTests(unittest.TestCase):
    def test_control_release_catalog_and_update_are_exposed_over_typed_rpc(self):
        rpc = FakeRpc()
        agent = RpcAgentClient(rpc)

        releases = agent.list_control_releases()
        self.assertEqual(releases.current_release, "control-v1.0.0-beta.13")
        self.assertEqual(releases.items[0].tag, "control-v1.0.0-beta.14")

        request = ControlReleaseUpdateRequest(
            tag="control-v1.0.0-beta.14",
            idempotency_key="12345678-1234-4234-8234-123456789abc",
        )
        accepted = agent.update_control_release(request)
        self.assertEqual(accepted.job_id, "control-update-1")
        self.assertEqual(rpc.calls, [
            ("control.releases.list", {}),
            ("control.release.update", {
                "tag": "control-v1.0.0-beta.14",
                "idempotency_key": "12345678-1234-4234-8234-123456789abc",
            }),
        ])

    def test_reads_are_validated_as_typed_contracts(self):
        rpc = FakeRpc()
        agent = RpcAgentClient(rpc)
        self.assertTrue(agent.is_connected())
        self.assertEqual(agent.host_overview().hostname, "appliance")
        self.assertEqual(agent.runtime_capabilities().runtime_fingerprint, "a" * 64)
        self.assertIsNone(agent.get_vm_config("vm-1"))
        gpu_inventory = agent.host_gpus()
        self.assertEqual(gpu_inventory.items[0].pci_bdf, "0000:03:00.0")
        self.assertFalse(gpu_inventory.stale)
        self.assertEqual(agent.storage_inventory().free_bytes, 1024)
        self.assertEqual(agent.import_media_transfer("a" * 32).job_id, "media-import-1")
        self.assertEqual(agent.list_vm_backups("vm-1").items, [])
        self.assertEqual(agent.get_vm_metrics("vm-1")["samples"], [])
        self.assertTrue(agent.delete_vm_config("vm-1", "a" * 64).deleted)
        self.assertEqual([method for method, _ in rpc.calls], [
            "agent.health", "host.overview", "host.capabilities", "vms.config.get", "host.gpus", "storage.inventory",
            "storage.media.import", "vms.backups.list", "vms.metrics.get", "vms.config.delete",
        ])

    def test_managed_media_operations_use_narrow_typed_rpc_methods(self):
        rpc = FakeRpc()
        agent = RpcAgentClient(rpc)
        convert = MediaConvertRequest(output_format="qcow2", idempotency_key="12345678-1234-4234-8234-123456789abc")
        apple = AppleRecoveryDownloadRequest(macos_version="sequoia", idempotency_key="12345678-1234-4234-8234-123456789abc")
        self.assertEqual(agent.convert_managed_media("a" * 32, convert).job_id, "media-operation-1")
        self.assertEqual(agent.download_apple_recovery(apple).job_id, "media-operation-1")
        self.assertEqual(rpc.calls, [
            ("storage.media.convert", {"media_id": "a" * 32, "output_format": "qcow2", "idempotency_key": "12345678-1234-4234-8234-123456789abc"}),
            ("storage.apple.download", {"macos_version": "sequoia", "idempotency_key": "12345678-1234-4234-8234-123456789abc"}),
        ])

    def test_actions_use_an_allowlisted_business_method(self):
        rpc = FakeRpc()
        agent = RpcAgentClient(rpc)
        request = VmActionRequest(operation=Operation.START, idempotency_key="start-vm-idem-1")
        self.assertEqual(agent.vm_action("vm-1", request).job_id, "job-1")
        method, payload = rpc.calls[0]
        self.assertEqual(method, "vms.action")
        self.assertEqual(payload["vm_id"], "vm-1")
        self.assertEqual(payload["action"]["operation"], "start")
        self.assertNotIn("shell", payload["action"])

    def test_backup_uses_an_explicit_rpc_method_and_idempotency_key(self):
        rpc = FakeRpc()
        agent = RpcAgentClient(rpc)
        request = VmBackupRequest(idempotency_key="12345678-1234-4234-8234-123456789abc")
        self.assertEqual(agent.create_vm_backup("vm-1", request).job_id, "backup-1")
        self.assertEqual(rpc.calls, [(
            "vms.backup", {"vm_id": "vm-1", "idempotency_key": "12345678-1234-4234-8234-123456789abc"},
        )])

    def test_backup_verification_uses_a_narrow_rpc_method(self):
        rpc = FakeRpc()
        agent = RpcAgentClient(rpc)
        request = VmBackupRequest(idempotency_key="12345678-1234-4234-8234-123456789abc")
        self.assertEqual(agent.verify_vm_backup("vm-1", "a" * 32 + ".vhb", request).job_id, "verify-1")
        self.assertEqual(rpc.calls, [(
            "vms.backup.verify", {
                "vm_id": "vm-1", "backup_id": "a" * 32 + ".vhb",
                "idempotency_key": "12345678-1234-4234-8234-123456789abc",
            },
        )])

    def test_telemetry_history_uses_explicit_allowlisted_calls_and_structured_filters(self):
        rpc = FakeRpc()
        agent = RpcAgentClient(rpc)
        agent.host_metrics_history("host.cpu_percent", {}, 60, None, None, 100)
        agent.get_vm_metrics_history("vm-1", "vm.cpu_percent", {}, 10, None, None, 50)
        self.assertEqual(rpc.calls, [
            ("host.metrics.history", {"name": "host.cpu_percent", "labels": {}, "resolution_seconds": 60,
                "since": None, "until": None, "limit": 100}),
            ("vms.metrics.history", {"vm_id": "vm-1", "name": "vm.cpu_percent", "labels": {},
                "resolution_seconds": 10, "since": None, "until": None, "limit": 50}),
        ])

    def test_jobs_list_uses_typed_contract_and_bounded_filter(self):
        rpc = FakeRpc()
        adapter = RpcAgentClient(rpc)
        result = adapter.list_jobs(10, 20, ["running"])
        self.assertEqual((result.limit, result.offset, result.total), (10, 20, 0))
        self.assertEqual(rpc.calls, [("jobs.list", {"limit": 10, "offset": 20, "states": ["running"]})])

    def test_catalog_mutations_use_only_versioned_allowlisted_rpc_methods(self):
        rpc = FakeRpc()
        adapter = RpcAgentClient(rpc)
        create = VmConfigCreate(id="macos", name="macOS", macos_version="tahoe", vcpus=4, ram_gb=8, disk_size_gb=64)
        update = VmConfigUpdate(name="Renamed", macos_version="tahoe", vcpus=4, ram_gb=8, disk_size_gb=64)
        adapter.create_vm_config(create, '"' + "a" * 64 + '"')
        adapter.update_vm_config("vm-1", update, '"' + "b" * 64 + '"')
        self.assertEqual([method for method, _ in rpc.calls], ["vms.config.create", "vms.config.update"])
        self.assertEqual(rpc.calls[0][1]["expected_revision"], '"' + "a" * 64 + '"')
        self.assertEqual(rpc.calls[1][1]["vm_id"], "vm-1")

    def test_picker_update_uses_allowlisted_typed_rpc(self):
        rpc = FakeRpc()
        adapter = RpcAgentClient(rpc)
        request = VmOpenCorePickerRequest(
            boot_selection=True,
            confirmation="vm-1",
            idempotency_key="00000000-0000-4000-8000-000000000001",
        )
        accepted = adapter.update_vm_opencore_picker("vm-1", request, '"' + "b" * 64 + '"')
        self.assertEqual(accepted.job_id, "picker-job")
        self.assertEqual(rpc.calls, [("vms.opencore.picker", {
            "vm_id": "vm-1", "boot_selection": True, "confirmation": "vm-1",
            "idempotency_key": "00000000-0000-4000-8000-000000000001",
            "expected_revision": '"' + "b" * 64 + '"',
        })])

    def test_smbios_diagnostics_uses_allowlisted_typed_rpc(self):
        rpc = FakeRpc()
        adapter = RpcAgentClient(rpc)
        request = VmSmbiosDiagnosticRequest(idempotency_key="00000000-0000-4000-8000-000000000002")
        accepted = adapter.diagnose_vm_smbios("vm-1", request, '"' + "b" * 64 + '"')
        self.assertEqual(accepted.job_id, "smbios-job")
        self.assertEqual(rpc.calls, [("vms.smbios.diagnose", {
            "vm_id": "vm-1", "idempotency_key": "00000000-0000-4000-8000-000000000002",
            "expected_revision": '"' + "b" * 64 + '"',
        })])

    def test_install_confirmation_uses_typed_allowlisted_patch_rpc(self):
        rpc = FakeRpc()
        adapter = RpcAgentClient(rpc)
        adapter.patch_vm_config("vm-1", {"install_phase": "installed", "install_evidence": "complete"}, '"' + "b" * 64 + '"')
        self.assertEqual(rpc.calls, [("vms.config.patch", {
            "vm_id": "vm-1",
            "fields": {"install_phase": "installed", "install_evidence": "complete"},
            "expected_revision": '"' + "b" * 64 + '"',
        })])


if __name__ == "__main__":
    unittest.main()
