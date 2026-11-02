from __future__ import annotations

import http.client
import threading
import unittest
from http.server import ThreadingHTTPServer

from web.api.http_redirect import _RedirectHandler, redirect_location


class HttpRedirectTests(unittest.TestCase):
    def test_redirect_keeps_path_query_and_uses_https_port(self):
        self.assertEqual(
            redirect_location("/vms/new?name=guest", "192.0.2.14", 9443),
            "https://192.0.2.14:9443/vms/new?name=guest",
        )

    def test_redirect_formats_ipv6_and_ipv4_mapped_addresses(self):
        self.assertEqual(
            redirect_location("/", "2001:db8::14", 8443),
            "https://[2001:db8::14]:8443/",
        )
        self.assertEqual(
            redirect_location("/", "::ffff:192.0.2.14", 8443),
            "https://192.0.2.14:8443/",
        )

    def test_rejects_absolute_and_protocol_relative_targets(self):
        for target in ("https://attacker.invalid/path", "//attacker.invalid/path", "/ok\r\nHost: attacker.invalid"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                redirect_location(target, "127.0.0.1")

    def test_http_response_uses_socket_local_address_not_host_header(self):
        handler = type("TestRedirectHandler", (_RedirectHandler,), {"https_port": 8443})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", "/?next=%2Fvms", headers={"Host": "attacker.invalid"})
            response = connection.getresponse()
            self.assertEqual(response.status, 308)
            self.assertEqual(response.getheader("Location"), "https://127.0.0.1:8443/?next=%2Fvms")
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
