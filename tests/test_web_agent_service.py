from __future__ import annotations

import tempfile
import time
import hashlib
import os
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.config import AdditionalDiskConfig, SMBIOSConfig, VMConfig, VMManagerStore
from core.graphics_profiles import EMBEDDED_KEXTS, GRAPHICS_PROFILES
from core.web_rpc import RpcRemoteError

LEGACY_PROFILE = "apple-gfx-pci"
REIMS_PROFILE = "reims-graphics-pci"


def _profile_kexts(profile):
    return set(EMBEDDED_KEXTS) | {bundle for bundle, _minimum in GRAPHICS_PROFILES[profile]}


def _release_runtime(profile=LEGACY_PROFILE, kext_mode="embedded"):
    return {
        "reims_update_opencore_kext_mode": kext_mode,
        "reims_update_opencore_kext_mode_source": "/managed/reims-util",
        "qemu_graphics_profile": profile,
        "qemu_graphics_profile_source": "/managed/qemu-system-x86_64",
    }
from core.web_agent import WebAgentService, _allowed_agent_client_uids


class _FakeProvisionContext:
    """Minimal JobContext for driving `_run_provision_job` directly."""

    def phase(self, stage, progress=None):
        return None

    def checkpoint(self, stage, payload, **_kwargs):
        return None

    def log(self, message):
        return None

    def check_cancelled(self):
        return None

class FakeTelemetry:
    def sample_host(self):
        return []

    def sample_vm(self, _identity):
        return []

    def sample_qmp(self, _vm_id, _qmp):
        return []


class FakeGpu:
    def as_dict(self):
        return {
            "pci_bdf": "0000:03:00.0", "vendor_id": "1002", "device_id": "744c",
            "name": "AMD GPU", "kind": "amd", "render_node": "/dev/dri/renderD128",
            "vulkan_uuid": None, "driver_file": "amdgpu", "vulkan_available": False,
            "used_by_host": None, "assigned_vm_id": None, "selection_supported": False,
            "limitation": "Reims ainda não oferece seleção por BDF",
        }


class WebAgentServiceTests(unittest.TestCase):
    def test_agent_rpc_allows_web_worker_and_root_cli(self):
        self.assertEqual(_allowed_agent_client_uids(946), {0, 946})

    def test_runtime_gpu_identity_requires_reported_effective_match(self):
        from core.config import VMConfig

        with tempfile.TemporaryDirectory() as directory:
            vm = VMConfig(
                id="vm-gpu", name="GPU", macos_version="tahoe",
                selected_gpu="0000:03:00.0", selected_gpu_uuid="vk-uuid",
                selected_gpu_device_uuid="reims-uuid", selected_gpu_pci_address="0000:03:00.0",
                selected_gpu_driver_file="/icd/amd.json",
            )
            service = WebAgentService(Path(directory) / "vms.json", running_vms=lambda: {})
            exact = service._runtime_for_vm(vm, {}, gpu_snapshot={"vm-gpu": {
                "configured": {"device_uuid": "reims-uuid", "pci_address": "0000:03:00.0", "driver_file": "/icd/amd.json"},
                "effective": {"device_uuid": "reims-uuid", "pci_address": "0000:03:00.0"},
            }})["gpu"]
            self.assertEqual(exact["verification"], "confirmed")
            self.assertEqual(exact["effective_uuid"], "reims-uuid")

            mismatch = service._runtime_for_vm(vm, {}, gpu_snapshot={"vm-gpu": {
                "configured": {"device_uuid": "reims-uuid", "pci_address": "0000:03:00.0", "driver_file": "/icd/amd.json"},
                "effective": {"device_uuid": "another", "pci_address": "0000:04:00.0"},
            }})["gpu"]
            self.assertEqual(mismatch["verification"], "mismatch")
            self.assertIsNone(mismatch["effective_uuid"])

            no_observation = service._runtime_for_vm(vm, {}, gpu_snapshot={"vm-gpu": {
                "configured": {"device_uuid": "reims-uuid", "pci_address": "0000:03:00.0", "driver_file": "/icd/amd.json"},
                "effective": None,
            }})["gpu"]
            self.assertEqual(no_observation["verification"], "unavailable")
            self.assertIsNone(no_observation["effective_bdf"])

    def test_serial_console_reads_only_bounded_redacted_reims_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "vms.json"
            vm = VMConfig(
                id="vm-1", name="Studio", macos_version="tahoe",
                smbios=SMBIOSConfig(serial_number="PRIVATE-SERIAL", board_serial="PRIVATE-MLB"),
            )
            VMManagerStore(catalog).add_vm(vm)
            vm_dir = root / "managed-vm"
            (vm_dir / "logs").mkdir(parents=True)
            (vm_dir / "vm.toml").write_text("[logs]\nenabled = true\n", encoding="utf-8")
            (vm_dir / "logs" / "serial.log").write_text(
                "x" * (64 * 1024) + "\ntoken=private-value PRIVATE-SERIAL\n\x1b[31mboot\x1b[0m\n",
                encoding="utf-8",
            )
            service = WebAgentService(catalog, running_vms=lambda: {})
            try:
                with patch("core.reims_ctl.vm_dir", return_value=vm_dir):
                    result = service.get_vm_serial_console({"vm_id": "vm-1"})
            finally:
                service.close_jobs()
            self.assertTrue(result["available"])
            self.assertTrue(result["truncated"])
            self.assertLessEqual(len(result["content"].encode("utf-8")), 64 * 1024)
            self.assertIn("token=[redacted]", result["content"])
            self.assertIn("[SMBIOS redacted]", result["content"])
            self.assertIn("boot", result["content"])
            self.assertNotIn("\x1b", result["content"])
            self.assertNotIn("private-value", result["content"])

    def test_serial_console_refuses_disabled_logs_and_symlink_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "vms.json"
            VMManagerStore(catalog).add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe"))
            vm_dir = root / "managed-vm"
            (vm_dir / "logs").mkdir(parents=True)
            (vm_dir / "vm.toml").write_text("[logs]\nenabled = false\n", encoding="utf-8")
            outside = root / "private.log"
            outside.write_text("must not be read", encoding="utf-8")
            (vm_dir / "logs" / "serial.log").symlink_to(outside)
            service = WebAgentService(catalog, running_vms=lambda: {})
            try:
                with patch("core.reims_ctl.vm_dir", return_value=vm_dir):
                    disabled = service.get_vm_serial_console({"vm_id": "vm-1"})
                    self.assertFalse(disabled["available"])
                    (vm_dir / "vm.toml").write_text("[logs]\nenabled = true\n", encoding="utf-8")
                    symlink = service.get_vm_serial_console({"vm_id": "vm-1"})
            finally:
                service.close_jobs()
            self.assertFalse(symlink["available"])
            self.assertNotIn("must not be read", symlink["content"])

    def test_projection_diff_reports_gpu_driver_mismatch_only_when_mapping_is_exact(self):
        vm = VMConfig(
            id="vm-1", name="Studio", macos_version="tahoe",
            selected_gpu="0000:01:00.0", selected_gpu_name="RTX",
            selected_gpu_uuid="gpu-uuid-1",
        )
        expected = {"driver_file": "/icd/nvidia.json", "device_id": "0x1234"}

        with patch("core.reims_ctl._gpu_config_para_slot", return_value=expected):
            matching = WebAgentService._projection_differences(vm, {"gpu": dict(expected)})
            mismatching = WebAgentService._projection_differences(
                vm, {"gpu": {"driver_file": "/icd/intel.json"}},
            )

        self.assertFalse(any(item.field == "selected_gpu" for item in matching))
        gpu_diff = next(item for item in mismatching if item.field == "selected_gpu")
        self.assertIn("0000:01:00.0", gpu_diff.saved_value)
        self.assertIn("/icd/intel.json", gpu_diff.last_projection_value)

    def test_projection_diff_treats_omitted_reims_topology_as_one_socket_defaults(self):
        from core.config import CpuTopologyConfig

        vm = VMConfig(
            id="vm-1", name="Studio", macos_version="tahoe", vcpus=4,
            cpu_topology=CpuTopologyConfig(2, 2, 1),
        )
        matching = WebAgentService._projection_differences(vm, {
            "cpus": 4, "q35": {"cpu_model": vm.cpu_model, "cpu_topology": {"sockets": 2, "cores": 2, "threads": 1}},
        })
        self.assertFalse(any(item.field == "cpu_topology" for item in matching))
        mismatching = WebAgentService._projection_differences(vm, {"cpus": 4, "q35": {"cpu_model": vm.cpu_model}})
        topology_diff = next(item for item in mismatching if item.field == "cpu_topology")
        self.assertEqual(topology_diff.saved_value, '{"sockets":2,"cores":2,"threads":1}')
        self.assertEqual(topology_diff.last_projection_value, '{"sockets":1,"cores":4,"threads":1}')

    def test_projection_diff_does_not_guess_gpu_when_mapping_is_unavailable(self):
        vm = VMConfig(
            id="vm-1", name="Studio", macos_version="tahoe",
            selected_gpu="0000:01:00.0", selected_gpu_uuid="gpu-uuid-1",
        )
        with patch("core.reims_ctl._gpu_config_para_slot", return_value=None):
            differences = WebAgentService._projection_differences(
                vm, {"gpu": {"driver_file": "/icd/nvidia.json"}},
            )
        self.assertFalse(any(item.field == "selected_gpu" for item in differences))

    def test_projection_diff_reports_additional_disks_without_exposing_full_paths(self):
        vm = VMConfig(
            id="vm-1", name="Studio", macos_version="tahoe",
            additional_disks=[AdditionalDiskConfig(path="/var/lib/vhackintosh/media/data.raw", format="raw", controller="nvme", order=0)],
        )
        differences = WebAgentService._projection_differences(vm, {
            "additional_disks": [{"path": "/srv/reims/media/other.raw", "format": "raw", "controller": "ahci", "order": 0}],
        })

        disk_diff = next(item for item in differences if item.field == "additional_disks")
        self.assertIn("data.raw", disk_diff.saved_value)
        self.assertIn("other.raw", disk_diff.last_projection_value)
        self.assertNotIn("/var/lib", disk_diff.saved_value)
        self.assertNotIn("/srv/reims", disk_diff.last_projection_value)

    def test_runtime_recovers_live_qemu_by_its_registered_qmp_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            vm = VMConfig(id="vm-1", name="Studio", macos_version="tahoe")
            VMManagerStore(catalog).add_vm(vm)
            qmp_path = Path(directory) / "vm-1" / "qmp.sock"
            process = SimpleNamespace(info={
                "pid": 321, "name": "qemu-system-x86_64",
                "cmdline": ["qemu-system-x86_64", "-qmp", f"unix:{qmp_path},server=on,wait=off"],
            })
            unrelated = SimpleNamespace(info={
                "pid": 654, "name": "qemu-system-x86_64",
                "cmdline": ["qemu-system-x86_64", "-qmp", "unix:/tmp/unmanaged.sock,server=on"],
            })
            service = WebAgentService(catalog, running_vms=lambda: {})
            with patch("core.reims_ctl.qmp_socket", return_value=qmp_path), patch(
                "core.web_agent.psutil.process_iter", return_value=[unrelated, process],
            ):
                recovered, complete = service._discover_qemu_processes([vm])
            self.assertTrue(complete)
            self.assertEqual(recovered, {"vm-1": 321})
            service.close_jobs()

    def test_runtime_does_not_label_live_qemu_stopped_when_unit_is_inactive(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            vm = VMConfig(id="vm-1", name="Studio", macos_version="tahoe")
            VMManagerStore(catalog).add_vm(vm)
            qmp_path = Path(directory) / "vm-1" / "qmp.sock"
            process = SimpleNamespace(info={
                "pid": 321, "name": "qemu-system-x86_64",
                "cmdline": ["qemu-system-x86_64", "-qmp", f"unix:{qmp_path},server=on,wait=off"],
            })
            unit = SimpleNamespace(
                unit="vhackintosh-vm@vm-1.service", load_state="loaded",
                active_state="inactive", sub_state="dead", main_pid=None,
                process_create_time=None, result="success",
            )
            service = WebAgentService(
                catalog, vm_unit_manager=SimpleNamespace(query=lambda _vm_id: unit),
            )
            with patch("core.reims_ctl.vms_rodando", return_value={}), patch(
                "core.reims_ctl.qmp_socket", return_value=qmp_path,
            ), patch("core.web_agent.psutil.process_iter", return_value=[process]):
                runtime = service.get_vm_runtime({"vm_id": "vm-1"})
            self.assertEqual(runtime["state"], "running")
            self.assertEqual(runtime["pid"], 321)
            self.assertFalse(runtime["qmp_available"])
            self.assertIn("unidade de supervisão não está ativa", runtime["message"])
            service.close_jobs()

    def test_qemu_custom_argument_capability_tracks_selected_runtime_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json", running_vms=lambda: {}, gpu_inventory=lambda: [])
            available = {
                "status": "available", "reims_version": "reims-ctl 1", "qemu_version": "QEMU 11",
                "qemu_options": ["-rtc", "-msg"], "binary_fingerprint": "a" * 64,
            }
            with patch("core.runtime_inventory.inspect_runtime", return_value=available):
                capability = next(item for item in service.runtime_capabilities({})["items"] if item["id"] == "vm.qemu_extra_args")
            self.assertTrue(capability["available"])

            partial = {**available, "status": "partial", "qemu_options": ["-rtc"]}
            with patch("core.runtime_inventory.inspect_runtime", return_value=partial):
                capability = next(item for item in service.runtime_capabilities({})["items"] if item["id"] == "vm.qemu_extra_args")
            self.assertFalse(capability["available"])
            service.close_jobs()

    def test_capabilities_distinguish_qemu_inventory_from_reims_audio_network_support(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json", running_vms=lambda: {}, gpu_inventory=lambda: [])
            runtime = {
                "status": "available", "reims_version": "reims-ctl 1", "reims_release_tag": "v1.3.0", "qemu_version": "QEMU 11",
                "qemu_options": ["-rtc", "-msg"], "qemu_devices": ["virtio-net-pci", "virtio-sound-pci"],
                "qemu_audio_backends": ["pipewire"],
                "qemu_cpu_models": ["Skylake-Client-noTSX-IBRS"],
                "reims_set_options": ["--cpus", "--memory", "--network", "--mac", "--audio", "--audio-backend"],
                "reims_create_options": ["--disk-size"],
                "reims_update_opencore_picker": True,
                "reims_update_opencore_kext_mode": "embedded",
                "binary_fingerprint": "a" * 64,
            }
            with patch("core.runtime_inventory.inspect_runtime", return_value=runtime):
                items = {item["id"]: item for item in service.runtime_capabilities({})["items"]}
            self.assertFalse(items["gpu.identity"]["available"])
            self.assertFalse(items["gpu.inventory_json"]["available"])
            self.assertTrue(items["vm.network_disable"]["available"])
            self.assertTrue(items["vm.network.nat"]["available"])
            self.assertFalse(items["vm.network.bridge"]["supported"])
            self.assertFalse(items["vm.network.bridge"]["available"])
            self.assertIn("helper restrito", items["vm.network.bridge"]["limitation"])
            self.assertFalse(items["vm.network.port_forwarding"]["available"])
            self.assertTrue(items["vm.audio.enabled"]["available"])
            self.assertTrue(items["vm.audio.backend"]["available"])
            self.assertTrue(items["vm.cpu.vcpus"]["available"])
            self.assertTrue(items["vm.cpu.model"]["available"])
            self.assertTrue(items["vm.memory"]["available"])
            for capability_id in (
                "vm.cpu.topology",
                "vm.cpu.flags",
                "vm.memory.numa",
                "vm.disks.controllers",
                "vm.display.multiple",
                "vm.pci.passthrough",
                "vm.power_sync.host",
            ):
                with self.subTest(capability_id=capability_id):
                    self.assertFalse(items[capability_id]["supported"])
                    self.assertFalse(items[capability_id]["available"])
                    self.assertTrue(items[capability_id]["limitation"])
            self.assertTrue(items["vm.serial.console"]["supported"])
            self.assertTrue(items["vm.serial.console"]["available"])
            self.assertTrue(items["vm.audio.virtio_device"]["supported"])
            self.assertFalse(items["vm.audio.virtio_device"]["available"])
            self.assertTrue(items["vm.opencore.picker"]["supported"])
            self.assertFalse(items["vm.opencore.picker"]["available"])
            self.assertIn("v1.3.0", items["vm.audio.virtio_device"]["limitation"])

            with patch("core.runtime_inventory.inspect_runtime", return_value={
                **runtime, "reims_set_options": [*runtime["reims_set_options"], "--ssh-port"],
            }):
                forwarding = {
                    item["id"]: item for item in service.runtime_capabilities({})["items"]
                }["vm.network.port_forwarding"]
            self.assertTrue(forwarding["supported"])
            self.assertTrue(forwarding["available"])

            with patch("core.runtime_inventory.inspect_runtime", return_value={
                **runtime, "reims_set_options": [*runtime["reims_set_options"], "--displays"],
            }):
                displays = {
                    item["id"]: item for item in service.runtime_capabilities({})["items"]
                }["vm.display.multiple"]
            self.assertTrue(displays["supported"])
            self.assertTrue(displays["available"])

            with patch("core.runtime_inventory.inspect_runtime", return_value={**runtime, "reims_release_tag": "v1.4.0"}):
                updated = {item["id"]: item for item in service.runtime_capabilities({})["items"]}
            self.assertTrue(updated["vm.audio.virtio_device"]["available"])
            self.assertFalse(items["vm.disks.multiple"]["supported"])
            service.close_jobs()

    def test_gpu_identity_capabilities_follow_only_reims_schema_features(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json", running_vms=lambda: {}, gpu_inventory=lambda: [])
            runtime = {
                "status": "available", "reims_version": "reims-ctl 2", "qemu_version": "QEMU 11",
                "qemu_options": ["-rtc", "-msg"], "qemu_devices": ["virtio-net-pci"],
                "qemu_machine_types": ["q35"], "qemu_cpu_models": ["Skylake-Client-noTSX-IBRS"],
                "qemu_command_options": ["-rtc", "-msg"], "qemu_audio_backends": ["pipewire"],
                "reims_set_options": ["--cpus", "--memory"], "reims_create_options": ["--disk-size"],
                "reims_schema_features": ["gpu_identity", "gpu_inventory_json"],
                "binary_fingerprint": "b" * 64,
            }
            with patch("core.runtime_inventory.inspect_runtime", return_value=runtime):
                items = {item["id"]: item for item in service.runtime_capabilities({})["items"]}
            self.assertTrue(items["gpu.identity"]["available"])
            self.assertTrue(items["gpu.inventory_json"]["available"])
            service.close_jobs()

    def test_cpu_topology_capability_requires_exact_reims_schema_feature(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json", running_vms=lambda: {}, gpu_inventory=lambda: [])
            for features, expected in (([], False), (["cpu_topology"], True), (["cpu_topology_experimental"], False)):
                runtime = {
                    "status": "available", "reims_schema_features": features,
                    "binary_fingerprint": "a" * 64,
                }
                with patch("core.runtime_inventory.inspect_runtime", return_value=runtime):
                    capability = next(item for item in service.runtime_capabilities({})["items"] if item["id"] == "vm.cpu.topology")
                self.assertEqual(capability["supported"], expected)
                self.assertEqual(capability["available"], expected)
            service.close_jobs()

    def test_virtio_audio_selection_requires_the_matching_reims_release_and_qemu_device(self):
        from types import SimpleNamespace

        base = {
            "status": "available", "reims_release_tag": "v1.3.0",
            "qemu_devices": ["virtio-sound-pci"],
            "reims_set_options": ["--audio", "--audio-backend"],
        }
        request = SimpleNamespace(audio_device="virtio-sound")
        with patch("core.runtime_inventory.inspect_runtime", return_value=base):
            with self.assertRaises(RpcRemoteError):
                WebAgentService._validate_audio_device(request)
        with patch("core.runtime_inventory.inspect_runtime", return_value={**base, "reims_release_tag": "v1.4.0"}):
            WebAgentService._validate_audio_device(request)
        with patch("core.runtime_inventory.inspect_runtime", return_value={**base, "reims_release_tag": "v1.4.0", "qemu_devices": []}):
            with self.assertRaises(RpcRemoteError):
                WebAgentService._validate_audio_device(request)

    def test_runtime_reconciles_active_vm_unit_separately_from_guest_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            VMManagerStore(catalog).add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe"))
            unit = SimpleNamespace(
                unit="vhackintosh-vm@vm-1.service", load_state="loaded",
                active_state="active", sub_state="running", main_pid=912,
                process_create_time=1234.5, result="success",
            )
            manager = SimpleNamespace(query=lambda _vm_id: unit)
            service = WebAgentService(catalog, running_vms=lambda: {}, vm_unit_manager=manager)

            runtime = service.get_vm_runtime({"vm_id": "vm-1"})

            self.assertEqual(runtime["state"], "starting")
            self.assertIsNone(runtime["pid"])
            self.assertEqual(runtime["supervisor"]["main_pid"], 912)
            self.assertEqual(runtime["supervisor"]["active_state"], "active")
            self.assertEqual(runtime["install_phase"], "pending")

    def test_runtime_reports_failed_vm_unit_as_degraded(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            VMManagerStore(catalog).add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe"))
            unit = SimpleNamespace(
                unit="vhackintosh-vm@vm-1.service", load_state="loaded",
                active_state="failed", sub_state="failed", main_pid=None,
                process_create_time=None, result="exit-code",
            )
            service = WebAgentService(
                catalog, running_vms=lambda: {},
                vm_unit_manager=SimpleNamespace(query=lambda _vm_id: unit),
            )

            runtime = service.get_vm_runtime({"vm_id": "vm-1"})

            self.assertEqual(runtime["state"], "degraded")
            self.assertEqual(runtime["supervisor"]["result"], "exit-code")
            self.assertIn("unidade do supervisor falhou", runtime["message"])

    def test_runtime_keeps_live_process_running_when_qmp_socket_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            VMManagerStore(catalog).add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe"))
            service = WebAgentService(
                catalog, running_vms=lambda: {"vm-1": 321}, qmp_factory=lambda _vm: None,
            )
            with patch("core.web_agent.psutil.Process") as process:
                process.return_value.create_time.return_value = 1234.5
                runtime = service.get_vm_runtime({"vm_id": "vm-1"})

            self.assertEqual(runtime["state"], "running")
            self.assertEqual(runtime["pid"], 321)
            self.assertFalse(runtime["qmp_available"])
            self.assertIn("QEMU está em execução", runtime["message"])

    def test_open_qmp_does_not_connect_directly_to_an_active_supervised_vm(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"VHACKINTOSH_VM_CONTROL_BASE": directory}
        ):
            catalog = Path(directory) / "vms.json"
            store = VMManagerStore(catalog)
            vm = VMConfig(id="vm-1", name="Studio", macos_version="tahoe")
            store.add_vm(vm)
            service = WebAgentService(
                catalog,
                running_vms=lambda: {"vm-1": 321},
                vm_unit_manager=SimpleNamespace(query=lambda _vm_id: SimpleNamespace(active_state="active")),
            )
            with patch("core.passthrough.QmpClient") as client:
                self.assertIsNone(service._open_qmp(vm))
                client.assert_not_called()

    def test_runtime_does_not_claim_stopped_when_supervisor_query_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            VMManagerStore(catalog).add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe"))
            def unavailable(_vm_id):
                raise OSError("systemd is unavailable")
            service = WebAgentService(
                catalog, running_vms=lambda: {},
                vm_unit_manager=SimpleNamespace(query=unavailable),
            )

            runtime = service.get_vm_runtime({"vm_id": "vm-1"})

            self.assertEqual(runtime["state"], "unknown")
            self.assertIn("unidade de VM", runtime["message"])

    def test_runtime_snapshot_reports_unknown_when_process_inventory_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            VMManagerStore(catalog).add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe"))

            def unavailable():
                raise OSError("process inventory unavailable")

            service = WebAgentService(catalog, running_vms=unavailable)
            snapshot = service.list_vm_runtimes({})
            self.assertEqual(snapshot["items"][0]["state"], "unknown")
            self.assertIn("Não foi possível confirmar", snapshot["items"][0]["message"])

            from core.web_rpc import RpcRemoteError
            with self.assertRaises(RpcRemoteError) as caught:
                service._get_running_vms()
            self.assertEqual(caught.exception.code, "runtime_state_unavailable")

    def test_gpu_inventory_probe_is_shared_between_agent_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            service = WebAgentService(
                Path(directory) / "vms.json",
                gpu_inventory=lambda: calls.append("probe") or [FakeGpu()],
            )
            first = service.host_gpus({})
            second = service.host_gpus({})
            self.assertEqual(first, second)
            self.assertEqual(first["items"][0]["pci_bdf"], "0000:03:00.0")
            self.assertFalse(first["stale"])
            self.assertEqual(calls, ["probe"])

    def test_stale_gpu_inventory_is_visible_but_not_selectable(self):
        with tempfile.TemporaryDirectory() as directory:
            gpu = FakeGpu()
            failing = [False]
            def provider():
                if failing[0]:
                    raise TimeoutError("probe failed")
                return [gpu]
            service = WebAgentService(Path(directory) / "vms.json", gpu_inventory=provider)
            service.gpu_inventory()
            failing[0] = True
            service.gpu_inventory._cached_at = -100
            result = service.host_gpus({})
            self.assertTrue(result["stale"])
            self.assertEqual(result["message"], "Sonda de GPU indisponível (TimeoutError).")
            self.assertEqual(result["items"][0]["pci_bdf"], "0000:03:00.0")
            self.assertFalse(result["items"][0]["selection_supported"])
            self.assertEqual(result["items"][0]["limitation"], result["message"])

    def test_allowlisted_handlers_expose_catalog_host_and_gpu_data(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            store = VMManagerStore(catalog)
            store.add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe", smbios=SMBIOSConfig()))
            service = WebAgentService(
                catalog,
                telemetry=FakeTelemetry(),
                telemetry_history_enabled=True,
                gpu_inventory=lambda: [FakeGpu()],
                running_vms=lambda: {},
            )
            handlers = service.handlers()
            self.assertEqual(set(handlers), {
                "agent.health", "host.overview", "host.metrics", "host.metrics.history", "telemetry.settings.get", "telemetry.settings.set", "reims.releases.list", "reims.release.update", "control.releases.list", "control.release.update", "host.gpus", "host.usb", "host.capabilities", "vms.list",
                "storage.inventory",
                "storage.media.import",
                "storage.media.convert",
                "storage.apple.download",
                "vms.backups.list",
                "vms.backup",
                "vms.backup.verify",
                "vms.backup.restore",
                "vms.clone",
                "vms.opencore.picker",
                "vms.smbios.diagnose",
                "vms.config.get", "vms.config.create", "vms.config.update", "vms.config.patch", "vms.config.delete",
                "vms.autostart.get", "vms.autostart.set",
            "vms.runtime.get", "vms.runtime.list", "vms.serial.get", "vms.metrics.get", "vms.metrics.history", "vms.action", "vms.provision",
            "vms.import.candidates", "vms.import",
                "jobs.list", "jobs.get", "jobs.cancel", "jobs.retry",
            })
            self.assertEqual(handlers["agent.health"]({})["status"], "ok")
            self.assertEqual(handlers["telemetry.settings.get"]({})["history_retention_hours"], 24)
            updated_settings = handlers["telemetry.settings.set"]({"history_retention_hours": 6})
            self.assertEqual(updated_settings["history_retention_hours"], 6)
            with self.assertRaises(RpcRemoteError):
                handlers["telemetry.settings.set"]({"history_retention_hours": 7})
            self.assertEqual(handlers["vms.list"]({})["items"][0]["config"]["id"], "vm-1")
            self.assertEqual(handlers["vms.config.get"]({"vm_id": "vm-1"})["config"]["name"], "Studio")
            self.assertFalse(handlers["host.usb"]({})["stale"])
            self.assertEqual(handlers["vms.runtime.get"]({"vm_id": "vm-1"})["state"], "stopped")
            self.assertEqual(handlers["vms.backups.list"]({"vm_id": "vm-1"}), {"vm_id": "vm-1", "items": []})
            runtime_snapshot = handlers["vms.runtime.list"]({})
            self.assertEqual([item["id"] for item in runtime_snapshot["items"]], ["vm-1"])
            self.assertEqual(runtime_snapshot["items"][0]["state"], "stopped")
            self.assertTrue(runtime_snapshot["observed_at"])
            self.assertEqual(handlers["host.gpus"]({})["items"][0]["pci_bdf"], "0000:03:00.0")
            inventory = handlers["storage.inventory"]({})
            self.assertEqual(inventory["items"], [])
            self.assertIsInstance(inventory["free_bytes"], int)
            capabilities = handlers["host.capabilities"]({})
            by_id = {item["id"]: item for item in capabilities["items"]}
            self.assertEqual(len(capabilities["runtime_fingerprint"]), 64)
            self.assertEqual(handlers["host.capabilities"]({})["runtime_fingerprint"], capabilities["runtime_fingerprint"])
            self.assertFalse(by_id["gpu.exact_selection"]["available"])
            self.assertEqual(by_id["gpu.exact_selection"]["limitation"], "Reims ainda não oferece seleção por BDF")
            self.assertTrue(by_id["vm.lifecycle"]["supported"])
            self.assertFalse(by_id["vm.lifecycle"]["available"])
            self.assertIn("supervisor", by_id["vm.lifecycle"]["limitation"])
            self.assertFalse(by_id["vm.provisioning"]["available"])
            self.assertEqual(handlers["vms.metrics.get"]({"vm_id": "vm-1"})["samples"], [])

    def test_vm_backup_submission_is_idempotent_scoped_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json")

            class Runner:
                def submit(self, **kwargs):
                    self.kwargs = kwargs
                    return ({"id": "backup-job"}, True)

            runner = Runner()
            service.job_runner = runner
            accepted = service.submit_vm_backup_job({
                "vm_id": "macos-vm", "idempotency_key": "12345678-1234-4234-8234-123456789abc",
            })
            self.assertEqual(accepted["job_id"], "backup-job")
            self.assertEqual(runner.kwargs["kind"], "vm.backup")
            self.assertEqual(runner.kwargs["target_vm_id"], "macos-vm")
            self.assertEqual(runner.kwargs["payload"], {"vm_id": "macos-vm"})
            self.assertEqual(runner.kwargs["idempotency_key"], "vm-backup:macos-vm:12345678-1234-4234-8234-123456789abc")
            with self.assertRaises(RpcRemoteError):
                service.submit_vm_backup_job({"vm_id": "../escape", "idempotency_key": "x"})

    def test_vm_backup_restore_submission_requires_target_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json")

            class Runner:
                def submit(self, **kwargs):
                    self.kwargs = kwargs
                    return ({"id": "restore-job"}, True)

            runner = Runner()
            service.job_runner = runner
            backup_id = "a" * 32 + ".vhb"
            with self.assertRaises(RpcRemoteError):
                service.submit_vm_backup_restore_job({
                    "vm_id": "macos-vm", "backup_id": backup_id, "confirmation": "wrong",
                    "idempotency_key": "12345678-1234-4234-8234-123456789abc",
                })
            accepted = service.submit_vm_backup_restore_job({
                "vm_id": "macos-vm", "backup_id": backup_id,
                "confirmation": f"macos-vm:{backup_id}",
                "idempotency_key": "12345678-1234-4234-8234-123456789abc",
            })
            self.assertEqual(accepted["job_id"], "restore-job")
            self.assertEqual(runner.kwargs["kind"], "vm.backup.restore")
            self.assertEqual(runner.kwargs["target_vm_id"], "macos-vm")
            self.assertEqual(runner.kwargs["payload"], {"vm_id": "macos-vm", "backup_id": backup_id})

    def test_vm_clone_submission_requires_source_and_destination_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json")

            class Runner:
                def submit(self, **kwargs):
                    self.kwargs = kwargs
                    return ({"id": "clone-job"}, True)

            runner = Runner()
            service.job_runner = runner
            payload = {
                "vm_id": "studio", "clone_id": "studio-copy", "name": "Studio copy",
                "confirmation": "wrong", "idempotency_key": "12345678-1234-4234-8234-123456789abc",
            }
            with self.assertRaises(RpcRemoteError):
                service.submit_vm_clone_job(payload)
            payload["confirmation"] = "studio:studio-copy"
            accepted = service.submit_vm_clone_job(payload)
            self.assertEqual(accepted["job_id"], "clone-job")
            self.assertEqual(runner.kwargs["kind"], "vm.clone")
            self.assertEqual(runner.kwargs["target_vm_id"], "studio")
            self.assertEqual(runner.kwargs["payload"], {
                "source_vm_id": "studio", "clone_id": "studio-copy", "name": "Studio copy",
            })

    def test_vm_clone_job_reports_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json")
            phases = []

            class Context:
                @staticmethod
                def phase(name, progress=None):
                    phases.append((name, progress))

                @staticmethod
                def check_cancelled():
                    return None

            def clone(self, source_id, clone_id, name, *, check_cancelled, on_progress):
                check_cancelled()
                on_progress(50, 100)
                return {"source_vm_id": source_id, "vm_id": clone_id, "name": name, "created": True}

            with patch("core.vm_clone.VMCloneService.clone", clone):
                result = service._run_vm_clone_job({
                    "source_vm_id": "studio", "clone_id": "studio-copy", "name": "Studio copy",
                }, Context())
            self.assertTrue(result["created"])
            self.assertIn(("Copiando VM e preparando identidade independente", 50.0), phases)
            self.assertEqual(phases[-1], ("Clone independente concluído", 100))

    def test_vm_backup_restore_job_reports_progress_and_result(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json")
            service.backup_root = Path(directory) / "backups"
            phases = []

            class Context:
                @staticmethod
                def phase(name, progress=None):
                    phases.append((name, progress))

                @staticmethod
                def check_cancelled():
                    return None

            def restore(self, vm_id, backup_id, *, check_cancelled, on_progress):
                check_cancelled()
                on_progress(75, 100)
                return {"vm_id": vm_id, "backup_id": backup_id, "restored": True, "file_count": 5}

            with patch("core.vm_backup.VMBackupService.restore", restore):
                result = service._run_vm_backup_restore_job({"vm_id": "macos-vm", "backup_id": "a" * 32 + ".vhb"}, Context())
            self.assertTrue(result["restored"])
            self.assertIn(("Preparando arquivos verificados para restauração", 75.0), phases)
            self.assertEqual(phases[-1], ("Restauração concluída", 100))

    def test_vm_backup_job_reports_copy_progress_and_result(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json")
            service.backup_root = Path(directory) / "backups"
            phases = []

            class Context:
                @staticmethod
                def phase(name, progress=None):
                    phases.append((name, progress))

                @staticmethod
                def check_cancelled():
                    return None

            class FakeBackup:
                def __init__(self, catalog, backup_root, *, vm_storage_root):
                    self.args = (catalog, backup_root, vm_storage_root)

                def create(self, vm_id, *, check_cancelled, on_progress):
                    self.vm_id = vm_id
                    check_cancelled()
                    on_progress(50, 100)
                    return Path("0123456789abcdef.vhb")

            with patch("core.vm_backup.VMBackupService", FakeBackup):
                result = service._run_vm_backup_job({"vm_id": "macos-vm"}, Context())
            self.assertEqual(result, {"vm_id": "macos-vm", "backup_id": "0123456789abcdef.vhb", "file_count": 7})
            self.assertEqual(phases[0], ("Validando VM parada", 0))
            self.assertIn(("Copiando e validando backup offline", 50.0), phases)
            self.assertEqual(phases[-1], ("Backup offline concluído", 100))

    def test_vm_backup_verification_job_reports_progress_and_result(self):
        import time

        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json", jobs_db_path=Path(directory) / "jobs.sqlite")
            service.backup_root = Path(directory) / "backups"
            phases = []

            class Context:
                @staticmethod
                def phase(name, progress=None):
                    phases.append((name, progress))

                @staticmethod
                def check_cancelled():
                    return None

            def verified(self, vm_id, backup_id, *, check_cancelled, on_progress):
                check_cancelled()
                on_progress(100, 100)
                return {"vm_id": vm_id, "backup_id": backup_id, "verified": True, "file_count": 6, "size_bytes": 100}

            verifier_patch = patch("core.vm_backup.VMBackupService.verify_backup", verified)
            verifier_patch.start()
            service.start_jobs()
            try:
                accepted = service.submit_vm_backup_verify_job({
                    "vm_id": "macos-vm", "backup_id": "a" * 32 + ".vhb",
                    "idempotency_key": "12345678-1234-4234-8234-123456789abc",
                })
                deadline = time.monotonic() + 2
                job = None
                while time.monotonic() < deadline:
                    job = service.get_job({"job_id": accepted["job_id"]})
                    if job["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(job["state"], "succeeded")
                self.assertTrue(job["result"]["verified"])
                direct = service._run_vm_backup_verify_job({
                    "vm_id": "macos-vm", "backup_id": "a" * 32 + ".vhb",
                }, Context())
                self.assertTrue(direct["verified"])
                self.assertIn(("Verificando hashes e discos do backup", 99.0), phases)
            finally:
                service.close_jobs()
                verifier_patch.stop()

    def test_storage_inventory_groups_direct_references_and_does_not_stat_external_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            store = VMManagerStore(catalog)
            vm_dir = store.config_dir / "vms" / "vm-1"
            vm_dir.mkdir(parents=True)
            disk = vm_dir / "macos.qcow2"
            disk.write_bytes(b"managed-disk-data")
            external = Path(directory).parent / f"external-{Path(directory).name}.qcow2"
            external.write_bytes(b"private-external-data")
            store.add_vm(VMConfig(
                id="vm-1", name="Studio", macos_version="tahoe", disk_path=str(disk), disk_size_gb=64,
            ))
            store.add_vm(VMConfig(
                id="vm-2", name="Test", macos_version="tahoe", disk_path=str(disk), disk_size_gb=64,
            ))
            store.add_vm(VMConfig(
                id="vm-3", name="External", macos_version="tahoe", disk_path=str(external), disk_size_gb=32,
            ))
            service = WebAgentService(catalog, telemetry=FakeTelemetry())

            inventory = service.storage_inventory({})
            by_refs = {tuple(item["references"]): item for item in inventory["items"]}
            managed = by_refs[("vm-1", "vm-2")]
            outside = by_refs[("vm-3",)]
            self.assertEqual(managed["availability"], "available")
            self.assertEqual(managed["location"], "managed")
            self.assertEqual(managed["virtual_size_bytes"], 64 * 1024**3)
            self.assertEqual(managed["allocated_bytes"], disk.stat().st_blocks * 512)
            self.assertEqual(outside["availability"], "external")
            self.assertEqual(outside["location"], "external")
            self.assertIsNone(outside["allocated_bytes"])
            self.assertEqual(outside["virtual_size_bytes"], 32 * 1024**3)
            external.unlink()

    def test_storage_inventory_discovers_unreferenced_managed_media_without_following_symlinks(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            store = VMManagerStore(Path(directory) / "vms.json")
            media_root = store.config_dir / "media"
            image_root = store.config_dir / "images" / "tahoe"
            vm_root = store.config_dir / "vms" / "orphan"
            media_root.mkdir(parents=True)
            image_root.mkdir(parents=True)
            vm_root.mkdir(parents=True)
            iso = media_root / "recovery.iso"
            cached_installer = image_root / "BaseSystem.img"
            qcow = vm_root / "orphan.qcow2"
            iso.write_bytes(b"iso-content")
            cached_installer.write_bytes(b"cached-recovery-image")
            qcow_header = bytearray(32)
            qcow_header[:4] = b"QFI\xfb"
            qcow_header[4:8] = (3).to_bytes(4, "big")
            qcow_header[24:32] = (120 * 1024**3).to_bytes(8, "big")
            qcow.write_bytes(qcow_header + b"\0" * 72)
            external = Path(outside) / "external.qcow2"
            external.write_bytes(b"external-content")
            (media_root / "shortcut.qcow2").symlink_to(external)
            (media_root / "external-folder").symlink_to(Path(outside), target_is_directory=True)

            inventory = WebAgentService(store.catalog_path).storage_inventory({})

            by_name = {item["name"]: item for item in inventory["items"]}
            self.assertEqual(set(by_name), {"orphan.qcow2", "recovery.iso", "BaseSystem.img"})
            self.assertEqual(by_name["recovery.iso"]["references"], [])
            self.assertEqual(by_name["recovery.iso"]["availability"], "available")
            self.assertEqual(by_name["recovery.iso"]["allocated_bytes"], iso.stat().st_blocks * 512)
            self.assertIsNone(by_name["recovery.iso"]["virtual_size_bytes"])
            self.assertEqual(by_name["BaseSystem.img"]["location"], "managed")
            self.assertEqual(by_name["BaseSystem.img"]["availability"], "available")
            self.assertEqual(by_name["BaseSystem.img"]["references"], [])
            self.assertEqual(by_name["BaseSystem.img"]["virtual_size_bytes"], cached_installer.stat().st_size)
            self.assertEqual(by_name["BaseSystem.img"]["allocated_bytes"], cached_installer.stat().st_blocks * 512)
            self.assertEqual(by_name["orphan.qcow2"]["virtual_size_bytes"], 120 * 1024**3)

    def test_storage_inventory_marks_referenced_additional_disk_available(self):
        with tempfile.TemporaryDirectory() as directory:
            store = VMManagerStore(Path(directory) / "vms.json")
            media_root = store.config_dir / "media"
            media_root.mkdir(parents=True)
            extra_disk = media_root / "data.raw"
            extra_disk.write_bytes(b"data disk")
            store.add_vm(VMConfig(
                id="vm-1", name="Studio", macos_version="tahoe",
                additional_disks=[AdditionalDiskConfig(path=str(extra_disk), format="raw", controller="nvme", order=0)],
            ))

            inventory = WebAgentService(store.catalog_path).storage_inventory({})

            item = next(entry for entry in inventory["items"] if entry["name"] == "data.raw")
            self.assertEqual(item["location"], "managed")
            self.assertEqual(item["availability"], "available")
            self.assertEqual(item["references"], ["vm-1"])
            self.assertEqual(item["virtual_size_bytes"], len(b"data disk"))

    def test_autostart_reads_and_mutations_are_owned_by_agent_with_revision_guard(self):
        from core.web_rpc import RpcRemoteError

        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            store = VMManagerStore(catalog)
            store.add_vm(VMConfig(id="vm-a", name="A", macos_version="tahoe"))
            store.add_vm(VMConfig(id="vm-b", name="B", macos_version="tahoe"))
            service = WebAgentService(catalog)
            handlers = service.handlers()
            self.assertIsNone(handlers["vms.autostart.get"]({}))

            revision = service.store.revision
            result = handlers["vms.autostart.set"]({
                "vm_id": "vm-a", "enabled": True, "expected_revision": revision,
            })
            self.assertTrue(result["enabled"])
            self.assertEqual(handlers["vms.autostart.get"]({})["id"], "vm-a")

            with self.assertRaises(RpcRemoteError) as conflict:
                handlers["vms.autostart.set"]({
                    "vm_id": "vm-b", "enabled": True, "expected_revision": revision,
                })
            self.assertEqual(conflict.exception.code, "catalog_conflict")
            self.assertEqual(service.store.get_auto_start_vm().id, "vm-a")

    def test_vm_action_is_explicitly_unavailable_until_supervisor_is_ready(self):
        from core.web_rpc import RpcRemoteError

        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json", running_vms=lambda: {})
            with self.assertRaises(RpcRemoteError) as caught:
                service.handlers()["vms.action"]({"vm_id": "vm-1"})
            self.assertEqual(caught.exception.code, "capability_unavailable")

    def test_vm_action_queues_graceful_shutdown_without_force_escalation(self):
        import os
        from core.web_rpc import RpcRemoteError
        from core.config import VMConfig, VMManagerStore

        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            VMManagerStore(catalog).add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe"))
            running = {"vm-1": os.getpid()}
            commands = []

            class FakeQmp:
                def command(self, name):
                    commands.append(name)
                    if name == "query-status":
                        return {"running": True}
                    if name == "system_powerdown":
                        running.clear()
                        return {}
                    raise AssertionError(f"unexpected QMP command: {name}")

                def close(self):
                    pass

            service = WebAgentService(
                catalog,
                jobs_db_path=Path(directory) / "jobs.sqlite",
                running_vms=lambda: dict(running),
                qmp_factory=lambda _vm: FakeQmp(),
            )
            service.start_jobs()
            try:
                accepted = service.vm_action({
                    "vm_id": "vm-1",
                    "action": {"operation": "shutdown", "idempotency_key": "shutdown-vm-0001"},
                })
                deadline = time.monotonic() + 2
                job = None
                while time.monotonic() < deadline:
                    job = service.get_job({"job_id": accepted["job_id"]})
                    if job["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(job["state"], "succeeded")
                self.assertEqual(job["result"]["state"], "stopped")
                self.assertEqual(commands, ["query-status", "system_powerdown"])
                self.assertNotIn("stop", commands)
                with self.assertRaises(RpcRemoteError) as caught:
                    service.vm_action({
                        "vm_id": "vm-1",
                        "action": {"operation": "force_stop", "confirmation": "vm-1", "idempotency_key": "force-vm-0001"},
                    })
                self.assertEqual(caught.exception.code, "capability_unavailable")
            finally:
                service.close_jobs()

    def test_forced_stop_bypasses_a_blocked_graceful_vm_job(self):
        import os
        from core.config import VMConfig, VMManagerStore

        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            VMManagerStore(catalog).add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe"))
            running = {"vm-1": os.getpid()}
            blocker_started = threading.Event()
            release_blocker = threading.Event()

            class FakeQmp:
                def command(self, name):
                    if name == "query-status":
                        return {"running": True}
                    if name == "quit":
                        running.clear()
                        return {}
                    raise AssertionError(f"unexpected QMP command: {name}")

                def close(self):
                    pass

            service = WebAgentService(
                catalog,
                jobs_db_path=Path(directory) / "jobs.sqlite",
                running_vms=lambda: dict(running),
                qmp_factory=lambda _vm: FakeQmp(),
                vm_unit_manager=SimpleNamespace(),
            )

            def block_normal_vm_lane(_payload, _context):
                blocker_started.set()
                if not release_blocker.wait(timeout=5):
                    raise TimeoutError("test did not release the normal VM worker")
                return {"state": "released"}

            service.job_runner.register("test.blocking_vm_job", block_normal_vm_lane)
            service.start_jobs()
            try:
                blocked, _ = service.job_runner.submit(
                    idempotency_key="blocking-job-0001",
                    kind="test.blocking_vm_job",
                    payload={},
                    target_vm_id="vm-1",
                )
                self.assertTrue(blocker_started.wait(timeout=2))

                accepted = service.vm_action({
                    "vm_id": "vm-1",
                    "action": {
                        "operation": "force_stop",
                        "confirmation": "vm-1",
                        "idempotency_key": "force-stop-job-0001",
                    },
                })
                deadline = time.monotonic() + 2
                forced = None
                while time.monotonic() < deadline:
                    forced = service.get_job({"job_id": accepted["job_id"]})
                    if forced["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)

                self.assertEqual(forced["state"], "succeeded")
                self.assertEqual(forced["result"]["state"], "stopped")
                self.assertEqual(service.job_store.get_job(blocked["id"])["state"], "running")
            finally:
                release_blocker.set()
                service.close_jobs()

    def test_vm_start_uses_only_the_allowlisted_systemd_vm_unit(self):
        import time
        import os
        from types import SimpleNamespace
        from core.config import VMConfig, VMManagerStore

        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            VMManagerStore(catalog).add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe"))
            calls = []
            running = {}

            class Units:
                started = False

                def query(self, vm_id):
                    self.assert_vm(vm_id)
                    return SimpleNamespace(
                        unit="vhackintosh-vm@vm-1.service",
                        active_state="active" if self.started else "inactive",
                    )

                def start(self, vm_id):
                    self.assert_vm(vm_id)
                    calls.append(("start", vm_id))
                    running[vm_id] = os.getpid()
                    self.started = True
                    return SimpleNamespace(unit="vhackintosh-vm@vm-1.service", active_state="active")

                @staticmethod
                def assert_vm(vm_id):
                    if vm_id != "vm-1":
                        raise AssertionError("unexpected VM unit")

            service = WebAgentService(
                catalog,
                jobs_db_path=Path(directory) / "jobs.sqlite",
                running_vms=lambda: dict(running),
                vm_unit_manager=Units(),
            )
            service.start_jobs()
            try:
                accepted = service.vm_action({
                    "vm_id": "vm-1",
                    "action": {"operation": "start", "idempotency_key": "start-vm-action-0001"},
                })
                deadline = time.monotonic() + 2
                job = None
                while time.monotonic() < deadline:
                    job = service.get_job({"job_id": accepted["job_id"]})
                    if job["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(job["state"], "succeeded")
                self.assertEqual(job["result"]["state"], "running")
                self.assertEqual(calls, [("start", "vm-1")])
            finally:
                service.close_jobs()

    def test_vm_start_retries_temporarily_unavailable_runtime_inventory(self):
        import os
        import time
        from types import SimpleNamespace
        from core.config import VMConfig, VMManagerStore

        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            VMManagerStore(catalog).add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe"))
            inventory_calls = 0

            def runtime_inventory():
                nonlocal inventory_calls
                inventory_calls += 1
                if inventory_calls == 2:
                    raise OSError("QEMU has not appeared in process inventory yet")
                return {"vm-1": os.getpid()} if inventory_calls >= 3 else {}

            class Units:
                started = False

                def query(self, _vm_id):
                    return SimpleNamespace(
                        unit="vhackintosh-vm@vm-1.service",
                        active_state="active" if self.started else "inactive",
                        result="",
                    )

                def start(self, _vm_id):
                    self.started = True
                    return SimpleNamespace(unit="vhackintosh-vm@vm-1.service", active_state="active")

            service = WebAgentService(
                catalog,
                jobs_db_path=Path(directory) / "jobs.sqlite",
                running_vms=runtime_inventory,
                vm_unit_manager=Units(),
            )
            service.start_jobs()
            try:
                accepted = service.vm_action({
                    "vm_id": "vm-1",
                    "action": {"operation": "start", "idempotency_key": "start-vm-inventory-race-0001"},
                })
                deadline = time.monotonic() + 2
                job = None
                while time.monotonic() < deadline:
                    job = service.get_job({"job_id": accepted["job_id"]})
                    if job["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(job["state"], "succeeded")
                self.assertEqual(job["result"]["state"], "running")
                self.assertGreaterEqual(inventory_calls, 3)
            finally:
                service.close_jobs()

    def test_vm_start_job_fails_if_supervisor_exits_before_qemu_is_confirmed(self):
        import time
        from types import SimpleNamespace
        from core.config import VMConfig, VMManagerStore

        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            VMManagerStore(catalog).add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe"))
            calls = []

            class Units:
                def query(self, vm_id):
                    calls.append(("query", vm_id))
                    return SimpleNamespace(
                        unit="vhackintosh-vm@vm-1.service",
                        active_state="inactive" if len(calls) == 1 else "failed",
                        result="exit-code",
                    )

                def start(self, vm_id):
                    calls.append(("start", vm_id))
                    return SimpleNamespace(unit="vhackintosh-vm@vm-1.service", active_state="active")

            service = WebAgentService(
                catalog,
                jobs_db_path=Path(directory) / "jobs.sqlite",
                running_vms=lambda: {},
                vm_unit_manager=Units(),
            )
            service.start_jobs()
            try:
                accepted = service.vm_action({
                    "vm_id": "vm-1",
                    "action": {"operation": "start", "idempotency_key": "start-vm-failed-unit-0001"},
                })
                deadline = time.monotonic() + 2
                job = None
                while time.monotonic() < deadline:
                    job = service.get_job({"job_id": accepted["job_id"]})
                    if job["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(job["state"], "failed")
                self.assertIn("falhou antes de confirmar o processo QEMU", job["error"]["message"])
                self.assertIn(("start", "vm-1"), calls)
            finally:
                service.close_jobs()

    def test_force_stop_uses_qmp_quit_and_never_signals_process(self):
        import os
        import time
        from types import SimpleNamespace
        from core.config import VMConfig, VMManagerStore

        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            VMManagerStore(catalog).add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe"))
            running = {"vm-1": os.getpid()}
            commands = []

            class FakeQmp:
                def command(self, name):
                    commands.append(name)
                    if name == "query-status":
                        return {"running": True}
                    if name == "quit":
                        running.clear()
                        return {}
                    raise AssertionError(f"unexpected QMP command: {name}")

                def close(self):
                    pass

            class Units:
                def query(self, _vm_id):
                    return SimpleNamespace(unit="vhackintosh-vm@vm-1.service", active_state="active")

                def start(self, _vm_id):
                    raise AssertionError("force stop must not restart the VM")

            service = WebAgentService(
                catalog,
                jobs_db_path=Path(directory) / "jobs.sqlite",
                running_vms=lambda: dict(running),
                qmp_factory=lambda _vm: FakeQmp(),
                vm_unit_manager=Units(),
            )
            service.start_jobs()
            try:
                accepted = service.vm_action({
                    "vm_id": "vm-1",
                    "action": {"operation": "force_stop", "confirmation": "vm-1", "idempotency_key": "force-vm-action-0001"},
                })
                deadline = time.monotonic() + 2
                job = None
                while time.monotonic() < deadline:
                    job = service.get_job({"job_id": accepted["job_id"]})
                    if job["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(job["state"], "succeeded")
                self.assertEqual(commands, ["query-status", "quit"])
            finally:
                service.close_jobs()

    def test_restart_waits_for_acpi_shutdown_before_starting_the_unit(self):
        import os
        import time
        from types import SimpleNamespace
        from core.config import VMConfig, VMManagerStore

        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            VMManagerStore(catalog).add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe"))
            running = {"vm-1": os.getpid()}
            calls = []

            class FakeQmp:
                def command(self, name):
                    calls.append(("qmp", name))
                    if name == "query-status":
                        return {"running": True}
                    if name == "system_powerdown":
                        running.clear()
                        return {}
                    raise AssertionError(f"unexpected QMP command: {name}")

                def close(self):
                    pass

            class Units:
                def query(self, _vm_id):
                    return SimpleNamespace(unit="vhackintosh-vm@vm-1.service", active_state="active")

                def start(self, vm_id):
                    calls.append(("start", vm_id))
                    running[vm_id] = os.getpid()
                    return SimpleNamespace(unit="vhackintosh-vm@vm-1.service", active_state="active")

            service = WebAgentService(
                catalog,
                jobs_db_path=Path(directory) / "jobs.sqlite",
                running_vms=lambda: dict(running),
                qmp_factory=lambda _vm: FakeQmp(),
                vm_unit_manager=Units(),
            )
            service.start_jobs()
            try:
                accepted = service.vm_action({
                    "vm_id": "vm-1",
                    "action": {"operation": "restart", "idempotency_key": "restart-vm-action-0001"},
                })
                deadline = time.monotonic() + 2
                job = None
                while time.monotonic() < deadline:
                    job = service.get_job({"job_id": accepted["job_id"]})
                    if job["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(job["state"], "succeeded")
                self.assertEqual(job["result"]["state"], "running")
                self.assertEqual(calls, [("qmp", "query-status"), ("qmp", "system_powerdown"), ("start", "vm-1")])
            finally:
                service.close_jobs()

    def test_vm_shutdown_job_cannot_be_cancelled_after_acpi_is_prepared(self):
        import os
        import threading
        from core.web_rpc import RpcRemoteError
        from core.config import VMConfig, VMManagerStore

        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            VMManagerStore(catalog).add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe"))
            running = {"vm-1": os.getpid()}
            command_started = threading.Event()
            release_command = threading.Event()

            class FakeQmp:
                def command(self, name):
                    if name == "query-status":
                        return {"running": True}
                    if name == "system_powerdown":
                        command_started.set()
                        release_command.wait(timeout=2)
                        running.clear()
                        return {}
                    raise AssertionError(f"unexpected QMP command: {name}")

                def close(self):
                    pass

            service = WebAgentService(
                catalog,
                jobs_db_path=Path(directory) / "jobs.sqlite",
                running_vms=lambda: dict(running),
                qmp_factory=lambda _vm: FakeQmp(),
            )
            service.start_jobs()
            try:
                accepted = service.vm_action({
                    "vm_id": "vm-1",
                    "action": {"operation": "shutdown", "idempotency_key": "shutdown-vm-0002"},
                })
                self.assertTrue(command_started.wait(timeout=2))
                current = service.get_job({"job_id": accepted["job_id"]})
                self.assertFalse(current["cancellable"])
                with self.assertRaises(RpcRemoteError) as caught:
                    service.cancel_job({"job_id": accepted["job_id"]})
                self.assertEqual(caught.exception.code, "job_not_cancellable")
            finally:
                release_command.set()
                service.close_jobs()

    def test_vm_shutdown_timeout_never_escalates_to_force_stop(self):
        import os
        from unittest.mock import patch
        from core.web_rpc import RpcRemoteError
        from core.config import VMConfig, VMManagerStore

        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "vms.json"
            VMManagerStore(catalog).add_vm(VMConfig(id="vm-1", name="Studio", macos_version="tahoe"))
            running = {"vm-1": os.getpid()}
            commands = []

            class FakeQmp:
                def command(self, name):
                    commands.append(name)
                    return {"running": True} if name == "query-status" else {}

                def close(self):
                    pass

            service = WebAgentService(
                catalog,
                jobs_db_path=Path(directory) / "jobs.sqlite",
                running_vms=lambda: dict(running),
                qmp_factory=lambda _vm: FakeQmp(),
            )
            service.start_jobs()
            try:
                with patch("core.web_agent.VM_SHUTDOWN_TIMEOUT_SECONDS", 0.05):
                    accepted = service.vm_action({
                        "vm_id": "vm-1",
                        "action": {"operation": "shutdown", "idempotency_key": "shutdown-vm-0003"},
                    })
                    deadline = time.monotonic() + 2
                    job = None
                    while time.monotonic() < deadline:
                        job = service.get_job({"job_id": accepted["job_id"]})
                        if job["state"] in {"succeeded", "failed"}:
                            break
                        time.sleep(0.01)
                self.assertEqual(job["state"], "failed")
                self.assertIn("nenhuma parada forçada foi executada", job["error"]["message"])
                self.assertEqual(commands, ["query-status", "system_powerdown"])
            finally:
                service.close_jobs()

    def test_runtime_capabilities_explain_gpu_probe_failure_without_claiming_support(self):
        with tempfile.TemporaryDirectory() as directory:
            def failed_gpu_probe():
                raise TimeoutError("private probe output must not leak")

            service = WebAgentService(Path(directory) / "vms.json", gpu_inventory=failed_gpu_probe)
            result = service.runtime_capabilities({})
            gpu = next(item for item in result["items"] if item["id"] == "gpu.exact_selection")
            self.assertTrue(gpu["supported"])
            self.assertFalse(gpu["available"])
            self.assertEqual(gpu["limitation"], "Sonda de GPU indisponível (TimeoutError).")
            self.assertNotIn("private probe output", str(result))

    def test_pause_resume_capability_stays_disabled_until_reims_guest_path_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json", gpu_inventory=lambda: [])
            result = service.runtime_capabilities({})
            capability = next(item for item in result["items"] if item["id"] == "vm.pause_resume")
            self.assertFalse(capability["supported"])
            self.assertFalse(capability["available"])
            self.assertIn("launcher Reims", capability["description"])
            self.assertIn("reconciliação", capability["limitation"])
            service.close_jobs()

    def test_default_agent_job_runner_starts_with_durable_store(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(
                Path(directory) / "vms.json",
                jobs_db_path=Path(directory) / "jobs.sqlite",
            )
            service.start_jobs()
            self.assertTrue(service.job_runner._started)
            self.assertTrue(service.force_stop_job_runner._started)
            self.assertIsNot(service.force_stop_job_runner, service.job_runner)
            service.close_jobs()

    def test_reims_release_inventory_reports_bundled_and_active_tags(self):
        from core.reims_release import Release

        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json")
            with patch("core.reims_release.list_releases", return_value=[Release(
                tag="v1.2.3", name="Runtime 1.2.3", published_at="2026-10-01T12:00:00Z",
                asset_url="https://github.com/felipeab10/reims-vgpu/releases/download/v1.2.3/reims-vgpu-linux-x86_64.tar.gz",
                asset_size=1024,
            )]), patch("core.reims_release.current_release", return_value="v1.2.2"), patch(
                "core.reims_build.prebuilt_current_release", return_value="v1.2.1",
            ):
                result = service.list_reims_releases({})
            self.assertEqual(result["repository"], "felipeab10/reims-vgpu")
            self.assertEqual(result["current_release"], "v1.2.2")
            self.assertEqual(result["active_release"], "v1.2.1")
            self.assertEqual(result["items"][0]["tag"], "v1.2.3")
            self.assertEqual(result["items"][0]["asset_size_bytes"], 1024)

    def test_reims_release_update_is_durable_and_marks_uncancellable_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            class Runner:
                def submit(self, **kwargs):
                    self.kwargs = kwargs
                    return ({"id": "release-job-1"}, True)

            runner = Runner()
            service = WebAgentService(Path(directory) / "vms.json", job_runner=runner)
            accepted = service.submit_reims_release_update({
                "idempotency_key": "12345678-1234-4234-8234-123456789abc",
                "tag": "v1.2.3",
            })
            self.assertEqual(accepted["job_id"], "release-job-1")
            self.assertEqual(runner.kwargs["kind"], "reims.release.update")
            self.assertEqual(runner.kwargs["payload"], {"tag": "v1.2.3"})
            self.assertEqual(runner.kwargs["idempotency_key"], "reims-release:v1.2.3:12345678-1234-4234-8234-123456789abc")
            self.assertIn("reims.release.update", service.handlers())

    def test_reims_release_update_handler_calls_verified_core_updater(self):
        from core.reims_release import UpdateResult

        class Context:
            def __init__(self): self.events = []
            def check_cancelled(self): self.events.append("checked")
            def phase(self, name, progress=None): self.events.append((name, progress))
            def checkpoint(self, name, payload): self.events.append((name, payload))
            def log(self, message): self.events.append(("log", message))

        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json")
            context = Context()
            from core import update_guard

            fake_tree = Path(directory) / "release"
            fake_tree.mkdir()
            maintenance_root = Path(directory) / "maintenance"
            real_maintenance_window = update_guard.maintenance_window
            with patch("core.reims_release.update_from_release", return_value=UpdateResult(
                ok=True, tag="v1.2.3", previous="v1.2.2", message="updated", changed=True,
            )) as update, patch(
                "core.update_guard.maintenance_window",
                side_effect=lambda **_kwargs: real_maintenance_window(
                    marker=maintenance_root / "maintenance.json",
                    lock_path=maintenance_root / "reservations.lock",
                    reservations_dir=maintenance_root / "reservations",
                ),
            ) as maintenance, patch(
                "core.reims_build.prebuilt_reims_ctl", return_value=fake_tree / "bin/reims-ctl"
            ), patch(
                "core.reims_build.prebuilt_reims_util", return_value=None
            ):
                with self.assertRaisesRegex(RuntimeError, "não contém reims-ctl e reims-util compatíveis"):
                    service._run_reims_release_update_job({"tag": "v1.2.3"}, context)
            self.assertEqual(update.call_args.kwargs["tag"], "v1.2.3")
            maintenance.assert_called_once()
            self.assertIn(("reims_release_update_started", {"tag": "v1.2.3"}), context.events)

    def test_reims_release_update_fails_and_restores_the_vm_when_kexts_shrink(self):
        """rc=0 per VM is not success: a shrunken EFI must abort the update."""
        import plistlib
        from core.reims_release import UpdateResult

        class Context:
            def __init__(self): self.events = []
            def check_cancelled(self): return None
            def phase(self, name, progress=None): self.events.append((name, progress))
            def checkpoint(self, name, payload, **_kwargs): self.events.append((name, payload))
            def log(self, message, level=None): self.events.append(("log", message, level))
            def run_subprocess(self, command, **_kwargs):
                if command[1] == "update-opencore":
                    (vm_dir / "OpenCore.qcow2").write_bytes(b"regenerated with Lilu only")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                raise AssertionError(command)

        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json", gpu_inventory=lambda: [])
            vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe")
            service.store.add_vm(vm)
            vm_dir = service.store.config_dir / "vms" / vm.id
            vm_dir.mkdir(parents=True)
            (vm_dir / "vm.toml").write_text('[q35]\nopencore = "OpenCore.qcow2"\n', encoding="utf-8")
            (vm_dir / "OpenCore.qcow2").write_bytes(b"original image")
            fake_tree = Path(directory) / "release"
            fake_tree.mkdir()
            seen = {"n": 0}

            def fake_run(command, **_kwargs):
                if command[1:3] == ["update-opencore", "--help"]:
                    return SimpleNamespace(returncode=0, stdout="Usage: reims-ctl update-opencore <NAME>", stderr="")
                if command[0] == "guestfish" and "download" in command:
                    seen["n"] += 1
                    bundles = _profile_kexts(LEGACY_PROFILE) if seen["n"] == 1 else {"Lilu.kext"}
                    plist = {"Kernel": {"Add": [{"BundlePath": n, "Enabled": True} for n in sorted(bundles)]}}
                    Path(command[-1]).write_bytes(plistlib.dumps(plist))
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if command[0] == "guestfish" and "ls" in command:
                    bundles = _profile_kexts(LEGACY_PROFILE) if seen["n"] == 1 else {"Lilu.kext"}
                    return SimpleNamespace(returncode=0, stdout="\n".join(sorted(bundles)) + "\n", stderr="")
                raise AssertionError(command)

            with patch("core.reims_release.update_from_release", return_value=UpdateResult(
                     ok=True, tag="v1.2.3", previous="v1.2.2", message="updated", changed=True,
                 )), patch("core.update_guard.maintenance_window"), patch(
                     "core.reims_build.prebuilt_reims_ctl", return_value=fake_tree / "bin/reims-ctl"
                 ), patch(
                     "core.reims_build.prebuilt_reims_util", return_value=fake_tree / "bin/reims-util"
                 ), patch("core.reims_build.install_prebuilt", return_value=True), patch(
                     "core.reims_ctl.reims_ctl_bin", return_value="/managed/reims-ctl"
                 ), patch(
                     "core.reims_ctl.vm_record", return_value={"name": vm.id, "dir": str(vm_dir), "state": "stopped", "pid": None}
                 ), patch("core.web_agent.shutil.which", return_value="/usr/bin/guestfish"), patch(
                     "core.runtime_inventory.inspect_runtime", return_value=_release_runtime()
                 ), patch("core.web_agent.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, r"com falha em 1 \(Studio\)"):
                    service._run_reims_release_update_job({"tag": "v1.2.3"}, Context())
            self.assertEqual((vm_dir / "OpenCore.qcow2").read_bytes(), b"original image")
            service.close_jobs()

    def test_reims_release_update_fails_when_the_kext_set_swaps_at_constant_size(self):
        """A same-size swap must fail: cardinality alone misses (or so it seems) it.

        `apple-gfx-pci` yields 8 ports; a `reims-graphics-pci` set yields 5 `Rgfx*`.
        A swap that keeps the count is just as broken as a shrink, and the EFI
        must be restored either way.
        """
        import plistlib
        from core.reims_release import UpdateResult

        before_bundles = sorted(_profile_kexts(LEGACY_PROFILE))
        after_bundles = sorted(_profile_kexts(REIMS_PROFILE))

        class Context:
            def __init__(self): self.events = []
            def check_cancelled(self): return None
            def phase(self, name, progress=None): self.events.append((name, progress))
            def checkpoint(self, name, payload, **_kwargs): self.events.append((name, payload))
            def log(self, message, level=None): self.events.append(("log", message, level))
            def run_subprocess(self, command, **_kwargs):
                if command[1] == "update-opencore":
                    (vm_dir / "OpenCore.qcow2").write_bytes(b"regenerated with a swapped set")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                raise AssertionError(command)

        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json", gpu_inventory=lambda: [])
            vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe")
            service.store.add_vm(vm)
            vm_dir = service.store.config_dir / "vms" / vm.id
            vm_dir.mkdir(parents=True)
            (vm_dir / "vm.toml").write_text('[q35]\nopencore = "OpenCore.qcow2"\n', encoding="utf-8")
            (vm_dir / "OpenCore.qcow2").write_bytes(b"original image")
            fake_tree = Path(directory) / "release"
            fake_tree.mkdir()
            seen = {"n": 0}

            def fake_run(command, **_kwargs):
                if command[1:3] == ["update-opencore", "--help"]:
                    return SimpleNamespace(returncode=0, stdout="Usage: reims-ctl update-opencore <NAME>", stderr="")
                if command[0] == "guestfish" and "download" in command:
                    seen["n"] += 1
                    bundles = before_bundles if seen["n"] == 1 else after_bundles
                    plist = {"Kernel": {"Add": [{"BundlePath": n, "Enabled": True} for n in bundles]}}
                    Path(command[-1]).write_bytes(plistlib.dumps(plist))
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if command[0] == "guestfish" and "ls" in command:
                    bundles = before_bundles if seen["n"] == 1 else after_bundles
                    return SimpleNamespace(returncode=0, stdout="\n".join(bundles) + "\n", stderr="")
                raise AssertionError(command)

            with patch("core.reims_release.update_from_release", return_value=UpdateResult(
                     ok=True, tag="v1.2.3", previous="v1.2.2", message="updated", changed=True,
                 )), patch("core.update_guard.maintenance_window"), patch(
                     "core.reims_build.prebuilt_reims_ctl", return_value=fake_tree / "bin/reims-ctl"
                 ), patch(
                     "core.reims_build.prebuilt_reims_util", return_value=fake_tree / "bin/reims-util"
                 ), patch("core.reims_build.install_prebuilt", return_value=True), patch(
                     "core.reims_ctl.reims_ctl_bin", return_value="/managed/reims-ctl"
                 ), patch(
                     "core.reims_ctl.vm_record", return_value={"name": vm.id, "dir": str(vm_dir), "state": "stopped", "pid": None}
                 ), patch("core.web_agent.shutil.which", return_value="/usr/bin/guestfish"), patch(
                     "core.web_agent.subprocess.run", side_effect=fake_run
                 ), patch(
                     "core.runtime_inventory.inspect_runtime", return_value=_release_runtime()
                 ):
                with self.assertRaisesRegex(RuntimeError, r"com falha em 1 \(Studio\)"):
                    service._run_reims_release_update_job({"tag": "v1.2.3"}, Context())
            self.assertEqual((vm_dir / "OpenCore.qcow2").read_bytes(), b"original image")
            service.close_jobs()

    def test_reims_release_update_leaves_no_orphan_backup_when_before_read_fails(self):
        """A `before` inspection failure mutates nothing, so it must not leave a backup.

        A host without `guestfish` fails on every VM; without this ordering the
        batch would drop one `.opencore-before-{tag}.qcow2` per managed VM.
        """
        from core.reims_release import UpdateResult

        class Context:
            def check_cancelled(self): return None
            def phase(self, *args, **kwargs): return None
            def checkpoint(self, *args, **kwargs): return None
            def log(self, *args, **kwargs): return None
            def run_subprocess(self, *args, **kwargs):
                raise AssertionError("nenhuma imagem pode ser tocada")

        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json", gpu_inventory=lambda: [])
            vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe")
            service.store.add_vm(vm)
            vm_dir = service.store.config_dir / "vms" / vm.id
            vm_dir.mkdir(parents=True)
            (vm_dir / "vm.toml").write_text('[q35]\nopencore = "OpenCore.qcow2"\n', encoding="utf-8")
            (vm_dir / "OpenCore.qcow2").write_bytes(b"original image")
            fake_tree = Path(directory) / "release"
            fake_tree.mkdir()

            def fake_run(command, **_kwargs):
                if command[1:3] == ["update-opencore", "--help"]:
                    return SimpleNamespace(returncode=0, stdout="Usage: reims-ctl update-opencore <NAME>", stderr="")
                raise AssertionError(command)

            with patch("core.reims_release.update_from_release", return_value=UpdateResult(
                     ok=True, tag="v1.2.3", previous="v1.2.2", message="updated", changed=True,
                 )), patch("core.update_guard.maintenance_window"), patch(
                     "core.reims_build.prebuilt_reims_ctl", return_value=fake_tree / "bin/reims-ctl"
                 ), patch(
                     "core.reims_build.prebuilt_reims_util", return_value=fake_tree / "bin/reims-util"
                 ), patch("core.reims_build.install_prebuilt", return_value=True), patch(
                     "core.reims_ctl.reims_ctl_bin", return_value="/managed/reims-ctl"
                 ), patch(
                     "core.reims_ctl.vm_record", return_value={"name": vm.id, "dir": str(vm_dir), "state": "stopped", "pid": None}
                 ), patch("core.web_agent.shutil.which", return_value=None), patch(
                     "core.web_agent.subprocess.run", side_effect=fake_run
                 ), patch(
                     "core.runtime_inventory.inspect_runtime", return_value=_release_runtime()
                 ):
                with self.assertRaisesRegex(RuntimeError, r"com falha em 1 \(Studio\)"):
                    service._run_reims_release_update_job({"tag": "v1.2.3"}, Context())
            self.assertEqual((vm_dir / "OpenCore.qcow2").read_bytes(), b"original image")
            self.assertEqual({entry.name for entry in vm_dir.iterdir()}, {"vm.toml", "OpenCore.qcow2"})
            service.close_jobs()

    def test_reims_release_update_aborts_the_whole_batch_on_add_kext_util(self):
        """An add-kext util without a ctl that forwards --add-kext must abort before any VM."""
        from core.reims_release import UpdateResult

        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json", gpu_inventory=lambda: [])
            vm = VMConfig(id="macos-studio", name="Studio", macos_version="tahoe")
            service.store.add_vm(vm)
            vm_dir = service.store.config_dir / "vms" / vm.id
            vm_dir.mkdir(parents=True)
            (vm_dir / "vm.toml").write_text('[q35]\nopencore = "OpenCore.qcow2"\n', encoding="utf-8")
            (vm_dir / "OpenCore.qcow2").write_bytes(b"original image")
            fake_tree = Path(directory) / "release"
            fake_tree.mkdir()

            class Context:
                def check_cancelled(self): return None
                def phase(self, *args, **kwargs): return None
                def checkpoint(self, *args, **kwargs): return None
                def log(self, *args, **kwargs): return None
                def run_subprocess(self, *args, **kwargs):
                    raise AssertionError("nenhuma VM pode ser tocada")

            def fake_run(command, **_kwargs):
                if command[1:3] == ["update-opencore", "--help"]:
                    return SimpleNamespace(returncode=0, stdout="Usage: reims-ctl update-opencore <NAME>", stderr="")
                raise AssertionError(command)

            with patch("core.reims_release.update_from_release", return_value=UpdateResult(
                     ok=True, tag="v1.2.3", previous="v1.2.2", message="updated", changed=True,
                 )), patch("core.update_guard.maintenance_window"), patch(
                     "core.reims_build.prebuilt_reims_ctl", return_value=fake_tree / "bin/reims-ctl"
                 ), patch(
                     "core.reims_build.prebuilt_reims_util", return_value=fake_tree / "bin/reims-util"
                 ), patch("core.reims_build.install_prebuilt", return_value=True), patch(
                     "core.reims_ctl.reims_ctl_bin", return_value="/managed/reims-ctl"
                 ), patch(
                     "core.runtime_inventory.inspect_runtime", return_value=_release_runtime(kext_mode="add-kext")
                 ), patch("core.web_agent.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "sem kexts embutidos"):
                    service._run_reims_release_update_job({"tag": "v1.2.3"}, Context())
            self.assertEqual((vm_dir / "OpenCore.qcow2").read_bytes(), b"original image")
            service.close_jobs()

    def test_control_release_catalog_and_update_job_are_allowlisted(self):
        from core.control_release import ControlRelease

        release = ControlRelease(
            tag="control-v1.0.0-beta.11", name="Control beta.11",
            published_at="2026-11-03T00:00:00Z", prerelease=True,
            asset_name="vhackintosh-control-1.0.0.beta.11-1-x86_64.pkg.tar.zst",
            asset_url="https://example.invalid/control.pkg", asset_size=1024,
            checksum_url="https://example.invalid/SHA256SUMS",
        )

        class Runner:
            def submit(self, **kwargs):
                self.kwargs = kwargs
                return ({"id": "control-release-job"}, True)

        with tempfile.TemporaryDirectory() as directory:
            runner = Runner()
            service = WebAgentService(Path(directory) / "vms.json", job_runner=runner)
            with patch("core.control_release.package_kind", return_value="arch"), \
                 patch("core.control_release.list_releases", return_value=[release]), \
                 patch("core.control_release.current_version", return_value="1.0.0-beta.10"):
                catalog = service.list_control_releases({})
            self.assertEqual(catalog["package_format"], "arch")
            self.assertEqual(catalog["items"][0]["tag"], release.tag)
            accepted = service.submit_control_release_update({
                "idempotency_key": "12345678-1234-4234-8234-123456789abc",
                "tag": release.tag,
            })
            self.assertEqual(accepted["job_id"], "control-release-job")
            self.assertEqual(runner.kwargs["kind"], "control.release.update")
            self.assertEqual(runner.kwargs["payload"], {"tag": release.tag})
            self.assertIn("control.release.update", service.handlers())

    def test_provision_rpc_is_durable_async_and_reports_checkpoints(self):
        observed_cpu_models = []

        class FakeProvisioner:
            def provision(self, request, *, on_event, cancel_event):
                from core.vm_provisioning import ProvisioningEvent

                observed_cpu_models.append(request.cpu_model)
                on_event(ProvisioningEvent("disk_opencore", "started", 50, "Criando", request.vm_id))
                on_event(ProvisioningEvent("disk_opencore", "completed", 90, "Pronto", request.vm_id))
                return SimpleNamespace(vm=SimpleNamespace(id=request.vm_id, name=request.name, disk_path="/vm/disk.qcow2"))

        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(
                Path(directory) / "vms.json",
                jobs_db_path=Path(directory) / "jobs.sqlite",
                provisioning_service=FakeProvisioner(),
            )
            service.start_jobs()
            try:
                with patch("core.runtime_inventory.inspect_runtime", return_value={"qemu_cpu_models": ["Haswell-noTSX"]}):
                    accepted = service.submit_provision_job({
                    "idempotency_key": "provision-idempotent-1", "id": "macos-test", "name": "Test",
                    "macos_version": "tahoe", "vcpus": 2, "ram_gb": 4,
                    "cpu_model": "Haswell-noTSX", "installer_policy": "none",
                    "selected_gpu_bdf": None, "selected_gpu_uuid": None,
                    })
                deadline = time.monotonic() + 2
                job = None
                while time.monotonic() < deadline:
                    job = service.get_job({"job_id": accepted["job_id"]})
                    if job["state"] == "succeeded":
                        break
                    time.sleep(0.01)
                self.assertEqual(job["state"], "succeeded")
                self.assertEqual(job["result"]["vm_id"], "macos-test")
                self.assertEqual(observed_cpu_models, ["Haswell-noTSX"])
                self.assertTrue(any(item["name"] == "disk_opencore" and item["completed"] for item in job["checkpoints"]))
                from web.api.contracts import JobDetail

                JobDetail.model_validate(job)
                with patch("core.runtime_inventory.inspect_runtime", return_value={"qemu_cpu_models": ["Haswell-noTSX"]}):
                    duplicate = service.submit_provision_job({
                        "idempotency_key": "provision-idempotent-1", "id": "macos-test", "name": "Test",
                        "macos_version": "tahoe", "vcpus": 2, "ram_gb": 4,
                        "cpu_model": "Haswell-noTSX", "installer_policy": "none",
                        "selected_gpu_bdf": None, "selected_gpu_uuid": None,
                    })
                self.assertEqual(duplicate["job_id"], accepted["job_id"])
            finally:
                service.close_jobs()

    def test_provision_rpc_propagates_gated_cpu_topology_to_provisioner(self):
        observed = []

        class FakeProvisioner:
            def provision(self, request, *, on_event, cancel_event):
                from core.vm_provisioning import ProvisioningEvent
                observed.append(request.cpu_topology)
                on_event(ProvisioningEvent("catalog", "completed", 100, "Criada", request.vm_id))
                return SimpleNamespace(vm=SimpleNamespace(id=request.vm_id, name=request.name, disk_path=""))

        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(
                Path(directory) / "vms.json", jobs_db_path=Path(directory) / "jobs.sqlite",
                provisioning_service=FakeProvisioner(),
            )
            service.start_jobs()
            try:
                with patch("core.runtime_inventory.inspect_runtime", return_value={"reims_schema_features": ["cpu_topology"]}):
                    accepted = service.submit_provision_job({
                        "idempotency_key": "provision-topology-key-1", "id": "topology-vm", "name": "Topology",
                        "macos_version": "tahoe", "vcpus": 4, "ram_gb": 4, "disk_size_gb": 64,
                        "cpu_topology": {"sockets": 2, "cores": 2, "threads": 1}, "installer_policy": "none",
                    })
                deadline = time.monotonic() + 2
                job = None
                while time.monotonic() < deadline:
                    job = service.get_job({"job_id": accepted["job_id"]})
                    if job["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(job["state"], "succeeded")
                self.assertEqual(observed[0].sockets, 2)
                self.assertEqual(observed[0].cores, 2)
            finally:
                service.close_jobs()

    def test_media_import_is_a_durable_idempotent_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transfer_id = "a" * 32
            transfer_root = root / "transfers"
            transfer_root.mkdir()
            image = bytearray(32775)
            image[16 * 2048 : 16 * 2048 + 7] = b"\x01CD001\x01"
            (transfer_root / f"{transfer_id}--Recovery.iso").write_bytes(image)
            service = WebAgentService(
                root / "vms.json",
                jobs_db_path=root / "jobs.sqlite",
                transfer_root=transfer_root,
            )
            service.start_jobs()
            try:
                accepted = service.submit_media_import_job({"transfer_id": transfer_id})
                deadline = time.monotonic() + 2
                job = None
                while time.monotonic() < deadline:
                    job = service.get_job({"job_id": accepted["job_id"]})
                    if job["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(job["state"], "succeeded", job)
                self.assertEqual(job["result"]["name"], "Recovery.iso")
                self.assertEqual((root / "media" / "Recovery.iso").read_bytes(), bytes(image))
                self.assertTrue((transfer_root / f"{transfer_id}--Recovery.iso").exists())
                duplicate = service.submit_media_import_job({"transfer_id": transfer_id})
                self.assertEqual(duplicate["job_id"], accepted["job_id"])

                missing_id = "b" * 32
                failed = service.submit_media_import_job({"transfer_id": missing_id})
                deadline = time.monotonic() + 2
                failed_job = None
                while time.monotonic() < deadline:
                    failed_job = service.get_job({"job_id": failed["job_id"]})
                    if failed_job["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(failed_job["state"], "failed")
                self.assertTrue(failed_job["retryable"])
                (transfer_root / f"{missing_id}--Recovery.iso").write_bytes(image)
                retried = service.retry_job({"job_id": failed["job_id"]})
                self.assertIn(retried["state"], {"queued", "running", "succeeded"})
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    retried = service.get_job({"job_id": failed["job_id"]})
                    if retried["state"] == "succeeded":
                        break
                    time.sleep(0.01)
                self.assertEqual(retried["state"], "succeeded")
            finally:
                service.close_jobs()

    def test_managed_media_conversion_runs_as_a_durable_job(self):
        import shutil
        import subprocess

        if shutil.which("qemu-img") is None:
            self.skipTest("qemu-img is required for the integration test")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "disk.raw"
            source.write_bytes(b"vHackintosh conversion test".ljust(1024 * 1024, b"\0"))
            service = WebAgentService(root / "vms.json", jobs_db_path=root / "jobs.sqlite")
            service.start_jobs()
            try:
                media_id = hashlib.sha256(os.fsencode(str(source.resolve()))).hexdigest()[:32]
                accepted = service.submit_media_convert_job({
                    "media_id": media_id,
                    "output_format": "qcow2",
                    "idempotency_key": "12345678-1234-4234-8234-123456789abc",
                })
                deadline = time.monotonic() + 10
                job = None
                while time.monotonic() < deadline:
                    job = service.get_job({"job_id": accepted["job_id"]})
                    if job["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(job["state"], "succeeded", job)
                output = media_root / "disk.qcow2"
                self.assertTrue(output.is_file())
                self.assertEqual(job["result"]["format"], "qcow2")
                restored = root / "roundtrip.raw"
                subprocess.run(["qemu-img", "convert", "-f", "qcow2", "-O", "raw", str(output), str(restored)], check=True)
                self.assertEqual(restored.read_bytes(), source.read_bytes())
            finally:
                service.close_jobs()

    def test_apple_recovery_download_is_a_durable_scoped_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def prepare(version, *, job_context=None):
                self.assertEqual(version, "sequoia")
                self.assertIsNotNone(job_context)
                destination = root / "images" / version / "BaseSystem.img"
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"recovery image")
                return destination
            from core import downloader
            with patch.object(downloader, "IMAGES_CACHE_DIR", root / "images"), patch.object(
                downloader.MacOSDownloader, "prepare_installer", side_effect=prepare,
            ):
                service = WebAgentService(root / "vms.json", jobs_db_path=root / "jobs.sqlite")
                try:
                    service.start_jobs()
                    accepted = service.submit_apple_recovery_download_job({
                        "macos_version": "sequoia",
                        "idempotency_key": "12345678-1234-4234-8234-123456789abc",
                    })
                    deadline = time.monotonic() + 3
                    job = None
                    while time.monotonic() < deadline:
                        job = service.get_job({"job_id": accepted["job_id"]})
                        if job["state"] in {"succeeded", "failed"}:
                            break
                        time.sleep(0.01)
                    self.assertEqual(job["state"], "succeeded", job)
                    self.assertEqual(job["result"]["macos_version"], "sequoia")
                    with self.assertRaises(RpcRemoteError):
                        service.submit_apple_recovery_download_job({
                            "macos_version": "unknown",
                            "idempotency_key": "12345678-1234-4234-8234-123456789abc",
                        })
                finally:
                    service.close_jobs()

    def test_media_import_job_checks_cooperative_cancellation_before_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transfer_id = "c" * 32
            transfer_root = root / "transfers"
            transfer_root.mkdir()
            image = bytearray(32775)
            image[16 * 2048 : 16 * 2048 + 7] = b"\x01CD001\x01"
            (transfer_root / f"{transfer_id}--Recovery.iso").write_bytes(image)
            service = WebAgentService(root / "vms.json", transfer_root=transfer_root)

            class Cancelled:
                @staticmethod
                def phase(_name, _progress=None):
                    return None

                @staticmethod
                def check_cancelled():
                    raise RuntimeError("cancellation requested")

            with self.assertRaisesRegex(RuntimeError, "cancellation requested"):
                service._run_media_import_job({"transfer_id": transfer_id}, Cancelled())

            media_root = root / "media"
            self.assertTrue(media_root.is_dir())
            self.assertEqual(list(media_root.iterdir()), [])
            self.assertTrue((transfer_root / f"{transfer_id}--Recovery.iso").is_file())

    def test_provision_rechecks_selected_gpu_uuid_against_live_inventory(self):
        from core.web_rpc import RpcRemoteError

        supported_gpu = {
            "pci_bdf": "0000:03:00.0", "name": "AMD Radeon", "vulkan_uuid": "gpu-uuid-1",
            "selection_supported": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(
                Path(directory) / "vms.json",
                jobs_db_path=Path(directory) / "jobs.sqlite",
                gpu_inventory=lambda: [supported_gpu],
            )
            service.start_jobs()
            try:
                request = {
                    "idempotency_key": "gpu-check-00001", "id": "gpu-vm", "name": "GPU VM",
                    "macos_version": "tahoe", "vcpus": 2, "ram_gb": 4,
                    "installer_policy": "none", "selected_gpu_bdf": supported_gpu["pci_bdf"],
                    "selected_gpu_uuid": "forged-uuid",
                }
                with self.assertRaises(RpcRemoteError) as caught:
                    service.submit_provision_job(request)
                self.assertEqual(caught.exception.code, "gpu_selection_unavailable")
            finally:
                service.close_jobs()

    def test_provision_rejects_stale_gpu_inventory(self):
        from core.web_rpc import RpcRemoteError

        gpu = {"pci_bdf": "0000:03:00.0", "name": "AMD Radeon", "vulkan_uuid": "gpu-uuid-1", "selection_supported": True}
        failing = [False]
        def provider():
            if failing[0]:
                raise TimeoutError("probe failed")
            return [gpu]
        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json", jobs_db_path=Path(directory) / "jobs.sqlite", gpu_inventory=provider)
            service.start_jobs()
            try:
                service.gpu_inventory()
                failing[0] = True
                service.gpu_inventory._cached_at = -100
                with self.assertRaises(RpcRemoteError) as caught:
                    service.submit_provision_job({
                        "idempotency_key": "gpu-stale-00001", "id": "gpu-vm", "name": "GPU VM",
                        "macos_version": "tahoe", "vcpus": 2, "ram_gb": 4,
                        "installer_policy": "none", "selected_gpu_bdf": gpu["pci_bdf"],
                        "selected_gpu_uuid": gpu["vulkan_uuid"],
                    })
                self.assertEqual(caught.exception.code, "gpu_selection_unavailable")
                self.assertIn("desatualizado", str(caught.exception))
            finally:
                service.close_jobs()

    def test_jobs_listing_is_bounded_filtered_and_detail_redacts_private_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(
                Path(directory) / "vms.json",
                jobs_db_path=Path(directory) / "jobs.sqlite",
            )
            store = service.job_store
            job, _ = store.create_job(idempotency_key="job-a", kind="vm.provision", target_vm_id="macos-a")
            store.set_checkpoint(job["id"], "disk", {"message": "disk ready", "installer_path": "/secret/image"}, completed=True)
            store.set_checkpoint(job["id"], "__request", {"installer_path": "/secret/image"})
            store.append_log(job["id"], "Bearer secret-value token=private-value progress", level="info")
            other, _ = store.create_job(idempotency_key="job-b", kind="vm.provision", target_vm_id="macos-b")
            store.transition(other["id"], "running")

            listed = service.list_jobs({"limit": 1, "offset": 0, "states": ["queued", "running"]})
            self.assertEqual(listed["total"], 2)
            self.assertEqual(len(listed["items"]), 1)
            self.assertNotIn("result", listed["items"][0])
            running = service.list_jobs({"states": ["running"]})
            self.assertEqual([item["id"] for item in running["items"]], [other["id"]])
            detail = service.get_job({"job_id": job["id"]})
            self.assertEqual(len(detail["checkpoints"]), 1)
            self.assertNotIn("installer_path", detail["checkpoints"][0]["payload"])
            self.assertIn("[redacted]", detail["logs"][0]["message"])
            self.assertNotIn("private-value", detail["logs"][0]["message"])

    def test_jobs_listing_rejects_unbounded_or_invalid_queries(self):
        from core.web_rpc import RpcRemoteError

        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json", jobs_db_path=Path(directory) / "jobs.sqlite")
            for payload in ({"limit": 101}, {"offset": 10001}, {"states": ["unknown"]}, {"states": []}):
                with self.subTest(payload=payload), self.assertRaises(RpcRemoteError) as caught:
                    service.list_jobs(payload)
                self.assertEqual(caught.exception.code, "invalid_job_query")

    def test_provision_job_feeds_the_inventory_graphics_profile_to_the_service(self):
        """The web path must pass the runtime's profile, not leave it to the disk.py."""
        from core.vm_provisioning import ProvisioningEvent

        class RecordingProvisioner:
            def __init__(self):
                self.request = None

            def provision(self, request, *, on_event=None, cancel_event=None, job_context=None):
                self.request = request
                if on_event:
                    on_event(ProvisioningEvent("disk_opencore", "completed", 90, "Pronto", request.vm_id))
                return SimpleNamespace(
                    vm=SimpleNamespace(id=request.vm_id, name=request.name, disk_path="/vm/disk.qcow2")
                )

        with tempfile.TemporaryDirectory() as directory:
            provisioner = RecordingProvisioner()
            service = WebAgentService(
                Path(directory) / "vms.json", jobs_db_path=Path(directory) / "jobs.sqlite",
                provisioning_service=provisioner,
            )
            try:
                with patch(
                    "core.runtime_inventory.inspect_runtime",
                    return_value={"qemu_graphics_profile": REIMS_PROFILE},
                ):
                    service._run_provision_job(
                        {"vm_id": "profile-vm", "name": "Profile VM", "macos_version": "tahoe",
                         "vcpus": 2, "ram_gb": 4, "installer_policy": "none"},
                        _FakeProvisionContext(),
                    )
                self.assertEqual(provisioner.request.graphics_profile, REIMS_PROFILE)

                # A caller-supplied profile must win over the inventory value.
                with patch(
                    "core.runtime_inventory.inspect_runtime",
                    return_value={"qemu_graphics_profile": REIMS_PROFILE},
                ):
                    service._run_provision_job(
                        {"vm_id": "pinned-vm", "name": "Pinned VM", "macos_version": "tahoe",
                         "vcpus": 2, "ram_gb": 4, "installer_policy": "none",
                         "graphics_profile": LEGACY_PROFILE},
                        _FakeProvisionContext(),
                    )
                self.assertEqual(provisioner.request.graphics_profile, LEGACY_PROFILE)

                # No evidence: stay None so the generator autodetects/refuses.
                with patch(
                    "core.runtime_inventory.inspect_runtime",
                    return_value={"qemu_graphics_profile": None},
                ):
                    service._run_provision_job(
                        {"vm_id": "unknown-vm", "name": "Unknown VM", "macos_version": "tahoe",
                         "vcpus": 2, "ram_gb": 4, "installer_policy": "none"},
                        _FakeProvisionContext(),
                    )
                self.assertIsNone(provisioner.request.graphics_profile)
            finally:
                service.close_jobs()

    def test_explicit_retry_reuses_job_id_and_completed_checkpoints(self):
        class RetryProvisioner:
            calls = 0

            def provision(self, request, *, on_event, cancel_event):
                from core.vm_provisioning import ProvisioningEvent

                self.calls += 1
                on_event(ProvisioningEvent("disk_opencore", "completed", 90, "Pronto", request.vm_id))
                if self.calls == 1:
                    raise RuntimeError("temporary failure")
                return SimpleNamespace(vm=SimpleNamespace(id=request.vm_id, name=request.name, disk_path="/vm/disk.qcow2"))

        with tempfile.TemporaryDirectory() as directory:
            provisioner = RetryProvisioner()
            service = WebAgentService(
                Path(directory) / "vms.json", jobs_db_path=Path(directory) / "jobs.sqlite",
                provisioning_service=provisioner,
            )
            service.start_jobs()
            try:
                accepted = service.submit_provision_job({
                    "idempotency_key": "retry-provision-0001", "id": "retry-vm", "name": "Retry VM",
                    "macos_version": "tahoe", "vcpus": 2, "ram_gb": 4,
                    "installer_policy": "none", "selected_gpu_bdf": None, "selected_gpu_uuid": None,
                })
                job_id = accepted["job_id"]
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    job = service.job_store.get_job(job_id)
                    if job["state"] == "failed":
                        break
                    time.sleep(0.01)
                self.assertEqual(job["state"], "failed")
                detail = service.get_job({"job_id": job_id})
                self.assertTrue(detail["retryable"])
                summary = service.list_jobs({})["items"][0]
                self.assertTrue(summary["retryable"])

                retried = service.retry_job({"job_id": job_id})
                self.assertEqual(retried["id"], job_id)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    job = service.job_store.get_job(job_id)
                    if job["state"] == "succeeded":
                        break
                    time.sleep(0.01)
                self.assertEqual(job["state"], "succeeded")
                self.assertTrue(any(item["name"] == "disk_opencore" and item["completed"]
                                    for item in service.job_store.get_checkpoints(job_id)))
                self.assertEqual(provisioner.calls, 2)
            finally:
                service.close_jobs()

    def test_retry_rejects_missing_and_non_retryable_jobs(self):
        from core.web_rpc import RpcRemoteError

        with tempfile.TemporaryDirectory() as directory:
            service = WebAgentService(Path(directory) / "vms.json", jobs_db_path=Path(directory) / "jobs.sqlite")
            service.start_jobs()
            try:
                self.assertIsNone(service.retry_job({"job_id": "missing-job"}))
                job, _ = service.job_store.create_job(idempotency_key="succeeded-job", kind="vm.provision")
                service.job_store.transition(job["id"], "running")
                service.job_store.transition(job["id"], "succeeded")
                self.assertFalse(service.get_job({"job_id": job["id"]})["retryable"])
                with self.assertRaises(RpcRemoteError) as rejected:
                    service.retry_job({"job_id": job["id"]})
                self.assertEqual(rejected.exception.code, "job_not_retryable")
            finally:
                service.close_jobs()


if __name__ == "__main__":
    unittest.main()
