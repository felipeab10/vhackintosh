"""Contrato da procedência única do `reims-util` (D1 do plano de integração).

O pacote do Control compila o `reims-util` do SHA fixado em
`packaging/reims-util.pin`; o build da ISO do appliance tem de exigir o MESMO
SHA do binário do host, em vez de aceitar qualquer um e gravar `commit=unknown`.

Estes testes dirigem o leitor real `packaging/common/reims-util-pin.sh`, não uma
reescrita em Python: a regra que vale em produção é o shell, então é o shell que
precisa de prova. Eles cobrem:

  (a) binário do host com SHA divergente do pin  → `reims_util_pin_verify_binary` recusa;
  (b) sidecar `.commit` ausente                  → recusa (procedência desconhecida);
  (c) binário correto com o sidecar do pin        → aceita.

A prova por mutação é o par (a)/(b) vs (c): qualquer relaxamento que troque o
`return 1` por aceitar o binário faz (a) e (b) falharem.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN_FILE = ROOT / "packaging/reims-util.pin"
PIN_SCRIPT = ROOT / "packaging/common/reims-util-pin.sh"
BUILD_UTIL = ROOT / "packaging/common/build-reims-util.sh"
BUILD_ISO = ROOT / "appliance/build-iso.sh"
UPDATE_SCRIPT = ROOT / "appliance/update-reims-util.sh"
PIN_REV = "0b9c3780a3ec2aa312a448b09b18320e5a2c9251"
LEGACY_REV = "c717e69f0a74ba050973dcca294ba35499145c56"


def _run_pin_verify(binary: Path) -> subprocess.CompletedProcess:
    """Executa o leitor real num shell e devolve o processo."""
    program = (
        'set -euo pipefail\n'
        f'. "{PIN_SCRIPT}"\n'
        'reims_util_pin_load\n'
        f'reims_util_pin_verify_binary "{binary}"\n'
    )
    return subprocess.run(
        ["bash", "-c", program],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60,
    )


def _fake_binary(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    path.chmod(0o755)
    return path


class ReimsUtilPinSourceTests(unittest.TestCase):
    """A tríade repositório+SHA+toolchain tem UMA fonte, lida pelos dois lados."""

    def test_pin_file_declares_the_full_triad(self):
        text = PIN_FILE.read_text(encoding="utf-8")
        values = dict(
            line.split("=", 1)
            for line in text.splitlines()
            if line and not line.startswith("#") and "=" in line
        )
        self.assertEqual(values["REPO"], "https://github.com/steelbrain/reims-util.git")
        self.assertEqual(values["REV"], PIN_REV)
        self.assertTrue(values["TOOLCHAIN"])
        self.assertRegex(values["REV"], r"^[0-9a-f]{40}$")

    def test_package_builder_reads_the_pin_instead_of_hardcoding(self):
        builder = BUILD_UTIL.read_text(encoding="utf-8")
        self.assertIn("reims-util-pin.sh", builder)
        self.assertIn("reims_util_pin_load", builder)
        # O SHA mora no pin, não no script do builder.
        self.assertNotIn(PIN_REV, builder)
        self.assertNotIn('REPO="https://github.com/steelbrain/reims-util.git"', builder)

    def test_iso_build_reads_the_same_pin_and_has_no_unknown_default(self):
        iso = BUILD_ISO.read_text(encoding="utf-8")
        self.assertIn("packaging/common/reims-util-pin.sh", iso)
        self.assertIn("reims_util_pin_load", iso)
        self.assertIn("reims_util_pin_verify_binary", iso)
        # `commit=unknown` era o default silencioso que o plano manda eliminar.
        self.assertNotIn("commit=unknown", iso)
        self.assertNotIn("REIMS_UTIL_COMMIT=\"unknown\"", iso)


class ReimsUtilPinVerifyTests(unittest.TestCase):
    """A regra do shell contra binários reais em disco (a, b, c)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.binary = _fake_binary(Path(self.tmp.name) / "reims-util")

    def tearDown(self):
        self.tmp.cleanup()

    def test_correct_binary_with_pinned_sidecar_is_accepted(self):
        Path(f"{self.binary}.commit").write_text(f"{PIN_REV}\n", encoding="ascii")
        result = _run_pin_verify(self.binary)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_divergent_sha_is_rejected(self):
        Path(f"{self.binary}.commit").write_text(f"{LEGACY_REV}\n", encoding="ascii")
        result = _run_pin_verify(self.binary)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provenance mismatch", result.stderr)

    def test_missing_sidecar_is_rejected(self):
        self.assertFalse(Path(f"{self.binary}.commit").exists())
        result = _run_pin_verify(self.binary)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provenance unknown", result.stderr)

    def test_empty_sidecar_is_rejected(self):
        Path(f"{self.binary}.commit").write_text("\n", encoding="ascii")
        result = _run_pin_verify(self.binary)
        self.assertNotEqual(result.returncode, 0)


class GraphicsProfilePinTests(unittest.TestCase):
    """A ISO declara o perfil a priori; não o detecta do QEMU (do host) de build.

    Regressão que estes testes guardam: `build-iso.sh` gerava o OpenCore no passo 4,
    antes de o QEMU da release ser baixado no passo 5, e o gerador passou a EXIGIR
    detecção de perfil — um host de build limpo abortava. A correção é declarar o
    perfil no pin e passá-lo explicitamente, sem depender de QEMU algum.
    """

    def _load_profile(self):
        program = (
            'set -euo pipefail\n'
            f'. "{PIN_SCRIPT}"\n'
            'reims_util_pin_load\n'
            'printf %s "$REIMS_UTIL_GRAPHICS_PROFILE"\n'
        )
        result = subprocess.run(
            ["bash", "-c", program], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=60, cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_pin_declares_a_known_graphics_profile(self):
        profile = self._load_profile()
        known = {"apple-gfx-pci", "reims-graphics-pci"}
        self.assertIn(profile, known)

    def test_iso_passes_the_pinned_profile_explicitly(self):
        iso = BUILD_ISO.read_text(encoding="utf-8")
        # Presença das três peças.
        self.assertIn('. "${PROJECT_ROOT}/packaging/common/reims-util-pin.sh"', iso)
        self.assertIn('reims_util_pin_load', iso)
        self.assertIn('--graphics-profile "${REIMS_UTIL_GRAPHICS_PROFILE}"', iso)
        self.assertIn('reims_util_pin_verify_qemu_device "${REIMS_QEMU_BIN}"', iso)
        # ... e ORDEM: presença textual sozinha passa com o script quebrado (o
        # mutante que move o source para depois do uso deixa a variável vazia e o
        # gerador recusa a flag). Aqui provamos a ordem que impede essa regressão.

        # (1) O pin é lido (source + load) ANTES da primeira leitura da variável.
        source_at = iso.index('. "${PROJECT_ROOT}/packaging/common/reims-util-pin.sh"')
        load_at = iso.index("reims_util_pin_load")
        use_at = iso.index("--graphics-profile", load_at)
        self.assertLess(
            load_at, use_at,
            "o pin do reims-util tem de ser carregado antes de "
            "REIMS_UTIL_GRAPHICS_PROFILE ser usado pelo gerador",
        )
        self.assertLess(
            source_at, load_at,
            "`reims_util_pin_load` só existe depois do source do pin",
        )

        # (2) A flag do perfil vai ao gerador ANTES de o QEMU da release existir
        # no disco: a ISO declara o que embarca sem esperar pelo QEMU, e é a
        # inexistência dele no passo 4 que abortava o build em host limpo. O
        # marcador é a EXTRAÇÃO do tarball (onde o QEMU aparece), não o `echo`
        # que anuncia o download — pôr o gerador depois do echo ainda é a
        # regressão.
        download_at = iso.index('tar -xzf "${REIMS_TMP_TAR}"')
        self.assertLess(
            use_at, download_at,
            "--graphics-profile tem de rodar antes de a release ser extraída "
            "(antes de existir QEMU); o contrário reintroduz a dependência do "
            "QEMU do host de build",
        )

        # (3) A verificação de device do QEMU EMBARCADO roda DEPOIS do download
        # (só existe binário para conferir depois de extrair o tarball).
        verify_at = iso.index('reims_util_pin_verify_qemu_device "${REIMS_QEMU_BIN}"')
        self.assertLess(
            download_at, verify_at,
            "a verificação do QEMU embarcado tem de rodar após o download",
        )

    def test_order_check_catches_source_moved_after_use(self):
        """Reproduz o mutante 2: source depois do uso -> a ordem tem de falhar.

        O mutante movia `. pin.sh` para DEPOIS do `--graphics-profile`, deixando
        `REIMS_UTIL_GRAPHICS_PROFILE` vazio no momento do uso (o gerador recusa
        `''`). O `assertIn` textual não pegava; a checagem de ordem pega.
        """
        original = BUILD_ISO.read_text(encoding="utf-8")
        source_line = '. "${PROJECT_ROOT}/packaging/common/reims-util-pin.sh"\n'
        self.assertIn(source_line, original)
        # Remove o source do topo e recoloca depois da linha do gerador.
        moved = original.replace(source_line, "", 1)
        use_line = '    --graphics-profile "${REIMS_UTIL_GRAPHICS_PROFILE}" \\\n'
        self.assertIn(use_line, moved)
        mutant = moved.replace(use_line, use_line + source_line, 1)

        source_at = mutant.index(source_line.strip())
        load_at = mutant.index("reims_util_pin_load")
        use_at = mutant.index("--graphics-profile", load_at)
        with self.assertRaises(AssertionError):
            self.assertLess(
                source_at, use_at,
                "source tem de preceder o uso de REIMS_UTIL_GRAPHICS_PROFILE",
            )

    def _verify_qemu(self, path):
        program = (
            'set -euo pipefail\n'
            f'. "{PIN_SCRIPT}"\n'
            'reims_util_pin_load\n'
            f'reims_util_pin_verify_qemu_device "{path}"\n'
        )
        return subprocess.run(
            ["bash", "-c", program], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=60, cwd=str(ROOT),
        )

    def test_bundled_qemu_matching_the_pin_is_accepted(self):
        profile = self._load_profile()
        with tempfile.TemporaryDirectory() as td:
            qemu = Path(td) / "qemu-system-x86_64"
            qemu.write_bytes(b"\x7fELF fake " + profile.encode())
            result = self._verify_qemu(qemu)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bundled_qemu_without_the_profile_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            qemu = Path(td) / "qemu-system-x86_64"
            qemu.write_bytes(b"\x7fELF no known device here")
            result = self._verify_qemu(qemu)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not advertise", result.stderr + result.stdout)

    def test_bundled_qemu_of_the_other_profile_is_rejected(self):
        profile = self._load_profile()
        other = "reims-graphics-pci" if profile == "apple-gfx-pci" else "apple-gfx-pci"
        with tempfile.TemporaryDirectory() as td:
            qemu = Path(td) / "qemu-system-x86_64"
            qemu.write_bytes(b"\x7fELF " + other.encode())
            result = self._verify_qemu(qemu)
        self.assertNotEqual(result.returncode, 0)


class ReimsUtilUpdateScriptPinsTheSameShaTests(unittest.TestCase):
    """`update-reims-util.sh` instala o SHA do pin, não o HEAD de `master`."""

    def test_update_script_defaults_to_the_pinned_revision(self):
        text = UPDATE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("reims-util-pin.sh", text)
        self.assertIn("REF=\"${REIMS_UTIL_REV}\"", text)
        self.assertIn('REF="${REIMS_UTIL_REF:-master}"', text)

    def test_pin_load_reports_the_triad(self):
        program = (
            'set -euo pipefail\n'
            f'. "{PIN_SCRIPT}"\n'
            'reims_util_pin_load\n'
            'printf "%s\\n%s\\n%s\\n" "$REIMS_UTIL_REPO" "$REIMS_UTIL_REV" "$REIMS_UTIL_TOOLCHAIN"\n'
        )
        result = subprocess.run(
            ["bash", "-c", program], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=60, cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        repo, rev, toolchain = result.stdout.splitlines()
        self.assertEqual(repo, "https://github.com/steelbrain/reims-util.git")
        self.assertEqual(rev, PIN_REV)
        self.assertTrue(toolchain)


if __name__ == "__main__":
    unittest.main()
