"""
vHackintosh — Integração com o reims-util (ecossistema reims-vgpu)

Gera dinamicamente imagens OpenCore (OpenCore.qcow2) para máquinas virtuais macOS x86_64
usando o utilitário nativo em Rust `reims-util opencore`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from core.config import LOGS_DIR, SMBIOSConfig

# Onde cada chamada ao reims-util é registrada (com stdout/stderr e returncode).
LOG_FILE = LOGS_DIR / "reims-util-opencore.log"


REIMS_UTIL_CANDIDATES = (
    Path(os.path.expanduser("~/.cargo/bin/reims-util")),
    Path("/root/.cargo/bin/reims-util"),
    Path("/usr/local/bin/reims-util"),
    Path("/opt/reims-vgpu/target/release/reims-util"),
)

# Onde o binário pode estar, na ordem em que é procurado, fora do CARGO_HOME e
# do PATH do processo.
REIMS_UTIL_EXTRA_LOCATIONS = (
    Path("/root/.cargo/bin/reims-util"),
    Path("/usr/local/bin/reims-util"),
    Path("/opt/reims-vgpu/target/release/reims-util"),
)


def _is_executable_file(candidate: Path) -> bool:
    try:
        return candidate.is_file() and os.access(candidate, os.X_OK)
    except OSError:
        return False


def _runs(candidate: Path) -> bool:
    """Confirma que o candidato EXECUTA, não apenas que existe.

    Um cargo install interrompido, ou um binário de outra arquitetura, deixa um
    arquivo executável que falha ao rodar; sem esta sonda ele venceria a busca e
    a geração nativa cairia no fallback por guestfish sem explicação.
    """
    try:
        res = subprocess.run(
            [str(candidate), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def _cargo_home_bin() -> Path:
    cargo_home = os.environ.get("CARGO_HOME") or os.path.expanduser("~/.cargo")
    return Path(cargo_home) / "bin" / "reims-util"


def candidate_binaries() -> tuple[Path, ...]:
    """Candidatos na ordem em que são testados.

    Espelha o `reims_util::binary()` do reims-ctl: primeiro o binário instalado
    ao lado do reims-ctl (o que o próprio appliance compilou, na revisão que o
    reims-ctl exige), depois o PATH — onde a ISO deixa o binário que carrega
    pronto em /usr/local/bin. Um reims-util antigo vindo da ISO nunca deve
    vencer o que o appliance acabou de instalar.
    """
    which_path = shutil.which("reims-util")
    raw: tuple[Optional[Path], ...] = (
        _cargo_home_bin(),
        *REIMS_UTIL_CANDIDATES[:2],
        Path(which_path) if which_path else None,
        *REIMS_UTIL_EXTRA_LOCATIONS[1:],
    )
    ordered: list[Path] = []
    for candidate in raw:
        if candidate is not None and candidate not in ordered:
            ordered.append(candidate)
    return tuple(ordered)


def search_summary() -> str:
    """Resumo curto dos lugares onde procuramos, para a mensagem da TUI."""
    seen: list[str] = []
    home = os.path.expanduser("~")
    for candidate in candidate_binaries():
        directory = str(candidate.parent)
        if directory.startswith(home):
            directory = "~" + directory[len(home):]
        if directory not in seen:
            seen.append(directory)
    return ", ".join(seen)


def find_reims_util(*, probe: bool = True) -> Optional[Path]:
    """Resolve o binário do reims-util no sistema ou ambiente de compilação."""
    env_bin = os.environ.get("REIMS_UTIL_BIN")
    if env_bin:
        cand = Path(env_bin)
        if _is_executable_file(cand) and (not probe or _runs(cand)):
            return cand

    for cand in candidate_binaries():
        if not _is_executable_file(cand):
            continue
        if probe and not _runs(cand):
            continue
        return cand

    return None


def is_available() -> bool:
    """Verifica se o utilitário reims-util está acessível e executável."""
    return find_reims_util() is not None


def generate_opencore(
    output_path: Path | str,
    smbios: SMBIOSConfig,
    *,
    no_ovmf: bool = True,
    timeout: Optional[int] = None,
    boot_args: Optional[str] = None,
    verbose_boot: bool = False,
    text_picker: bool = False,
) -> bool:
    """Gera uma imagem OpenCore personalizada utilizando `reims-util opencore`.

    Args:
        output_path: Caminho de destino para a imagem (.qcow2 ou raw).
        smbios: Objeto SMBIOSConfig contendo model, serial_number, board_serial, smuuid e rom.
        no_ovmf: Se True, não grava arquivos OVMF ao lado da imagem (preserva OVMF_VARS existente).
        timeout: Segundos que o boot picker aguarda (padrão: None -> usa o default do reims-util).
        boot_args: Argumentos de boot customizados do kernel macOS.
        verbose_boot: Se True, ativa mensagens detalhadas de inicialização do kernel (-v).
        text_picker: Se True, usa o seletor em modo texto do OpenCore.

    Returns:
        True se a imagem foi gerada com sucesso e é válida.
    """
    util_bin = find_reims_util()
    if not util_bin:
        return False

    dest = Path(output_path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(util_bin),
        "opencore",
        "-o",
        str(dest),
        "--model",
        smbios.model,
        "--serial",
        smbios.serial_number,
        "--mlb",
        smbios.board_serial,
        "--uuid",
        smbios.smuuid,
        "--rom",
        smbios.rom,
    ]

    if no_ovmf:
        cmd.append("--no-ovmf")
    if timeout is not None:
        cmd.extend(["--timeout", str(timeout)])
    if boot_args:
        cmd.extend(["--boot-args", boot_args])
    if verbose_boot:
        cmd.append("--verbose-boot")
    if text_picker:
        cmd.append("--text-picker")

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_FILE

    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        with log_file.open("a", encoding="utf-8") as lf:
            lf.write(f"\n--- reims-util opencore ({dest.name}) ---\n")
            lf.write(f"Command: {' '.join(cmd)}\n")
            lf.write(f"Returncode: {res.returncode}\n")
            if res.stdout:
                lf.write(f"Stdout: {res.stdout.strip()}\n")
            if res.stderr:
                lf.write(f"Stderr: {res.stderr.strip()}\n")

        if res.returncode != 0:
            return False

        return dest.is_file() and dest.stat().st_size > 0
    except (OSError, subprocess.SubprocessError) as exc:
        with log_file.open("a", encoding="utf-8") as lf:
            lf.write(f"Exception during reims-util invocation: {exc}\n")
        return False
