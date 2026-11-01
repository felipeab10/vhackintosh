"""Read-only discovery and reviewed import of VMs already registered in Reims."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import struct
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import VMConfig, SMBIOSConfig
from core.qemu_args import validate_qemu_extra_args


class ReimsImportError(ValueError):
    """A Reims configuration cannot be imported without losing safety/data."""


_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_MAC = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
_MAX_TOML_BYTES = 2 * 1024 * 1024
_GIB = 1024**3


def _regular_file(path: Path, *, label: str, read_bytes: bool = False) -> bytes | None:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ReimsImportError(f"{label} não está acessível: {path}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ReimsImportError(f"{label} precisa ser um arquivo regular, sem link simbólico: {path}")
    if not read_bytes:
        return None
    if before.st_size > _MAX_TOML_BYTES:
        raise ReimsImportError(f"{label} ultrapassa o limite de leitura segura.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReimsImportError(f"{label} mudou durante a leitura.") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ReimsImportError(f"{label} mudou durante a leitura.")
        chunks = bytearray()
        while len(chunks) <= _MAX_TOML_BYTES:
            chunk = os.read(fd, min(65536, _MAX_TOML_BYTES + 1 - len(chunks)))
            if not chunk:
                return bytes(chunks)
            chunks.extend(chunk)
        raise ReimsImportError(f"{label} ultrapassa o limite de leitura segura.")
    finally:
        os.close(fd)


def _resolve_config_path(directory: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ReimsImportError(f"O caminho de {label} está ausente ou inválido.")
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else directory / raw
    try:
        lexical = Path(os.path.abspath(candidate))
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ReimsImportError(f"{label} não foi encontrado: {candidate}") from exc
    if lexical != resolved:
        raise ReimsImportError(f"{label} contém link simbólico ou caminho ambíguo: {candidate}")
    return resolved


def _disk_size_gib(path: Path, image_format: Any) -> int:
    if image_format not in {"raw", "qcow2"}:
        raise ReimsImportError("O formato do disco ainda não pode ser revisado pelo painel.")
    try:
        before = path.lstat()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReimsImportError("O disco não pôde ser aberto com segurança para revisão.") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ReimsImportError("O disco mudou durante a revisão.")
        if image_format == "raw":
            size = opened.st_size
        else:
            header = os.pread(fd, 32, 0)
            if len(header) != 32 or header[:4] != b"QFI\xfb" or struct.unpack(">I", header[4:8])[0] not in {2, 3}:
                raise ReimsImportError("O arquivo não contém um cabeçalho QCOW2 v2/v3 reconhecido.")
            size = struct.unpack(">Q", header[24:32])[0]
        if size <= 0:
            raise ReimsImportError("O disco declara tamanho virtual inválido.")
        return (size + _GIB - 1) // _GIB
    finally:
        os.close(fd)


def _inspect_entry(entry: dict[str, Any], registered_ids: set[str]) -> dict[str, Any]:
    name = entry.get("name")
    directory_value = entry.get("dir")
    if not isinstance(name, str) or not _ID.fullmatch(name):
        candidate_id = "reims-" + hashlib.sha256(
            f"{name!r}\0{directory_value!r}".encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        return {
            "id": candidate_id, "name": str(name or "VM Reims")[:80],
            "directory": str(directory_value or "")[:1024], "disk_path": "",
            "vcpus": 0, "ram_gb": 0, "disk_size_gb": 0, "mac": None,
            "display_resolution": "1920x1080", "display_hidpi": False,
            "importable": False,
            "limitation": "O nome registrado no Reims não pode ser usado como identificador seguro do catálogo.",
        }
    directory = Path(directory_value).expanduser() if isinstance(directory_value, str) else Path(".")
    base = {
        "id": name,
        "name": name,
        "directory": str(directory)[:2048],
        "disk_path": "",
        "vcpus": 0,
        "ram_gb": 0,
        "disk_size_gb": 0,
        "mac": None,
        "display_resolution": "1920x1080",
        "display_hidpi": False,
        "importable": False,
        "limitation": None,
    }
    try:
        if name in registered_ids:
            raise ReimsImportError("Esta VM já está no catálogo do vHackintosh.")
        if not directory.is_absolute():
            raise ReimsImportError("O diretório registrado é relativo; revise-o pelo Reims antes de importar.")
        directory_stat = directory.lstat()
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise ReimsImportError("O diretório da VM não é uma pasta real ou é um link simbólico.")
        resolved_directory = directory.resolve(strict=True)
        if resolved_directory != Path(os.path.abspath(directory)):
            raise ReimsImportError("O diretório da VM contém link simbólico; a importação foi bloqueada.")
        config_path = resolved_directory / "vm.toml"
        raw = _regular_file(config_path, label="vm.toml", read_bytes=True)
        try:
            config = tomllib.loads((raw or b"").decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ReimsImportError("vm.toml está inválido.") from exc
        if not isinstance(config, dict) or config.get("name") != name:
            raise ReimsImportError("O nome de vm.toml não corresponde ao registro Reims.")
        if config.get("profile") != "q35-x86_64":
            raise ReimsImportError("Somente VMs q35 x86-64 podem ser importadas pelo painel.")
        cpus, memory = config.get("cpus"), config.get("memory_mib")
        if isinstance(cpus, bool) or not isinstance(cpus, int) or not 1 <= cpus <= 256:
            raise ReimsImportError("A quantidade de vCPUs em vm.toml é inválida.")
        if isinstance(memory, bool) or not isinstance(memory, int) or not 1024 <= memory <= 2 * 1024 * 1024:
            raise ReimsImportError("A memória em vm.toml é inválida.")
        q35 = config.get("q35")
        if not isinstance(q35, dict):
            raise ReimsImportError("A configuração q35/EFI não está presente.")
        disk = _resolve_config_path(resolved_directory, config.get("disk"), label="disco")
        _regular_file(disk, label="Disco da VM")
        for field, label in (("opencore", "OpenCore"), ("firmware_code", "firmware OVMF"), ("firmware_vars", "variáveis OVMF")):
            configured = q35.get(field)
            if not configured:
                raise ReimsImportError(f"O caminho de {label} não está configurado em vm.toml.")
            artifact = _resolve_config_path(resolved_directory, configured, label=label)
            _regular_file(artifact, label=label)
        args = config.get("extra_args", [])
        if not isinstance(args, list) or any(not isinstance(token, str) for token in args):
            raise ReimsImportError("A lista extra_args de vm.toml é inválida.")
        launch_args = list(args)
        if launch_args[:2] == ["-action", "reboot=shutdown"]:
            launch_args = launch_args[2:]
        try:
            validate_qemu_extra_args(launch_args)
        except ValueError as exc:
            raise ReimsImportError("Há argumentos QEMU que o painel ainda não consegue validar e preservar.") from exc
        network = config.get("network") or {}
        mac = network.get("mac") if isinstance(network, dict) else None
        if mac is not None and (not isinstance(mac, str) or not _MAC.fullmatch(mac)):
            raise ReimsImportError("O MAC configurado no Reims é inválido.")
        display = config.get("display") or {}
        if not isinstance(display, dict):
            raise ReimsImportError("A seção display de vm.toml é inválida.")
        count = display.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 8:
            raise ReimsImportError("A quantidade de displays em vm.toml é inválida.")
        disk_size = _disk_size_gib(disk, config.get("disk_format", "qcow2"))
        if disk_size > 4096:
            raise ReimsImportError("O disco excede o limite de 4096 GiB do catálogo atual.")
        resolution = display.get("resolution", "1920x1080")
        if not isinstance(resolution, str) or len(resolution) > 32:
            raise ReimsImportError("A resolução em vm.toml é inválida.")
        hidpi = display.get("hidpi", False)
        refresh = display.get("refresh")
        if not isinstance(hidpi, bool):
            raise ReimsImportError("A opção HiDPI em vm.toml é inválida.")
        if refresh is not None and (isinstance(refresh, bool) or not isinstance(refresh, int) or not 1 <= refresh <= 1000):
            raise ReimsImportError("A taxa de atualização em vm.toml é inválida.")
        try:
            from core.display import validate_display_options
            validate_display_options(resolution, hidpi, refresh)
        except ValueError as exc:
            raise ReimsImportError("A resolução ou as opções de tela ainda não são compatíveis com o painel.") from exc
        cpu_model = q35.get("cpu_model", "Skylake-Client-noTSX-IBRS")
        if not isinstance(cpu_model, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,100}", cpu_model):
            raise ReimsImportError("O modelo de CPU em vm.toml é inválido.")
        base.update({
            "name": str(config.get("name", name))[:80],
            "directory": str(resolved_directory),
            "disk_path": str(disk),
            "vcpus": cpus,
            "ram_gb": max(1, (memory + 1023) // 1024),
            "disk_size_gb": max(1, disk_size),
            "mac": mac,
            "display_resolution": resolution,
            "display_hidpi": hidpi,
            "importable": True,
        })
    except ReimsImportError as exc:
        base["limitation"] = str(exc)[:512]
    except (OSError, TypeError, ValueError) as exc:
        base["limitation"] = f"Não foi possível validar a VM registrada ({type(exc).__name__})."
    return base


def list_import_candidates(
    registry_path: Path,
    registered_ids: set[str],
    running_vms: dict[str, int] | None = None,
    *,
    runtime_observed: bool = False,
) -> list[dict[str, Any]]:
    if registry_path.is_symlink():
        raise ReimsImportError("O registro Reims é um link simbólico e não pode ser importado.")
    if not registry_path.exists():
        return []
    raw = _regular_file(registry_path, label="Registro Reims", read_bytes=True)
    try:
        registry = tomllib.loads((raw or b"").decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ReimsImportError("O registro Reims está inválido; nenhuma VM foi alterada.") from exc
    entries = registry.get("vm", []) if isinstance(registry, dict) else None
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ReimsImportError("A lista de VMs do registro Reims está inválida.")
    candidates = [_inspect_entry(entry, registered_ids) for entry in entries]
    if runtime_observed:
        for candidate in candidates:
            pid = running_vms.get(candidate["id"]) if running_vms is not None else None
            candidate["runtime_state"] = "running" if pid else "stopped" if running_vms is not None else "unknown"
            candidate["runtime_pid"] = pid
            if pid and candidate["importable"]:
                candidate["importable"] = False
            elif running_vms is None and candidate["importable"]:
                candidate["importable"] = False
    return candidates


def build_vm_config(candidate: dict[str, Any], macos_version: str) -> VMConfig:
    """Create only catalog metadata; the existing VM directory/EFI stays untouched."""
    if not candidate.get("importable"):
        raise ReimsImportError("A VM não passou pela revisão de importação.")
    fresh = _inspect_entry({"name": candidate.get("id"), "dir": candidate.get("directory")}, set())
    if not fresh.get("importable") or fresh.get("disk_path") != candidate.get("disk_path"):
        raise ReimsImportError("A configuração Reims mudou desde a prévia; atualize a revisão antes de importar.")
    candidate = fresh
    source_dir = Path(candidate["directory"])
    raw = _regular_file(source_dir / "vm.toml", label="vm.toml", read_bytes=True)
    try:
        config = tomllib.loads((raw or b"").decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ReimsImportError("vm.toml mudou durante a importação.") from exc
    q35 = config.get("q35") if isinstance(config, dict) else {}
    q35 = q35 if isinstance(q35, dict) else {}
    audio = config.get("audio") if isinstance(config, dict) else {}
    audio = audio if isinstance(audio, dict) else {}
    network = config.get("network") if isinstance(config, dict) else {}
    network = network if isinstance(network, dict) else {}
    ssh_port = network.get("ssh_port")
    if isinstance(ssh_port, bool) or not isinstance(ssh_port, int) or not 1 <= ssh_port <= 65535:
        ssh_port = None
    display = config.get("display") if isinstance(config, dict) else {}
    display = display if isinstance(display, dict) else {}
    display_count = display.get("count")
    if isinstance(display_count, bool) or not isinstance(display_count, int) or not 1 <= display_count <= 8:
        display_count = None
    args = list(config.get("extra_args", []))
    if args[:2] == ["-action", "reboot=shutdown"]:
        args = args[2:]
    return VMConfig(
        id=candidate["id"],
        name=candidate["name"],
        macos_version=macos_version,
        vcpus=candidate["vcpus"],
        cpu_model=q35.get("cpu_model") or "Skylake-Client-noTSX-IBRS",
        ram_gb=candidate["ram_gb"],
        disk_size_gb=candidate["disk_size_gb"],
        disk_path=candidate["disk_path"],
        smbios=SMBIOSConfig(),
        audio_device="usb-audio",
        audio_enabled=audio.get("enabled", True) is True,
        audio_backend=audio.get("backend") or "auto",
        network_enabled=network.get("enabled", True) is True,
        network_mac=network.get("mac"),
        network_ssh_port=ssh_port,
        fullscreen=bool(display.get("borderless", False)),
        display_resolution=candidate["display_resolution"],
        display_count=display_count,
        display_hidpi=candidate["display_hidpi"],
        display_refresh_hz=display.get("refresh"),
        qemu_extra_args=args,
        created_at=datetime.now(timezone.utc).isoformat(),
        install_phase="pending",
        power_sync_mode="vm-only",
    )
