"""Minimal HTTP listener that redirects requests to the local HTTPS panel."""

from __future__ import annotations

import ipaddress
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def redirect_location(request_target: str, local_address: str, https_port: int = 8443) -> str:
    """Build a same-host redirect without trusting the request's Host header."""
    if (
        not request_target.startswith("/")
        or request_target.startswith("//")
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in request_target)
    ):
        raise ValueError("only origin-form HTTP request targets are supported")
    if not 1 <= https_port <= 65535:
        raise ValueError("HTTPS port is outside the valid TCP port range")

    address = local_address.split("%", 1)[0]
    try:
        parsed_address = ipaddress.ip_address(address)
    except ValueError as exc:
        raise ValueError("the accepted connection has no valid local IP address") from exc
    if parsed_address.is_unspecified or parsed_address.is_multicast:
        raise ValueError("the accepted connection has no concrete local IP address")
    if isinstance(parsed_address, ipaddress.IPv6Address) and parsed_address.ipv4_mapped:
        parsed_address = parsed_address.ipv4_mapped

    host = f"[{parsed_address.compressed}]" if parsed_address.version == 6 else parsed_address.compressed
    return f"https://{host}:{https_port}{request_target}"


class _RedirectHandler(BaseHTTPRequestHandler):
    https_port = 8443
    server_version = "vHackintosh-Redirect"
    sys_version = ""

    def _redirect(self) -> None:
        try:
            local_address = self.connection.getsockname()[0]
            location = redirect_location(self.path, local_address, self.https_port)
        except (IndexError, TypeError, ValueError):
            self.send_error(400, "Invalid redirect request")
            return

        self.send_response(308)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _redirect
    do_HEAD = _redirect
    do_POST = _redirect
    do_PUT = _redirect
    do_PATCH = _redirect
    do_DELETE = _redirect
    do_OPTIONS = _redirect

    def log_message(self, format: str, *args: object) -> None:
        # Avoid logging credentials or user-supplied paths from HTTP requests.
        return


def main() -> int:
    host = os.environ.get("VHACKINTOSH_HTTP_HOST", "0.0.0.0")
    http_port = int(os.environ.get("VHACKINTOSH_HTTP_PORT", "80"))
    https_port = int(os.environ.get("VHACKINTOSH_WEB_PORT", "8443"))
    if not 1 <= https_port <= 65535:
        raise SystemExit("VHACKINTOSH_WEB_PORT is outside the valid TCP port range")

    handler = type("ConfiguredRedirectHandler", (_RedirectHandler,), {"https_port": https_port})
    server = ThreadingHTTPServer((host, http_port), handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
