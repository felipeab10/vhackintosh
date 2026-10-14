import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import SMBIOSConfig, VMConfig, VMManagerStore
from core.smbios import GenSMBIOS


class IdentityPersistenceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
