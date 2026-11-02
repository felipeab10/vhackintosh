from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.disk import DiskProvisioner
from core.vm_lifecycle import decide_reimsctl


class OVMFVarsRecoveryTests(unittest.TestCase):
    def test_start_seeds_missing_nvram_from_packaged_template(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "vm"
            vm_dir.mkdir()
            (vm_dir / "macos.qcow2").touch()
            (vm_dir / "OpenCore.qcow2").touch()
            (vm_dir / "OVMF_CODE.fd").touch()
            template = root / "templates" / "OVMF_VARS.fd"
            template.parent.mkdir()
            template.write_bytes(b"clean-4m-nvram")
            qemu = root / "qemu-system-x86_64"
            qemu.touch()

            with patch("core.reims_ctl.reims_ctl_bin", return_value="/usr/bin/reims-ctl"), \
                 patch("core.reims_build.qemu_binary", return_value=qemu), \
                 patch("core.disk.DEFAULT_OVMF_VARS_TEMPLATES", [template]):
                available, reason = decide_reimsctl("/opt/reims-vgpu", "sequoia", vm_dir)

            self.assertTrue(available, reason)
            self.assertEqual((vm_dir / "OVMF_VARS.fd").read_bytes(), b"clean-4m-nvram")
            self.assertEqual((vm_dir / "OVMF_VARS.fd").stat().st_mode & 0o777, 0o600)

    def test_start_never_overwrites_existing_nvram(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "vm"
            vm_dir.mkdir()
            for name in ("macos.qcow2", "OpenCore.qcow2", "OVMF_CODE.fd"):
                (vm_dir / name).touch()
            nvram = vm_dir / "OVMF_VARS.fd"
            nvram.write_bytes(b"existing-nvram-state")
            template = root / "OVMF_VARS.template.fd"
            template.write_bytes(b"clean-template")
            qemu = root / "qemu-system-x86_64"
            qemu.touch()

            with patch("core.reims_ctl.reims_ctl_bin", return_value="/usr/bin/reims-ctl"), \
                 patch("core.reims_build.qemu_binary", return_value=qemu), \
                 patch("core.disk.DEFAULT_OVMF_VARS_TEMPLATES", [template]):
                available, reason = decide_reimsctl("/opt/reims-vgpu", "sequoia", vm_dir)

            self.assertTrue(available, reason)
            self.assertEqual(nvram.read_bytes(), b"existing-nvram-state")


if __name__ == "__main__":
    unittest.main()
