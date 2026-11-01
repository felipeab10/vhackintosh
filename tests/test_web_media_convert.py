from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from core.web_media_convert import (
    InvalidMediaConversion,
    ManagedMediaConverter,
    MediaConversionCollision,
)


@unittest.skipUnless(shutil.which("qemu-img"), "qemu-img is required for conversion tests")
class ManagedMediaConverterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "media"
        self.root.mkdir(mode=0o700)
        self.source = self.root / "source.raw"
        self.payload = (b"vhackintosh-conversion-test\x00" * 32768)
        self.source.write_bytes(self.payload)

    def tearDown(self):
        self.temp.cleanup()

    def converter(self, **kwargs):
        return ManagedMediaConverter(self.root, timeout_seconds=30, **kwargs)

    def test_raw_to_qcow2_and_back_preserves_content_and_reports_progress(self):
        progress = []
        converter = self.converter(on_progress=progress.append)
        qcow = converter.convert("source.raw", "converted.qcow2", "raw", "qcow2")
        self.assertEqual((qcow.name, qcow.source_format, qcow.format),
                         ("converted.qcow2", "raw", "qcow2"))
        self.assertGreater(qcow.size_bytes, 0)
        self.assertTrue(progress)
        self.assertEqual(progress[-1], 100.0)
        raw = converter.convert("converted.qcow2", "roundtrip.img", "qcow2", "raw")
        self.assertEqual((raw.name, raw.format, raw.size_bytes), ("roundtrip.img", "raw", len(self.payload)))
        self.assertEqual((self.root / "roundtrip.img").read_bytes(), self.payload)
        self.assertTrue(self.source.is_file())
        self.assertFalse(list(self.root.glob(".convert-*")))

    def test_refuses_destination_collision_without_overwriting(self):
        existing = self.root / "converted.qcow2"
        existing.write_bytes(b"keep")
        with self.assertRaises(MediaConversionCollision):
            self.converter().convert("source.raw", existing.name, "raw", "qcow2")
        self.assertEqual(existing.read_bytes(), b"keep")
        self.assertFalse(list(self.root.glob(".convert-*")))

    def test_rejects_path_traversal_symlinks_and_mismatched_output_extension(self):
        converter = self.converter()
        with self.assertRaises(InvalidMediaConversion):
            converter.convert("../source.raw", "out.qcow2", "raw", "qcow2")
        with self.assertRaises(InvalidMediaConversion):
            converter.convert("source.raw", "out.qcow2", "raw", "raw")
        external = Path(self.temp.name) / "external.raw"
        external.write_bytes(self.payload)
        (self.root / "linked.raw").symlink_to(external)
        with self.assertRaises(InvalidMediaConversion):
            converter.convert("linked.raw", "out.qcow2", "raw", "qcow2")
        self.assertFalse((self.root / "out.qcow2").exists())

    def test_rejects_qcow2_backing_chain(self):
        base = self.root / "base.qcow2"
        overlay = self.root / "overlay.qcow2"
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(base), "1M"], check=True)
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b",
                        str(base), str(overlay)], check=True)
        with self.assertRaises(InvalidMediaConversion):
            self.converter().convert(overlay.name, "copy.qcow2", "qcow2", "qcow2")
        self.assertFalse((self.root / "copy.qcow2").exists())

    def test_cancellation_during_qemu_img_terminates_child_and_cleans_staging(self):
        checks = 0

        def cancel_during_conversion():
            nonlocal checks
            checks += 1
            if checks == 3:
                raise RuntimeError("cancel requested")

        converter = self.converter(check_cancelled=cancel_during_conversion)
        with self.assertRaisesRegex(RuntimeError, "cancel requested"):
            converter.convert("source.raw", "cancelled.qcow2", "raw", "qcow2")
        self.assertFalse((self.root / "cancelled.qcow2").exists())
        self.assertFalse(list(self.root.glob(".convert-*")))
        self.assertTrue(self.source.exists())

    def test_failed_converter_cleans_staging_without_publishing(self):
        converter = self.converter()
        converter._convert_process = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            InvalidMediaConversion("simulated qemu-img failure")
        )
        with self.assertRaises(InvalidMediaConversion):
            converter.convert("source.raw", "failed.qcow2", "raw", "qcow2")
        self.assertFalse((self.root / "failed.qcow2").exists())
        self.assertFalse(list(self.root.glob(".convert-*")))


if __name__ == "__main__":
    unittest.main()
