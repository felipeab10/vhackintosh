"""Local discovery for the appliance's web-management console."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import sqlite3
import socket
import ssl
import subprocess
from pathlib import Path


AUTH_DATABASE = Path("/var/lib/vhackintosh/web/auth.sqlite")


def lan_addresses(interface_addresses: dict | None = None) -> list[str]:
    """Return usable LAN/global addresses, excluding loopback and virtual links."""
    if interface_addresses is None:
        try:
            import psutil

            interface_addresses = psutil.net_if_addrs()
            interface_stats = psutil.net_if_stats()
        except (ImportError, OSError):
            return []
    else:
        interface_stats = None

    hidden_prefixes = ("lo", "docker", "podman", "virbr", "veth", "br-", "zt", "tailscale")
    found: set[str] = set()
    for interface, entries in interface_addresses.items():
        if interface.lower().startswith(hidden_prefixes):
            continue
        if interface_stats is not None:
            state = interface_stats.get(interface)
            if state is None or not state.isup:
                continue
        for entry in entries:
            if entry.family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            try:
                address = ipaddress.ip_address(entry.address.split("%", 1)[0])
            except ValueError:
                continue
            if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local:
                continue
            found.add(address.compressed)
    return sorted(found, key=lambda item: (":" in item, ipaddress.ip_address(item).packed))


def certificate_fingerprint(path: str | Path = "/var/lib/vhackintosh/web/tls/server.crt") -> str | None:
    """Return the local certificate's SHA-256 fingerprint in display form."""
    try:
        certificate = Path(path).read_text(encoding="ascii")
        der = ssl.PEM_cert_to_DER_cert(certificate)
    except (OSError, UnicodeError, ValueError):
        return None
    return ":".join(hashlib.sha256(der).hexdigest()[index : index + 2].upper() for index in range(0, 64, 2))


def service_is_active() -> bool | None:
    """Return the systemd service state, or None when systemd is unavailable."""
    if not Path("/run/systemd/system").exists():
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "--quiet", "vhackintosh-web.service"],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def administrator_is_activated(database: str | Path = AUTH_DATABASE) -> bool | None:
    """Return whether the local web account has been initialized."""
    path = Path(database)
    if not path.is_file():
        return False
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='administrator'"
            ).fetchone()
            if table is None:
                return False
            return connection.execute("SELECT 1 FROM administrator WHERE id=1").fetchone() is not None
        finally:
            connection.close()
    except sqlite3.Error:
        return None


def access_details(
    *,
    port: int | None = None,
    addresses: list[str] | None = None,
    fingerprint: str | None = None,
    auth_database: str | Path = AUTH_DATABASE,
) -> list[str]:
    """Build user-facing access lines for IPv4 and IPv6 LAN addresses."""
    if port is None:
        try:
            port = int(os.environ.get("VHACKINTOSH_WEB_PORT", "8443"))
        except ValueError:
            port = 8443
    if not 1 <= port <= 65535:
        port = 8443
    addresses = lan_addresses() if addresses is None else addresses
    if fingerprint is None:
        certificate = os.environ.get("VHACKINTOSH_WEB_CERT")
        if not certificate:
            tls_dir = Path(os.environ.get("VHACKINTOSH_WEB_TLS_DIR", "/var/lib/vhackintosh/web/tls"))
            certificate = str(tls_dir / "server.crt")
        fingerprint = certificate_fingerprint(certificate)
    active = service_is_active()
    activated = administrator_is_activated(auth_database)
    if activated is True:
        state = "conta configurada" if active else "conta configurada; serviço parado"
    elif activated is False:
        state = "configuração inicial pendente" if active else "serviço parado; configuração inicial pendente"
    else:
        state = "status da conta indisponível"
    lines = [f"Painel web: {'HTTPS ativo' if active else 'verifique o serviço'} · {state}"]
    if addresses:
        lines.extend(
            f"https://[{address}]:{port}" if ":" in address else f"https://{address}:{port}"
            for address in addresses
        )
    else:
        lines.append("Endereço LAN indisponível; verifique a conexão de rede.")
    if fingerprint:
        lines.append(f"Certificado SHA-256: {fingerprint}")
    if activated is False:
        lines.append("Primeiro acesso: execute vhackintosh-web-admin setup no console local do appliance.")
    return lines
