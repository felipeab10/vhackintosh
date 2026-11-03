from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.control_release import (
    ControlReleaseError,
    _asset_for,
    _verify_checksum,
    is_current_release,
    package_kind,
)


class ControlReleaseTests(unittest.TestCase):
    def test_package_kind_recognizes_appliance_and_common_distros(self):
        self.assertEqual(package_kind({"ID": "cachyos"}), "arch")
        self.assertEqual(package_kind({"ID": "ubuntu"}), "deb")
        self.assertEqual(package_kind({"ID": "fedora"}), "rpm")
        self.assertEqual(package_kind({"ID": "linux", "ID_LIKE": "debian"}), "deb")
        with self.assertRaises(ControlReleaseError):
            package_kind({"ID": "gentoo"})
        with patch("core.control_release._os_release", return_value={"ID": "cachyos"}):
            self.assertEqual(package_kind(), "arch")

    def test_release_assets_are_selected_for_the_native_format(self):
        release = {"assets": [
            {"name": "vhackintosh-control_1.0.0-beta.11_amd64.deb"},
            {"name": "vhackintosh-control-1.0.0.beta.11-1.fc44.x86_64.rpm"},
            {"name": "vhackintosh-control-1.0.0.beta.11-1-x86_64.pkg.tar.zst"},
        ]}
        self.assertEqual(_asset_for(release, "deb")["name"], "vhackintosh-control_1.0.0-beta.11_amd64.deb")
        self.assertEqual(_asset_for(release, "rpm")["name"], "vhackintosh-control-1.0.0.beta.11-1.fc44.x86_64.rpm")
        self.assertEqual(_asset_for(release, "arch")["name"], "vhackintosh-control-1.0.0.beta.11-1-x86_64.pkg.tar.zst")

    def test_current_release_matches_package_version_across_formats(self):
        self.assertTrue(is_current_release("control-v1.0.0-beta.11", "1.0.0-beta.11"))
        self.assertTrue(is_current_release("control-v1.0.0-beta.11", "1.0.0.beta.11-1"))
        self.assertTrue(is_current_release("control-v1.0.0-beta.11", "1.0.0.beta.11-1.fc44"))
        self.assertFalse(is_current_release("control-v1.0.0-beta.12", "1.0.0-beta.11"))

    def test_checksum_is_bound_to_the_exact_package_name(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "control.pkg"
            package.write_bytes(b"verified Control package")
            checksum = hashlib.sha256(package.read_bytes()).hexdigest()
            sums = Path(directory) / "SHA256SUMS"
            sums.write_text(f"{checksum}  control.pkg\n", encoding="utf-8")
            _verify_checksum(package, sums, "control.pkg")
            sums.write_text(f"{'0' * 64}  control.pkg\n", encoding="utf-8")
            with self.assertRaises(ControlReleaseError):
                _verify_checksum(package, sums, "control.pkg")


if __name__ == "__main__":
    unittest.main()
