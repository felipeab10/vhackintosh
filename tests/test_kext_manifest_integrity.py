"""Contrato de integridade do set.toml (fonte da verdade dos executáveis dos kexts).

Acordado com Nova: o Python valida porque a ISO constrói por
`appliance/build_opencore.kext_injection`, que nunca toca o Rust. O manifesto fica ao lado
dos bundles do perfil (`profile_kext_dir(profile, base)/set.toml`); ausência só é aceita para
`apple-gfx-pci` (perfil legado flat, exceção nomeada); todo perfil novo exige manifesto;
cada `[[kext]]` com `sha256` exige `ExecutablePath` e o hash do executável tem de bater.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from core.graphics_profiles import verify_kext_manifest

NEW = "reims-graphics-pci"

def _profile_root(base: Path) -> Path:
    """Diretório onde `verify_kext_manifest` procura o set.toml para NEW."""
    return base / NEW

def _write_bundle(root: Path, bundle: str, executable: str, payload: bytes) -> str:
    exe = root / bundle / executable
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(payload)
    plist = root / bundle / "Contents" / "Info.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(b"<plist/>")
    return hashlib.sha256(payload).hexdigest()

def _write_manifest(root: Path, body: str) -> None:
    (root / "set.toml").write_text(body, encoding="utf-8")

class KextManifestIntegrityTests(unittest.TestCase):
    def test_absent_manifest_is_accepted_only_for_named_legacy_profile(self):
        """Legado flat é a exceção; a ausência no perfil novo deve falhar fechado."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ReimsVgpu.kext").mkdir()
            verify_kext_manifest("apple-gfx-pci", root)  # exceção explícita
            with self.assertRaisesRegex(RuntimeError, "manifesto obrigatório ausente"):
                verify_kext_manifest(NEW, root)

    def test_matching_hash_passes(self):
        with tempfile.TemporaryDirectory() as td:
            profile_root = _profile_root(Path(td))
            digest = _write_bundle(profile_root, "Foo.kext", "Contents/MacOS/Foo", b"payload")
            _write_manifest(profile_root, f'[[kext]]\nBundlePath = "Foo.kext"\nExecutablePath = "Contents/MacOS/Foo"\nsha256 = "{digest}"\n')
            verify_kext_manifest(NEW, Path(td))  # não levanta

    def test_hex_compare_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as td:
            profile_root = _profile_root(Path(td))
            digest = _write_bundle(profile_root, "Foo.kext", "Contents/MacOS/Foo", b"payload")
            _write_manifest(profile_root, f'[[kext]]\nBundlePath = "Foo.kext"\nExecutablePath = "Contents/MacOS/Foo"\nsha256 = "{digest.upper()}"\n')
            verify_kext_manifest(NEW, Path(td))

    def test_one_byte_change_is_refused_naming_bundle_path_expected_actual(self):
        with tempfile.TemporaryDirectory() as td:
            profile_root = _profile_root(Path(td))
            digest = _write_bundle(profile_root, "RgfxGate.kext", "Contents/MacOS/RgfxGate", b"payload")
            (profile_root / "RgfxGate.kext" / "Contents/MacOS/RgfxGate").write_bytes(b"payloaX")
            _write_manifest(profile_root, f'[[kext]]\nBundlePath = "RgfxGate.kext"\nExecutablePath = "Contents/MacOS/RgfxGate"\nsha256 = "{digest}"\n')
            with self.assertRaises(RuntimeError) as ctx:
                verify_kext_manifest(NEW, Path(td))
            message = str(ctx.exception)
            self.assertIn("RgfxGate.kext", message)
            self.assertIn("Contents/MacOS/RgfxGate", message)
            self.assertIn(digest, message)  # esperado
            self.assertIn(hashlib.sha256(b"payloaX").hexdigest(), message)  # obtido

    def test_sha256_without_executable_path_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            profile_root = _profile_root(Path(td))
            profile_root.mkdir(parents=True, exist_ok=True)
            _write_manifest(profile_root, '[[kext]]\nBundlePath = "Foo.kext"\nsha256 = "00"\n')
            with self.assertRaises(RuntimeError):
                verify_kext_manifest(NEW, Path(td))

    def test_entry_without_sha256_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            profile_root = _profile_root(Path(td))
            profile_root.mkdir(parents=True, exist_ok=True)
            _write_manifest(profile_root, '[[kext]]\nBundlePath = "Foo.kext"\nExecutablePath = "Contents/MacOS/Foo"\n')
            verify_kext_manifest(NEW, Path(td))

    def test_missing_executable_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            profile_root = _profile_root(Path(td))
            (profile_root / "Foo.kext").mkdir(parents=True, exist_ok=True)
            _write_manifest(profile_root, '[[kext]]\nBundlePath = "Foo.kext"\nExecutablePath = "Contents/MacOS/Foo"\nsha256 = "00"\n')
            with self.assertRaises(RuntimeError):
                verify_kext_manifest(NEW, Path(td))

    def test_real_reims_graphics_manifest_verifies(self):
        """O manifesto real dos 5 bundles tem de verificar como está no disco."""
        from core.graphics_profiles import profile_kext_dir
        directory = profile_kext_dir(NEW)
        self.assertTrue((directory / "set.toml").is_file(), f"manifesto obrigatório ausente: {directory / 'set.toml'}")
        verify_kext_manifest(NEW)

if __name__ == "__main__":
    unittest.main()
