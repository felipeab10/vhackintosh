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

    def test_packaged_apv_binary_precedes_stale_cargo_and_path_copies(self):
        with tempfile.TemporaryDirectory() as td:
            cargo_home = Path(td) / "cargo"

            with patch.dict("os.environ", {"CARGO_HOME": str(cargo_home)}, clear=True), patch(
                "shutil.which", return_value="/usr/local/bin/reims-util"
            ):
                order = [str(candidate) for candidate in reims_util.candidate_binaries()]

            packaged = "/opt/vhackintosh/resources/reims-util/reims-util"
            self.assertEqual(order[0], packaged)
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
            def fake_run(command, **_kwargs):
                if command[1:3] == ["opencore", "--help"]:
                    return Mock(returncode=0, stdout="--no-apv-arm64-port", stderr="")
                if command[0] == "/bin/reims-util":
                    Path(command[command.index("-o") + 1]).write_bytes(b"MOCK_QCOW2_CONTENT")
                return Mock(returncode=0, stdout="", stderr="")
            with patch("core.reims_util.find_reims_util", return_value=Path("/bin/reims-util")), patch(
                "shutil.which", return_value="/usr/bin/qemu-img"
            ), patch(
                "subprocess.run", side_effect=fake_run
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
                called_cmd = mock_run.call_args_list[1].args[0]
                self.assertEqual(called_cmd[0], "/bin/reims-util")
                self.assertEqual(called_cmd[1], "opencore")
                self.assertIn("-o", called_cmd)
                self.assertNotEqual(called_cmd[called_cmd.index("-o") + 1], str(dest))
                self.assertEqual(dest.read_bytes(), b"MOCK_QCOW2_CONTENT")
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
                # O binário velho embute os kexts; não se passa --add-kext a ele.
                self.assertNotIn("--add-kext", called_cmd)

    def test_generate_opencore_passes_vendored_kexts_to_a_binary_that_dropped_them(self):
        """O master (>= fe9bfe1) não embute mais os kexts: eles vão por --add-kext."""
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "OpenCore.qcow2"
            def fake_run(command, **_kwargs):
                if command[1:3] == ["opencore", "--help"]:
                    return Mock(returncode=0, stdout="  --add-kext <PATH>  Add a kext bundle", stderr="")
                if command[0] == "/bin/reims-util":
                    Path(command[command.index("-o") + 1]).write_bytes(b"MOCK_QCOW2_CONTENT")
                return Mock(returncode=0, stdout="", stderr="")

            with patch("core.reims_util.find_reims_util", return_value=Path("/bin/reims-util")), patch(
                "shutil.which", return_value="/usr/bin/qemu-img"
            ), patch(
                "core.reims_util.VENDORED_KEXTS_DIR", Path("/vendored")
            ), patch(
                "pathlib.Path.is_dir", return_value=True
            ), patch(
                "subprocess.run", side_effect=fake_run
            ) as mock_run:
                ok = reims_util.generate_opencore(dest, self.sample_smbios, no_ovmf=True)
                self.assertTrue(ok)
                called_cmd = mock_run.call_args_list[1].args[0]
                # Um --add-kext por kext vendorizado, com os gates de kernel corretos.
                self.assertEqual(called_cmd.count("--add-kext"), len(reims_util.VENDORED_KEXTS))
                self.assertIn("/vendored/ReimsVgpu.kext,25.0.0,25.99.99", called_cmd)
                self.assertIn("/vendored/APVDisplayPort.kext,25.4.0,25.99.99", called_cmd)
                # A ordem tem de casar com VENDORED_KEXTS: o reims-util recusa link para kext posterior.
                add_positions = [i for i, arg in enumerate(called_cmd) if arg == "--add-kext"]
                given = [called_cmd[i + 1].rsplit("/", 1)[1].split(",")[0] for i in add_positions]
                self.assertEqual(given, [name for name, _ in reims_util.VENDORED_KEXTS])

    def test_generate_opencore_refuses_binary_with_neither_embedded_nor_added_kexts(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "OpenCore.qcow2"
            def fake_run(command, **_kwargs):
                if command[1:3] == ["opencore", "--help"]:
                    return Mock(returncode=0, stdout="  --model <MODEL>  The Mac model", stderr="")
                return Mock(returncode=0, stdout="", stderr="")

            with patch("core.reims_util.find_reims_util", return_value=Path("/bin/reims-util")), patch(
                "subprocess.run", side_effect=fake_run
            ):
                self.assertFalse(reims_util.generate_opencore(dest, self.sample_smbios))

    def test_generate_opencore_refuses_add_kext_binary_without_vendored_kexts(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "OpenCore.qcow2"
            def fake_run(command, **_kwargs):
                if command[1:3] == ["opencore", "--help"]:
                    return Mock(returncode=0, stdout="  --add-kext <PATH>  Add a kext bundle", stderr="")
                return Mock(returncode=0, stdout="", stderr="")

            with patch("core.reims_util.find_reims_util", return_value=Path("/bin/reims-util")), patch(
                "core.reims_util.VENDORED_KEXTS_DIR", Path("/nao-existe")
            ), patch("subprocess.run", side_effect=fake_run) as mock_run:
                self.assertFalse(reims_util.generate_opencore(dest, self.sample_smbios))
                # Não tenta gerar uma EFI sem kexts e depois mentir que deu certo.
                self.assertEqual(len(mock_run.call_args_list), 1)

    def test_vendored_kexts_match_required_kexts_contract(self):
        """A lista do core tem de casar com REQUIRED_KEXTS e com os bundles em disco.

        Lilu.kext é exceção: o binário ainda o embute, então não é vendorizado.
        """
        from appliance.build_opencore import REQUIRED_KEXTS

        vendored = dict(reims_util.VENDORED_KEXTS)
        self.assertEqual(set(vendored), set(REQUIRED_KEXTS) - {"Lilu.kext"})
        for name, minimum in vendored.items():
            self.assertEqual(minimum, REQUIRED_KEXTS[name])
            self.assertTrue(
                (reims_util.profile_kext_dir("apple-gfx-pci") / name / "Contents" / "Info.plist").is_file(),
                f"bundle ausente: {name}",
            )

    def test_graphics_profiles_match_builder_table(self):
        """Core e builder têm de concordar na tabela de perfis, ou as duas EFIs divergem."""
        from appliance import build_opencore

        self.assertEqual(reims_util.GRAPHICS_PROFILES, build_opencore.GRAPHICS_PROFILES)
        for profile, bundles in reims_util.GRAPHICS_PROFILES.items():
            directory = reims_util.profile_kext_dir(profile)
            for name, _minimum in bundles:
                with self.subTest(profile=profile, name=name):
                    self.assertTrue((directory / name / "Contents" / "Info.plist").is_file())

    def test_generate_opencore_injects_the_requested_profile_from_the_real_directory(self):
        """Cada perfil monta --add-kext a partir do diretório REAL (sem mock de path).

        Só roda quando o binário master (com --add-kext) existe; o binário embutido da
        frota não exercita este caminho.
        """
        master = Path("/tmp/ru-check/target/release/reims-util")
        if not master.is_file():
            self.skipTest("binário master do reims-util indisponível")
        with patch("core.reims_util.find_reims_util", return_value=master):
            help_text = subprocess.run(
                [str(master), "opencore", "--help"], capture_output=True, text=True
            ).stdout
            self.assertIn("--add-kext", help_text)
            with tempfile.TemporaryDirectory() as td:
                for profile, bundles in reims_util.GRAPHICS_PROFILES.items():
                    with self.subTest(profile=profile):
                        dest = Path(td) / f"{profile}.qcow2"
                        self.assertTrue(
                            reims_util.generate_opencore(
                                dest, self.sample_smbios, graphics_profile=profile
                            )
                        )
                        listing = subprocess.run(
                            ["guestfish", "--ro", "-a", str(dest), "-m", "/dev/sda1",
                             "ls", "/EFI/OC/Kexts"],
                            check=True, capture_output=True, text=True,
                        ).stdout.splitlines()
                        expected = {"Lilu.kext"} | {name for name, _ in bundles}
                        self.assertEqual(set(listing), expected)

    def test_unknown_graphics_profile_is_refused_even_by_an_embeddding_binary(self):
        """Perfil desconhecido falha ANTES da geração, mesmo se o binário ignora o argumento."""
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "OpenCore.qcow2"
            def fake_run(command, **_kwargs):
                if command[1:3] == ["opencore", "--help"]:
                    return Mock(returncode=0, stdout="  --no-apv-arm64-port  legacy", stderr="")
                Path(command[command.index("-o") + 1]).write_bytes(b"MOCK")
                return Mock(returncode=0, stdout="", stderr="")

            with patch("core.reims_util.find_reims_util", return_value=Path("/bin/reims-util")), patch(
                "subprocess.run", side_effect=fake_run
            ) as mock_run:
                self.assertFalse(
                    reims_util.generate_opencore(
                        dest, self.sample_smbios, graphics_profile="perfil-inventado"
                    )
                )
                # Nem chega a rodar opencore: o perfil é validado antes.
                self.assertEqual(len(mock_run.call_args_list), 0)

    def test_mutation_new_profile_directory_missing_fails_generation(self):
        """Mutação: renomear o diretório do perfil novo tem de derrubar a geração dele.

        Usa o diretório REAL (renomeia e restaura), não um mock de path.
        """
        master = Path("/tmp/ru-check/target/release/reims-util")
        if not master.is_file():
            self.skipTest("binário master do reims-util indisponível")
        real_dir = reims_util.profile_kext_dir("reims-graphics-pci")
        mutated = real_dir.with_name(real_dir.name + ".MUTATED")
        self.assertTrue(real_dir.is_dir(), "o diretório do perfil novo tem de existir")
        with patch("core.reims_util.find_reims_util", return_value=master):
            with tempfile.TemporaryDirectory() as td:
                dest = Path(td) / "OpenCore.qcow2"
                real_dir.rename(mutated)
                try:
                    self.assertFalse(
                        reims_util.generate_opencore(
                            dest, self.sample_smbios, graphics_profile="reims-graphics-pci"
                        )
                    )
                    self.assertFalse(dest.exists())
                finally:
                    mutated.rename(real_dir)

    def test_generate_opencore_autodetects_new_profile_from_qemu(self):
        """Sem graphics_profile, o perfil vem do device do QEMU (não é legado silencioso)."""
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "OpenCore.qcow2"
            qemu = Path(td) / "qemu"
            qemu.write_bytes(b"prefix reims-graphics-pci suffix")
            def fake_run(command, **_kwargs):
                if command[1:3] == ["opencore", "--help"]:
                    return Mock(returncode=0, stdout="  --add-kext <PATH>", stderr="")
                if "-o" in command:
                    Path(command[command.index("-o") + 1]).write_bytes(b"MOCK")
                return Mock(returncode=0, stdout="", stderr="")

            with patch("core.reims_util.find_reims_util", return_value=Path("/bin/reims-util")), patch(
                "appliance.build_opencore._candidate_qemu_binaries", return_value=[qemu]
            ), patch("subprocess.run", side_effect=fake_run) as mock_run:
                self.assertTrue(reims_util.generate_opencore(dest, self.sample_smbios))
                gen_cmd = next(
                    call.args[0] for call in mock_run.call_args_list if "-o" in call.args[0]
                )
                given = [
                    gen_cmd[i + 1].rsplit("/", 1)[1].split(",")[0]
                    for i, arg in enumerate(gen_cmd) if arg == "--add-kext"
                ]
                self.assertEqual(
                    given, [name for name, _ in reims_util.GRAPHICS_PROFILES["reims-graphics-pci"]]
                )

    def test_generate_opencore_autodetect_refuses_without_qemu_instead_of_assuming_legacy(self):
        """Sem QEMU legível NÃO assume o legado: recusa, como o builder.

        O ponto de provisionamento (`core/disk.py`) chama sem perfil; assumir legado num host
        cujo QEMU espera `reims-graphics-pci` entrega a VM sem aceleração e o sintoma só
        aparece no guest. Mesmo sentido de `detect_graphics_profile` (RuntimeError).
        """
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "OpenCore.qcow2"
            def fake_run(command, **_kwargs):
                if command[1:3] == ["opencore", "--help"]:
                    return Mock(returncode=0, stdout="  --add-kext <PATH>", stderr="")
                if "-o" in command:
                    Path(command[command.index("-o") + 1]).write_bytes(b"MOCK")
                return Mock(returncode=0, stdout="", stderr="")

            with patch("core.reims_util.find_reims_util", return_value=Path("/bin/reims-util")), patch(
                "appliance.build_opencore._candidate_qemu_binaries", return_value=[]
            ), patch("subprocess.run", side_effect=fake_run) as mock_run:
                self.assertFalse(reims_util.generate_opencore(dest, self.sample_smbios))
                self.assertFalse(dest.exists())
                self.assertFalse(any("-o" in call.args[0] for call in mock_run.call_args_list))

    def test_generate_opencore_explicit_legacy_profile_still_works_without_qemu(self):
        """O legado continua disponível quando o CHAMADOR o pede — não como consolo."""
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "OpenCore.qcow2"
            def fake_run(command, **_kwargs):
                if command[1:3] == ["opencore", "--help"]:
                    return Mock(returncode=0, stdout="  --add-kext <PATH>", stderr="")
                if "-o" in command:
                    Path(command[command.index("-o") + 1]).write_bytes(b"MOCK")
                return Mock(returncode=0, stdout="", stderr="")

            with patch("core.reims_util.find_reims_util", return_value=Path("/bin/reims-util")), patch(
                "appliance.build_opencore._candidate_qemu_binaries", return_value=[]
            ), patch("subprocess.run", side_effect=fake_run) as mock_run:
                self.assertTrue(
                    reims_util.generate_opencore(
                        dest, self.sample_smbios, graphics_profile="apple-gfx-pci"
                    )
                )
                gen_cmd = next(
                    call.args[0] for call in mock_run.call_args_list if "-o" in call.args[0]
                )
                self.assertTrue(
                    any("ReimsVgpu.kext,25.0.0,25.99.99" in arg for arg in gen_cmd), gen_cmd
                )

    def test_generate_opencore_failure_handling(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "OpenCore.qcow2"
            mock_res = Mock(returncode=1, stdout="Failed", stderr="Error generating")

            with patch("core.reims_util.find_reims_util", return_value=Path("/bin/reims-util")), patch(
                "subprocess.run", return_value=mock_res
            ):
                ok = reims_util.generate_opencore(dest, self.sample_smbios)
                self.assertFalse(ok)

    def test_disk_provisioner_generates_per_vm_efi_with_reims_util(self):
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
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_code"
            ), patch(
                "core.reims_util.generate_opencore"
            ) as mock_gen_oc, patch.object(
                DiskProvisioner, "_inject_smbios_to_opencore_qcow2"
            ) as mock_guestfish:
                bundle = DiskProvisioner.provision(vm)

                mock_gen_oc.assert_called_once()
                self.assertEqual(mock_gen_oc.call_args.args[0], bundle.opencore_path)
                self.assertTrue(mock_gen_oc.call_args.kwargs["no_ovmf"])
                self.assertTrue(mock_gen_oc.call_args.kwargs["show_picker"] is False)
                mock_guestfish.assert_not_called()
                self.assertEqual(bundle.opencore_path, vm_storage / "native-test" / "OpenCore.qcow2")

    def test_disk_provisioner_refuses_to_create_efi_without_apv_reims_util(self):
        with tempfile.TemporaryDirectory() as td:
            vm_storage = Path(td) / "vms"
            vm = VMConfig(
                id="fallback-test",
                name="FallbackTest",
                macos_version="sequoia",
                disk_size_gb=32,
                smbios=self.sample_smbios,
            )

            with patch("core.disk.VM_STORAGE_BASE", vm_storage), patch.object(
                DiskProvisioner, "_create_qcow2_disk"
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_code"
            ), patch("core.reims_util.generate_opencore", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "kexts APV"):
                    DiskProvisioner.provision(vm)

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
                "core.reims_util.generate_opencore", return_value=True
            ):
                DiskProvisioner.provision(vm)

                mock_gen.assert_called_once_with("sequoia")
                self.assertEqual(vm.smbios.serial_number, "C02NEWSERIAL1")

    def test_disk_provisioning_reports_reims_util_generation(self):
        with tempfile.TemporaryDirectory() as td:
            vm_storage = Path(td) / "vms"
            vm = VMConfig(
                id="reason-test",
                name="ReasonTest",
                macos_version="sequoia",
                disk_size_gb=32,
                smbios=self.sample_smbios,
            )
            descriptions: list[str] = []

            with patch("core.disk.VM_STORAGE_BASE", vm_storage), patch.object(
                DiskProvisioner, "_create_qcow2_disk"
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_code"
            ), patch("core.reims_util.generate_opencore", return_value=True), patch.object(
                Progress,
                "update",
                autospec=True,
                side_effect=lambda _self, _task, **kw: descriptions.append(str(kw.get("description", ""))),
            ):
                DiskProvisioner.provision(vm)

            joined = " ".join(descriptions)
            self.assertIn("SMBIOS exclusivo", joined)
            self.assertIn("reims-util oficial", joined)

    def test_opencore_config_edit_preserves_the_existing_efi_image(self):
        with tempfile.TemporaryDirectory() as td:
            image = Path(td) / "OpenCore.qcow2"
            image.write_bytes(b"ULTIMATE-EFI-WITH-DRIVERS")
            vm = VMConfig(
                id="preserve-efi",
                name="Preserve EFI",
                macos_version="tahoe",
                disk_size_gb=32,
                smbios=self.sample_smbios,
            )

            def edit_plist(staged, _vm, _picker):
                with Path(staged).open("ab") as stream:
                    stream.write(b"-SMBIOS-UPDATED")

            with patch("shutil.which", side_effect=lambda tool: "/usr/bin/guestfish" if tool == "guestfish" else None), \
                 patch.object(DiskProvisioner, "_inject_opencore_config_in_place", side_effect=edit_plist), \
                 patch("core.reims_util.is_available") as reims_available, \
                 patch("core.reims_util.generate_opencore") as generate_opencore:
                DiskProvisioner.inject_opencore_config(image, vm)

            reims_available.assert_not_called()
            generate_opencore.assert_not_called()
            self.assertEqual(image.read_bytes(), b"ULTIMATE-EFI-WITH-DRIVERS-SMBIOS-UPDATED")
            self.assertEqual(list(image.parent.iterdir()), [image])

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
