from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.config import AdditionalDiskConfig, SMBIOSConfig, VMConfig, VMManagerStore
from core.vm_backup import VMBackupConflict, VMBackupRunning, VMBackupUnsafe, VMBackupService


VM_TOML = '''name = "studio"
disk = "macos.qcow2"

[q35]
opencore = "OpenCore.qcow2"
firmware_code = "OVMF_CODE.fd"
firmware_vars = "OVMF_VARS.fd"
'''


class VMBackupServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = self.root / "config" / "vms.json"
        self.catalog.parent.mkdir()
        self.vm_root = self.root / "config" / "vms"
        self.vm_dir = self.vm_root / "studio"
        self.vm_dir.mkdir(parents=True)
        self.backups = self.root / "backups"
        self.reservations_lock = self.root / "runtime" / "reservations.lock"
        self.reservations_lock.parent.mkdir()
        self.files = {
            "macos.qcow2": b"disk bytes",
            "OpenCore.qcow2": b"efi bytes",
            "OVMF_CODE.fd": b"code bytes",
            "OVMF_VARS.fd": b"nvram bytes",
            "vm.toml": VM_TOML.encode(),
        }
        for name, content in self.files.items():
            (self.vm_dir / name).write_bytes(content)
        self.vm = VMConfig(
            id="studio", name="Studio", macos_version="tahoe",
            disk_path=str(self.vm_dir / "macos.qcow2"),
            smbios=SMBIOSConfig(serial_number="SERIAL", board_serial="BOARD"),
        )
        VMManagerStore(self.catalog).add_vm(self.vm)

    def tearDown(self):
        self.temp.cleanup()

    def service(self, *, reservations=None, running=({}, True), inspector=None):
        return VMBackupService(
            self.catalog,
            self.backups,
            vm_storage_root=self.vm_root,
            reservations_lock=self.reservations_lock,
            reservations_reader=reservations or (lambda: []),
            running_reader=lambda: running,
            qcow_inspector=inspector or (lambda _path: None),
        )

    def test_creates_atomic_manifested_backup_with_config_identity_and_hashes(self):
        result = self.service().create("studio")
        self.assertTrue(result.is_dir())
        self.assertEqual(result.suffix, ".vhb")
        self.assertEqual({p.name for p in result.iterdir()}, set(self.files) | {"vm-config.json", "manifest.json"})
        manifest = json.loads((result / "manifest.json").read_text())
        self.assertEqual(manifest["format"], "vhackintosh-vm-backup")
        self.assertEqual(manifest["vm_id"], "studio")
        config = json.loads((result / "vm-config.json").read_text())
        self.assertEqual(config["vm_id"], "studio")
        self.assertEqual(config["config"]["smbios"]["serial_number"], "SERIAL")
        self.assertEqual(config["catalog_sha256"], hashlib.sha256(self.catalog.read_bytes()).hexdigest())
        for entry in manifest["files"]:
            content = (result / entry["name"]).read_bytes()
            self.assertEqual(entry["size_bytes"], len(content))
            self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())
        self.assertFalse(list(self.backups.glob("studio/.backup-*")))

    def test_refuses_running_reservation_or_uncertain_runtime_state(self):
        for reservations, running, error in (
            ([{"vm_id": "studio"}], ({}, True), VMBackupRunning),
            ([], ({"studio": 1234}, True), VMBackupRunning),
            ([], ({}, False), VMBackupUnsafe),
        ):
            with self.subTest(error=error), self.assertRaises(error):
                self.service(reservations=lambda rows=reservations: rows, running=running).create("studio")

    def test_cancel_during_copy_removes_staging_and_never_publishes(self):
        (self.vm_dir / "macos.qcow2").write_bytes(b"x" * (3 * 1024 * 1024))
        checks = 0
        progress = []

        def check_cancelled():
            nonlocal checks
            checks += 1
            if checks == 3:
                raise RuntimeError("cancel requested")

        with self.assertRaisesRegex(RuntimeError, "cancel requested"):
            self.service().create(
                "studio",
                check_cancelled=check_cancelled,
                on_progress=lambda copied, total: progress.append((copied, total)),
            )
        self.assertTrue(progress)
        self.assertEqual(list((self.backups / "studio").glob("*.vhb")), [])
        self.assertEqual(list((self.backups / "studio").glob(".backup-*")), [])

    def test_lists_only_published_bundles_and_marks_size_mismatch_invalid(self):
        backup = self.service().create("studio")
        items = self.service().list_backups("studio")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["backup_id"], backup.name)
        self.assertTrue(items[0]["metadata_valid"])
        self.assertEqual(items[0]["file_count"], 7)
        (backup / "macos.qcow2").write_bytes(b"tampered-size")
        corrupted = self.service().list_backups("studio")
        self.assertFalse(corrupted[0]["metadata_valid"])
        self.assertIn("não deve ser restaurado", corrupted[0]["integrity_note"])

    def test_explicit_verification_rechecks_hashes_and_qcow_chains(self):
        backup = self.service().create("studio")
        progress = []
        result = self.service().verify_backup(
            "studio", backup.name,
            on_progress=lambda copied, total: progress.append((copied, total)),
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["file_count"], 6)
        self.assertEqual(len(progress[-1]), 2)
        self.assertEqual(progress[-1][0], progress[-1][1])

        disk = backup / "macos.qcow2"
        disk.write_bytes(b"tampered!!")  # Same length: the cheap inventory remains valid.
        self.assertTrue(self.service().list_backups("studio")[0]["metadata_valid"])
        with self.assertRaisesRegex(VMBackupUnsafe, "Checksum não confere"):
            self.service().verify_backup("studio", backup.name)

    def test_verification_observes_cancellation_without_changing_bundle(self):
        backup = self.service().create("studio")
        checks = 0

        def cancel():
            nonlocal checks
            checks += 1
            if checks == 3:
                raise RuntimeError("verification cancelled")

        with self.assertRaisesRegex(RuntimeError, "verification cancelled"):
            self.service().verify_backup("studio", backup.name, check_cancelled=cancel)
        self.assertEqual(len(self.service().list_backups("studio")), 1)

    def test_restore_verifies_and_replaces_managed_files_and_catalog(self):
        service = self.service()
        backup = service.create("studio")
        store = VMManagerStore(self.catalog)
        current = store.get_vm("studio")
        current.name = "Renamed after backup"
        current.vcpus = 12
        store.update_vm(current)
        (self.vm_dir / "macos.qcow2").write_bytes(b"changed disk")
        result = service.restore("studio", backup.name)
        restored = VMManagerStore(self.catalog).get_vm("studio")
        self.assertTrue(result["restored"])
        self.assertEqual(restored.name, "Studio")
        self.assertEqual(restored.vcpus, self.vm.vcpus)
        self.assertEqual((self.vm_dir / "macos.qcow2").read_bytes(), self.files["macos.qcow2"])
        self.assertEqual((self.vm_dir / "OVMF_VARS.fd").read_bytes(), self.files["OVMF_VARS.fd"])
        self.assertFalse(list(self.vm_root.glob(".studio.restore-journal.json")))
        self.assertFalse(list(self.vm_root.glob(".studio.rollback-*")))

    def test_restore_refuses_corrupted_bundle_and_insufficient_free_space(self):
        service = self.service()
        backup = service.create("studio")
        (backup / "macos.qcow2").write_bytes(b"tampered-size")
        with self.assertRaises(VMBackupUnsafe):
            service.restore("studio", backup.name)
        (backup / "macos.qcow2").write_bytes(self.files["macos.qcow2"])
        # Rebuild the valid archive metadata after the deliberate corruption.
        backup = service.create("studio")
        original = (self.vm_dir / "macos.qcow2").read_bytes()
        with patch("core.vm_backup.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(VMBackupConflict, "Espaço livre insuficiente"):
                service.restore("studio", backup.name)
        self.assertEqual((self.vm_dir / "macos.qcow2").read_bytes(), original)
        self.assertFalse(list(self.vm_root.glob(".studio.restore-*")))

    def test_restore_rolls_back_files_and_catalog_when_catalog_publish_fails(self):
        service = self.service()
        backup = service.create("studio")
        (self.vm_dir / "macos.qcow2").write_bytes(b"current disk")
        with patch("core.vm_backup.VMManagerStore.update_vm", side_effect=OSError("catalog write failed")):
            with self.assertRaisesRegex(OSError, "catalog write failed"):
                service.restore("studio", backup.name)
        self.assertEqual((self.vm_dir / "macos.qcow2").read_bytes(), b"current disk")
        self.assertFalse(list(self.vm_root.glob(".studio.restore-journal.json")))
        self.assertFalse(list(self.vm_root.glob(".studio.rollback-*")))

    def test_restore_recovers_interrupted_directory_swap_before_retry(self):
        service = self.service()
        backup = service.create("studio")
        rollback = self.vm_root / ".studio.rollback-0123456789abcdef0123456789abcdef"
        live = self.vm_dir
        live.rename(rollback)
        journal = self.vm_root / ".studio.restore-journal.json"
        journal.write_text(json.dumps({
            "version": 1,
            "vm_id": "studio",
            "stage": ".studio.restore-abcdef0123456789abcdef0123456789",
            "rollback": rollback.name,
            "backup_id": backup.name,
            "phase": "old_moved",
            "previous_config": self.vm.to_dict(),
        }))
        service.restore("studio", backup.name)
        self.assertTrue(live.is_dir())
        self.assertEqual((live / "macos.qcow2").read_bytes(), self.files["macos.qcow2"])
        self.assertFalse(journal.exists())

    def test_rejects_invalid_id_external_disk_symlink_and_missing_artifact(self):
        with self.assertRaises(VMBackupUnsafe):
            self.service().create("../studio")
        store = VMManagerStore(self.catalog)
        vm = store.get_vm("studio")
        vm.disk_path = str(self.root / "outside.qcow2")
        store.update_vm(vm)
        with self.assertRaises(VMBackupUnsafe):
            self.service().create("studio")
        vm.disk_path = str(self.vm_dir / "macos.qcow2")
        store.update_vm(vm)
        (self.vm_dir / "OVMF_VARS.fd").unlink()
        (self.vm_dir / "OVMF_VARS.fd").symlink_to(self.root / "outside.qcow2")
        with self.assertRaises(VMBackupUnsafe):
            self.service().create("studio")

    def test_rejects_qcow_backing_chain_and_external_installer_reference(self):
        def chained(path):
            raise VMBackupUnsafe("backing chain")

        with self.assertRaises(VMBackupUnsafe):
            self.service(inspector=chained).create("studio")
        (self.vm_dir / "vm.toml").write_text(VM_TOML.replace(
            'name = "studio"', 'name = "studio"\ninstaller = "/outside/installer.iso"',
        ))
        with self.assertRaises(VMBackupUnsafe):
            self.service().create("studio")

    @unittest.skipUnless(shutil.which("qemu-img"), "qemu-img is required to verify real backing-chain detection")
    def test_real_qemu_img_reports_standalone_and_backing_files(self):
        base = self.root / "base.qcow2"
        overlay = self.root / "overlay.qcow2"
        subprocess.run(["qemu-img", "create", "-f", "qcow2", str(base), "1M"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base),
                        str(overlay)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        service = self.service()
        self.assertEqual(service._inspect_qcow(base), [base.resolve()])
        self.assertEqual(service._inspect_qcow(overlay), [overlay.resolve(), base.resolve()])

    def test_backup_verify_and_restore_preserve_safe_sibling_backing_file(self):
        backing = self.vm_dir / "macos-base.qcow2"
        backing.write_bytes(b"base image")

        def inspector(path):
            if path.name == "macos.qcow2":
                return [path, path.parent / "macos-base.qcow2"]
            return [path]

        service = self.service(inspector=inspector)
        backup = service.create("studio")
        self.assertEqual((backup / "macos-base.qcow2").read_bytes(), b"base image")
        self.assertTrue(service.verify_backup("studio", backup.name)["verified"])
        (self.vm_dir / "macos.qcow2").write_bytes(b"changed overlay")
        backing.write_bytes(b"changed base")
        result = service.restore("studio", backup.name)
        self.assertTrue(result["restored"])
        self.assertEqual((self.vm_dir / "macos.qcow2").read_bytes(), self.files["macos.qcow2"])
        self.assertEqual(backing.read_bytes(), b"base image")

    def test_qcow_chain_rejects_external_paths_cycles_and_symlinks(self):
        outside = self.root / "outside.qcow2"
        outside.write_bytes(b"outside")
        disk = self.vm_dir / "macos.qcow2"
        for chain in ([disk, outside], [disk, disk]):
            with self.subTest(chain=chain), self.assertRaises(VMBackupUnsafe):
                self.service(inspector=lambda _path, chain=chain: chain).create("studio")
        (self.vm_dir / "base.qcow2").symlink_to(outside)
        with self.assertRaises(VMBackupUnsafe):
            self.service(inspector=lambda path: [path, path.parent / "base.qcow2"]).create("studio")

    def _configure_additional_qcow(self, disk_path: Path):
        backing = disk_path.with_name(f"{disk_path.stem}-base.qcow2")
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(b"additional overlay")
        backing.write_bytes(b"additional base")
        self.vm.additional_disks = [AdditionalDiskConfig(
            path=str(disk_path), format="qcow2", controller="nvme", order=0,
        )]
        VMManagerStore(self.catalog).update_vm(self.vm)
        with (self.vm_dir / "vm.toml").open("a", encoding="utf-8") as handle:
            handle.write(
                "\n[[additional_disks]]\n"
                f'path = "{disk_path}"\n'
                'format = "qcow2"\ncontroller = "nvme"\norder = 0\n'
            )
        return backing

    @staticmethod
    def _additional_qcow_inspector(path: Path):
        if path.name == "macos.qcow2":
            backing = path.parent / "macos-base.qcow2"
            return [path, backing] if backing.exists() else [path]
        if path.name == "data.qcow2":
            return [path, path.parent / "data-base.qcow2"]
        return [path]

    def test_multiple_disk_backup_verify_restore_includes_external_chain(self):
        extra = self.root / "config" / "media" / "data.qcow2"
        backing = self._configure_additional_qcow(extra)
        raw_disk = self.root / "config" / "images" / "archive.raw"
        raw_disk.parent.mkdir()
        raw_disk.write_bytes(b"raw archive disk")
        self.vm.additional_disks.append(AdditionalDiskConfig(
            path=str(raw_disk), format="raw", controller="ahci", order=1,
        ))
        VMManagerStore(self.catalog).update_vm(self.vm)
        with (self.vm_dir / "vm.toml").open("a", encoding="utf-8") as handle:
            handle.write(
                "\n[[additional_disks]]\n"
                f'path = "{raw_disk}"\n'
                'format = "raw"\ncontroller = "ahci"\norder = 1\n'
            )
        inspector = self._additional_qcow_inspector
        service = self.service(inspector=inspector)
        backup = service.create("studio")
        self.assertEqual(
            {path.name for path in (backup / "additional-disks" / "00").iterdir()},
            {"data.qcow2", "data-base.qcow2"},
        )
        self.assertEqual((backup / "additional-disks" / "01" / "archive.raw").read_bytes(), b"raw archive disk")
        self.assertTrue(service.verify_backup("studio", backup.name)["verified"])
        extra.write_bytes(b"changed overlay")
        backing.write_bytes(b"changed base")
        raw_disk.write_bytes(b"changed raw archive")
        result = service.restore("studio", backup.name)
        self.assertTrue(result["restored"])
        self.assertEqual(extra.read_bytes(), b"additional overlay")
        self.assertEqual(backing.read_bytes(), b"additional base")
        self.assertEqual(raw_disk.read_bytes(), b"raw archive disk")
        restored_disks = VMManagerStore(self.catalog).get_vm("studio").additional_disks
        self.assertEqual([disk.path for disk in restored_disks], [str(extra), str(raw_disk)])
        self.assertFalse(list(extra.parent.glob(".studio.restore-*.new")))
        self.assertFalse(list(extra.parent.glob(".studio.restore-*.rollback")))

    @unittest.skipUnless(shutil.which("qemu-img"), "qemu-img is required for a real multi-disk chain round trip")
    def test_real_qemu_img_multi_disk_chain_round_trip(self):
        media = self.root / "config" / "media"
        media.mkdir()
        base = media / "data-base.qcow2"
        overlay = media / "data.qcow2"
        subprocess.run(["qemu-img", "create", "-f", "qcow2", str(base), "1M"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", base.name,
                        str(overlay)], cwd=media, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.vm.additional_disks = [AdditionalDiskConfig(
            path=str(overlay), format="qcow2", controller="nvme", order=0,
        )]
        VMManagerStore(self.catalog).update_vm(self.vm)
        with (self.vm_dir / "vm.toml").open("a", encoding="utf-8") as handle:
            handle.write(
                "\n[[additional_disks]]\n"
                f'path = "{overlay}"\n'
                'format = "qcow2"\ncontroller = "nvme"\norder = 0\n'
            )

        def inspect(path):
            if path.name in {"macos.qcow2", "OpenCore.qcow2"}:
                return [path]  # These fixture files are intentionally not QCOW images.
            return VMBackupService._inspect_qcow(path)

        service = self.service(inspector=inspect)
        backup = service.create("studio")
        verified = service.verify_backup("studio", backup.name)
        self.assertTrue(verified["verified"])
        overlay.write_bytes(b"damaged")
        restored = service.restore("studio", backup.name)
        self.assertTrue(restored["restored"])
        subprocess.run(["qemu-img", "check", str(overlay)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_multiple_disk_restore_rolls_back_external_files_on_catalog_failure(self):
        extra = self.root / "config" / "media" / "data.qcow2"
        backing = self._configure_additional_qcow(extra)
        service = self.service(inspector=self._additional_qcow_inspector)
        backup = service.create("studio")
        extra.write_bytes(b"current overlay")
        backing.write_bytes(b"current base")
        (self.vm_dir / "macos.qcow2").write_bytes(b"current primary")
        with patch("core.vm_backup.VMManagerStore.update_vm", side_effect=OSError("catalog write failed")):
            with self.assertRaisesRegex(OSError, "catalog write failed"):
                service.restore("studio", backup.name)
        self.assertEqual(extra.read_bytes(), b"current overlay")
        self.assertEqual(backing.read_bytes(), b"current base")
        self.assertEqual((self.vm_dir / "macos.qcow2").read_bytes(), b"current primary")
        self.assertFalse((self.vm_root / ".studio.restore-journal.json").exists())

    def test_multiple_disk_restore_recovers_after_partial_external_publish(self):
        extra = self.root / "config" / "media" / "data.qcow2"
        backing = self._configure_additional_qcow(extra)
        service = self.service(inspector=self._additional_qcow_inspector)
        backup = service.create("studio")
        extra.write_bytes(b"current overlay")
        backing.write_bytes(b"current base")
        (self.vm_dir / "macos.qcow2").write_bytes(b"current primary")
        original_replace = __import__("os").replace

        def fail_on_second_external_publish(source, destination):
            if Path(source).name.endswith(".new") and Path(destination).name == "data-base.qcow2":
                raise OSError("injected external publish interruption")
            return original_replace(source, destination)

        with patch("core.vm_backup.os.replace", side_effect=fail_on_second_external_publish):
            with self.assertRaisesRegex(OSError, "external publish interruption"):
                service.restore("studio", backup.name)
        self.assertEqual(extra.read_bytes(), b"current overlay")
        self.assertEqual(backing.read_bytes(), b"current base")
        self.assertEqual((self.vm_dir / "macos.qcow2").read_bytes(), b"current primary")
        self.assertFalse((self.vm_root / ".studio.restore-journal.json").exists())
        self.assertFalse(list(extra.parent.glob(".studio.restore-*.rollback")))

    def test_multiple_disk_backup_rejects_unmanaged_or_symlinked_paths_and_corruption(self):
        media = self.root / "config" / "media"
        media.mkdir()
        external = self.root / "outside.qcow2"
        external.write_bytes(b"outside")
        self.vm.additional_disks = [AdditionalDiskConfig(path=str(external), order=0)]
        VMManagerStore(self.catalog).update_vm(self.vm)
        with self.assertRaises(VMBackupUnsafe):
            self.service().create("studio")

        disk = media / "data.qcow2"
        backing = self._configure_additional_qcow(disk)
        disk.unlink()
        disk.symlink_to(external)
        with self.assertRaises(VMBackupUnsafe):
            self.service(inspector=self._additional_qcow_inspector).create("studio")
        disk.unlink()
        disk.write_bytes(b"additional overlay")
        service = self.service(inspector=self._additional_qcow_inspector)
        backup = service.create("studio")
        archived_base = backup / "additional-disks" / "00" / "data-base.qcow2"
        archived_base.write_bytes(b"X" * len(b"additional base"))
        with self.assertRaisesRegex(VMBackupUnsafe, "Checksum não confere"):
            service.verify_backup("studio", backup.name)

    def test_cleans_staging_on_interrupted_copy_and_does_not_publish_partial_backup(self):
        service = self.service()
        with patch.object(service, "_copy_hash", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                service.create("studio")
        self.assertEqual(list((self.backups / "studio").glob("*.vhb")), [])
        self.assertEqual(list((self.backups / "studio").glob(".backup-*")), [])
        for name, content in self.files.items():
            self.assertEqual((self.vm_dir / name).read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
