import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import VMConfig
from core.qemu_args import validate_qemu_extra_args
from core.reims_ctl import write_vm_toml


class QemuExtraArgumentsTests(unittest.TestCase):
    def test_allowlisted_pairs_are_preserved_in_order(self):
        args = ["-msg", "timestamp=on", "-rtc", "base=utc"]
        self.assertEqual(validate_qemu_extra_args(args), args)

    def test_rejects_unowned_options_bad_pairs_and_duplicates(self):
        for args in (
            ["-device", "vfio-pci"],
            ["-rtc", "base=localtime"],
            ["-rtc"],
            ["-rtc", "base=utc", "-rtc", "base=utc"],
            ["-msg", "timestamp=on", "-qmp", "stdio"],
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_qemu_extra_args(args)

    def test_writer_keeps_appliance_control_tokens_and_projects_custom_tokens(self):
        vm = VMConfig(id="test-vm", name="Test", macos_version="tahoe")
        vm.qemu_extra_args = ["-rtc", "base=utc", "-msg", "timestamp=on"]
        vm.cpu_model = "Haswell-noTSX"
        with tempfile.TemporaryDirectory() as temp, patch("core.reims_ctl.vm_dir", return_value=Path(temp)):
            path = write_vm_toml(vm)
            with path.open("rb") as file:
                config = tomllib.load(file)
        self.assertEqual(config["extra_args"], ["-action", "reboot=shutdown", *vm.qemu_extra_args])
        self.assertEqual(config["q35"]["cpu_model"], "Haswell-noTSX")


if __name__ == "__main__":
    unittest.main()
