from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from urllib.error import HTTPError
from pathlib import Path
from unittest.mock import patch

from core.web_activation import WebActivationStore
from web.api.app import (
    ActivationCode,
    SQLiteAuthStore,
    _ActivationPortalHTTPError,
    _activation_failure_label,
    _activation_portal_post,
    create_app,
)
from fastapi.testclient import TestClient


class WebActivationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "activation.json"
        self.store = WebActivationStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_generates_stable_high_entropy_code_and_private_state(self):
        first = self.store.get_or_create()
        second = self.store.get_or_create()
        self.assertEqual(first["appliance_id"], second["appliance_id"])
        self.assertEqual(first["activation_code"], second["activation_code"])
        self.assertRegex(first["activation_code"], r"^VH-(?:[A-F0-9]{4}-){5}[A-F0-9]{4}$")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_inventory_contains_only_cpu_model_memory_and_gpu_models(self):
        inventory = {
            "cpu_model": "Example CPU",
            "memory_total_bytes": 16 * 1024**3,
            "gpus": [{"vendor": "NVIDIA", "model": "RTX 3050"}],
            "reims_vgpu_repository": "felipeab10/reims-vgpu",
            "reims_vgpu_release": "v1.2.3",
        }
        saved = self.store.save_submission(inventory, portal_request_id="a" * 32)
        self.assertEqual(set(saved["inventory"]), {"cpu_model", "memory_total_bytes", "gpus", "reims_vgpu_repository", "reims_vgpu_release"})
        self.assertEqual(saved["inventory"]["gpus"], [{"vendor": "NVIDIA", "model": "RTX 3050"}])
        self.assertNotIn("hostname", json.dumps(saved))
        self.assertNotIn("architecture", json.dumps(saved))
        self.assertNotIn("logical_cpus", json.dumps(saved))
        self.assertNotIn("pci_bdf", json.dumps(saved))
        inventory["memory_total_bytes"] = 17 * 1024**4
        with self.assertRaises(ValueError):
            self.store.save_submission(inventory)
        inventory["memory_total_bytes"] = 16 * 1024**3
        inventory["hostname"] = "must-not-be-accepted"
        with self.assertRaises(ValueError):
            self.store.save_submission(inventory)

    def test_rejects_group_readable_state(self):
        self.store.get_or_create()
        os.chmod(self.path, 0o640)
        with self.assertRaises(PermissionError):
            self.store.get_or_create()

    def test_rotates_legacy_short_code_when_migrating(self):
        self.path.write_text(json.dumps({
            "schema_version": 1,
            "appliance_id": "a" * 32,
            "activation_code": "VH-1234-5678-9ABC",
            "status": "not_submitted",
            "created_at": "2026-10-31T00:00:00+00:00",
            "updated_at": "2026-10-31T00:00:00+00:00",
            "portal_request_id": None,
            "inventory": None,
        }), encoding="utf-8")
        os.chmod(self.path, 0o600)
        migrated = self.store.get_or_create()
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["appliance_id"], "a" * 32)
        self.assertNotEqual(migrated["activation_code"], "VH-1234-5678-9ABC")
        self.assertRegex(migrated["activation_code"], r"^VH-(?:[A-F0-9]{4}-){5}[A-F0-9]{4}$")


class WebActivationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WebActivationStore(root / "activation.json")
        self.agent = _FakeAgent()
        app = create_app(
            agent=self.agent,
            auth_store=SQLiteAuthStore(root / "auth.sqlite"),
            activation_code=ActivationCode("first-admin-code-long-enough"),
            cookie_secure=False,
            activation_store=self.store,
        )
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def _login(self):
        response = self.client.post(
            "/api/v1/session/bootstrap",
            headers={"Origin": "http://testserver"},
            json={"code": "first-admin-code-long-enough", "username": "admin", "password": "very long secure password"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {"Origin": "http://testserver", "X-CSRF-Token": self.client.cookies.get("vh_csrf")}

    def test_activation_code_is_only_available_after_admin_setup(self):
        self.assertEqual(self.client.get("/api/v1/activation").status_code, 401)
        self._login()
        response = self.client.get("/api/v1/activation")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertRegex(response.json()["activation_code"], r"^VH-(?:[A-F0-9]{4}-){5}[A-F0-9]{4}$")

    def test_activation_status_remains_available_when_agent_is_offline(self):
        csrf = self._login()
        self.agent.connected = False
        response = self.client.get("/api/v1/activation", headers=csrf)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "not_submitted")

    def test_submit_requires_csrf_and_sends_only_whitelisted_inventory(self):
        csrf = self._login()
        self.assertEqual(self.client.post("/api/v1/activation/submit").status_code, 403)
        portal_reply = {"requestId": "a" * 32, "status": "pending", "receivedAt": "2026-10-31T00:00:00Z"}
        with patch("core.reims_release.current_release", return_value="v1.2.3"), patch("web.api.app._activation_portal_post", return_value=portal_reply) as send:
            response = self.client.post("/api/v1/activation/submit", headers=csrf, json={})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "pending")
        args = send.call_args.args
        self.assertEqual(args[1], "/api/appliance/activations")
        self.assertEqual(set(args[2]["inventory"]), {"cpuModel", "memoryTotalBytes", "gpus", "reimsVgpuRepository", "reimsVgpuRelease"})
        self.assertEqual(args[2]["inventory"]["reimsVgpuRepository"], "felipeab10/reims-vgpu")
        self.assertEqual(args[2]["inventory"]["reimsVgpuRelease"], "v1.2.3")
        self.assertEqual(args[2]["inventory"]["cpuModel"], "Test CPU")
        self.assertEqual(args[2]["inventory"]["memoryTotalBytes"], 16 * 1024**3)
        self.assertNotIn("logicalCpus", json.dumps(args[2]))
        self.assertNotIn("hostname", json.dumps(args[2]))
        self.assertNotIn("architecture", json.dumps(args[2]))
        self.assertNotIn("addresses", json.dumps(args[2]))
        self.assertNotIn("serial", json.dumps(args[2]).lower())
        self.assertEqual(self.store.get_or_create()["status"], "pending")
        with patch("core.reims_release.current_release", return_value="v1.2.3"):
            unchanged = self.client.get("/api/v1/activation", headers=csrf)
        self.assertFalse(unchanged.json()["inventory_changed"])
        self.agent.memory_total_bytes += 1024**3
        with patch("core.reims_release.current_release", return_value="v1.2.3"):
            changed = self.client.get("/api/v1/activation", headers=csrf)
        self.assertTrue(changed.json()["inventory_changed"])
        with patch("core.reims_release.current_release", return_value="v1.2.3"), patch("web.api.app._activation_portal_post", return_value=portal_reply) as update:
            updated = self.client.post("/api/v1/activation/submit", headers=csrf, json={})
        self.assertEqual(updated.status_code, 202, updated.text)
        self.assertEqual(update.call_args.args[2]["inventory"]["memoryTotalBytes"], self.agent.memory_total_bytes)
        with patch("core.reims_release.current_release", return_value="v1.2.3"):
            self.assertFalse(self.client.get("/api/v1/activation", headers=csrf).json()["inventory_changed"])
        with patch("core.reims_release.current_release", return_value="v1.2.4"):
            self.assertTrue(self.client.get("/api/v1/activation", headers=csrf).json()["inventory_changed"])
        with patch("web.api.app._activation_portal_post", return_value={"status": "approved"}) as sync:
            refreshed = self.client.post("/api/v1/activation/sync", headers=csrf, json={})
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(refreshed.json()["status"], "approved")
        self.assertEqual(sync.call_args.args[1], "/api/appliance/activations/status")
        self.assertEqual(self.store.get_or_create()["status"], "approved")

    def test_portal_url_must_be_exact_https_site_origin(self):
        with self.assertRaises(ValueError):
            create_app(activation_portal_url="http://vhackintosh.proclin.com.br")
        with self.assertRaises(ValueError):
            create_app(activation_portal_url="https://127.0.0.1")

    def test_local_http_portal_requires_explicit_loopback_opt_in(self):
        with self.assertRaises(ValueError):
            create_app(activation_portal_url="http://127.0.0.1:3000")
        local = create_app(
            activation_portal_url="http://127.0.0.1:3000",
            allow_local_activation_portal=True,
        )
        self.assertEqual(local.state.activation_portal_url, "http://127.0.0.1:3000")
        for url in (
            "http://192.168.1.10:3000",
            "http://127.0.0.1:3000/path",
            "https://127.0.0.1:3000",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                create_app(activation_portal_url=url, allow_local_activation_portal=True)


class WebActivationPortalDiagnosticsTests(unittest.TestCase):
    def test_http_error_logs_status_without_remote_body(self):
        remote_body = b"private response details and activation code"
        http_error = HTTPError(
            "https://vhackintosh.proclin.com.br/api/appliance/activations",
            422,
            "Unprocessable Entity",
            hdrs=None,
            fp=io.BytesIO(remote_body),
        )
        opener = unittest.mock.Mock()
        opener.open.side_effect = http_error
        with patch("web.api.app.build_opener", return_value=opener):
            with self.assertRaises(_ActivationPortalHTTPError) as raised:
                _activation_portal_post(
                    "https://vhackintosh.proclin.com.br",
                    "/api/appliance/activations",
                    {"test": True},
                    client_version="0.1.0",
                )
        sent_request = opener.open.call_args.args[0]
        self.assertEqual(sent_request.get_header("User-agent"), "vHackintosh-Appliance/0.1.0")
        label = _activation_failure_label(raised.exception)
        self.assertEqual(label, "portal_http_status=422")
        self.assertNotIn("private", label)
        self.assertNotIn("activation code", label)


class _FakeAgent:
    connected = True

    def is_connected(self):
        return self.connected

    def host_overview(self):
        from web.api.contracts import HostOverview
        return HostOverview(
            hostname="private-host-label", architecture="x86_64", cpu_model="Test CPU",
            logical_cpus=8, memory_total_bytes=self.memory_total_bytes, memory_available_bytes=8 * 1024**3,
            uptime_seconds=20, addresses=["192.168.1.5"],
        )

    def __init__(self):
        self.memory_total_bytes = 16 * 1024**3

    def host_gpus(self):
        return []


if __name__ == "__main__":
    unittest.main()
