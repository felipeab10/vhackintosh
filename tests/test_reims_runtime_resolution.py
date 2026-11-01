from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import reims_build
from core.vm_lifecycle import _ensure_reims_release
from core.reims_build import reims_ctl_install_dir
from core.reims_ctl import reims_ctl_bin


class UserInstalledReimsRuntimeTests(unittest.TestCase):
    def test_explicit_cargo_home_resolves_and_installs_reims_ctl_without_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "toolchain" / "bin" / "reims-ctl"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)

            with patch.dict("os.environ", {
                "CARGO_HOME": str(root / "toolchain"),
                "HOME": str(root / "unrelated-home"),
                "PATH": "",
            }, clear=True):
                self.assertEqual(reims_ctl_bin(), str(binary))
                self.assertEqual(reims_ctl_install_dir(), binary.parent)

    def test_resolves_installed_user_runtime_when_no_release_bundle_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "qemu/current/bin/qemu-system-x86_64"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            runtime = root / "reims-ctl"

            with (
                patch.object(reims_build, "find_reims_dir", return_value=None),
                patch.object(reims_build, "qemu_binary", return_value=binary),
                patch.object(reims_build, "qemu_data_dir", return_value=runtime),
                patch("core.reims_ctl.reims_ctl_bin", return_value="/tmp/reims-ctl"),
            ):
                self.assertEqual(reims_build.find_launch_runtime_dir(), runtime)

    def test_developer_host_prefers_its_installed_runtime_over_appliance_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "qemu/current/bin/qemu-system-x86_64"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            runtime = root / "user-runtime"
            appliance_bundle = root / "opt/reims-vgpu"

            with (
                patch("core.reims_build.os.geteuid", return_value=1000),
                patch.object(reims_build, "find_reims_dir", return_value=appliance_bundle),
                patch.object(reims_build, "qemu_binary", return_value=binary),
                patch.object(reims_build, "qemu_data_dir", return_value=runtime),
                patch("core.reims_ctl.reims_ctl_bin", return_value="/tmp/reims-ctl"),
            ):
                self.assertEqual(reims_build.find_launch_runtime_dir(), runtime)

    def test_validates_installed_runtime_without_downloading_or_replacing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "reims-ctl"
            binary = runtime / "qemu/current/bin/qemu-system-x86_64"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)

            with (
                patch("core.reims_build.qemu_data_dir", return_value=runtime),
                patch("core.reims_build.qemu_binary", return_value=binary),
                patch("core.reims_ctl.reims_ctl_bin", return_value="/tmp/reims-ctl"),
                patch("core.reims_build.evaluate", side_effect=AssertionError("must not replace installed runtime")),
                patch("core.reims_build.install_prebuilt", side_effect=AssertionError("must not download")),
            ):
                self.assertTrue(_ensure_reims_release(str(runtime)))

    def test_rejects_installed_runtime_when_its_qemu_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "reims-ctl"
            with (
                patch("core.reims_build.qemu_data_dir", return_value=runtime),
                patch("core.reims_build.qemu_binary", return_value=runtime / "missing-qemu"),
                patch("core.reims_ctl.reims_ctl_bin", return_value="/tmp/reims-ctl"),
            ):
                self.assertFalse(_ensure_reims_release(str(runtime)))

    def test_prebuilt_install_is_told_which_vm_owns_the_reservation(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            runtime = root / "runtime"
            binary = runtime / "qemu/current/bin/qemu-system-x86_64"
            binary.parent.mkdir(parents=True)
            binary.write_text("qemu", encoding="utf-8")
            ctl = root / "reims-ctl"
            ctl.write_text("ctl", encoding="utf-8")
            state = SimpleNamespace(reims_dir=bundle, binary_path=binary)
            with (
                patch("core.reims_build.qemu_data_dir", return_value=runtime),
                patch("core.reims_build.qemu_binary", return_value=binary),
                patch("core.reims_ctl.reims_ctl_bin", return_value=str(ctl)),
                patch("core.reims_build.evaluate", return_value=state),
                patch("core.reims_build.install_prebuilt", return_value=True) as install,
            ):
                self.assertTrue(_ensure_reims_release(str(bundle), "vm-test"))
            install.assert_called_once_with(bundle, reservation_vm_id="vm-test")


if __name__ == "__main__":
    unittest.main()
