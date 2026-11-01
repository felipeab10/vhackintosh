"""Single-worker HTTPS server entry point for the appliance Control API."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import os
import re
import socket
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import uvicorn
from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core.web_rpc import UnixRpcClient

from .agent import RpcAgentClient
from .app import ActivationCode, create_app


@dataclass(frozen=True)
class TlsMaterial:
    certificate: Path
    private_key: Path
    fingerprint_sha256: str


def _tls_subject_alt_names() -> tuple[str, ...]:
    names = {"DNS:localhost", "DNS:vhackintosh.local", "IP:127.0.0.1", "IP:::1"}
    hostname = socket.gethostname()
    if re.fullmatch(r"[A-Za-z0-9.-]{1,253}", hostname):
        names.add(f"DNS:{hostname}")
    try:
        import psutil

        for entries in psutil.net_if_addrs().values():
            for entry in entries:
                if entry.family not in {socket.AF_INET, socket.AF_INET6}:
                    continue
                address = entry.address.split("%", 1)[0]
                try:
                    parsed = ipaddress.ip_address(address)
                except ValueError:
                    continue
                if not parsed.is_unspecified and not parsed.is_multicast and not parsed.is_link_local:
                    names.add(f"IP:{parsed.compressed}")
    except (ImportError, OSError):
        pass
    return tuple(sorted(names))


def ensure_tls_material(
    directory: str | os.PathLike[str],
    *,
    certificate: str | os.PathLike[str] | None = None,
    private_key: str | os.PathLike[str] | None = None,
    openssl: str = "/usr/bin/openssl",
) -> TlsMaterial:
    """Create one appliance-local TLS identity or validate configured files."""
    cert_path = Path(certificate) if certificate else Path(directory) / "server.crt"
    key_path = Path(private_key) if private_key else Path(directory) / "server.key"
    if bool(certificate) != bool(private_key):
        raise ValueError("certificate and private key must be configured together")
    if certificate is None:
        parent = cert_path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            parent.chmod(0o700)
        except OSError:
            pass
        lock_path = parent / ".tls.lock"
        names_path = parent / ".tls-san-names"
        subject_alt_names = _tls_subject_alt_names()
        names_value = "\n".join(subject_alt_names)
        with open(lock_path, "a+b") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                previous_names = names_path.read_text(encoding="ascii")
            except OSError:
                previous_names = ""
            if not cert_path.is_file() or not key_path.is_file() or previous_names != names_value:
                cert_fd, cert_tmp = tempfile.mkstemp(prefix=".server-cert.", dir=parent)
                key_fd, key_tmp = tempfile.mkstemp(prefix=".server-key.", dir=parent)
                names_fd, names_tmp = tempfile.mkstemp(prefix=".server-san.", dir=parent)
                os.close(cert_fd)
                os.close(key_fd)
                os.close(names_fd)
                try:
                    os.chmod(key_tmp, 0o600)
                    os.chmod(names_tmp, 0o600)
                    Path(names_tmp).write_text(names_value, encoding="ascii")
                    result = subprocess.run(
                        [
                            openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
                            "-days", "3650", "-subj", "/CN=vHackintosh Appliance",
                            "-addext", f"subjectAltName={','.join(subject_alt_names)}",
                            "-keyout", key_tmp, "-out", cert_tmp,
                        ],
                        check=False,
                        capture_output=True,
                        timeout=30,
                        env={"PATH": "/usr/bin:/bin", "HOME": "/", "LANG": "C", "LC_ALL": "C"},
                    )
                    if result.returncode != 0:
                        raise RuntimeError("OpenSSL não conseguiu gerar a identidade TLS local")
                    os.chmod(cert_tmp, 0o644)
                    os.replace(key_tmp, key_path)
                    os.replace(cert_tmp, cert_path)
                    os.replace(names_tmp, names_path)
                finally:
                    Path(cert_tmp).unlink(missing_ok=True)
                    Path(key_tmp).unlink(missing_ok=True)
                    Path(names_tmp).unlink(missing_ok=True)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    if not cert_path.is_file() or not key_path.is_file():
        raise FileNotFoundError("certificado TLS e chave privada não foram encontrados")
    if cert_path.stat().st_size > 1_000_000 or key_path.stat().st_size > 1_000_000:
        raise ValueError("arquivo TLS excede o limite seguro de 1 MiB")
    if key_path.stat().st_mode & 0o077:
        raise PermissionError("chave TLS deve ser legível somente por seu proprietário")
    cert_pem = cert_path.read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(cert_pem)
    return TlsMaterial(
        certificate=cert_path,
        private_key=key_path,
        fingerprint_sha256=hashlib.sha256(der).hexdigest().upper(),
    )


def _load_bootstrap_code(path: Path) -> ActivationCode:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ActivationCode()
    if not path.is_file() or info.st_mode & 0o077 or info.st_size > 256:
        raise PermissionError("arquivo de ativação local precisa ser regular, privado e limitado")
    code = path.read_text(encoding="utf-8").strip()
    return ActivationCode(code) if code else ActivationCode()


def create_server_app(
    *,
    rpc_socket: str | os.PathLike[str] = "/run/vhackintosh-agent/agent.sock",
    auth_db: str | os.PathLike[str] = "/var/lib/vhackintosh/web/auth.sqlite",
    bootstrap_file: str | os.PathLike[str] = "/run/vhackintosh-web/bootstrap-code",
    frontend_dist: str | os.PathLike[str] = "/opt/vhackintosh/web/current/web/frontend/dist",
    rpc_timeout: float = 3.0,
    web_port: int = 8443,
    tls_fingerprint: str | None = None,
    transfer_directory: str | os.PathLike[str] | None = None,
):
    """Build the HTTPS app with one typed agent client and optional SPA files."""
    app = create_app(
        agent=RpcAgentClient(UnixRpcClient(rpc_socket, timeout=rpc_timeout)),
        activation_code=_load_bootstrap_code(Path(bootstrap_file)),
        auth_db_path=auth_db,
        transfer_directory=transfer_directory,
        web_port=web_port,
        tls_fingerprint=tls_fingerprint,
    )
    app.state.bootstrap_code_file = Path(bootstrap_file)
    dist = Path(frontend_dist)
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        file = dist / "index.html"
        if not file.is_file():
            raise HTTPException(status_code=503, detail="Painel web ainda não foi empacotado")
        return FileResponse(file)

    @app.get("/{asset_path:path}", include_in_schema=False)
    async def spa_fallback(asset_path: str) -> FileResponse:
        if asset_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        root = dist.resolve()
        candidate = (dist / asset_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise HTTPException(status_code=404, detail="Not found")
        if candidate.is_file():
            return FileResponse(candidate)
        index_file = dist / "index.html"
        if not index_file.is_file():
            raise HTTPException(status_code=503, detail="Painel web ainda não foi empacotado")
        return FileResponse(index_file)

    return app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="vHackintosh local HTTPS management service")
    parser.add_argument("--host", default=os.environ.get("VHACKINTOSH_WEB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("VHACKINTOSH_WEB_PORT", "8443")))
    parser.add_argument("--tls-dir", default=os.environ.get("VHACKINTOSH_WEB_TLS_DIR", "/var/lib/vhackintosh/web/tls"))
    parser.add_argument("--cert", default=os.environ.get("VHACKINTOSH_WEB_CERT"))
    parser.add_argument("--key", default=os.environ.get("VHACKINTOSH_WEB_KEY"))
    parser.add_argument("--rpc-socket", default=os.environ.get("VHACKINTOSH_AGENT_SOCKET", "/run/vhackintosh-agent/agent.sock"))
    parser.add_argument("--auth-db", default=os.environ.get("VHACKINTOSH_WEB_AUTH_DB", "/var/lib/vhackintosh/web/auth.sqlite"))
    parser.add_argument("--bootstrap-file", default=os.environ.get("VHACKINTOSH_WEB_BOOTSTRAP_FILE", "/run/vhackintosh-web/bootstrap-code"))
    parser.add_argument("--frontend-dist", default=os.environ.get("VHACKINTOSH_WEB_FRONTEND_DIST", "/opt/vhackintosh/web/current/web/frontend/dist"))
    parser.add_argument("--rpc-timeout", type=float, default=float(os.environ.get("VHACKINTOSH_WEB_RPC_TIMEOUT", "3")))
    parser.add_argument("--transfer-directory", default=os.environ.get("VHACKINTOSH_WEB_TRANSFER_DIR", "/var/lib/vhackintosh/web/transfers"))
    args = parser.parse_args(argv)
    tls = ensure_tls_material(args.tls_dir, certificate=args.cert, private_key=args.key)
    app = create_server_app(
        rpc_socket=args.rpc_socket,
        auth_db=args.auth_db,
        bootstrap_file=args.bootstrap_file,
        frontend_dist=args.frontend_dist,
        rpc_timeout=args.rpc_timeout,
        transfer_directory=args.transfer_directory,
        web_port=args.port,
        tls_fingerprint=tls.fingerprint_sha256,
    )
    print(f"TLS-SHA256={tls.fingerprint_sha256}", flush=True)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        ssl_certfile=str(tls.certificate),
        ssl_keyfile=str(tls.private_key),
        workers=1,
        access_log=False,
        server_header=False,
        timeout_graceful_shutdown=5,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
