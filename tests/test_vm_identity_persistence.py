import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import SMBIOSConfig, VMConfig, VMManagerStore
from core.smbios import GenSMBIOS


class IdentityPersistenceTests(unittest.TestCase):
    def test_invalid_catalog_is_preserved_and_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config"
            logs = config / "logs"
            data = config / "vms.json"
            data.parent.mkdir(parents=True)
            original = b'{"vms": [broken json'
            data.write_bytes(original)
            with patch("core.config.CONFIG_DIR", config), patch("core.config.LOGS_DIR", logs), patch("core.config.VMS_FILE", data):
                store = VMManagerStore()
                self.assertEqual(store.list_vms(), [])
                with self.assertRaisesRegex(RuntimeError, "could not be loaded"):
                    store.add_vm(VMConfig(id="new", name="New", macos_version="tahoe"))
            self.assertEqual(data.read_bytes(), original)

    def test_identity_survives_store_restart_with_private_file_mode(self):
        identity = SMBIOSConfig("iMac20,1", "C02TEST12345", "C0270000000000001",
                                "12345678-1234-5678-1234-567812345678", "02AABBCCDDEE")
        vm = VMConfig(id="unit-test", name="Unit", macos_version="sequoia", smbios=identity)
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config"
            logs = config / "logs"
            data = config / "vms.json"
            with patch("core.config.CONFIG_DIR", config), patch("core.config.LOGS_DIR", logs), patch("core.config.VMS_FILE", data):
                first = VMManagerStore()
                first.add_vm(vm)
                second = VMManagerStore()
                restored = second.get_vm("unit-test")
                self.assertEqual(restored.smbios, identity)
                self.assertEqual(data.stat().st_mode & 0o777, 0o600)
                GenSMBIOS.validate(restored.smbios)

    def test_duplicate_identity_is_rejected_across_vms(self):
        identity = SMBIOSConfig("iMac20,1", "C02TEST12345", "C0270000000000001",
                                "12345678-1234-5678-1234-567812345678", "02AABBCCDDEE")
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config"
            logs = config / "logs"
            data = config / "vms.json"
            with patch("core.config.CONFIG_DIR", config), patch("core.config.LOGS_DIR", logs), patch("core.config.VMS_FILE", data):
                store = VMManagerStore()
                store.add_vm(VMConfig(id="first", name="First", macos_version="tahoe", smbios=identity))
                conflicting = SMBIOSConfig("iMac20,1", "C02OTHER12345", "C0270000000000002",
                                           "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", identity.rom)
                with self.assertRaisesRegex(ValueError, "conflicts with another VM"):
                    store.add_vm(VMConfig(id="second", name="Second", macos_version="tahoe", smbios=conflicting))

    def test_old_identity_free_vm_loads_without_generating_one(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config"
            logs = config / "logs"
            data = config / "vms.json"
            data.parent.mkdir()
            data.write_text(json.dumps({"vms": {"legacy": {"id": "legacy", "name": "Legacy", "macos_version": "tahoe"}}}))
            with patch("core.config.CONFIG_DIR", config), patch("core.config.LOGS_DIR", logs), patch("core.config.VMS_FILE", data), patch.object(GenSMBIOS, "generate", side_effect=AssertionError("must not generate while loading")):
                vm = VMManagerStore().get_vm("legacy")
            self.assertEqual(vm.smbios.serial_number, "")

    def test_update_vm_without_identity_change_succeeds(self):
        identity = SMBIOSConfig("iMac20,1", "C02TEST12345", "C0270000000000001",
                                "12345678-1234-5678-1234-567812345678", "02AABBCCDDEE")
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config"
            logs = config / "logs"
            data = config / "vms.json"
            with patch("core.config.CONFIG_DIR", config), patch("core.config.LOGS_DIR", logs), patch("core.config.VMS_FILE", data):
                store = VMManagerStore()
                vm = VMConfig(id="tahoe-test", name="Tahoe", macos_version="tahoe", smbios=identity)
                store.add_vm(vm)

                # Mutate non-identity fields and call update_vm
                vm.last_booted_at = "2026-10-15T01:00:00"
                vm.install_phase = "installing"
                vm.ram_gb = 16
                store.update_vm(vm)

                updated = store.get_vm("tahoe-test")
                self.assertEqual(updated.last_booted_at, "2026-10-15T01:00:00")
                self.assertEqual(updated.install_phase, "installing")
                self.assertEqual(updated.ram_gb, 16)
                self.assertEqual(updated.smbios, identity)

    def test_update_vm_with_identity_change_requires_backup(self):
        identity = SMBIOSConfig("iMac20,1", "C02TEST12345", "C0270000000000001",
                                "12345678-1234-5678-1234-567812345678", "02AABBCCDDEE")
        new_identity = SMBIOSConfig("iMac20,1", "C02NEW999999", "C0279999999999999",
                                    "87654321-4321-8765-4321-876543210987", "02FFEEBBDDCC")
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config"
            logs = config / "logs"
            data = config / "vms.json"
            vm_dir = config / "vms" / "tahoe-test"
            vm_dir.mkdir(parents=True)
            with patch("core.config.CONFIG_DIR", config), patch("core.config.LOGS_DIR", logs), patch("core.config.VMS_FILE", data):
                store = VMManagerStore()
                vm = VMConfig(id="tahoe-test", name="Tahoe", macos_version="tahoe", smbios=identity, disk_path=str(vm_dir / "hdd.qcow2"))
                store.add_vm(vm)

                # Without backup, changing SMBIOS must fail
                vm.smbios = new_identity
                with self.assertRaisesRegex(ValueError, "requires a verified OpenCore backup"):
                    store.update_vm(vm)

                # With backup in place, changing SMBIOS must succeed
                backup_dir = vm_dir / "smbios-backup-before-regeneration"
                backup_dir.mkdir()
                (backup_dir / "OpenCore.qcow2").write_bytes(b"dummy backup")
                store.update_vm(vm)

                self.assertEqual(store.get_vm("tahoe-test").smbios, new_identity)

    def test_add_vm_cannot_overwrite_with_different_identity(self):
        identity = SMBIOSConfig("iMac20,1", "C02TEST12345", "C0270000000000001",
                                "12345678-1234-5678-1234-567812345678", "02AABBCCDDEE")
        new_identity = SMBIOSConfig("iMac20,1", "C02NEW999999", "C0279999999999999",
                                    "87654321-4321-8765-4321-876543210987", "02FFEEBBDDCC")
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config"
            logs = config / "logs"
            data = config / "vms.json"
            with patch("core.config.CONFIG_DIR", config), patch("core.config.LOGS_DIR", logs), patch("core.config.VMS_FILE", data):
                store = VMManagerStore()
                store.add_vm(VMConfig(id="tahoe-test", name="Tahoe", macos_version="tahoe", smbios=identity))

                with self.assertRaisesRegex(ValueError, "Refusing to overwrite VM with a different SMBIOS identity"):
                    store.add_vm(VMConfig(id="tahoe-test", name="Tahoe Updated", macos_version="tahoe", smbios=new_identity))

                # Overwriting with same identity should succeed
                store.add_vm(VMConfig(id="tahoe-test", name="Tahoe Renamed", macos_version="tahoe", smbios=identity))
                self.assertEqual(store.get_vm("tahoe-test").name, "Tahoe Renamed")


if __name__ == "__main__":
    unittest.main()
