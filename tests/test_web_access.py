from __future__ import annotations

import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.web_access import access_details, certificate_fingerprint, lan_addresses


class WebAccessTests(unittest.TestCase):
    def test_addresses_filter_internal_virtual_and_link_local_interfaces(self):
        interfaces = {
            "lo": [SimpleNamespace(family=socket.AF_INET, address="127.0.0.1")],
            "docker0": [SimpleNamespace(family=socket.AF_INET, address="172.17.0.1")],
            "enp1s0": [
                SimpleNamespace(family=socket.AF_INET, address="192.168.1.20"),
                SimpleNamespace(family=socket.AF_INET6, address="fe80::1%enp1s0"),
                SimpleNamespace(family=socket.AF_INET6, address="fd00::1234%enp1s0"),
            ],
        }
        self.assertEqual(lan_addresses(interfaces), ["192.168.1.20", "fd00::1234"])

    def test_access_details_formats_ipv6_and_reports_local_first_account_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "auth.sqlite"
            with mock.patch("core.web_access.service_is_active", return_value=True), \
                 mock.patch.dict("os.environ", {"VHACKINTOSH_WEB_PORT": "9443"}):
                lines = access_details(
                    addresses=["192.168.1.20", "fd00::1234"],
                    fingerprint="AA:BB",
                    auth_database=database,
                )
            self.assertEqual(lines[0], "Painel web: HTTPS ativo · configuração inicial pendente")
            self.assertIn("https://192.168.1.20:9443", lines)
            self.assertIn("https://[fd00::1234]:9443", lines)
            self.assertIn("Certificado SHA-256: AA:BB", lines)
            self.assertTrue(any("vhackintosh-web-admin setup" in line for line in lines))
            self.assertFalse(any("one-time-local-code" in line for line in lines))

            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE administrator (id INTEGER PRIMARY KEY)")
                connection.execute("INSERT INTO administrator (id) VALUES (1)")
            with mock.patch("core.web_access.service_is_active", return_value=True):
                activated = access_details(
                    addresses=[],
                    fingerprint=None,
                    auth_database=database,
                )
            self.assertEqual(activated[0], "Painel web: HTTPS ativo · conta configurada")
            self.assertFalse(any("Código de configuração" in line for line in activated))

    def test_first_access_never_displays_a_bootstrap_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch("core.web_access.service_is_active", return_value=True):
                lines = access_details(addresses=[], fingerprint=None, auth_database=root / "missing.sqlite")
            self.assertTrue(any("vhackintosh-web-admin setup" in line for line in lines))
            self.assertFalse(any("código" in line.lower() or "secret" in line.lower() for line in lines))

    def test_only_active_physical_or_lan_interfaces_are_reported(self):
        import psutil

        interfaces = {
            "enp1s0": [SimpleNamespace(family=socket.AF_INET, address="192.168.1.20")],
            "wlan0": [SimpleNamespace(family=socket.AF_INET, address="192.168.1.21")],
        }
        stats = {
            "enp1s0": SimpleNamespace(isup=True),
            "wlan0": SimpleNamespace(isup=False),
        }
        with mock.patch.object(psutil, "net_if_addrs", return_value=interfaces), \
             mock.patch.object(psutil, "net_if_stats", return_value=stats):
            self.assertEqual(lan_addresses(), ["192.168.1.20"])

    def test_fingerprint_rejects_an_invalid_certificate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.crt"
            path.write_text("not a PEM certificate", encoding="ascii")
            self.assertIsNone(certificate_fingerprint(path))


if __name__ == "__main__":
    unittest.main()
