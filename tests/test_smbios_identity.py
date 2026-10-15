import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import SMBIOSConfig
from core.smbios import GenSMBIOS, SMBIOSError


class SMBIOSIdentityTests(unittest.TestCase):
    def test_validate_complete_identity(self):
        GenSMBIOS.validate(SMBIOSConfig(
            model="iMac20,1", serial_number="C02TEST12345", board_serial="C0270000000000001",
            smuuid="12345678-1234-5678-1234-567812345678", rom="02AABBCCDDEE"))

    def test_validate_rejects_missing_identity(self):
        with self.assertRaises(SMBIOSError):
            GenSMBIOS.validate(SMBIOSConfig())

    def test_injects_consistent_identity_and_clears_duplicates(self):
        config = {
            "PlatformInfo": {
                "Generic": {},
                "DataHub": {"SystemSerialNumber": "old"},
                "PlatformNVRAM": {"MLB": "old"},
                "SMBIOS": {"BoardSerialNumber": "old"},
            }
        }
        identity = SMBIOSConfig(
            model="iMac20,1", serial_number="C02TEST12345", board_serial="C0270000000000001",
            smuuid="12345678-1234-5678-1234-567812345678", rom="02AABBCCDDEE")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.plist"
            path.write_bytes(plistlib.dumps(config))
            self.assertTrue(GenSMBIOS.inject_into_config_plist(path, identity))
            actual = plistlib.loads(path.read_bytes())["PlatformInfo"]
        generic = actual["Generic"]
        self.assertEqual(generic["SystemSerialNumber"], identity.serial_number)
        self.assertEqual(generic["MLB"], identity.board_serial)
        self.assertEqual(generic["ROM"], bytes.fromhex(identity.rom))
        self.assertNotIn("SystemSerialNumber", actual["DataHub"])
        self.assertNotIn("MLB", actual["PlatformNVRAM"])
        self.assertNotIn("BoardSerialNumber", actual["SMBIOS"])

    def test_macserial_pair_and_random_vm_fields(self):
        from pathlib import Path
        from unittest.mock import Mock
        with patch.object(GenSMBIOS, "_macserial", return_value=Path("/fake/macserial")), patch(
            "core.smbios.subprocess.run",
            return_value=Mock(stdout="C02TEST12345 | C0270000000000001\n", returncode=0),
        ) as run:
            identity = GenSMBIOS.generate("tahoe")
        self.assertEqual(identity.serial_number, "C02TEST12345")
        self.assertEqual(identity.board_serial, "C0270000000000001")
        self.assertRegex(identity.smuuid, r"^[0-9A-F-]{36}$")
        self.assertRegex(identity.rom, r"^02[0-9A-F]{10}$")
        self.assertEqual(run.call_args.args[0][1:], ["-m", "iMac20,1", "-g", "-n", "1"])

    def test_generator_retries_identity_collision(self):
        from pathlib import Path
        from unittest.mock import Mock
        existing = SMBIOSConfig(
            model="iMac20,1", serial_number="C02TEST12345", board_serial="C0270000000000001",
            smuuid="AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", rom="02AABBCCDDEE")
        replacement = Mock(stdout="C02TEST12346 | C0270000000000002\n", returncode=0)
        with patch("core.config.VMManagerStore") as store, patch.object(
            GenSMBIOS, "_macserial", return_value=Path("/fake/macserial")
        ), patch("core.smbios.subprocess.run", side_effect=[
            Mock(stdout="C02TEST12345 | C0270000000000001\n", returncode=0), replacement
        ]) as run, patch("core.smbios.uuid.uuid4", side_effect=[
            __import__("uuid").UUID("AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"),
            __import__("uuid").UUID("BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"),
        ]):
            store.return_value.list_vms.return_value = [Mock(smbios=existing)]
            identity = GenSMBIOS.generate("tahoe")
        self.assertEqual(identity.serial_number, "C02TEST12346")
        self.assertNotEqual(identity.smuuid, existing.smuuid)
        self.assertEqual(run.call_count, 2)

    def test_macserial_rejects_multiple_records(self):
        from pathlib import Path
        from unittest.mock import Mock
        with patch.object(GenSMBIOS, "_macserial", return_value=Path("/fake/macserial")), patch(
            "core.smbios.subprocess.run",
            return_value=Mock(stdout="C02TEST12345 | C0270000000000001\nC02TEST12346 | C0270000000000002\n", returncode=0),
        ):
            with self.assertRaises(SMBIOSError):
                GenSMBIOS.generate("tahoe")

    def test_missing_macserial_fails_closed(self):
        with patch.object(GenSMBIOS, "_macserial", side_effect=SMBIOSError("missing")):
            with self.assertRaises(SMBIOSError):
                GenSMBIOS.generate("tahoe")

    def test_macserial_resolves_and_self_heals_chmod(self):
        with tempfile.TemporaryDirectory() as td:
            dummy_bin = Path(td) / "macserial"
            dummy_bin.write_bytes(b"\x7fELFfake")
            dummy_bin.chmod(0o644)
            self.assertFalse(bool(dummy_bin.stat().st_mode & 0o111))
            with patch("core.smbios.MACSERIAL_CANDIDATES", (dummy_bin,)):
                found = GenSMBIOS._macserial()
                self.assertEqual(found, dummy_bin)
                self.assertTrue(bool(dummy_bin.stat().st_mode & 0o111))

    def test_macserial_chmod_failure_raises_smbioserror(self):
        with tempfile.TemporaryDirectory() as td:
            dummy_bin = Path(td) / "macserial"
            dummy_bin.write_bytes(b"\x7fELFfake")
            dummy_bin.chmod(0o644)
            with patch("core.smbios.MACSERIAL_CANDIDATES", (dummy_bin,)), patch.object(
                Path, "chmod", side_effect=OSError("Read-only file system")
            ):
                with self.assertRaises(SMBIOSError) as ctx:
                    GenSMBIOS._macserial()
                self.assertIn("macserial is not executable", str(ctx.exception))



if __name__ == "__main__":
    unittest.main()
