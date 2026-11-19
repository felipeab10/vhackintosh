from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.opencore_path import DEFAULT_OPENCORE_NAME, opencore_image_path
from core.reims_ctl import write_vm_toml


class OpenCorePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vm_dir = Path(self.temp.name)

    def test_distinct_case_variants_can_be_quarantined_reversibly(self):
        (self.vm_dir / "vm.toml").write_text('[q35]\nopencore = "opencore.qcow2"\n')
        declared = self.vm_dir / "opencore.qcow2"
        legacy = self.vm_dir / "OpenCore.qcow2"
        declared.write_bytes(b"declared image")
        legacy.write_bytes(b"legacy image")

        self.assertEqual(opencore_image_path(self.vm_dir, quarantine_conflict=True), declared)
        self.assertEqual(declared.read_bytes(), b"declared image")
        self.assertFalse(legacy.exists())
        quarantined = list((self.vm_dir / "opencore-quarantine").iterdir())
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].name.split(".")[0], "OpenCore")
        self.assertEqual(quarantined[0].read_bytes(), b"legacy image")

    def test_same_inode_variants_are_not_moved(self):
        (self.vm_dir / "vm.toml").write_text('[q35]\nopencore = "opencore.qcow2"\n')
        declared = self.vm_dir / "opencore.qcow2"
        legacy = self.vm_dir / "OpenCore.qcow2"
        declared.write_bytes(b"same image")
        try:
            os.link(declared, legacy)
        except OSError:
            self.skipTest("hard links unavailable")

        self.assertEqual(opencore_image_path(self.vm_dir, quarantine_conflict=True), declared)
        self.assertTrue(declared.exists())
        self.assertTrue(legacy.exists())
        self.assertFalse((self.vm_dir / "opencore-quarantine").exists())

    def test_only_legacy_variant_is_used_as_is(self):
        legacy = self.vm_dir / "OpenCore.qcow2"
        legacy.write_bytes(b"legacy")
        self.assertEqual(opencore_image_path(self.vm_dir, quarantine_conflict=True), legacy)

    def test_reims_ctl_default_matches_resolver_default(self):
        import inspect

        source = inspect.getsource(write_vm_toml)
        self.assertIn(f'q35.setdefault("opencore", "{DEFAULT_OPENCORE_NAME}")', source)

    def test_missing_image_resolves_to_declared_name(self):
        (self.vm_dir / "vm.toml").write_text('[q35]\nopencore = "opencore.qcow2"\n')
        self.assertEqual(opencore_image_path(self.vm_dir, quarantine_conflict=True), self.vm_dir / "opencore.qcow2")


if __name__ == "__main__":
    unittest.main()
