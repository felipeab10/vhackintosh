"""Provisionamento que falha na EFI não pode deixar a VM meio-feita no disco.

Regressão (Demanda 002): `DiskProvisioner.provision` escreve o OVMF (VARS e CODE)
no passo 3 e só gera o OpenCore no passo 4. Quando `reims_util.generate_opencore`
devolve False — por exemplo porque o `reims-util` instalado não entrega os kexts —
o `RuntimeError` subia depois do OVMF já estar no diretório: uma VM sem bootloader
mas com firmware, indistinguível de um provisionamento completo para quem for
diagnosticar depois. O diretório tem de voltar ao que era antes da chamada.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import SMBIOSConfig, VMConfig
from core.disk import DiskProvisioner

class _RecordingStore:
    def list_vms(self):
        return []

    def add_vm(self, vm, **kwargs):
        return None

class _StaticSMBIOS:
    @staticmethod
    def generate(version):
        return SMBIOSConfig(
            model="iMac20,1",
            serial_number="C02TEST12345",
            board_serial="C0270000000000001",
            smuuid="12345678-1234-5678-1234-567812345678",
            rom="02AABBCCDDEE",
        )


class FailedOpencoreLeavesNoPartialProvisionTests(unittest.TestCase):
    sample_smbios = SMBIOSConfig(
        model="iMac20,1",
        serial_number="C02TEST12345",
        board_serial="C0270000000000001",
        smuuid="12345678-1234-5678-1234-567812345678",
        rom="02AABBCCDDEE",
    )

    def _vm(self, vm_id: str) -> VMConfig:
        return VMConfig(
            id=vm_id,
            name=vm_id,
            macos_version="sequoia",
            disk_size_gb=32,
            smbios=self.sample_smbios,
        )

    def test_known_graphics_profile_is_forwarded_to_opencore_generator(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "vms"
            vm = self._vm("profile-provision")
            with patch("core.disk.VM_STORAGE_BASE", storage), patch.object(
                DiskProvisioner, "_create_qcow2_disk"
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_vars", side_effect=_write_ovmf("OVMF_VARS.fd")
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_code", side_effect=_write_ovmf("OVMF_CODE.fd")
            ), patch("core.reims_util.generate_opencore", return_value=True) as generate:
                DiskProvisioner.provision(vm, graphics_profile="reims-graphics-pci")

            self.assertEqual(generate.call_args.kwargs["graphics_profile"], "reims-graphics-pci")

    def test_none_profile_preserves_generator_autodetection(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "vms"
            vm = self._vm("autodetect-provision")
            with patch("core.disk.VM_STORAGE_BASE", storage), patch.object(
                DiskProvisioner, "_create_qcow2_disk"
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_vars", side_effect=_write_ovmf("OVMF_VARS.fd")
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_code", side_effect=_write_ovmf("OVMF_CODE.fd")
            ), patch("core.reims_util.generate_opencore", return_value=True) as generate:
                DiskProvisioner.provision(vm)

            self.assertNotIn("graphics_profile", generate.call_args.kwargs)

    def test_service_provider_reaches_the_real_opencore_generator(self):
        """Integration: the real service + real DiskProvisioner reach generate_opencore.

        This exercises the wiring that unit tests of `provision` alone cannot:
        `VMProvisioningService.provision` -> `DiskProvisioner.provision` ->
        `reims_util.generate_opencore(graphics_profile=...)`.
        """
        from core.vm_provisioning import (
            InstallerPolicy,
            VMProvisioningRequest,
            VMProvisioningService,
        )

        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "vms"
            service = VMProvisioningService(
                store=_RecordingStore(),
                smbios_generator=_StaticSMBIOS(),
                graphics_profile_provider=lambda: "reims-graphics-pci",
                storage_root=storage,
                id_factory=lambda: "integ01",
            )
            request = VMProvisioningRequest(
                name="Integration", macos_version="sequoia", vcpus=4, ram_gb=8,
                disk_size_gb=32, installer_policy=InstallerPolicy.NONE,
            )
            with patch("core.disk.VM_STORAGE_BASE", storage), patch.object(
                DiskProvisioner, "_create_qcow2_disk", _touch_qcow2
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_vars", side_effect=_write_ovmf("OVMF_VARS.fd")
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_code", side_effect=_write_ovmf("OVMF_CODE.fd")
            ), patch("core.reims_util.generate_opencore", side_effect=_write_opencore) as generate:
                service.provision(request)

            self.assertEqual(generate.call_args.kwargs["graphics_profile"], "reims-graphics-pci")

    def test_failure_removes_the_ovmf_it_just_wrote(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "vms"
            vm = self._vm("partial-provision")

            with patch("core.disk.VM_STORAGE_BASE", storage), patch.object(
                DiskProvisioner, "_create_qcow2_disk"
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_vars", side_effect=_write_ovmf("OVMF_VARS.fd")
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_code", side_effect=_write_ovmf("OVMF_CODE.fd")
            ), patch("core.reims_util.generate_opencore", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "kexts APV"):
                    DiskProvisioner.provision(vm)

            vm_dir = storage / vm.id
            self.assertFalse(
                (vm_dir / "OVMF_VARS.fd").exists(),
                "o VARS escrito por este provisionamento ficou para trás",
            )
            self.assertFalse(
                (vm_dir / "OVMF_CODE.fd").exists(),
                "o CODE escrito por este provisionamento ficou para trás",
            )
            self.assertFalse((vm_dir / "OpenCore.qcow2").exists())

    def test_failure_preserves_ovmf_that_predated_the_call(self):
        """Uma retentativa sobre uma VM que já tinha firmware não o perde."""
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td) / "vms"
            vm = self._vm("retry-provision")
            vm_dir = storage / vm.id
            vm_dir.mkdir(parents=True)
            (vm_dir / "OVMF_VARS.fd").write_bytes(b"NVRAM-ANTERIOR")
            (vm_dir / "OVMF_CODE.fd").write_bytes(b"CODE-ANTERIOR")

            with patch("core.disk.VM_STORAGE_BASE", storage), patch.object(
                DiskProvisioner, "_create_qcow2_disk"
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_vars", side_effect=_write_ovmf("OVMF_VARS.fd")
            ), patch.object(
                DiskProvisioner, "ensure_ovmf_code", side_effect=_write_ovmf("OVMF_CODE.fd")
            ), patch("core.reims_util.generate_opencore", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "kexts APV"):
                    DiskProvisioner.provision(vm)

            self.assertEqual((vm_dir / "OVMF_VARS.fd").read_bytes(), b"NVRAM-ANTERIOR")
            self.assertEqual((vm_dir / "OVMF_CODE.fd").read_bytes(), b"CODE-ANTERIOR")


def _write_opencore(output_path, *args, **kwargs):
    """Stand-in for reims_util.generate_opencore: writes the EFI it promises."""
    Path(output_path).write_bytes(b"OPENCORE")
    return True

def _touch_qcow2(disk_path, size_gb, *, job_context=None):
    """Stand-in for the qcow2 creator: makes the file the caller expects."""
    disk_path = Path(disk_path)
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    disk_path.write_bytes(b"QCOW2")
    return None

def _write_ovmf(name: str):
    """Simula o `ensure_ovmf_*` real: idempotente, não sobrescreve o que existe."""

    def _ensure(dest_dir: Path) -> bool:
        dest = Path(dest_dir) / name
        if not dest.exists():
            dest.write_bytes(b"OVMF-NOVO")
        return True

    return _ensure


if __name__ == "__main__":
    unittest.main()
