"""Profile-driven EFI kext sets for apple-gfx-pci and reims-graphics-pci."""
from __future__ import annotations

import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class GraphicsProfileTests(unittest.TestCase):
    def test_profiles_are_disjoint_and_vendored(self):
        from appliance import build_opencore as builder

        self.assertEqual(set(builder.GRAPHICS_PROFILES), {"apple-gfx-pci", "reims-graphics-pci"})
        legacy = {name for name, _ in builder.GRAPHICS_PROFILES["apple-gfx-pci"]}
        rgfx = {name for name, _ in builder.GRAPHICS_PROFILES["reims-graphics-pci"]}
        self.assertTrue(legacy.isdisjoint(rgfx))
        for profile, bundles in builder.GRAPHICS_PROFILES.items():
            directory = builder.profile_kext_dir(profile)
            for name, _minimum in bundles:
                with self.subTest(profile=profile, bundle=name):
                    self.assertTrue((directory / name / "Contents/Info.plist").is_file())
                    self.assertTrue(list((directory / name / "Contents/MacOS").iterdir()))

    def test_kext_injection_selects_only_requested_profile(self):
        from appliance import build_opencore as builder

        help_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="--add-kext", stderr="")
        with patch.object(builder.subprocess, "run", return_value=help_result):
            for profile in builder.GRAPHICS_PROFILES:
                args = builder.kext_injection(Path("/tmp/reims-util-master"), profile)
                supplied = [Path(value.split(",", 1)[0]).name for flag, value in zip(args[::2], args[1::2]) if flag == "--add-kext"]
                expected = [name for name, _ in builder.GRAPHICS_PROFILES[profile]]
                self.assertEqual(supplied, expected)
                self.assertNotIn("Lilu.kext", supplied)
                self.assertEqual(args.count("--add-kext"), len(expected))
                for flag, value in zip(args[::2], args[1::2]):
                    self.assertEqual(flag, "--add-kext")
                    _path, minimum, maximum = value.split(",")
                    self.assertEqual(maximum, "25.99.99")
                    self.assertEqual(minimum, dict(builder.GRAPHICS_PROFILES[profile])[_path.split("/")[-1]])

    def test_unknown_profile_fails_closed_mutation(self):
        from appliance import build_opencore as builder

        for call in (
            lambda: builder.profile_kextexts("mutated-unknown-profile"),
            lambda: builder.kext_injection(Path("/tmp/reims-util-master"), "mutated-unknown-profile"),
            lambda: builder.detect_graphics_profile(None, explicit="mutated-unknown-profile"),
        ):
            with self.subTest(call=call), self.assertRaisesRegex(RuntimeError, "desconhecido"):
                call()

    def test_qemu_device_detection_is_binary_evidence_and_refuses_unknown_or_ambiguous(self):
        from appliance import build_opencore as builder

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "qemu"
            path.write_bytes(b"prefix apple-gfx-pci suffix")
            self.assertEqual(builder.detect_graphics_profile(path), "apple-gfx-pci")
            path.write_bytes(b"prefix reims-graphics-pci suffix")
            self.assertEqual(builder.detect_graphics_profile(path), "reims-graphics-pci")
            path.write_bytes(b"apple-gfx-pci and reims-graphics-pci")
            self.assertEqual(builder.detect_graphics_profile(path), "reims-graphics-pci")
            path.write_bytes(b"ordinary QEMU")
            with self.assertRaisesRegex(RuntimeError, "nenhum device"):
                builder.detect_graphics_profile(path)

    def test_mutated_unknown_profile_is_rejected_by_cli(self):
        proc = subprocess.run(
            ["python3", str(ROOT / "appliance/build_opencore.py"),
             "--reims-util", "/does/not/matter", "--output", "/tmp/out.qcow2",
             "--config-output", "/tmp/out.plist", "--graphics-profile", "mutated-unknown"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid choice", proc.stderr)
        self.assertIn("apple-gfx-pci,reims-graphics-pci", proc.stderr)

    @unittest.skipUnless(shutil.which("qemu-img") and shutil.which("guestfish"), "qemu-img/guestfish unavailable")
    def test_efi_generation_for_each_profile(self):
        """Generate both EFI images with the available reims-util master binary."""
        from appliance import build_opencore as builder

        util = Path("/tmp/ru-check/target/release/reims-util")
        if not util.is_file() or "--add-kext" not in subprocess.run(
            [str(util), "opencore", "--help"], capture_output=True, text=True
        ).stdout:
            self.skipTest("master reims-util --add-kext binary unavailable")
        for profile in builder.GRAPHICS_PROFILES:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as td:
                image = Path(td) / "OpenCore.qcow2"
                config = Path(td) / "config.plist"
                builder.build(util, image, config, graphics_profile=profile)
                data = plistlib.loads(config.read_bytes())
                entries = data["Kernel"]["Add"]
                expected = {"Lilu.kext": "25.0.0", **dict(builder.GRAPHICS_PROFILES[profile])}
                selected = {entry["BundlePath"]: entry for entry in entries if entry.get("Enabled")}
                self.assertEqual(set(selected), set(expected))
                for name, minimum in expected.items():
                    self.assertEqual(selected[name]["MinKernel"], minimum)
                    self.assertEqual(selected[name]["MaxKernel"], "25.99.99")
                listing = subprocess.run(
                    ["guestfish", "--ro", "-a", str(image), "-m", "/dev/sda1", "ls", "/EFI/OC/Kexts"],
                    check=True, capture_output=True, text=True,
                ).stdout.splitlines()
                self.assertEqual(set(listing), set(expected))

if __name__ == "__main__":
    unittest.main()
