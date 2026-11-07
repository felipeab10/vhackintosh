from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.control_release import (
    ControlReleaseError,
    ControlRelease,
    _asset_for,
    _release_version_key,
    _verify_checksum,
    is_current_release,
    list_releases,
    package_architecture,
    package_kind,
    install_release,
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

    def test_package_architecture_matches_host_and_rejects_unsupported_hosts(self):
        self.assertEqual(package_architecture("x86_64"), "x86_64")
        self.assertEqual(package_architecture("amd64"), "x86_64")
        self.assertEqual(package_architecture("aarch64"), "aarch64")
        self.assertEqual(package_architecture("arm64"), "aarch64")
        with self.assertRaisesRegex(ControlReleaseError, "não tem pacote Control compatível"):
            package_architecture("riscv64")

    def test_release_assets_are_selected_for_the_native_format(self):
        release = {"assets": [
            {"name": "vhackintosh-control_1.0.0-beta.11_amd64.deb"},
            {"name": "vhackintosh-control-1.0.0.beta.11-1.fc44.x86_64.rpm"},
            {"name": "vhackintosh-control-1.0.0.beta.11-1-x86_64.pkg.tar.zst"},
        ]}
        self.assertEqual(_asset_for(release, "deb")["name"], "vhackintosh-control_1.0.0-beta.11_amd64.deb")
        self.assertEqual(_asset_for(release, "rpm")["name"], "vhackintosh-control-1.0.0.beta.11-1.fc44.x86_64.rpm")
        self.assertEqual(_asset_for(release, "arch")["name"], "vhackintosh-control-1.0.0.beta.11-1-x86_64.pkg.tar.zst")

    def test_release_assets_are_selected_for_the_host_architecture(self):
        release = {"assets": [
            {"name": "vhackintosh-control_1.0.0-beta.11_arm64.deb"},
            {"name": "vhackintosh-control-1.0.0.beta.11-1.fc44.aarch64.rpm"},
            {"name": "vhackintosh-control-1.0.0.beta.11-1-aarch64.pkg.tar.zst"},
        ]}
        self.assertEqual(_asset_for(release, "deb", "aarch64")["name"], "vhackintosh-control_1.0.0-beta.11_arm64.deb")
        self.assertEqual(_asset_for(release, "rpm", "aarch64")["name"], "vhackintosh-control-1.0.0.beta.11-1.fc44.aarch64.rpm")
        self.assertEqual(_asset_for(release, "arch", "aarch64")["name"], "vhackintosh-control-1.0.0.beta.11-1-aarch64.pkg.tar.zst")

    def test_current_release_matches_package_version_across_formats(self):
        self.assertTrue(is_current_release("control-v1.0.0-beta.11", "1.0.0-beta.11"))
        self.assertTrue(is_current_release("control-v1.0.0-beta.11", "1.0.0.beta.11-1"))
        self.assertTrue(is_current_release("control-v1.0.0-beta.11", "1.0.0.beta.11-1.fc44"))
        self.assertFalse(is_current_release("control-v1.0.0-beta.12", "1.0.0-beta.11"))

    def test_releases_are_sorted_newest_to_oldest_by_semver(self):
        tags = [
            "control-v1.0.0-beta.9",
            "control-v1.0.0-beta.14",
            "control-v1.0.0-beta.13",
            "control-v1.0.0",
            "control-v1.0.1-beta.1",
        ]

        def release_item(tag):
            return {
                "tag_name": tag,
                "name": tag,
                "published_at": "2026-11-03T00:00:00Z",
                "prerelease": "-" in tag,
                "assets": [
                    {"name": "vhackintosh-control-1.0.0-beta.14-1-x86_64.pkg.tar.zst", "size": 1024,
                     "browser_download_url": f"https://example.invalid/{tag}.pkg"},
                    {"name": "SHA256SUMS", "browser_download_url": "https://example.invalid/SHA256SUMS"},
                ],
            }

        class Response:
            returncode = 0
            stdout = json.dumps([release_item(tag) for tag in tags])

        with patch("core.control_release.shutil.which", return_value="/usr/bin/curl"), \
             patch("core.control_release.subprocess.run", return_value=Response()):
            releases = list_releases(kind="arch")

        self.assertEqual([release.tag for release in releases], [
            "control-v1.0.1-beta.1",
            "control-v1.0.0",
            "control-v1.0.0-beta.14",
            "control-v1.0.0-beta.13",
            "control-v1.0.0-beta.9",
        ])
        self.assertGreater(
            _release_version_key("control-v1.0.0-beta.14"),
            _release_version_key("control-v1.0.0-beta.9"),
        )

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


class ControlInstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.version_file = self.root / "control-version"
        self.bundle_file = self.root / "control-bundle-id"
        self.web_root = self.root / "web"
        self.current = self.web_root / "current"
        self.installed_version = "1.0.0.beta.20-1"
        self.maintenance = False
        self.commands = []
        self.activate("1.0.0-beta.20", "a" * 64)
        self.release = ControlRelease(
            tag="control-v1.0.0-beta.22", name="Control", published_at="", prerelease=True,
            asset_name="vhackintosh-control-1.0.0.beta.22-1-x86_64.pkg.tar.zst",
            asset_url="https://example.invalid/control.pkg", asset_size=7,
            checksum_url="https://example.invalid/SHA256SUMS",
        )
        self.restart = self.enter_patch("_schedule_restart")
        self.enter_patch("VERSION_FILE", self.version_file)
        self.enter_patch("BUNDLE_ID_FILE", self.bundle_file)
        self.enter_patch("WEB_ROOT", self.web_root)
        self.enter_patch("current_version", side_effect=lambda: self.installed_version)
        self.enter_patch("os.geteuid", return_value=0)
        self.enter_patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}")
        self.enter_patch("_download", side_effect=self.download)
        self.native_run = self.enter_patch("subprocess.run", side_effect=self.run_installer)
        guard = patch("core.update_guard.maintenance_window", self.window)
        guard.start()
        self.addCleanup(guard.stop)

    def enter_patch(self, name, *args, **kwargs):
        patched = patch(f"core.control_release.{name}", *args, **kwargs)
        result = patched.start()
        self.addCleanup(patched.stop)
        return result

    def activate(self, version, bundle_id):
        bundle = self.web_root / "bundles" / bundle_id
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "BUNDLE.json").write_text(json.dumps({"bundle_id": bundle_id}))
        self.version_file.write_text(version)
        self.bundle_file.write_text(bundle_id)
        self.current.unlink(missing_ok=True)
        self.current.symlink_to(f"bundles/{bundle_id}")

    def download(self, url, destination):
        if url.endswith("SHA256SUMS"):
            digest = hashlib.sha256(b"package").hexdigest()
            destination.write_text(f"{digest}  {self.release.asset_name}\n")
        else:
            destination.write_bytes(b"package")

    @contextmanager
    def window(self):
        self.maintenance = True
        try:
            yield
        finally:
            self.maintenance = False

    def run_installer(self, command, **kwargs):
        self.assertTrue(self.maintenance)
        self.commands.append(command)
        self.assertEqual(command[0], "/usr/bin/systemd-run")
        self.assertIn("--wait", command)
        self.assertIn("--pipe", command)
        self.assertIn("--setenv=VHACKINTOSH_CONTROL_UPDATE_DEFER_RESTART=1", command)
        self.assertEqual(command[command.index("--") + 1], "/usr/bin/pacman")
        self.installed_version = "1.0.0.beta.22-1"
        self.activate("1.0.0-beta.22", "b" * 64)
        return SimpleNamespace(returncode=0, stdout="installed", stderr="")

    def test_update_replaces_stale_marker_and_bundle_before_scheduling_restart(self):
        result = install_release(self.release, kind="arch", schedule_restart=True)
        self.assertTrue(result.changed)
        self.assertEqual(self.version_file.read_text(), "1.0.0-beta.22")
        self.assertEqual(self.current.resolve().name, "b" * 64)
        self.assertFalse(self.maintenance)
        self.restart.assert_called_once()

    def test_package_database_success_with_stale_marker_is_a_failed_update(self):
        def partial(command, **kwargs):
            self.installed_version = "1.0.0.beta.22-1"
            return SimpleNamespace(returncode=0, stdout="installed", stderr="Read-only file system")

        self.native_run.side_effect = partial
        with self.assertRaisesRegex(ControlReleaseError, "marcador Control continua em 1.0.0-beta.20"):
            install_release(self.release, kind="arch", schedule_restart=True)
        self.restart.assert_not_called()
        self.assertFalse(self.maintenance)

    def test_new_marker_with_old_active_bundle_is_a_failed_update(self):
        def partial(command, **kwargs):
            response = self.run_installer(command, **kwargs)
            self.current.unlink()
            self.current.symlink_to(f"bundles/{'a' * 64}")
            return response

        self.native_run.side_effect = partial
        with self.assertRaisesRegex(ControlReleaseError, "outro bundle"):
            install_release(self.release, kind="arch", schedule_restart=True)
        self.restart.assert_not_called()

    def test_native_install_failure_keeps_markers_and_does_not_restart(self):
        self.native_run.side_effect = None
        self.native_run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="package install failed")
        with self.assertRaisesRegex(ControlReleaseError, "package install failed"):
            install_release(self.release, kind="arch", schedule_restart=True)
        self.assertEqual(self.version_file.read_text(), "1.0.0-beta.20")
        self.restart.assert_not_called()

    def test_busy_vm_prevents_installer_service_from_starting(self):
        from core.update_guard import MaintenanceBlocked

        with patch("core.update_guard.maintenance_window", side_effect=MaintenanceBlocked("VMs ativas")):
            with self.assertRaisesRegex(ControlReleaseError, "VMs ativas"):
                install_release(self.release, kind="arch", schedule_restart=True)
        self.native_run.assert_not_called()
        self.restart.assert_not_called()

    def test_missing_systemd_run_does_not_fall_back_to_sandboxed_package_manager(self):
        with patch("core.control_release.shutil.which", side_effect=lambda name: None if name == "systemd-run" else f"/usr/bin/{name}"):
            with self.assertRaisesRegex(ControlReleaseError, "systemd-run"):
                install_release(self.release, kind="arch", schedule_restart=True)
        self.native_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
