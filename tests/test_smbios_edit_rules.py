"""Rules for editing the SMBIOS identity of an existing, stopped VM."""

import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.config import SMBIOSConfig
from core.smbios import (
    EDIT_MODEL_MAX_MACOS,
    MACOS_VERSION_RANK,
    SMBIOSEditError,
    SMBIOSError,
    GenSMBIOS,
)


def identity(**overrides):
    values = {
        "model": "iMac20,1",
        "serial_number": "C02D38RCPN5T",
        "board_serial": "C02204300GUPHC1A8",
        "smuuid": "12345678-1234-5678-1234-567812345678",
        "rom": "02AABBCCDDEE",
    }
    values.update(overrides)
    return SMBIOSConfig(**values)


MACPRO = dict(
    model="MacPro7,1",
    serial_number="F5KZN00HP7QM",
    board_serial="F5K021701GUK3F7FB",
)


class SmbiosEditValidationTests(unittest.TestCase):
    def test_accepts_a_coherent_identity(self):
        result = GenSMBIOS.validate_for_edit(identity())
        self.assertEqual(result.identity.serial_number, "C02D38RCPN5T")
        self.assertEqual(result.warnings, ())
        self.assertEqual(result.changed_fields, ())

    def test_normalizes_case_and_mac_formatted_rom(self):
        result = GenSMBIOS.validate_for_edit(
            identity(serial_number="c02d38rcpn5t", rom="02:aa:bb:cc:dd:ee",
                     smuuid="12345678-1234-5678-1234-567812345678")
        )
        self.assertEqual(result.identity.serial_number, "C02D38RCPN5T")
        self.assertEqual(result.identity.rom, "02AABBCCDDEE")
        self.assertTrue(any("serial_number" in warning for warning in result.warnings))

    def test_refuses_a_model_outside_the_allowlist(self):
        with self.assertRaises(SMBIOSEditError) as ctx:
            GenSMBIOS.validate_for_edit(identity(model="MacBookPro18,3"))
        self.assertIn("supported edit list", str(ctx.exception))

    def test_refuses_a_model_older_than_the_requested_macos(self):
        with self.assertRaises(SMBIOSEditError) as ctx:
            GenSMBIOS.validate_for_edit(identity(model="iMac18,3"), macos_version="tahoe")
        self.assertIn("newest supported macOS is ventura", str(ctx.exception))
        self.assertEqual(
            GenSMBIOS.validate_for_edit(identity(model="iMac18,3"), macos_version="ventura")
            .identity.model,
            "iMac18,3",
        )

    def test_accepts_macpro_with_its_own_factory_prefix(self):
        result = GenSMBIOS.validate_for_edit(identity(**MACPRO), macos_version="tahoe")
        self.assertEqual(result.identity.model, "MacPro7,1")

    def test_refuses_short_serial_and_foreign_factory_code(self):
        for serial in ("C02D38RCPN5", "XYZ38RCPN5T"):
            with self.assertRaises(SMBIOSEditError):
                GenSMBIOS.validate_for_edit(identity(serial_number=serial))

    def test_refuses_the_iso_neutral_identities(self):
        with self.assertRaises(SMBIOSEditError) as serial_error:
            GenSMBIOS.validate_for_edit(
                identity(serial_number="C02VHA000001", board_serial="C02VHA00000000001")
            )
        self.assertIn("neutral serial", str(serial_error.exception))
        with self.assertRaises(SMBIOSEditError):
            GenSMBIOS.validate_for_edit(identity(board_serial="C02VHA00000000001"))

    def test_refuses_mlb_with_a_different_factory_prefix(self):
        with self.assertRaises(SMBIOSEditError) as ctx:
            GenSMBIOS.validate_for_edit(identity(board_serial="F5K204300GUPHC1A8"))
        self.assertIn("factory code", str(ctx.exception))

    def test_refuses_degenerate_mlb(self):
        for mlb in ("C02D38RCPN5T", "00000000000000000", "C02204300GUPHC1A"):
            with self.assertRaises(SMBIOSEditError):
                GenSMBIOS.validate_for_edit(identity(board_serial=mlb))

    def test_refuses_nil_or_malformed_uuid(self):
        for value in ("00000000-0000-0000-0000-000000000000", "not-a-uuid", ""):
            with self.assertRaises(SMBIOSEditError):
                GenSMBIOS.validate_for_edit(identity(smuuid=value))

    def test_refuses_bad_rom(self):
        for rom in ("02AABBCCDD", "01AABBCCDDEE", "00AABBCCDDEE", "FFFFFFFFFFFF", "02AABBCCDDEG"):
            with self.assertRaises(SMBIOSEditError):
                GenSMBIOS.validate_for_edit(identity(rom=rom))

    def test_accepts_other_locally_administered_rom_with_a_warning(self):
        result = GenSMBIOS.validate_for_edit(identity(rom="06AABBCCDDEE"))
        self.assertEqual(result.identity.rom, "06AABBCCDDEE")
        self.assertTrue(any("02" in warning for warning in result.warnings))

    def test_refuses_duplicate_identity_of_another_vm_case_insensitively(self):
        other = identity(smuuid="12345678-1234-5678-1234-567812345678".lower())
        with self.assertRaises(SMBIOSEditError) as ctx:
            GenSMBIOS.validate_for_edit(identity(), other_identities=[other])
        self.assertIn("smuuid", str(ctx.exception))

    def test_reports_only_the_fields_that_changed(self):
        previous = identity()
        result = GenSMBIOS.validate_for_edit(
            identity(board_serial="C02204300GUPHC1B9"), previous=previous
        )
        self.assertEqual(result.changed_fields, ("board_serial",))

    def test_model_change_requires_a_new_serial_pair(self):
        with self.assertRaises(SMBIOSEditError) as ctx:
            GenSMBIOS.validate_for_edit(identity(model="iMac19,1"), previous=identity())
        self.assertIn("new serial/MLB pair", str(ctx.exception))
        result = GenSMBIOS.validate_for_edit(identity(**MACPRO), previous=identity(), macos_version="tahoe")
        self.assertEqual(result.changed_fields, ("model", "serial_number", "board_serial"))

    def test_model_table_only_names_known_macos_versions(self):
        for model, newest in EDIT_MODEL_MAX_MACOS.items():
            self.assertIn(newest, MACOS_VERSION_RANK, model)
        self.assertEqual(EDIT_MODEL_MAX_MACOS["iMac20,1"], "tahoe")


class SmbiosGenerationTests(unittest.TestCase):
    def test_generate_for_model_refuses_before_running_macserial(self):
        with patch.object(GenSMBIOS, "_macserial") as macserial:
            with self.assertRaises(SMBIOSEditError):
                GenSMBIOS.generate_for_model("MacBookPro18,3")
        macserial.assert_not_called()

    def test_generate_for_model_refuses_incoherent_macos_version(self):
        with patch.object(GenSMBIOS, "_macserial") as macserial:
            with self.assertRaises(SMBIOSEditError):
                GenSMBIOS.generate_for_model("iMac18,3", "sequoia")
        macserial.assert_not_called()

    def test_generate_for_model_returns_a_validated_pair(self):
        pair = Mock(stdout="F5KCR1ZPP7QM | F5K021701GUK3F7FB\n", returncode=0)
        with patch.object(GenSMBIOS, "_macserial", return_value=Path("/fake/macserial")), patch(
            "core.smbios.subprocess.run", return_value=pair
        ) as run, patch("core.smbios.uuid.uuid4", return_value=__import__("uuid").UUID(
            "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"
        )), patch("core.config.CONFIG_DIR", Path("/nonexistent-vhackintosh-config")):
            result = GenSMBIOS.generate_for_model("MacPro7,1", "tahoe")
        self.assertEqual(result.model, "MacPro7,1")
        self.assertEqual(result.serial_number, "F5KCR1ZPP7QM")
        self.assertRegex(result.rom, r"^02[0-9A-F]{10}$")
        self.assertEqual(run.call_args.args[0][1:], ["-m", "MacPro7,1", "-g", "-n", "1"])


class SmbiosDecodeTests(unittest.TestCase):
    INFO = (
        "       Country:  C02 - China (Quanta Computer)\n"
        "          Year:    D - 2020\n"
        "          Week:    3 - 29 (15.07.2020-21.07.2020)\n"
        "          Line:  8RC - 1406 (copy 5)\n"
        "         Model: PN5T - iMac20,1\n"
        "   SystemModel: iMac (Retina 5K, 27-inch, 2020)\n"
        "         Valid: Possibly\n"
    )

    def test_decodes_the_model_from_a_serial(self):
        with patch.object(GenSMBIOS, "_macserial", return_value=Path("/fake/macserial")), patch(
            "core.smbios.subprocess.run", return_value=Mock(stdout=self.INFO, stderr="", returncode=0)
        ):
            decoded = GenSMBIOS.decode_serial("C02D38RCPN5T")
        self.assertEqual(decoded["model"], "iMac20,1")
        self.assertEqual(decoded["model_code"], "PN5T")
        self.assertEqual(decoded["warnings"], [])

    def test_unknown_model_decodes_to_none(self):
        info = self.INFO.replace("PN5T - iMac20,1", "0001 - Unknown").replace(
            "   SystemModel: iMac (Retina 5K, 27-inch, 2020)\n", ""
        )
        with patch.object(GenSMBIOS, "_macserial", return_value=Path("/fake/macserial")), patch(
            "core.smbios.subprocess.run", return_value=Mock(stdout=info, stderr="", returncode=0)
        ):
            decoded = GenSMBIOS.decode_serial("C02VHA000001")
        self.assertIsNone(decoded["model"])

    def test_undecodable_output_fails_closed(self):
        with patch.object(GenSMBIOS, "_macserial", return_value=Path("/fake/macserial")), patch(
            "core.smbios.subprocess.run", return_value=Mock(stdout="", stderr="", returncode=0)
        ):
            with self.assertRaises(SMBIOSError):
                GenSMBIOS.decode_serial("C02D38RCPN5T")


class SmbiosPairVerificationTests(unittest.TestCase):
    def info(self, model="iMac20,1"):
        return SmbiosDecodeTests.INFO.replace("iMac20,1", model) if model != "iMac20,1" else SmbiosDecodeTests.INFO

    def test_accepts_a_matching_pair_with_a_valid_checksum(self):
        verify = Mock(stdout="Valid MLB length: modern\nValid MLB checksum.\n", stderr="", returncode=0)
        with patch.object(GenSMBIOS, "_macserial", return_value=Path("/fake/macserial")), patch(
            "core.smbios.subprocess.run",
            side_effect=[Mock(stdout=self.info(), stderr="", returncode=0), verify],
        ):
            result = GenSMBIOS.verify_with_macserial(identity())
        self.assertTrue(result["ok"])
        self.assertEqual(result["mlb_checksum"], "valid")

    def test_rejects_a_serial_from_another_model(self):
        with patch.object(GenSMBIOS, "_macserial", return_value=Path("/fake/macserial")), patch(
            "core.smbios.subprocess.run",
            side_effect=[
                Mock(stdout=self.info("iMac20,1"), stderr="", returncode=0),
                Mock(stdout="Valid MLB checksum.\n", stderr="", returncode=0),
            ],
        ):
            result = GenSMBIOS.verify_with_macserial(identity(model="iMac19,1"))
        self.assertFalse(result["ok"])
        self.assertFalse(result["model_matches"])

    def test_invalid_mlb_checksum_is_read_from_stdout_not_exit_code(self):
        # Measured: macserial --verify prints WARN and still exits 0.
        with patch.object(GenSMBIOS, "_macserial", return_value=Path("/fake/macserial")), patch(
            "core.smbios.subprocess.run",
            side_effect=[
                Mock(stdout=self.info(), stderr="", returncode=0),
                Mock(stdout="Valid MLB length: modern\nWARN: Invalid MLB checksum!\n", stderr="", returncode=0),
            ],
        ):
            result = GenSMBIOS.verify_with_macserial(identity())
        self.assertEqual(result["mlb_checksum"], "invalid")
        self.assertFalse(result["ok"])

    def test_missing_macserial_fails_closed(self):
        with patch.object(GenSMBIOS, "_macserial", side_effect=SMBIOSEditError("missing")):
            result = GenSMBIOS.verify_with_macserial(identity())
        self.assertFalse(result["ok"])
        self.assertEqual(result["mlb_checksum"], "unavailable")


class SmbiosAllowlistIsGeneratableTests(unittest.TestCase):
    """The allowlist must stay inside what the pinned macserial can produce.

    The identity edit injects into the existing config.plist, so the EFI builder
    is no longer in the path and this is the only capability gate left. A model
    that macserial cannot generate, or whose serial decodes to another model,
    would silently ship an incoherent identity.
    """

    def setUp(self):
        try:
            self.tool = GenSMBIOS._macserial()
        except SMBIOSError as exc:
            self.skipTest(f"macserial unavailable: {exc}")

    def _generate_pair(self, model: str) -> str:
        result = subprocess.run(
            [str(self.tool), "-m", model, "-g", "-n", "1"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        records = [line for line in result.stdout.splitlines() if "|" in line]
        self.assertEqual(len(records), 1, f"{model}: macserial did not return one pair: {result.stdout!r}")
        return records[0].split("|")[0].strip()

    def test_every_allowlisted_model_generates_and_decodes_to_itself(self):
        for model in EDIT_MODEL_MAX_MACOS:
            with self.subTest(model=model):
                serial = self._generate_pair(model)
                self.assertRegex(serial, r"^[A-Z0-9]{12}$", model)
                decoded = GenSMBIOS.decode_serial(serial)
                self.assertEqual(decoded["model"], model, f"{model} serial {serial} decoded to {decoded['model']!r}")
                self.assertEqual(decoded["warnings"], [], decoded["warnings"])

    def test_the_table_keeps_macpro7_1_and_no_duplicate_keys(self):
        self.assertIn("MacPro7,1", EDIT_MODEL_MAX_MACOS)
        self.assertEqual(len(EDIT_MODEL_MAX_MACOS), len(set(EDIT_MODEL_MAX_MACOS)))


class SmbiosInjectionTests(unittest.TestCase):
    """The in-place injection is the whole edit: identity in, nothing else out."""

    def plist(self, **platform_overrides):
        platform = {
            "Automatic": False,
            "DataHub": {"SystemSerialNumber": "old-serial", "MLB": "old-mlb"},
            "SMBIOS": {"BoardSerialNumber": "old-board"},
            "PlatformNVRAM": {"BID": "Mac-CFF7D910A743CAAF", "MLB": "old-mlb"},
        }
        platform.update(platform_overrides)
        return {
            "Kernel": {
                "Patch": [{"Comment": "Hide the VM", "Enabled": True, "Find": b"x", "Replace": b"y"}],
                "Add": [{"BundlePath": "MCEReporterDisabler.kext", "Enabled": True}],
            },
            "Misc": {"Boot": {"ShowPicker": True}, "Security": {"SecureBootModel": "Default", "Vault": "Optional"}},
            "NVRAM": {"Add": {"7C436110-AB2A-4BBB-A880-FE41995C9F82": {"csr-active-config": b"\x00\x00\x00\x00"}}},
            "PlatformInfo": platform,
        }

    def _inject(self, config):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.plist"
            path.write_bytes(plistlib.dumps(config))
            GenSMBIOS.inject_into_config_plist(path, identity())
            return plistlib.loads(path.read_bytes())

    def test_creates_generic_when_the_plist_only_has_legacy_sections(self):
        written = self._inject(self.plist())
        generic = written["PlatformInfo"]["Generic"]
        self.assertEqual(generic["SystemProductName"], "iMac20,1")
        self.assertEqual(generic["SystemSerialNumber"], "C02D38RCPN5T")
        self.assertEqual(generic["MLB"], "C02204300GUPHC1A8")
        self.assertEqual(generic["SystemUUID"], "12345678-1234-5678-1234-567812345678")
        self.assertEqual(generic["ROM"], bytes.fromhex("02AABBCCDDEE"))
        self.assertIs(written["PlatformInfo"]["Automatic"], True)

    def test_clears_stale_duplicates_from_the_legacy_sections(self):
        written = self._inject(self.plist())
        platform = written["PlatformInfo"]
        self.assertNotIn("SystemSerialNumber", platform["DataHub"])
        self.assertNotIn("MLB", platform["DataHub"])
        self.assertNotIn("BoardSerialNumber", platform["SMBIOS"])
        self.assertNotIn("MLB", platform["PlatformNVRAM"])
        self.assertEqual(platform["PlatformNVRAM"]["BID"], "Mac-CFF7D910A743CAAF")

    def test_leaves_boot_behaviour_and_security_settings_untouched(self):
        before = self.plist()
        written = self._inject(before)
        for key in ("Kernel", "Misc", "NVRAM"):
            self.assertEqual(written[key], before[key], key)

    def test_creates_generic_but_refuses_a_corrupt_one_and_missing_platforminfo(self):
        # Absent Generic is created; a Generic that is not a dictionary, or a
        # config with no PlatformInfo at all, is refused rather than overwritten.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.plist"
            path.write_bytes(plistlib.dumps(self.plist(Generic="corrupt")))
            with self.assertRaises(SMBIOSError):
                GenSMBIOS.inject_into_config_plist(path, identity())
            path.write_bytes(plistlib.dumps({"Kernel": {}}))
            with self.assertRaises(SMBIOSError):
                GenSMBIOS.inject_into_config_plist(path, identity())


if __name__ == "__main__":
    unittest.main()
