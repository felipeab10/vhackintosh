from __future__ import annotations

import hashlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ControlPackagePayloadTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
