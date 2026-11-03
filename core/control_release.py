"""Query and install published vHackintosh Control releases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

CONTROL_REPO = "felipeab10/vhackintosh"
RELEASES_API = f"https://api.github.com/repos/{CONTROL_REPO}/releases"
VERSION_FILE = Path("/usr/lib/vhackintosh/control-version")
API_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 900


class ControlReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ControlRelease:
    tag: str
    name: str
    published_at: str
    prerelease: bool
    asset_name: str
    asset_url: str
    asset_size: int
    checksum_url: str


@dataclass(frozen=True)
class ControlUpdateResult:
    tag: str
    previous: str
    changed: bool
    package_name: str


def package_kind(os_release: dict[str, str] | None = None) -> str:
    """Resolve the native package format supported by this host."""
    values = _os_release() if os_release is None else os_release
    distro_id = values.get("ID", "").lower()
    like = values.get("ID_LIKE", "").lower().split()
    if distro_id in {"arch", "cachyos", "manjaro", "endeavouros"} or "arch" in like:
        return "arch"
    if distro_id in {"ubuntu", "debian", "linuxmint", "pop"} or {"ubuntu", "debian"} & set(like):
        return "deb"
    if distro_id in {"fedora", "rhel", "rocky", "almalinux", "centos"} or {"fedora", "rhel"} & set(like):
        return "rpm"
    raise ControlReleaseError("A distribuição Linux não tem um pacote Control compatível.")


def _os_release() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key and re.fullmatch(r"[A-Z0-9_]+", key):
                result[key] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return result


def _asset_for(release: dict, kind: str) -> dict | None:
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None
    pattern = {
        "arch": re.compile(r"^vhackintosh-control-[0-9A-Za-z.+_~-]+-1-x86_64\.pkg\.tar\.zst$"),
        "deb": re.compile(r"^vhackintosh-control_[0-9A-Za-z.+_~-]+_amd64\.deb$"),
        "rpm": re.compile(r"^vhackintosh-control-[0-9A-Za-z.+_~-]+-1\.[^.]+\.x86_64\.rpm$"),
    }[kind]
    for asset in assets:
        if isinstance(asset, dict) and pattern.fullmatch(str(asset.get("name", ""))):
            return asset
    return None


def list_releases(*, kind: str | None = None, os_release: dict[str, str] | None = None,
                  include_prerelease: bool = True) -> list[ControlRelease]:
    kind = kind or package_kind(os_release)
    runner = shutil.which("curl") or shutil.which("wget")
    if not runner:
        raise ControlReleaseError("É necessário curl ou wget para consultar as releases.")
    releases: list[ControlRelease] = []
    page = 1
    while True:
        url = f"{RELEASES_API}?per_page=100&page={page}"
        command = ([runner, "-fsSL", "-H", "Accept: application/vnd.github+json", url]
                   if Path(runner).name == "curl" else
                   [runner, "-qO-", "--header=Accept: application/vnd.github+json", url])
        try:
            response = subprocess.run(command, capture_output=True, text=True,
                                      check=False, timeout=API_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ControlReleaseError(f"Não foi possível consultar as releases: {exc}") from exc
        if response.returncode:
            raise ControlReleaseError("A API do GitHub não respondeu ao consultar as releases do Control.")
        try:
            data = json.loads(response.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise ControlReleaseError("A API do GitHub retornou dados inválidos para as releases do Control.") from exc
        if not isinstance(data, list):
            raise ControlReleaseError("A API do GitHub retornou uma lista de releases inválida.")
        for item in data:
            if not isinstance(item, dict) or item.get("draft"):
                continue
            tag = item.get("tag_name")
            if not isinstance(tag, str) or not tag.startswith("control-v"):
                continue
            prerelease = bool(item.get("prerelease"))
            if prerelease and not include_prerelease:
                continue
            asset = _asset_for(item, kind)
            checksum = next((candidate for candidate in item.get("assets", [])
                             if isinstance(candidate, dict) and candidate.get("name") == "SHA256SUMS"), None)
            if not asset or not checksum:
                continue
            releases.append(ControlRelease(
                tag=tag,
                name=str(item.get("name") or tag),
                published_at=str(item.get("published_at") or ""),
                prerelease=prerelease,
                asset_name=str(asset["name"]),
                asset_url=str(asset.get("browser_download_url") or ""),
                asset_size=int(asset.get("size") or 0),
                checksum_url=str(checksum.get("browser_download_url") or ""),
            ))
        if len(data) < 100:
            break
        page += 1
    return releases


def current_version(*, package: str | None = None, version_file: Path = VERSION_FILE) -> str:
    if package:
        return package
    try:
        value = version_file.read_text(encoding="utf-8").strip()
        return value or "desconhecida"
    except OSError:
        pass
    for command in (("pacman", "-Q", "vhackintosh-control"),
                    ("dpkg-query", "-W", "-f=${Version}", "vhackintosh-control"),
                    ("rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", "vhackintosh-control")):
        if not shutil.which(command[0]):
            continue
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split()[-1]
    return "desconhecida"


def is_current_release(tag: str, installed: str) -> bool:
    if tag.startswith("control-v") and installed.startswith("control-v"):
        return tag == installed
    version = installed.strip()
    # Arch: 1.0.0.beta.11-1; Fedora: 1.0.0.beta.11-1.fc44.
    version = re.sub(r"-1(?:\.[A-Za-z0-9_]+)?$", "", version)
    version = version.replace(".beta.", "-beta.")
    return tag == f"control-v{version}"


def _download(url: str, destination: Path) -> None:
    runner = shutil.which("curl") or shutil.which("wget")
    if not runner:
        raise ControlReleaseError("É necessário curl ou wget para baixar a release.")
    command = ([runner, "-fL", "--retry", "2", "--connect-timeout", "20", "-o", str(destination), url]
               if Path(runner).name == "curl" else
               [runner, "-q", "--tries=3", "-O", str(destination), url])
    if Path(runner).name == "wget":
        command = [runner, "-q", "--tries=3", "-O", str(destination), url]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False,
                                timeout=DOWNLOAD_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControlReleaseError(f"Falha ao baixar o pacote do Control: {exc}") from exc
    if result.returncode or not destination.is_file() or destination.stat().st_size == 0:
        raise ControlReleaseError("O download do pacote do Control falhou ou veio vazio.")


def _verify_checksum(package: Path, sums: Path, package_name: str) -> None:
    expected = None
    for line in sums.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
        if match and Path(match.group(2)).name == package_name:
            expected = match.group(1).lower()
            break
    if not expected:
        raise ControlReleaseError("O checksum SHA-256 do pacote não foi publicado junto da release.")
    digest = hashlib.sha256()
    with package.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ControlReleaseError("O checksum do pacote não confere; a instalação foi cancelada.")


def _install_command(kind: str, package: Path) -> list[str]:
    if kind == "arch" and shutil.which("pacman"):
        # Appliance ISOs have shipped the shared package payload before the
        # package database was registered. These narrowly scoped paths allow
        # the first self-update to adopt those exact package-owned files.
        return ["pacman", "-U", "--noconfirm",
                "--overwrite", "opt/vhackintosh/*",
                "--overwrite", "usr/lib/systemd/system/vhackintosh-*",
                "--overwrite", "usr/lib/sysusers.d/vhackintosh-web.conf",
                "--overwrite", "usr/lib/tmpfiles.d/vhackintosh-web.conf",
                "--overwrite", "usr/lib/vhackintosh/control-version",
                "--overwrite", "usr/local/bin/vhackintosh-*",
                "--overwrite", "etc/vhackintosh/sway.conf", str(package)]
    if kind == "deb" and shutil.which("apt-get"):
        return ["apt-get", "install", "-y", str(package)]
    if kind == "rpm" and shutil.which("dnf"):
        return ["dnf", "install", "-y", str(package)]
    raise ControlReleaseError("O gerenciador de pacotes deste Linux não está disponível.")


def install_release(release: ControlRelease, *, kind: str | None = None,
                    log: Callable[[str], None] | None = None,
                    schedule_restart: bool = False) -> ControlUpdateResult:
    """Verify and install the native release package, then restart Control."""
    if os.geteuid() != 0:
        raise ControlReleaseError("A atualização do Control exige privilégios de administrador.")
    kind = kind or package_kind()
    previous = current_version()
    logger = log or (lambda _message: None)
    with tempfile.TemporaryDirectory(prefix="vhackintosh-control-update-") as temporary:
        root = Path(temporary)
        package = root / release.asset_name
        checksums = root / "SHA256SUMS"
        logger(f"Baixando {release.asset_name} de {release.tag}")
        _download(release.asset_url, package)
        _download(release.checksum_url, checksums)
        _verify_checksum(package, checksums, release.asset_name)
        logger("Checksum SHA-256 validado")
        environment = os.environ.copy()
        environment["VHACKINTOSH_CONTROL_UPDATE_DEFER_RESTART"] = "1"
        command = _install_command(kind, package)
        logger("Instalando o pacote nativo do Control")
        try:
            if kind in {"arch", "deb", "rpm"}:
                from core.update_guard import MaintenanceBlocked, maintenance_window

                try:
                    with maintenance_window():
                        result = subprocess.run(command, capture_output=True, text=True, check=False,
                                                timeout=DOWNLOAD_TIMEOUT, env=environment)
                except MaintenanceBlocked as exc:
                    raise ControlReleaseError(str(exc)) from exc
            else:
                result = subprocess.run(command, capture_output=True, text=True, check=False,
                                        timeout=DOWNLOAD_TIMEOUT, env=environment)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ControlReleaseError(f"O gerenciador de pacotes não concluiu a atualização: {exc}") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[-1000:]
            raise ControlReleaseError(detail or "O gerenciador de pacotes recusou a atualização.")
    if schedule_restart:
        _schedule_restart()
    return ControlUpdateResult(tag=release.tag, previous=previous,
                               changed=previous != release.tag, package_name=release.asset_name)


def _schedule_restart() -> None:
    systemd_run = shutil.which("systemd-run")
    if not systemd_run:
        raise ControlReleaseError("O pacote foi instalado, mas systemd-run não está disponível para reiniciar os serviços.")
    command = [systemd_run, "--quiet", "--collect", "--unit=vhackintosh-control-restart",
               "--on-active=5s", "/usr/bin/systemctl", "restart",
               "vhackintosh-agent.service", "vhackintosh-web.service"]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=15)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[:512]
        raise ControlReleaseError(detail or "Não foi possível agendar o reinício dos serviços Control.")
