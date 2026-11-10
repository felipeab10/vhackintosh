"""Gate do `reims-util` exercitado contra binários REAIS, não mocks.

Regressão que estes testes guardam (Demanda 002): `tests/test_reims_util.py` e
`tests/test_web_provision_subprocess.py` interceptam `subprocess.run` global e
devolvem um `--help` fabricado. O gate de `core/reims_util.py` lê esse mesmo
`--help`, então ele nunca foi avaliado contra um binário de verdade: os testes
passavam verdes mesmo quando o critério estava errado, e foi assim que um gate
por `--no-apv-arm64-port` derrubou o binário master sem ninguém notar.

Há duas gerações de reims-util e as duas são aceitas:

  1. o pin antigo (`c717e69`) embute os 9 kexts na imagem e **não** conhece
     `--add-kext`; o `--no-apv-arm64-port` aparece no `--help`;
  2. o master (`>= fe9bfe1`) não embute kexts de guest — só Lilu — e expõe
     `--add-kext`; o `--no-apv-arm64-port` foi aposentado (aceito e ignorado,
     escondido do `--help`).

Contra os dois, `generate_opencore` tem de produzir uma EFI com as 9 kexts
habilitadas. Os testes são `skipif`: num CI de terceiros os binários não
existem, e um skip é honesto — um mock ali seria a mesma cegueira de novo.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import reims_util
from core.config import SMBIOSConfig

# Os binários de referência: o master (não vendorizado, existe só onde alguém o
# compilou para testar a migração) e o pin que o pacote do Control instala.
MASTER_BINARY = Path("/tmp/ru-check/target/release/reims-util")
PIN_BINARY = Path("/opt/vhackintosh/resources/reims-util/reims-util")

# O que a EFI final tem de carregar. Lilu.kext é o único kext que o master ainda
# embute; os outros oito vêm de `resources/reims-kexts/` por `--add-kext`.
EXPECTED_KEXTS = {
    "Lilu.kext",
    "ReimsVgpu.kext",
    "AppleGraphicsDeviceControlBridge.kext",
    "AppleParavirtGPULoader.kext",
    "AppleParavirtIOSurface.kext",
    "IOGPUFamilyBridge.kext",
    "AppleParavirtGPUIOGPUFamily.kext",
    "IOGraphicsFamilyBridge.kext",
    "APVDisplayPort.kext",
}


def _help_text(binary: Path) -> str:
    result = subprocess.run(
        [str(binary), "opencore", "--help"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
    )
    return result.stdout or ""


def _kexts_in_image(image: Path) -> set[str]:
    """Lê os kexts embutidos na imagem gerada, via guestfish."""
    listing = subprocess.run(
        ["guestfish", "--ro", "-a", str(image), "-m", "/dev/sda1", "ls", "/EFI/OC/Kexts"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=300,
    )
    return {line.strip() for line in listing.stdout.splitlines() if line.strip()}


@unittest.skipUnless(shutil.which("guestfish"), "guestfish é necessário para inspecionar a imagem")
@unittest.skipUnless(shutil.which("qemu-img"), "qemu-img é necessário para validar a imagem")
class RealReimsUtilGateTests(unittest.TestCase):
    """O gate e a geração da EFI, contra os binários que existem nesta máquina."""

    sample_smbios = SMBIOSConfig(
        model="iMac20,1",
        serial_number="C02TEST12345",
        board_serial="C0270000000000001",
        smuuid="12345678-1234-5678-1234-567812345678",
        rom="02AABBCCDDEE",
    )

    def _generate(self, binary: Path, directory: Path) -> tuple[bool, Path]:
        output = Path(directory) / "OpenCore.qcow2"
        with patch("core.reims_util.find_reims_util", return_value=binary), patch(
            "core.reims_util.LOGS_DIR", Path(directory)
        ), patch("core.reims_util.LOG_FILE", Path(directory) / "reims-util-opencore.log"):
            return (
                reims_util.generate_opencore(
                    output,
                    self.sample_smbios,
                    no_ovmf=True,
                    timeout=reims_util.OPENCORE_PICKER_TIMEOUT,
                    boot_args=reims_util.OPENCORE_BOOT_ARGS,
                ),
                output,
            )

    @unittest.skipUnless(PIN_BINARY.is_file(), f"binário pin ausente em {PIN_BINARY}")
    def test_gate_accepts_the_pinned_binary_that_forwards_the_kexts(self):
        """O pin atual é o master add-kext: entra por `--add-kext` e sai com as 9.

        O pin antigo embutia os kexts e expunha `--no-apv-arm64-port`; o pin
        instalado hoje (fe9bfe1) não conhece a flag aposentada e exige os
        `--add-kext` vendorizados. Conteúdo pós-migração, mesma cobertura.
        """
        help_text = _help_text(PIN_BINARY)
        self.assertIn(
            reims_util._ADD_KEXT_FLAG,
            help_text,
            "o pin deveria expor --add-kext; é o critério atual do gate",
        )
        self.assertNotIn(
            reims_util._LEGACY_APV_FLAG,
            help_text,
            "a flag aposentada não está no --help do pin add-kext",
        )
        with tempfile.TemporaryDirectory() as td:
            ok, output = self._generate(PIN_BINARY, td)
            self.assertTrue(ok, "o gate recusou o pin, que é o binário em produção")
            self.assertEqual(
                _kexts_in_image(output),
                EXPECTED_KEXTS,
                "o pin não embute os kexts: eles têm de vir pelos --add-kext vendorizados",
            )

    @unittest.skipUnless(MASTER_BINARY.is_file(), f"binário master ausente em {MASTER_BINARY}")
    def test_gate_accepts_the_master_binary_that_dropped_the_kexts(self):
        """O master não expõe a flag aposentada: um gate por ela o reprovaria.

        Este é o teste que faltava. Ele falha com o gate antigo (`--no-apv-arm64-port`)
        e passa com o critério novo (`--add-kext`), contra o binário de verdade.
        """
        help_text = _help_text(MASTER_BINARY)
        self.assertIn(
            reims_util._ADD_KEXT_FLAG,
            help_text,
            "o master deveria expor --add-kext; é o critério que o substitui",
        )
        self.assertNotIn(
            reims_util._LEGACY_APV_FLAG,
            help_text,
            "a flag aposentada não pode voltar ao --help do master",
        )
        with tempfile.TemporaryDirectory() as td:
            ok, output = self._generate(MASTER_BINARY, td)
            self.assertTrue(
                ok,
                "o gate recusou o master: é a regressão que derrubou o --no-apv-arm64-port",
            )
            self.assertEqual(
                _kexts_in_image(output),
                EXPECTED_KEXTS,
                "o master não embute os kexts: eles têm de vir pelos --add-kext vendorizados",
            )

    @unittest.skipUnless(
        MASTER_BINARY.is_file() and reims_util.VENDORED_KEXTS_DIR.is_dir(),
        "kexts vendorizados ausentes",
    )
    def test_master_without_vendored_kexts_refuses_instead_of_shipping_a_blind_efi(self):
        """Sem os kexts em disco, o master recusa; não gera uma EFI sem drivers."""
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "OpenCore.qcow2"
            with patch("core.reims_util.find_reims_util", return_value=MASTER_BINARY), patch(
                "core.reims_util.LOGS_DIR", Path(td)
            ), patch("core.reims_util.LOG_FILE", Path(td) / "log.txt"), patch(
                "core.reims_util.VENDORED_KEXTS_DIR", Path(td) / "nao-existe"
            ):
                self.assertFalse(
                    reims_util.generate_opencore(output, self.sample_smbios, no_ovmf=True)
                )
                self.assertFalse(output.exists(), "nenhuma imagem parcial pode ficar para trás")


if __name__ == "__main__":
    unittest.main()
