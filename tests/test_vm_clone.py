from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import AdditionalDiskConfig, SMBIOSConfig, VMConfig, VMManagerStore
from core.vm_backup import VMBackupConflict, VMBackupRunning, VMBackupService, VMBackupUnsafe
from core.vm_clone import VMCloneService


CLONE_TOML = '''name = "studio"
disk = "macos.qcow2"

[network]
enabled = true
mac = "52:54:00:00:00:01"

[q35]
opencore = "OpenCore.qcow2"
firmware_code = "OVMF_CODE.fd"
firmware_vars = "OVMF_VARS.fd"
'''


class VMCloneServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = self.root / "config" / "vms.json"
        self.catalog.parent.mkdir()
        self.vm_root = self.root / "config" / "vms"
        self.source_dir = self.vm_root / "studio"
        self.source_dir.mkdir(parents=True)
        self.original = {
            "macos.qcow2": b"guest disk",
            "OpenCore.qcow2": b"original OpenCore",
            "OVMF_CODE.fd": b"code",
            "OVMF_VARS.fd": b"saved variables",
            "vm.toml": CLONE_TOML.encode(),
        }
        for name, data in self.original.items():
            (self.source_dir / name).write_bytes(data)
        self.vm = VMConfig(
            id="studio", name="Studio", macos_version="sequoia", vcpus=6,
            disk_path=str(self.source_dir / "macos.qcow2"),
            smbios=SMBIOSConfig(model="iMac20,1", serial_number="C02TESTSERIAL", board_serial="C0270000000000001",
                                smuuid="11111111-2222-4333-8444-555555555555", rom="020000000001"),
        )
        VMManagerStore(self.catalog).add_vm(self.vm)
        self.template = self.root / "OVMF_VARS.fd"
        self.template.write_bytes(b"clean nvram template")

    def tearDown(self):
        self.temp.cleanup()

    def service(self, *, running=({}, True), mac="52:54:00:aa:bb:cc", inspector=None):
        (self.root / "runtime").mkdir(exist_ok=True)
        return VMCloneService(
            self.catalog,
            vm_storage_root=self.vm_root,
            reservations_lock=self.root / "runtime" / "reservations.lock",
            reservations_reader=lambda: [],
            running_reader=lambda: running,
            qcow_inspector=inspector or (lambda _path: None),
            smbios_factory=lambda _source: SMBIOSConfig(
                model="iMac20,1", serial_number="C02CLONESERIAL", board_serial="C0270000000000002",
                smuuid="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", rom="020000000002",
            ),
            mac_factory=lambda: mac,
            opencore_rewriter=lambda path, clone: path.write_bytes(f"OpenCore:{clone.smbios.serial_number}".encode()),
            nvram_template=self.template,
        )

    def test_clone_copies_managed_files_and_assigns_independent_identity(self):
        result = self.service().clone("studio", "studio-copy", "Studio copy")
        clone_dir = self.vm_root / "studio-copy"
        clone = VMManagerStore(self.catalog).get_vm("studio-copy")
        self.assertTrue(result["created"])
        self.assertEqual(result["mac"], "52:54:00:aa:bb:cc")
        self.assertEqual(clone.name, "Studio copy")
        self.assertEqual(clone.disk_path, str(clone_dir / "macos.qcow2"))
        self.assertFalse(clone.auto_start)
        self.assertNotEqual(clone.smbios.serial_number, self.vm.smbios.serial_number)
        self.assertEqual((clone_dir / "macos.qcow2").read_bytes(), self.original["macos.qcow2"])
        self.assertEqual((clone_dir / "OVMF_VARS.fd").read_bytes(), self.template.read_bytes())
        self.assertEqual((clone_dir / "OpenCore.qcow2").read_bytes(), b"OpenCore:C02CLONESERIAL")
        toml = (clone_dir / "vm.toml").read_text()
        self.assertIn('name = "studio-copy"', toml)
        self.assertIn('mac = "52:54:00:aa:bb:cc"', toml)
        self.assertEqual({name: (self.source_dir / name).read_bytes() for name in self.original}, self.original)
        self.assertFalse(list(self.vm_root.glob(".studio-copy.clone-*")))
        self.assertIsNone(clone.additional_disks, "legacy VMs without a list keep the field unowned")
        manifest = json.loads((clone_dir / "clone-manifest.json").read_text())
        self.assertEqual(manifest["format"], "vhackintosh-vm-clone")
        main_record = next(item for item in manifest["disks"] if item["role"] == "primary")
        self.assertEqual(main_record["sha256"], hashlib.sha256((clone_dir / "macos.qcow2").read_bytes()).hexdigest())

    def test_clone_rejects_running_source_existing_identity_and_mac_collision(self):
        with self.assertRaises(VMBackupRunning):
            self.service(running=({"studio": 4321}, True)).clone("studio", "copy-one", "Copy")
        with self.assertRaises(VMBackupConflict):
            self.service().clone("studio", "studio", "Copy")
        with self.assertRaises(VMBackupConflict):
            self.service(mac="52:54:00:00:00:01").clone("studio", "copy-two", "Copy")
        self.assertFalse((self.vm_root / "copy-one").exists())

    def test_vm_toml_writer_changes_only_unique_identity_fields(self):
        duplicate = CLONE_TOML.replace('[q35]', 'mac = "52:54:00:00:00:03"\n\n[q35]')
        with self.assertRaises(VMBackupUnsafe):
            VMCloneService._replace_toml_value(duplicate, "mac", "52:54:00:00:00:02", section="network")

    def test_clone_failure_before_publish_preserves_source_and_removes_staging(self):
        service = self.service()
        service.opencore_rewriter = lambda *_args: (_ for _ in ()).throw(RuntimeError("EFI update failed"))
        with self.assertRaisesRegex(RuntimeError, "EFI update failed"):
            service.clone("studio", "failed-copy", "Failed copy")
        self.assertFalse((self.vm_root / "failed-copy").exists())
        self.assertFalse(list(self.vm_root.glob(".failed-copy.clone-*")))
        self.assertFalse((self.vm_root / ".failed-copy.clone-journal.json").exists())
        self.assertEqual({name: (self.source_dir / name).read_bytes() for name in self.original}, self.original)
        self.assertIsNone(VMManagerStore(self.catalog).get_vm("failed-copy"))

    def test_clone_catalog_failure_rolls_back_published_directory(self):
        with patch("core.vm_clone.VMManagerStore.add_vm", side_effect=OSError("catalog write failed")):
            with self.assertRaisesRegex(OSError, "catalog write failed"):
                self.service().clone("studio", "failed-copy", "Failed copy")
        self.assertFalse((self.vm_root / "failed-copy").exists())
        self.assertFalse((self.vm_root / ".failed-copy.clone-journal.json").exists())
        self.assertEqual((self.source_dir / "macos.qcow2").read_bytes(), self.original["macos.qcow2"])

    def _configure_additional_disk(self, disk_path: Path, *, fmt: str, controller: str = "nvme"):
        self.vm.additional_disks = [AdditionalDiskConfig(str(disk_path), fmt, controller, 0)]
        VMManagerStore(self.catalog).update_vm(self.vm)
        entry = f'''\n[[additional_disks]]\npath = {json.dumps(str(disk_path))}\nformat = "{fmt}"\ncontroller = "{controller}"\norder = 0\n'''
        (self.source_dir / "vm.toml").write_text(CLONE_TOML + entry, encoding="utf-8")

    def test_clone_copies_additional_raw_disk_and_rewrites_catalog_and_toml(self):
        media = self.root / "config" / "media"
        media.mkdir()
        source_extra = media / "Data.raw"
        source_bytes = b"independent raw disk data" * 200
        source_extra.write_bytes(source_bytes)
        self._configure_additional_disk(source_extra, fmt="raw", controller="ahci")

        self.service().clone("studio", "studio-copy", "Studio copy")

        clone_dir = self.vm_root / "studio-copy"
        cloned_extra = clone_dir / "additional-disk-00.raw"
        cloned_config = VMManagerStore(self.catalog).get_vm("studio-copy")
        self.assertEqual(cloned_extra.read_bytes(), source_bytes)
        self.assertEqual(cloned_config.additional_disks, [
            AdditionalDiskConfig(str(cloned_extra), "raw", "ahci", 0),
        ])
        self.assertIn(f'path = "{cloned_extra}"', (clone_dir / "vm.toml").read_text())
        self.assertEqual(source_extra.read_bytes(), source_bytes)
        manifest = json.loads((clone_dir / "clone-manifest.json").read_text())
        record = next(item for item in manifest["disks"] if item["role"] == "additional")
        self.assertEqual(record["file"], cloned_extra.name)
        self.assertEqual(record["controller"], "ahci")
        self.assertEqual(record["sha256"], hashlib.sha256(source_bytes).hexdigest())

    @unittest.skipUnless(shutil.which("qemu-img"), "qemu-img required for real backing-chain test")
    def test_clone_materializes_real_additional_qcow_backing_chain(self):
        media = self.root / "config" / "media"
        media.mkdir()
        base = media / "base.qcow2"
        overlay = media / "overlay.qcow2"
        subprocess.run(["qemu-img", "create", "-f", "qcow2", str(base), "2M"], check=True, capture_output=True)
        subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(overlay)], check=True, capture_output=True)
        original_base = base.read_bytes()
        original_overlay = overlay.read_bytes()
        self._configure_additional_disk(overlay, fmt="qcow2")

        def inspect(path):
            if path == overlay or path.name == "additional-disk-00.qcow2":
                return VMBackupService._inspect_qcow(path)
            return None

        self.service(inspector=inspect).clone("studio", "studio-copy", "Studio copy")

        cloned_extra = self.vm_root / "studio-copy" / "additional-disk-00.qcow2"
        chain = VMBackupService._inspect_qcow(cloned_extra)
        self.assertEqual(len(chain), 1)
        self.assertEqual(base.read_bytes(), original_base)
        self.assertEqual(overlay.read_bytes(), original_overlay)
        manifest = json.loads((cloned_extra.parent / "clone-manifest.json").read_text())
        record = next(item for item in manifest["disks"] if item["role"] == "additional")
        self.assertTrue(record["backing_chain_materialized"])
        self.assertEqual(record["sha256"], hashlib.sha256(cloned_extra.read_bytes()).hexdigest())

    @unittest.skipUnless(shutil.which("qemu-img"), "qemu-img required for real backing-chain test")
    def test_clone_materializes_real_primary_qcow_backing_chain(self):
        base = self.source_dir / "base.qcow2"
        primary = self.source_dir / "macos.qcow2"
        subprocess.run(["qemu-img", "create", "-f", "qcow2", str(base), "2M"], check=True, capture_output=True)
        subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(primary)], check=True, capture_output=True)
        original_base = base.read_bytes()
        original_primary = primary.read_bytes()

        def inspect(path):
            if path == primary or path.name == "macos.qcow2":
                return VMBackupService._inspect_qcow(path)
            return None

        self.service(inspector=inspect).clone("studio", "studio-copy", "Studio copy")

        cloned_primary = self.vm_root / "studio-copy" / "macos.qcow2"
        self.assertEqual(len(VMBackupService._inspect_qcow(cloned_primary)), 1)
        self.assertEqual(base.read_bytes(), original_base)
        self.assertEqual(primary.read_bytes(), original_primary)
        manifest = json.loads((cloned_primary.parent / "clone-manifest.json").read_text())
        record = next(item for item in manifest["disks"] if item["role"] == "primary")
        self.assertTrue(record["backing_chain_materialized"])

    def test_clone_failure_after_additional_copy_rolls_back_every_staged_disk(self):
        media = self.root / "config" / "media"
        media.mkdir()
        source_extra = media / "Data.raw"
        source_bytes = b"preserve source while clone rolls back" * 1024
        source_extra.write_bytes(source_bytes)
        self._configure_additional_disk(source_extra, fmt="raw")
        service = self.service()
        service.opencore_rewriter = lambda *_args: (_ for _ in ()).throw(RuntimeError("EFI update failed"))

        with self.assertRaisesRegex(RuntimeError, "EFI update failed"):
            service.clone("studio", "failed-copy", "Failed copy")

        self.assertEqual(source_extra.read_bytes(), source_bytes)
        self.assertFalse((self.vm_root / "failed-copy").exists())
        self.assertFalse(list(self.vm_root.glob(".failed-copy.clone-*")))
        self.assertIsNone(VMManagerStore(self.catalog).get_vm("failed-copy"))

    def test_clone_rejects_external_additional_disk_without_touching_it(self):
        external = self.root / "external.raw"
        external.write_bytes(b"external managed? no")
        self._configure_additional_disk(external, fmt="raw")
        with self.assertRaises(VMBackupUnsafe):
            self.service().clone("studio", "studio-copy", "Studio copy")
        self.assertEqual(external.read_bytes(), b"external managed? no")
        self.assertFalse((self.vm_root / "studio-copy").exists())


if __name__ == "__main__":
    unittest.main()
