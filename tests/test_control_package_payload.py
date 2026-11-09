from __future__ import annotations

import configparser
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ControlPackagePayloadTests(unittest.TestCase):
    def test_package_builds_and_stages_the_pinned_apv_reims_util(self):
        builder = (ROOT / "packaging/common/build-reims-util.sh").read_text(encoding="utf-8")
        build_all = (ROOT / "packaging/build-all.sh").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/release-control.yml").read_text(encoding="utf-8")
        revision = "0b9c3780a3ec2aa312a448b09b18320e5a2c9251"

        self.assertIn(revision, builder)
        self.assertIn("cargo \"+${TOOLCHAIN}\" build --locked --release", builder)
        self.assertIn("--add-kext", builder)
        self.assertNotIn("--no-apv-arm64-port", builder)
        self.assertIn('"${PAYLOAD_BASE}/opt/vhackintosh/resources/reims-util"', build_all)
        self.assertIn("/opt/vhackintosh/resources/reims-util/reims-util", workflow)
        self.assertIn("--add-kext", workflow)
        self.assertNotIn("--no-apv-arm64-port", workflow)

    def test_staging_includes_the_pinned_macserial_tool_and_redirect_service(self):
        stage = (ROOT / "packaging/common/stage-app.sh").read_text(encoding="utf-8")
        binary = ROOT / "resources/macserial/macserial"
        expected_sha256 = "b0a4a3581c0f06b3ddce915165eb3a1726acfa876192fe9ee5ef9ea08a469ad8"

        self.assertEqual(hashlib.sha256(binary.read_bytes()).hexdigest(), expected_sha256)
        self.assertIn('"${ROOT}/resources/macserial/macserial"', stage)
        self.assertIn('"${ROOT}/resources/macserial/LICENSE.txt"', stage)
        self.assertIn('"${ROOT}/resources/macserial/README.md"', stage)
        self.assertIn(expected_sha256, stage)
        self.assertIn('"${ROOT}/packaging/systemd/vhackintosh-http-redirect.service"', stage)
        self.assertIn('"${DESTDIR}/usr/lib/systemd/system/vhackintosh-http-redirect.service"', stage)

    def test_staged_control_bundle_normalizes_restrictive_source_permissions(self):
        stage = (ROOT / "packaging/common/stage-app.sh").read_text(encoding="utf-8")
        normalizer = ROOT / "packaging/common/normalize-tree-permissions.sh"
        self.assertIn('normalize-tree-permissions.sh" "${APP}"', stage)

        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            core = bundle / "core"
            core.mkdir(parents=True)
            unreadable_module = core / "hardware.py"
            unreadable_module.write_text("value = 1\n", encoding="utf-8")
            unreadable_module.chmod(0o600)
            executable = bundle / "launcher"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
            core.chmod(0o700)

            subprocess.run(["bash", str(normalizer), str(bundle)], check=True)

            self.assertEqual(os.stat(core).st_mode & 0o777, 0o755)
            self.assertEqual(os.stat(unreadable_module).st_mode & 0o777, 0o644)
            self.assertEqual(os.stat(executable).st_mode & 0o777, 0o755)

    def test_desktop_package_stages_the_matching_ovmf_code_and_vars_templates(self):
        stage = (ROOT / "packaging/common/stage-app.sh").read_text(encoding="utf-8")
        self.assertIn('"${APP}/templates"', stage)
        self.assertIn('"${ROOT}/templates/OVMF_VARS.fd"', stage)
        self.assertIn('"${ROOT}/templates/OVMF_CODE_4M.fd"', stage)

    def test_all_package_managers_enable_the_redirect_unit(self):
        scripts = {
            "Arch": ROOT / "packaging/arch/vhackintosh-control.install",
            "Debian": ROOT / "packaging/deb/postinst",
            "Fedora": ROOT / "packaging/fedora/vhackintosh-control.spec",
        }
        for distro, path in scripts.items():
            with self.subTest(distro=distro):
                self.assertIn("vhackintosh-http-redirect.service", path.read_text(encoding="utf-8"))

    def test_redirect_unit_is_hardened_and_can_bind_port_80_as_service_user(self):
        unit = ROOT / "packaging/systemd/vhackintosh-http-redirect.service"
        text = unit.read_text(encoding="utf-8")
        self.assertIn("User=vhackintosh-web", text)
        self.assertIn("Environment=VHACKINTOSH_HTTP_PORT=80", text)
        self.assertIn("Environment=VHACKINTOSH_WEB_PORT=8443", text)
        self.assertIn("AmbientCapabilities=CAP_NET_BIND_SERVICE", text)
        self.assertIn("CapabilityBoundingSet=CAP_NET_BIND_SERVICE", text)
        self.assertIn("NoNewPrivileges=yes", text)
        self.assertRegex(text, r"(?m)^ExecStart=/usr/bin/python3 -m web\.api\.http_redirect$")

    def test_fedora_package_owns_the_shared_tool_and_redirect_unit(self):
        spec = (ROOT / "packaging/fedora/vhackintosh-control.spec").read_text(encoding="utf-8")
        self.assertRegex(spec, r"(?m)^/opt/vhackintosh/resources/macserial$")
        self.assertRegex(spec, r"(?m)^/usr/lib/systemd/system/vhackintosh-http-redirect\.service$")

    def test_desktop_vm_unit_uses_the_logged_in_display_instead_of_seizing_a_vt(self):
        unit_path = ROOT / "packaging/systemd/vhackintosh-vm@.service"
        unit_text = unit_path.read_text(encoding="utf-8")
        unit = configparser.ConfigParser(strict=False, interpolation=None)
        unit.read_string(unit_text)

        self.assertIn("Environment=VHACKINTOSH_VM_DISPLAY_MODE=desktop", unit_text)
        self.assertNotIn("vhackintosh-display.service", unit["Unit"].get("Requires", ""))
        self.assertNotIn("vhackintosh-display.service", unit["Unit"].get("After", ""))
        self.assertNotIn("Environment=XDG_RUNTIME_DIR", unit_text)
        self.assertNotIn("Environment=WAYLAND_DISPLAY", unit_text)

        stage = (ROOT / "packaging/common/stage-app.sh").read_text(encoding="utf-8")
        self.assertIn('"${ROOT}/packaging/systemd/vhackintosh-vm@.service"', stage)


if __name__ == "__main__":
    unittest.main()
