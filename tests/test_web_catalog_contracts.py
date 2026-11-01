"""Tests for web-facing VM catalog revisions and API contract models."""

import tempfile
import unittest
from pathlib import Path

from core.config import ConcurrentCatalogUpdate, VMConfig, VMManagerStore
from web.api.contracts import (
    ApiError,
    CapabilitiesResponse,
    HostTelemetry,
    JobDetail,
    JobAccepted,
    QemuDevicePropertyInventory,
    VmConfigEnvelope,
    VmActionRequest,
    VmConfig as ApiVmConfig,
    VmConfigCreate,
    VmConfigDraft,
    VmProvisionRequest,
    VmRuntime,
    VmTelemetry,
)


class WebCatalogContractTests(unittest.TestCase):
    def test_vm_config_round_trips_new_and_unknown_options(self):
        legacy = {
            "id": "legacy",
            "name": "Legacy",
            "macos_version": "tahoe",
            "force_x11": True,
            "display_resolution": "1920x1080",
            "vendor_future_option": {"enabled": True},
        }
        vm = VMConfig.from_dict(legacy)
        self.assertEqual(vm.extra_config["force_x11"], True)
        self.assertEqual(vm.extra_config["vendor_future_option"], {"enabled": True})
        restored = VMConfig.from_dict(vm.to_dict())
        self.assertEqual(restored.to_dict(), vm.to_dict())
        self.assertEqual(restored.power_sync_mode, "vm-only")
        self.assertEqual(restored.qemu_extra_args, [])

    def test_catalog_revision_rejects_stale_writer_without_lost_update(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vms.json"
            first = VMManagerStore(path)
            first.add_vm(VMConfig(id="one", name="One", macos_version="tahoe"))
            stale = VMManagerStore(path)
            current = VMManagerStore(path)
            revision = current.revision
            current.add_vm(
                VMConfig(id="two", name="Two", macos_version="sequoia"),
                expected_revision=revision,
            )
            stale_vm = stale.get_vm("one")
            stale_vm.name = "Stale overwrite"
            with self.assertRaises(ConcurrentCatalogUpdate):
                stale.update_vm(stale_vm, expected_revision=stale.revision)
            reloaded = VMManagerStore(path)
            self.assertEqual(reloaded.get_vm("one").name, "One")
            self.assertIsNotNone(reloaded.get_vm("two"))

    def test_api_contract_projects_config_and_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            store = VMManagerStore(Path(directory) / "vms.json")
            vm = VMConfig(id="macos", name="macOS", macos_version="tahoe")
            store.add_vm(vm)
            response = ApiVmConfig(
                **{
                    **vm.to_dict(),
                    "revision": store.revision,
                }
            )
            self.assertEqual(response.id, "macos")
            self.assertEqual(response.revision, store.revision)

    def test_cpu_model_contract_rejects_qemu_option_injection(self):
        payload = VmConfigCreate.model_json_schema()["examples"][0]
        payload["cpu_model"] = "Skylake-Client,+vmx"
        with self.assertRaises(ValueError):
            VmConfigCreate.model_validate(payload)

    def test_cpu_topology_contract_requires_strict_factors_with_vcpu_product(self):
        payload = VmConfigCreate.model_json_schema()["examples"][0]
        payload.update(cpu_topology={"sockets": 2, "cores": 2, "threads": 1}, vcpus=4)
        self.assertEqual(VmConfigCreate.model_validate(payload).cpu_topology.sockets, 2)
        payload["cpu_topology"] = {"sockets": 2, "cores": 2, "threads": 2}
        with self.assertRaises(ValueError):
            VmConfigCreate.model_validate(payload)
        payload["cpu_topology"] = {"sockets": True, "cores": 2, "threads": 1}
        with self.assertRaises(ValueError):
            VmConfigCreate.model_validate(payload)

    def test_ssh_forward_contract_requires_valid_port_and_enabled_network(self):
        payload = VmConfigCreate.model_json_schema()["examples"][0]
        payload["network_ssh_port"] = 2222
        self.assertEqual(VmConfigCreate.model_validate(payload).network_ssh_port, 2222)
        payload["network_ssh_port"] = 65536
        with self.assertRaises(ValueError):
            VmConfigCreate.model_validate(payload)

    def test_vm_display_count_is_limited_by_reims_schema_range(self):
        payload = VmProvisionRequest.model_json_schema()["examples"][0]
        payload["display_count"] = 8
        self.assertEqual(VmProvisionRequest.model_validate(payload).display_count, 8)
        payload["display_count"] = 9
        with self.assertRaises(ValueError):
            VmProvisionRequest.model_validate(payload)
        payload["network_ssh_port"] = 2222
        payload["network_enabled"] = False
        with self.assertRaises(ValueError):
            VmConfigCreate.model_validate(payload)

    def test_cpu_topology_legacy_catalog_round_trip_omits_unowned_field(self):
        legacy = VMConfig.from_dict({"id": "legacy", "name": "Legacy", "macos_version": "tahoe"})
        self.assertNotIn("cpu_topology", legacy.to_dict())
        custom = VMConfig.from_dict({
            "id": "custom", "name": "Custom", "macos_version": "tahoe", "vcpus": 8,
            "cpu_topology": {"sockets": 2, "cores": 2, "threads": 2},
        })
        self.assertEqual(custom.cpu_topology.sockets, 2)
        self.assertEqual(VMConfig.from_dict(custom.to_dict()).cpu_topology, custom.cpu_topology)
        with self.assertRaises(ValueError):
            VMConfig.from_dict({
                "id": "invalid", "name": "Invalid", "macos_version": "tahoe", "vcpus": 4,
                "cpu_topology": {"sockets": 2, "cores": 2, "threads": 2},
            })

    def test_qemu_device_help_contract_does_not_infer_support_from_unknown(self):
        result = QemuDevicePropertyInventory.model_validate({
            "status": "unknown", "source": "qemu -device example,help",
            "properties": [], "meaning": "A ajuda não estabeleceu disponibilidade.",
        })
        self.assertEqual(result.status, "unknown")
        with self.assertRaises(ValueError):
            QemuDevicePropertyInventory.model_validate({
                "status": "unsupported", "source": "help", "properties": [], "meaning": "desconhecido",
            })

    def test_key_openapi_examples_validate_against_their_typed_contracts(self):
        examples = (
            (ApiError, "code"),
            (VmConfigDraft, "macos_version"),
            (VmConfigCreate, "id"),
            (VmProvisionRequest, "installer_policy"),
            (VmActionRequest, "operation"),
            (JobAccepted, "job_id"),
            (VmConfigEnvelope, "config"),
            (CapabilitiesResponse, "runtime_fingerprint"),
            (JobDetail, "checkpoints"),
            (VmRuntime, "qmp_available"),
            (HostTelemetry, "samples"),
            (VmTelemetry, "vm_id"),
        )
        for contract, required_key in examples:
            with self.subTest(contract=contract.__name__):
                payload = contract.model_json_schema()["examples"][0]
                self.assertIn(required_key, payload)
                contract.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
