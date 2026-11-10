"""O instalador limpo declara o perfil legado; sem isso nenhuma VM nasceria.

A instalação do zero (`ABInstallerTest`) provisiona a VM de teste ANTES de o
runtime/QEMU existir no host (a checagem de `reims_ctl.reims_ctl_bin()` só vem
depois). Com o gerador fail-closed, um `graphics_profile=None` ali não acha
device para autodetectar e `generate_opencore` recusa — a VM não é criada. Como
a instalação limpa é frota legada por definição, o call site passa
`apple-gfx-pci` EXPLÍCITO, que é decisão declarada e não default silencioso.

Este arquivo cobre os dois lados do contrato: o instalador manda o perfil, e o
provisionamento sem perfil e sem QEMU recusa em vez de adivinhar.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.config import SMBIOSConfig, VMConfig
from core.disk import DiskProvisioner


class InstallerRunnerDeclaresLegacyProfileTests(unittest.TestCase):
    def test_clean_install_provisions_with_the_explicit_legacy_profile(self):
        """`run_interactive` must pass `apple-gfx-pci`, not rely on autodetection."""
        from core import installer_runner

        captured = {}

        def fake_provision(vm, installer_img=None, **kwargs):
            captured.update(kwargs)
            captured["installer_img"] = installer_img
            raise _StopProvision()  # interrupt the TUI after the call we assert

        with patch.object(
            installer_runner.console, "clear"
        ), patch.object(
            installer_runner, "Prompt"
        ) as prompt, patch.object(
            installer_runner, "IntPrompt"
        ) as intprompt, patch.object(
            installer_runner, "Confirm"
        ), patch.object(
            installer_runner.DiskProvisioner, "provision", side_effect=fake_provision
        ), patch(
            "core.readiness.SystemReadinessChecker.check_all",
            return_value=SimpleNamespace(can_run_vms=True, checks=[]),
        ), patch(
            "core.downloader.MacOSDownloader.get_cached_image_path",
            return_value=Path("/tmp/BaseSystem.img"),
        ), patch(
            "core.smbios.GenSMBIOS.generate", return_value=_sample_smbios()
        ):
            prompt.ask.return_value = "1"
            intprompt.ask.return_value = 64
            with self.assertRaises(_StopProvision):
                installer_runner.ABInstallerTest.run_interactive()

        self.assertEqual(captured.get("graphics_profile"), "apple-gfx-pci")


class RefuseWithoutProfileOrQemuTests(unittest.TestCase):
    def _vm(self) -> VMConfig:
        return VMConfig(
            id="clean-provision", name="clean", macos_version="sequoia",
            disk_size_gb=32, smbios=_sample_smbios(),
        )

    def test_provision_refuses_and_unwinds_when_no_profile_and_no_qemu(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "vms"
            vm = self._vm()
            with patch("core.disk.VM_STORAGE_BASE", storage), patch.object(
                DiskProvisioner, "_create_qcow2_disk", _touch_qcow2
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_vars", side_effect=_write_ovmf("OVMF_VARS.fd")
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_code", side_effect=_write_ovmf("OVMF_CODE.fd")
            ), patch(
                "appliance.build_opencore._candidate_qemu_binaries", return_value=[]
            ), patch(
                "core.reims_util.find_reims_util", return_value=Path("/bin/reims-util")
            ):
                with self.assertRaisesRegex(RuntimeError, "kexts APV"):
                    DiskProvisioner.provision(vm)  # no graphics_profile

            vm_dir = storage / vm.id
            self.assertFalse((vm_dir / "OVMF_VARS.fd").exists())
            self.assertFalse((vm_dir / "OVMF_CODE.fd").exists())
            self.assertFalse((vm_dir / "OpenCore.qcow2").exists())


class _StopProvision(RuntimeError):
    """Sentinel to unwind the interactive flow after the provision call."""


def _sample_smbios() -> SMBIOSConfig:
    return SMBIOSConfig(
        model="iMac20,1",
        serial_number="C02TEST12345",
        board_serial="C0270000000000001",
        smuuid="12345678-1234-5678-1234-567812345678",
        rom="02AABBCCDDEE",
    )


def _touch_qcow2(disk_path, size_gb, *, job_context=None):
    disk_path = Path(disk_path)
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    disk_path.write_bytes(b"QCOW2")


def _write_ovmf(name: str):
    def _ensure(dest_dir: Path) -> bool:
        dest = Path(dest_dir) / name
        if not dest.exists():
            dest.write_bytes(b"OVMF-NOVO")
        return True

    return _ensure


if __name__ == "__main__":
    unittest.main()
