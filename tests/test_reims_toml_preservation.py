from __future__ import annotations

import tempfile
import stat
import json
import base64
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import AdditionalDiskConfig, CpuTopologyConfig, VMConfig
from core import reims_ctl


class ReimsTomlPreservationTests(unittest.TestCase):
    def test_installer_projection_uses_reims_q35_schema_and_migrates_legacy_root_field(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "sequoia-vm"
            vm_dir.mkdir()
            path = vm_dir / "vm.toml"
            path.write_text(
                'installer = "/old/cache/BaseSystem.img"\n'
                '[q35]\ncustom_runtime_field = "preserve"\n',
                encoding="utf-8",
            )
            vm = VMConfig(id="sequoia-vm", name="Sequoia", macos_version="sequoia")
            installer = "/images/sequoia/BaseSystem.img"
            with patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                reims_ctl.write_vm_toml(vm, installer=installer)
            projected = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("installer", projected)
            self.assertEqual(projected["q35"]["installer"], installer)
            self.assertEqual(projected["q35"]["custom_runtime_field"], "preserve")

            with patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                reims_ctl.write_vm_toml(vm)
            without_installer = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("installer", without_installer)
            self.assertNotIn("installer", without_installer["q35"])
            self.assertEqual(without_installer["q35"]["custom_runtime_field"], "preserve")

    def test_reims_gpu_runtime_identity_is_feature_gated_and_strictly_parsed(self):
        payload = [{
            "name": "gpu-vm",
            "gpu_identity": {
                "configured": {"device_uuid": "reims-uuid", "pci_address": "0000:03:00.0", "driver_file": "/icd/amd.json"},
                "effective": None,
            },
        }]
        with patch("core.runtime_inventory.inspect_runtime", return_value={"reims_schema_features": []}):
            self.assertIsNone(reims_ctl.vm_gpu_identity_snapshot())
        with patch("core.runtime_inventory.inspect_runtime", return_value={"reims_schema_features": ["gpu_identity", "gpu_inventory_json"]}), patch.object(
            reims_ctl, "reims_ctl_bin", return_value="/usr/bin/reims-ctl",
        ), patch.object(reims_ctl, "_run_reims_gpus_bounded", return_value=json.dumps(payload)) as run:
            result = reims_ctl.vm_gpu_identity_snapshot()
        self.assertEqual(result["gpu-vm"]["configured"]["device_uuid"], "reims-uuid")
        self.assertIsNone(result["gpu-vm"]["effective"])
        run.assert_called_once_with("/usr/bin/reims-ctl", "list", "--json")
        duplicate = payload + payload
        with patch("core.runtime_inventory.inspect_runtime", return_value={"reims_schema_features": ["gpu_identity", "gpu_inventory_json"]}), patch.object(
            reims_ctl, "reims_ctl_bin", return_value="/usr/bin/reims-ctl",
        ), patch.object(reims_ctl, "_run_reims_gpus_bounded", return_value=json.dumps(duplicate)):
            self.assertIsNone(reims_ctl.vm_gpu_identity_snapshot())

    def test_gpu_identity_projection_requires_runtime_feature_and_exact_inventory_match(self):
        from core.web_gpu import GpuInventoryItem

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "gpu-vm"
            vm_dir.mkdir()
            vm = VMConfig(
                id="gpu-vm", name="GPU VM", macos_version="tahoe",
                selected_gpu="0000:03:00.0", selected_gpu_name="GPU",
                selected_gpu_uuid="vulkan-uuid", selected_gpu_device_uuid="reims-uuid",
                selected_gpu_pci_address="0000:03:00.0", selected_gpu_driver_file="/icd/amd.json",
            )
            gpu = GpuInventoryItem(
                pci_bdf="0000:03:00.0", vendor_id="1002", device_id="744c", name="GPU",
                kind="amd", render_node="/dev/dri/renderD128", vulkan_uuid="vulkan-uuid",
                driver_file="/icd/amd.json", vulkan_available=True, used_by_host=False,
                selection_supported=True, reims_device_uuid="reims-uuid",
                reims_pci_address="0000:03:00.0", reims_shares_driver=True,
            )
            with patch.object(reims_ctl, "VM_STORAGE_BASE", root), patch(
                "core.runtime_inventory.inspect_runtime", return_value={"reims_schema_features": ["gpu_identity", "gpu_inventory_json"]},
            ), patch("core.web_gpu.inventory_gpus", return_value=[gpu]):
                path = reims_ctl.write_vm_toml(vm)
            projected = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(projected["gpu"], {
                "device_uuid": "reims-uuid", "pci_address": "0000:03:00.0",
                "driver_file": "/icd/amd.json", "name": "GPU",
            })

    def test_gpu_identity_projection_refuses_changed_inventory_without_rewriting_file(self):
        from core.web_gpu import GpuInventoryItem

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "gpu-vm"
            vm_dir.mkdir()
            path = vm_dir / "vm.toml"
            before = '[gpu]\ndevice_uuid = "old-uuid"\npci_address = "0000:03:00.0"\ndriver_file = "/icd/old.json"\n'
            path.write_text(before, encoding="utf-8")
            vm = VMConfig(
                id="gpu-vm", name="GPU VM", macos_version="tahoe",
                selected_gpu="0000:03:00.0", selected_gpu_uuid="vulkan-uuid",
                selected_gpu_device_uuid="old-uuid", selected_gpu_pci_address="0000:03:00.0",
                selected_gpu_driver_file="/icd/old.json",
            )
            gpu = GpuInventoryItem(
                pci_bdf="0000:03:00.0", vendor_id="1002", device_id="744c", name="GPU",
                kind="amd", render_node="/dev/dri/renderD128", vulkan_uuid="vulkan-uuid",
                driver_file="/icd/new.json", vulkan_available=True, used_by_host=False,
                selection_supported=True, reims_device_uuid="new-uuid",
                reims_pci_address="0000:03:00.0", reims_shares_driver=False,
            )
            with patch.object(reims_ctl, "VM_STORAGE_BASE", root), patch(
                "core.runtime_inventory.inspect_runtime", return_value={"reims_schema_features": ["gpu_identity", "gpu_inventory_json"]},
            ), patch("core.web_gpu.inventory_gpus", return_value=[gpu]), self.assertRaisesRegex(
                reims_ctl.GPUSelectionError, "mudou desde que foi configurada",
            ):
                reims_ctl.write_vm_toml(vm)
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_cpu_topology_projection_is_feature_gated_and_omission_preserves_existing_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "topology-vm"
            vm_dir.mkdir()
            path = vm_dir / "vm.toml"
            original = '[q35]\ncpu_topology = { sockets = 2, cores = 2, threads = 1 }\nlegacy_option = "kept"\n'
            path.write_text(original, encoding="utf-8")
            legacy_vm = VMConfig(id="topology-vm", name="Updated", macos_version="tahoe", vcpus=4)
            with patch.object(reims_ctl, "VM_STORAGE_BASE", root), patch(
                "core.runtime_inventory.inspect_runtime", return_value={"reims_schema_features": []},
            ):
                reims_ctl.write_vm_toml(legacy_vm)
            preserved = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(preserved["q35"]["cpu_topology"], {"sockets": 2, "cores": 2, "threads": 1})
            self.assertEqual(preserved["q35"]["legacy_option"], "kept")

            unsupported_change = VMConfig(
                id="topology-vm", name="Changed", macos_version="tahoe", vcpus=4,
                cpu_topology=CpuTopologyConfig(1, 2, 2),
            )
            before = path.read_bytes()
            with patch.object(reims_ctl, "VM_STORAGE_BASE", root), patch(
                "core.runtime_inventory.inspect_runtime", return_value={"reims_schema_features": []},
            ), self.assertRaisesRegex(ValueError, "não anuncia cpu_topology"):
                reims_ctl.write_vm_toml(unsupported_change)
            self.assertEqual(path.read_bytes(), before)

            with patch.object(reims_ctl, "VM_STORAGE_BASE", root), patch(
                "core.runtime_inventory.inspect_runtime", return_value={"reims_schema_features": ["cpu_topology"]},
            ):
                reims_ctl.write_vm_toml(unsupported_change)
            projected = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(projected["q35"]["cpu_topology"], {"sockets": 1, "cores": 2, "threads": 2})

    def test_projection_writes_additional_disks_and_preserves_unowned_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "disks-vm"
            vm_dir.mkdir()
            data_disk = root / "data.qcow2"
            archive = root / "archive.raw"
            data_disk.write_bytes(b"data")
            archive.write_bytes(b"archive")
            vm = VMConfig(
                id="disks-vm", name="Disk VM", macos_version="tahoe",
                additional_disks=[
                    AdditionalDiskConfig(str(data_disk), "qcow2", "ahci", 0),
                    AdditionalDiskConfig(str(archive), "raw", "nvme", 1),
                ],
            )
            with patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                reims_ctl.write_vm_toml(vm)
            first = tomllib.loads((vm_dir / "vm.toml").read_text(encoding="utf-8"))
            self.assertEqual(first["additional_disks"], [
                {"path": str(data_disk), "format": "qcow2", "controller": "ahci", "order": 0},
                {"path": str(archive), "format": "raw", "controller": "nvme", "order": 1},
            ])

            # Older vHackintosh catalog records don't own unknown runtime
            # extensions; writing other settings must leave the TOML tables intact.
            legacy = VMConfig(id="disks-vm", name="Renamed", macos_version="tahoe")
            with patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                reims_ctl.write_vm_toml(legacy)
            preserved = tomllib.loads((vm_dir / "vm.toml").read_text(encoding="utf-8"))
            self.assertEqual(preserved["additional_disks"], first["additional_disks"])

    def test_registry_projection_preserves_other_entries_and_unknown_fields(self):
        source = '''
version = 2
custom_policy = { mode = "strict", tags = ["a", "b"] }

[[vm]]
name = "existing"
dir = "/srv/reims/existing"
custom_label = "keep"

[[vm]]
name = "target"
dir = "/old/path"
custom_vm_option = true
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vms.toml"
            path.write_text(source, encoding="utf-8")
            vm = VMConfig(id="target", name="Target", macos_version="tahoe")
            with patch.object(reims_ctl, "registry_file", return_value=path), patch.object(
                reims_ctl, "VM_STORAGE_BASE", Path(directory) / "managed-vms"
            ):
                reims_ctl.ensure_registered(vm)

            result = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["version"], 2)
            self.assertEqual(result["custom_policy"], {"mode": "strict", "tags": ["a", "b"]})
            self.assertEqual(result["vm"][0], {
                "name": "existing", "dir": "/srv/reims/existing", "custom_label": "keep",
            })
            self.assertEqual(result["vm"][1], {
                "name": "target", "dir": str(Path(directory) / "managed-vms" / "target"),
                "custom_vm_option": True,
            })
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_projection_keeps_an_imported_external_vm_path_and_disk_format(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "external" / "registered-vm"
            vm_dir.mkdir(parents=True)
            vm_toml = vm_dir / "vm.toml"
            vm_toml.write_text('''
name = "registered-vm"
profile = "q35-x86_64"
cpus = 4
memory_mib = 8192
disk = "/mnt/vm-images/system.raw"
disk_format = "raw"

[display]
count = 2
resolution = "2560x1440"
hidpi = true

[q35]
opencore = "/mnt/vm-images/custom-opencore.qcow2"
opencore_format = "qcow2"
firmware_code = "/usr/share/OVMF/custom.fd"
firmware_vars = "custom-vars.fd"
cpu_model = "Haswell-noTSX"
''', encoding="utf-8")
            registry = root / "vms.toml"
            registry.write_text(
                f'[[vm]]\nname = "registered-vm"\ndir = {json.dumps(str(vm_dir))}\n',
                encoding="utf-8",
            )
            vm = VMConfig(
                id="registered-vm", name="Registered VM", macos_version="tahoe",
                disk_path="/mnt/vm-images/system.raw", fullscreen=True,
            )

            with patch.object(reims_ctl, "VM_STORAGE_BASE", root / "managed"), patch.object(
                reims_ctl, "registry_file", return_value=registry
            ):
                self.assertEqual(reims_ctl.vm_dir(vm), vm_dir)
                reims_ctl.write_vm_configuration(vm)

            result = tomllib.loads(vm_toml.read_text(encoding="utf-8"))
            self.assertEqual(result["disk"], "/mnt/vm-images/system.raw")
            self.assertEqual(result["disk_format"], "raw")
            self.assertEqual(result["display"]["count"], 2)
            self.assertEqual(result["display"]["resolution"], "1920x1080")
            self.assertEqual(result["q35"]["opencore"], "/mnt/vm-images/custom-opencore.qcow2")
            self.assertEqual(result["q35"]["firmware_code"], "/usr/share/OVMF/custom.fd")
            self.assertEqual(result["q35"]["firmware_vars"], "custom-vars.fd")

    def test_invalid_registry_is_refused_without_replacing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vms.toml"
            original = b"vm = [\n"
            path.write_bytes(original)
            vm = VMConfig(id="target", name="Target", macos_version="tahoe")
            with patch.object(reims_ctl, "registry_file", return_value=path):
                with self.assertRaisesRegex(ValueError, "preservar o registro Reims"):
                    reims_ctl.ensure_registered(vm)
            self.assertEqual(path.read_bytes(), original)

    def test_symlink_registry_is_refused_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "external.toml"
            original = '[[vm]]\nname = "other"\ndir = "/srv/other"\n'
            target.write_text(original, encoding="utf-8")
            path = root / "vms.toml"
            path.symlink_to(target)
            vm = VMConfig(id="target", name="Target", macos_version="tahoe")
            with patch.object(reims_ctl, "registry_file", return_value=path):
                with self.assertRaisesRegex(ValueError, "link simbólico"):
                    reims_ctl.ensure_registered(vm)
            self.assertTrue(path.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_duplicate_registry_entries_are_refused_without_replacing_them(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vms.toml"
            original = '[[vm]]\nname = "target"\ndir = "/one"\n\n[[vm]]\nname = "target"\ndir = "/two"\n'
            path.write_text(original, encoding="utf-8")
            vm = VMConfig(id="target", name="Target", macos_version="tahoe")
            with patch.object(reims_ctl, "registry_file", return_value=path):
                with self.assertRaisesRegex(ValueError, "entradas duplicadas"):
                    reims_ctl.ensure_registered(vm)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_projection_preserves_unmanaged_values_and_nested_tables(self):
        source = '''
name = "old-name"
profile = "q35-x86_64"
extra_vendor = { mode = "future", flags = ["one", "two"] }
extra_args = ["-action", "reboot=shutdown", "-rtc", "base=utc"]

[display]
count = 2
resolution = "2560x1440"
custom_timing = "keep"

[audio]
enabled = true
model = "virtio"

[network]
enabled = true
mac = "52:54:00:12:34:56"
ssh_port = 2222

[logs]
enabled = true
level = "debug"
path = "custom.log"

[q35]
opencore = "OriginalOpenCore.qcow2"
firmware_vars = "original-vars.fd"
cpu_model = "Haswell-noTSX"
cpu_flags = "+aes,+xsave"
disk_port = 7
unknown_q35_option = true
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "preserve-vm"
            vm_dir.mkdir()
            path = vm_dir / "vm.toml"
            path.write_text(source, encoding="utf-8")
            path.chmod(0o644)
            vm = VMConfig(
                id="preserve-vm", name="Updated VM", macos_version="tahoe",
                vcpus=6, ram_gb=12, disk_size_gb=96,
                display_resolution="1920x1080", cpu_model="Skylake-Client-noTSX-IBRS",
            )

            with patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                self.assertEqual(reims_ctl.write_vm_toml(vm), path)

            result = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["name"], "preserve-vm")
            self.assertEqual(result["cpus"], 6)
            self.assertEqual(result["memory_mib"], 12288)
            self.assertEqual(result["extra_vendor"], {"mode": "future", "flags": ["one", "two"]})
            self.assertEqual(result["display"]["custom_timing"], "keep")
            self.assertEqual(result["display"]["resolution"], "1920x1080")
            self.assertEqual(result["audio"]["model"], "virtio")
            self.assertEqual(result["network"]["mac"], "52:54:00:12:34:56")
            self.assertEqual(result["network"]["ssh_port"], 2222)
            self.assertEqual(result["logs"]["path"], "custom.log")
            self.assertEqual(result["q35"]["unknown_q35_option"], True)
            self.assertEqual(result["q35"]["opencore"], "OriginalOpenCore.qcow2")
            self.assertEqual(result["q35"]["cpu_flags"], "+aes,+xsave")
            self.assertEqual(result["q35"]["disk_port"], 7)
            self.assertEqual(result["extra_args"], ["-action", "reboot=shutdown", "-rtc", "base=utc"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_writer_projects_structured_network_and_audio_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm = VMConfig(
                id="structured-vm", name="Structured", macos_version="tahoe",
                network_enabled=False, network_mac="52:54:00:11:22:33",
                audio_enabled=False, audio_backend="pipewire",
            )
            with patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                path = reims_ctl.write_vm_toml(vm)
            result = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(result["network"]["enabled"])
        self.assertEqual(result["network"]["mac"], "52:54:00:11:22:33")
        self.assertEqual(result["audio"]["backend"], "pipewire")
        self.assertFalse(result["audio"]["enabled"])

    def test_writer_projects_and_disables_only_the_local_ssh_forward(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm = VMConfig(
                id="ssh-vm", name="SSH VM", macos_version="tahoe",
                network_ssh_port=2222,
            )
            with patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                path = reims_ctl.write_vm_toml(vm)
            projected = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(projected["network"]["ssh_port"], 2222)

            vm.network_ssh_port = 0
            with patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                reims_ctl.write_vm_toml(vm)
            disabled = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("ssh_port", disabled["network"])

    def test_writer_projects_multiple_displays_only_when_reims_advertises_option(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm = VMConfig(id="multi-display", name="Multi Display", macos_version="tahoe", display_count=2)
            with patch("core.runtime_inventory.inspect_runtime", return_value={"status": "available", "reims_set_options": ["--displays"]}), patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                path = reims_ctl.write_vm_toml(vm)
            self.assertEqual(tomllib.loads(path.read_text(encoding="utf-8"))["display"]["count"], 2)

            original = path.read_bytes()
            vm.display_count = 3
            with patch("core.runtime_inventory.inspect_runtime", return_value={"status": "available", "reims_set_options": []}), patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                with self.assertRaisesRegex(ValueError, "não confirma alteração"):
                    reims_ctl.write_vm_toml(vm)
            self.assertEqual(path.read_bytes(), original)

    def test_writer_rejects_malformed_network_mac(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "invalid-mac"
            vm_dir.mkdir()
            path = vm_dir / "vm.toml"
            path.write_text('name = "before"\n', encoding="utf-8")
            vm = VMConfig(id="invalid-mac", name="After", macos_version="tahoe", network_mac="bad")
            with patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                with self.assertRaisesRegex(ValueError, "MAC salvo inválido"):
                    reims_ctl.write_vm_toml(vm)
            self.assertEqual(tomllib.loads(path.read_text(encoding="utf-8"))["name"], "before")

    def test_invalid_existing_toml_is_preserved_and_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "broken-vm"
            vm_dir.mkdir()
            path = vm_dir / "vm.toml"
            original = b"name = [\n"
            path.write_bytes(original)
            vm = VMConfig(id="broken-vm", name="Broken", macos_version="tahoe")

            with patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                with self.assertRaisesRegex(ValueError, "preservar o vm.toml"):
                    reims_ctl.write_vm_toml(vm)

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(vm_dir.iterdir()), [path])

    def test_symlink_vm_toml_is_refused_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "linked-vm"
            vm_dir.mkdir()
            target = root / "external.toml"
            original = 'name = "linked-vm"\n'
            target.write_text(original, encoding="utf-8")
            path = vm_dir / "vm.toml"
            path.symlink_to(target)
            vm = VMConfig(id="linked-vm", name="Linked", macos_version="tahoe")
            with patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                with self.assertRaisesRegex(ValueError, "link simbólico"):
                    reims_ctl.write_vm_toml(vm)
            self.assertTrue(path.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_unsupported_existing_qemu_arguments_are_refused_without_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "legacy-vm"
            vm_dir.mkdir()
            path = vm_dir / "vm.toml"
            original = 'name = "legacy-vm"\nextra_args = ["-device", "unsafe"]\n'
            path.write_text(original, encoding="utf-8")
            vm = VMConfig(id="legacy-vm", name="Legacy", macos_version="tahoe")

            with patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                with self.assertRaisesRegex(ValueError, "não está liberada"):
                    reims_ctl.write_vm_toml(vm)

            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_toml_serializer_round_trips_array_tables(self):
        document = {
            "name": "vm",
            "device": [
                {"name": "disk", "options": {"readonly": True}},
                {"name": "net", "options": {"readonly": False}},
            ],
            "after_devices": 42,
        }
        self.assertEqual(tomllib.loads(reims_ctl._toml_dumps(document)), document)

    def test_failed_registry_projection_rolls_back_vm_toml(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm = VMConfig(id="transaction-vm", name="Transaction", macos_version="tahoe")
            vm_dir = root / vm.id
            vm_dir.mkdir()
            vm_toml = vm_dir / "vm.toml"
            vm_original = b'name = "before"\ncustom = true\n'
            vm_toml.write_bytes(vm_original)
            registry = root / "vms.toml"
            registry_original = b'[[vm]]\nname = "other"\ndir = "/other"\n'
            registry.write_bytes(registry_original)

            with patch.object(reims_ctl, "VM_STORAGE_BASE", root), patch.object(
                reims_ctl, "registry_file", return_value=registry
            ), patch.object(reims_ctl, "ensure_registered", side_effect=OSError("simulated registry failure")):
                with self.assertRaisesRegex(OSError, "simulated registry failure"):
                    reims_ctl.write_vm_configuration(vm)

            self.assertEqual(vm_toml.read_bytes(), vm_original)
            self.assertEqual(registry.read_bytes(), registry_original)
            self.assertFalse((vm_dir / ".vhackintosh-reims-projection.json").exists())

    def test_interrupted_projection_journal_restores_both_original_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm = VMConfig(id="recover-vm", name="Recover", macos_version="tahoe")
            vm_dir = root / vm.id
            vm_dir.mkdir()
            vm_toml = vm_dir / "vm.toml"
            registry = root / "vms.toml"
            vm_original = b'name = "before"\n'
            registry_original = b'[[vm]]\nname = "other"\ndir = "/other"\n'
            vm_toml.write_bytes(b'name = "partially-written"\n')
            registry.write_bytes(b'[[vm]]\nname = "recover-vm"\ndir = "/partial"\n')
            with patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                journal = reims_ctl._projection_journal_path(vm)
            journal.write_text(json.dumps({
                "version": 1,
                "vm_id": vm.id,
                "vm_toml": str(vm_toml),
                "registry": str(registry),
                "before": {
                    "vm_toml": base64.b64encode(vm_original).decode("ascii"),
                    "registry": base64.b64encode(registry_original).decode("ascii"),
                },
                "committed": False,
            }), encoding="utf-8")

            with patch.object(reims_ctl, "VM_STORAGE_BASE", root):
                reims_ctl._recover_vm_configuration(vm, journal, registry)

            self.assertEqual(vm_toml.read_bytes(), vm_original)
            self.assertEqual(registry.read_bytes(), registry_original)
            self.assertFalse(journal.exists())


if __name__ == "__main__":
    unittest.main()
