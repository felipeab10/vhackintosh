"""
vHackintosh — Detecção do Modo de Execução (Live USB vs Sistema Instalado)

Centraliza a lógica que distingue uma sessão de pendrive/ISO (``live``) de um
sistema definitivo gravado em disco (``installed``).

A presença do binário ``vhackintosh-install`` NÃO é utilizada como sinal de Live
USB: o instalador é copiado propositalmente para o sistema definitivo (atalho
permanente ``[I]`` no gerenciador), portanto existe nos dois modos.

Ordem de precedência:
  1. Helper canônico ``vhackintosh-mode`` (mesma lógica usada no boot/tty1).
  2. Marcador positivo ``/etc/vhackintosh/installed`` gravado pelo instalador.
  3. Tipo do sistema de arquivos raiz (overlay/squashfs => Live).
  4. Origem da raiz (``/dev/loop*`` => Live; ``/dev/*`` => instalado).
  5. Fallbacks exclusivos do Archiso (``/run/archiso`` e parâmetros de cmdline).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Tuple

INSTALLED_MARKER = Path("/etc/vhackintosh/installed")
MODE_HELPER = "vhackintosh-mode"
LIVE_ROOT_FSTYPES = frozenset({"overlay", "squashfs", "erofs"})
ARCHISO_CMDLINE_PARAMS = ("archisobasedir", "archisosearchuuid", "img_dev", "img_loop")

MODE_LIVE = "live"
MODE_INSTALLED = "installed"


def _read_root_mount() -> Tuple[str, str]:
    """Retorna ``(origem, tipo_do_sistema_de_arquivos)`` da raiz ``/``."""
    try:
        result = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE,FSTYPE", "/"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split()
            if len(parts) >= 2:
                return parts[0], parts[1]
    except Exception:
        pass

    # Fallback puro-Python lendo /proc/mounts
    source, fstype = "", ""
    try:
        with open("/proc/mounts", "r", errors="ignore") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) >= 3 and fields[1] == "/":
                    source, fstype = fields[0], fields[2]
                    break
    except Exception:
        pass
    return source, fstype


def _cmdline_has_archiso_params() -> bool:
    try:
        with open("/proc/cmdline", "r", errors="ignore") as handle:
            cmdline = handle.read()
    except Exception:
        return False
    return any(f"{param}=" in cmdline for param in ARCHISO_CMDLINE_PARAMS)


def _detect_via_helper() -> str | None:
    helper = shutil.which(MODE_HELPER)
    if not helper:
        return None
    try:
        result = subprocess.run([helper], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    mode = result.stdout.strip().lower()
    if mode in (MODE_LIVE, MODE_INSTALLED):
        return mode
    return None


def detect_system_mode() -> str:
    """Retorna ``"live"`` (pendrive/ISO) ou ``"installed"`` (sistema em disco)."""
    helper_mode = _detect_via_helper()
    if helper_mode:
        return helper_mode

    # Marcador positivo gravado pelo instalador bare-metal.
    if INSTALLED_MARKER.exists():
        return MODE_INSTALLED

    source, fstype = _read_root_mount()
    if fstype in LIVE_ROOT_FSTYPES:
        return MODE_LIVE
    if source.startswith("/dev/loop"):
        return MODE_LIVE
    if source.startswith("/dev/"):
        return MODE_INSTALLED

    # Fallbacks exclusivos do Archiso (não usam a string genérica "archiso").
    if Path("/run/archiso").is_dir() or _cmdline_has_archiso_params():
        return MODE_LIVE

    # Padrão seguro: sem evidência de live, assume sistema instalado.
    return MODE_INSTALLED


def is_live_media() -> bool:
    """Atalho booleano: ``True`` quando executando de pendrive/ISO."""
    return detect_system_mode() == MODE_LIVE


def has_installer_binary() -> bool:
    """Indica se o instalador bare-metal está disponível (atalho ``[I]``)."""
    return shutil.which("vhackintosh-install") is not None
