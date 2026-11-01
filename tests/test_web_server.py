"""Local HTTPS material and SPA delivery tests."""

import tempfile
import subprocess
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from web.api.server import _load_bootstrap_code, create_server_app, ensure_tls_material


class WebServerTests(unittest.TestCase):
    def test_bootstrap_code_stays_private_and_available_for_local_tui_until_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            code_file = Path(directory) / "bootstrap-code"
            code_file.write_text("long-one-time-activation-code", encoding="ascii")
            code_file.chmod(0o600)
            activation = _load_bootstrap_code(code_file)
            self.assertTrue(code_file.is_file())
            self.assertTrue(activation.consume("long-one-time-activation-code"))
            self.assertTrue(code_file.is_file(), "the API route removes the display copy after admin creation")

    def test_successful_bootstrap_removes_local_tui_code_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code_file = root / "bootstrap-code"
            code_file.write_text("long-one-time-activation-code", encoding="ascii")
            code_file.chmod(0o600)
            app = create_server_app(
                rpc_socket=root / "missing-agent.sock",
                auth_db=root / "auth.sqlite",
                bootstrap_file=code_file,
                frontend_dist=root / "missing-dist",
            )
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/session/bootstrap",
                    headers={"Origin": "http://testserver"},
                    json={
                        "code": "long-one-time-activation-code",
                        "username": "admin",
                        "password": "a sufficiently long password",
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(code_file.exists())

    def test_tls_identity_is_created_once_with_private_key_and_stable_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            material = ensure_tls_material(directory)
            second = ensure_tls_material(directory)
            self.assertEqual(material.fingerprint_sha256, second.fingerprint_sha256)
            self.assertEqual(len(material.fingerprint_sha256), 64)
            self.assertEqual(material.private_key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(material.certificate.stat().st_mode & 0o777, 0o644)
            san = subprocess.run(
                ["/usr/bin/openssl", "x509", "-in", str(material.certificate), "-noout", "-ext", "subjectAltName"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("DNS:vhackintosh.local", san)
            self.assertIn("IP Address:127.0.0.1", san)

    def test_spa_fallback_serves_direct_routes_and_keeps_api_namespace_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dist = root / "dist"
            (dist / "assets").mkdir(parents=True)
            (dist / "index.html").write_text("<main>Control HUD</main>")
            (dist / "assets" / "app.js").write_text("export {};")
            app = create_server_app(
                rpc_socket=root / "missing-agent.sock",
                auth_db=root / "auth.sqlite",
                bootstrap_file=root / "missing-code",
                frontend_dist=dist,
            )
            with TestClient(app) as client:
                self.assertIn("Control HUD", client.get("/").text)
                self.assertIn("Control HUD", client.get("/vms/vm-1/gpu").text)
                self.assertEqual(client.get("/assets/app.js").text, "export {};")
                self.assertEqual(client.get("/api/unknown").status_code, 404)


if __name__ == "__main__":
    unittest.main()
