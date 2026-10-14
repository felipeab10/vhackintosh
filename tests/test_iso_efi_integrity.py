import plistlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IDENTITY_KEYS = {"SystemSerialNumber", "BoardSerialNumber", "MLB", "ROM", "SystemUUID"}


class TemplateIntegrityTests(unittest.TestCase):
    def test_ovmf_code_is_generic_and_vars_matches_neutral_template(self):
        vars_path = ROOT / "templates/OVMF_VARS.fd"
        code_path = ROOT / "templates/OVMF_CODE_4M.fd"
        packaged_vars = ROOT / "appliance/archiso/airootfs/opt/vhackintosh/templates/OVMF_VARS.fd"
        packaged_code = ROOT / "appliance/archiso/airootfs/opt/vhackintosh/templates/OVMF_CODE_4M.fd"
        self.assertEqual(vars_path.stat().st_size, 540672)
        self.assertEqual(code_path.stat().st_size, 3653632)
        self.assertEqual(vars_path.read_bytes(), packaged_vars.read_bytes())
        self.assertEqual(code_path.read_bytes(), packaged_code.read_bytes())
        contents = vars_path.read_bytes().lower()
        for token in (b"systemserialnumber", b"boardserialnumber", b"mlb"):
            self.assertNotIn(token, contents)
        self.assertNotIn(b"macserial", code_path.read_bytes().lower())

    def test_required_kexts_are_enabled_for_supported_kernel_range(self):
        config = plistlib.loads((ROOT / "templates/config.plist").read_bytes())
        entries = {entry["BundlePath"]: entry for entry in config["Kernel"]["Add"]}
        for name in ("Lilu.kext", "ReimsVgpu.kext"):
            self.assertIn(name, entries)
            self.assertTrue(entries[name]["Enabled"])
            self.assertEqual(entries[name]["MinKernel"], "25.0.0")
            self.assertEqual(entries[name]["MaxKernel"], "25.99.99")

    def test_template_identity_fields_are_neutral(self):
        config = plistlib.loads((ROOT / "templates/config.plist").read_bytes())
        for section_name in ("DataHub", "Generic", "PlatformNVRAM", "SMBIOS"):
            section = config["PlatformInfo"][section_name]
            for key in IDENTITY_KEYS & section.keys():
                value = section[key]
                if key == "SystemUUID":
                    self.assertEqual(value, "00000000-0000-0000-0000-000000000000")
                elif key == "ROM":
                    self.assertTrue(not value or not any(value))
                else:
                    self.assertEqual(value, "")

    def test_image_config_matches_external_template(self):
        import shutil
        import subprocess
        import tempfile
        if not shutil.which("guestfish") or not (ROOT / "templates/OpenCore.qcow2").exists():
            self.skipTest("guestfish/OpenCore image not available")
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "config.plist"
            proc = subprocess.run([
                "guestfish", "--ro", "-a", str(ROOT / "templates/OpenCore.qcow2"),
                "-m", "/dev/sda1", "download", "/EFI/OC/config.plist", str(dest)
            ], capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace"))
            self.assertEqual(dest.read_bytes(), (ROOT / "templates/config.plist").read_bytes())


if __name__ == "__main__":
    unittest.main()
