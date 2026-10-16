import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rich.progress import Progress

from core.config import SMBIOSConfig, VMConfig
from core.disk import DiskProvisioner
from core import reims_util


def _fake_util(path: Path, exit_code: int = 0) -> Path:
    """Um reims-util de mentira: existe, é executável e responde `--version`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    path.chmod(0o755)
    return path


class ReimsUtilLocatorTests(unittest.TestCase):
    """A ordem de busca espelha o `reims_util::binary()` do reims-ctl.

    Regressão que estes testes guardam: com `shutil.which` na frente, o
    reims-util que a ISO carrega pronto em /usr/local/bin vencia o binário que o
    appliance acabou de compilar em $CARGO_HOME/bin — e a imagem do OpenCore
    passava a ser gerada por um reims-util diferente do que o reims-ctl usaria.
    """

    def test_prefers_the_binary_the_appliance_itself_installed(self):
        with tempfile.TemporaryDirectory() as td:
            fresh = _fake_util(Path(td) / "cargo" / "bin" / "reims-util")
            stale = _fake_util(Path(td) / "usr-local-bin" / "reims-util")

            with patch.dict("os.environ", {}, clear=True), patch(
                "core.reims_util.candidate_binaries", return_value=(fresh, stale)
            ):
                self.assertEqual(reims_util.find_reims_util(), fresh)

    def test_uses_the_binary_the_iso_carries_when_nothing_was_built_here(self):
        with tempfile.TemporaryDirectory() as td:
            baked = _fake_util(Path(td) / "reims-util")

            with patch.dict("os.environ", {}, clear=True), patch(
                "core.reims_util.candidate_binaries",
                return_value=(Path(td) / "ausente" / "reims-util", baked),
            ):
                self.assertEqual(reims_util.find_reims_util(), baked)

    def test_skips_a_candidate_that_does_not_run(self):
        with tempfile.TemporaryDirectory() as td:
            broken = _fake_util(Path(td) / "quebrado", exit_code=127)
            working = _fake_util(Path(td) / "ok")

            with patch.dict("os.environ", {}, clear=True), patch(
                "core.reims_util.candidate_binaries", return_value=(broken, working)
            ):
                self.assertEqual(reims_util.find_reims_util(), working)

    def test_reports_nothing_when_every_candidate_is_broken(self):
        with tempfile.TemporaryDirectory() as td:
            broken = _fake_util(Path(td) / "quebrado", exit_code=1)

            with patch.dict("os.environ", {}, clear=True), patch(
                "core.reims_util.candidate_binaries", return_value=(broken,)
            ):
                self.assertIsNone(reims_util.find_reims_util())
                self.assertFalse(reims_util.is_available())

    def test_cargo_home_comes_before_path_and_before_the_baked_copy(self):
        with tempfile.TemporaryDirectory() as td:
            cargo_home = Path(td) / "cargo"

            with patch.dict("os.environ", {"CARGO_HOME": str(cargo_home)}, clear=True), patch(
                "shutil.which", return_value="/usr/local/bin/reims-util"
            ):
                order = [str(candidate) for candidate in reims_util.candidate_binaries()]

            self.assertIn(str(cargo_home / "bin" / "reims-util"), order)
            self.assertLess(
                order.index(str(cargo_home / "bin" / "reims-util")),
                order.index("/usr/local/bin/reims-util"),
            )

    def test_search_summary_names_where_it_looked(self):
        with patch(
            "core.reims_util.candidate_binaries",
            return_value=(Path("/usr/local/bin/reims-util"),),
        ):
            self.assertIn("/usr/local/bin", reims_util.search_summary())


class ReimsUtilTests(unittest.TestCase):
    def setUp(self):
        self.sample_smbios = SMBIOSConfig(
            model="iMac20,1",
            serial_number="C02TEST12345",
            board_serial="C0270000000000001",
            smuuid="12345678-1234-5678-1234-567812345678",
            rom="02AABBCCDDEE",
        )

    def test_find_reims_util_via_env(self):
        with tempfile.TemporaryDirectory() as td:
            fake_bin = Path(td) / "custom-reims-util"
            fake_bin.write_bytes(b"#!/bin/sh\nexit 0\n")
            fake_bin.chmod(0o755)

            with patch.dict("os.environ", {"REIMS_UTIL_BIN": str(fake_bin)}):
                found = reims_util.find_reims_util()
                self.assertEqual(found, fake_bin)

    def test_find_reims_util_uses_path_before_the_fixed_baked_locations(self):
        with tempfile.TemporaryDirectory() as td:
            fake_bin = _fake_util(Path(td) / "reims-util")

            with patch.dict("os.environ", {}, clear=True), patch(
                "shutil.which", return_value=str(fake_bin)
            ), patch(
                "core.reims_util.candidate_binaries",
                return_value=(fake_bin, Path("/usr/local/bin/reims-util")),
            ):
                found = reims_util.find_reims_util()
                self.assertEqual(found, fake_bin)

    def test_find_reims_util_not_found(self):
        with patch.dict("os.environ", {}, clear=True), patch(
            "shutil.which", return_value=None
        ), patch("pathlib.Path.is_file", return_value=False):
            found = reims_util.find_reims_util()
            self.assertIsNone(found)
            self.assertFalse(reims_util.is_available())

    def test_generate_opencore_constructs_proper_arguments(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "OpenCore.qcow2"
            mock_res = Mock(returncode=0, stdout="", stderr="")

            # Mock dest.is_file() and dest.stat().st_size
            dest.write_bytes(b"MOCK_QCOW2_CONTENT")

            with patch("core.reims_util.find_reims_util", return_value=Path("/bin/reims-util")), patch(
                "subprocess.run", return_value=mock_res
            ) as mock_run:
                ok = reims_util.generate_opencore(
                    dest,
                    self.sample_smbios,
                    no_ovmf=True,
                    timeout=5,
                    boot_args="keepsyms=1 vti=9",
                    verbose_boot=True,
                    text_picker=True,
                )
                self.assertTrue(ok)
                called_cmd = mock_run.call_args[0][0]
                self.assertEqual(called_cmd[0], "/bin/reims-util")
                self.assertEqual(called_cmd[1], "opencore")
                self.assertIn("-o", called_cmd)
                self.assertIn(str(dest), called_cmd)
                self.assertIn("--model", called_cmd)
                self.assertIn("iMac20,1", called_cmd)
                self.assertIn("--serial", called_cmd)
                self.assertIn("C02TEST12345", called_cmd)
                self.assertIn("--mlb", called_cmd)
                self.assertIn("C0270000000000001", called_cmd)
                self.assertIn("--uuid", called_cmd)
                self.assertIn("12345678-1234-5678-1234-567812345678", called_cmd)
                self.assertIn("--rom", called_cmd)
                self.assertIn("02AABBCCDDEE", called_cmd)
                self.assertIn("--no-ovmf", called_cmd)
                self.assertIn("--timeout", called_cmd)
                self.assertIn("5", called_cmd)
                self.assertIn("--boot-args", called_cmd)
                self.assertIn("keepsyms=1 vti=9", called_cmd)
                self.assertIn("--verbose-boot", called_cmd)
                self.assertIn("--text-picker", called_cmd)

    def test_generate_opencore_failure_handling(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "OpenCore.qcow2"
            mock_res = Mock(returncode=1, stdout="Failed", stderr="Error generating")

            with patch("core.reims_util.find_reims_util", return_value=Path("/bin/reims-util")), patch(
                "subprocess.run", return_value=mock_res
            ):
                ok = reims_util.generate_opencore(dest, self.sample_smbios)
                self.assertFalse(ok)

    def test_disk_provisioner_prefers_reims_util(self):
        with tempfile.TemporaryDirectory() as td:
            vm_storage = Path(td) / "vms"
            vm = VMConfig(
                id="native-test",
                name="NativeTest",
                macos_version="sequoia",
                disk_size_gb=32,
                smbios=self.sample_smbios,
            )

            with patch("core.disk.VM_STORAGE_BASE", vm_storage), patch.object(
                DiskProvisioner, "_create_qcow2_disk"
            ) as mock_qcow2, patch.object(
                DiskProvisioner, "ensure_ovmf_code"
            ), patch(
                "core.reims_util.is_available", return_value=True
            ), patch(
                "core.reims_util.generate_opencore"
            ) as mock_gen_oc, patch.object(
                DiskProvisioner, "_inject_smbios_to_opencore_qcow2"
            ) as mock_guestfish:
                def fake_generate(out_path, *args, **kwargs):
                    p = Path(out_path)
                    p.write_bytes(b"FAKE_QCOW2_DATA")
                    return True

                mock_gen_oc.side_effect = fake_generate

                bundle = DiskProvisioner.provision(vm)

                self.assertTrue(mock_gen_oc.called)
                self.assertFalse(mock_guestfish.called)
                self.assertEqual(bundle.opencore_path, vm_storage / "native-test" / "OpenCore.qcow2")

    def test_disk_provisioner_fallback_to_guestfish(self):
        with tempfile.TemporaryDirectory() as td:
            vm_storage = Path(td) / "vms"
            vm = VMConfig(
                id="fallback-test",
                name="FallbackTest",
                macos_version="sequoia",
                disk_size_gb=32,
                smbios=self.sample_smbios,
            )

            dummy_template = Path(td) / "template-OpenCore.qcow2"
            dummy_template.write_bytes(b"TEMPLATE_DATA")

            with patch("core.disk.VM_STORAGE_BASE", vm_storage), patch.object(
                DiskProvisioner, "_create_qcow2_disk"
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_code"
            ), patch(
                "core.disk.DiskProvisioner.get_template_opencore_path",
                return_value=dummy_template,
            ), patch(
                "core.reims_util.is_available", return_value=False
            ), patch(
                "shutil.which", return_value="/usr/bin/guestfish"
            ), patch.object(
                DiskProvisioner, "_inject_smbios_to_opencore_qcow2"
            ) as mock_guestfish:
                bundle = DiskProvisioner.provision(vm)
                self.assertTrue(mock_guestfish.called)
                self.assertEqual(bundle.opencore_path, vm_storage / "fallback-test" / "OpenCore.qcow2")

    def test_disk_provisioner_generates_fresh_serial_for_new_vm_if_empty(self):
        with tempfile.TemporaryDirectory() as td:
            vm_storage = Path(td) / "vms"
            vm = VMConfig(
                id="fresh-serial-test",
                name="FreshSerialTest",
                macos_version="sequoia",
                disk_size_gb=32,
                smbios=SMBIOSConfig(),  # Empty SMBIOS
            )

            generated_identity = SMBIOSConfig(
                model="iMac20,1",
                serial_number="C02NEWSERIAL1",
                board_serial="C0270000000000009",
                smuuid="87654321-4321-8765-4321-876543210987",
                rom="02AABB112233",
            )

            with patch("core.disk.VM_STORAGE_BASE", vm_storage), patch.object(
                DiskProvisioner, "_create_qcow2_disk"
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_code"
            ), patch(
                "core.smbios.GenSMBIOS.generate", return_value=generated_identity
            ) as mock_gen, patch(
                "core.reims_util.is_available", return_value=True
            ), patch(
                "core.reims_util.generate_opencore", return_value=True
            ):
                def fake_generate(out_path, *args, **kwargs):
                    Path(out_path).write_bytes(b"DATA")
                    return True

                with patch("core.reims_util.generate_opencore", side_effect=fake_generate):
                    DiskProvisioner.provision(vm)

                mock_gen.assert_called_once_with("sequoia")
                self.assertEqual(vm.smbios.serial_number, "C02NEWSERIAL1")

    def test_disk_fallback_says_that_reims_util_is_missing(self):
        """O usuário viu "injetando EFI via guestfish" sem saber o motivo."""
        with tempfile.TemporaryDirectory() as td:
            vm_storage = Path(td) / "vms"
            vm = VMConfig(
                id="reason-test",
                name="ReasonTest",
                macos_version="sequoia",
                disk_size_gb=32,
                smbios=self.sample_smbios,
            )
            dummy_template = Path(td) / "template-OpenCore.qcow2"
            dummy_template.write_bytes(b"TEMPLATE_DATA")
            descriptions: list[str] = []

            with patch("core.disk.VM_STORAGE_BASE", vm_storage), patch.object(
                DiskProvisioner, "_create_qcow2_disk"
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_code"
            ), patch.object(
                DiskProvisioner, "get_template_opencore_path", return_value=dummy_template
            ), patch(
                "core.reims_util.is_available", return_value=False
            ), patch(
                "shutil.which", return_value="/usr/bin/guestfish"
            ), patch.object(
                DiskProvisioner, "_inject_smbios_to_opencore_qcow2"
            ), patch.object(
                Progress,
                "update",
                autospec=True,
                side_effect=lambda _self, _task, **kw: descriptions.append(str(kw.get("description", ""))),
            ):
                DiskProvisioner.provision(vm)

            joined = " ".join(descriptions)
            self.assertIn("reims-util ausente", joined)
            self.assertIn("via guestfish", joined)

    def test_real_reims_util_execution_if_present(self):
        if not reims_util.is_available():
            self.skipTest("reims-util binary not present on this system")

        with tempfile.TemporaryDirectory() as td:
            output_img = Path(td) / "OpenCore.qcow2"
            success = reims_util.generate_opencore(
                output_img,
                self.sample_smbios,
                no_ovmf=True,
                timeout=5,
            )
            self.assertTrue(success)
            self.assertTrue(output_img.exists())
            self.assertGreater(output_img.stat().st_size, 0)

            # Check qemu-img integrity if qemu-img is installed
            import shutil
            if shutil.which("qemu-img"):
                check = subprocess.run(
                    ["qemu-img", "check", str(output_img)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(check.returncode, 0, f"qemu-img check failed: {check.stderr}")


if __name__ == "__main__":
    unittest.main()
