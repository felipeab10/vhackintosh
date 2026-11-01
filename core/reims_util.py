"""
vHackintosh — Integração com o reims-util (ecossistema reims-vgpu)

Gera dinamicamente imagens OpenCore (OpenCore.qcow2) para máquinas virtuais macOS x86_64
usando o utilitário nativo em Rust `reims-util opencore`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from core.config import LOGS_DIR, SMBIOSConfig

# Onde cada chamada ao reims-util é registrada (com stdout/stderr e returncode).
LOG_FILE = LOGS_DIR / "reims-util-opencore.log"
OPENCORE_BOOT_ARGS = "keepsyms=1 tlbto_us=0 vti=9"
OPENCORE_PICKER_TIMEOUT = 45


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
    show_picker: bool = False,
    job_context=None,
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
        show_picker: Se True, mantém o seletor OpenCore visível em todos os boots.

    Returns:
        True se a imagem foi gerada com sucesso e é válida.
    """
    util_bin = find_reims_util()
    if not util_bin:
        return False
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # O APV arm64-port é a correção validada para o primeiro desktop no Tahoe.
    # Binários antigos ainda geravam uma EFI válida, mas sem esses kexts. Não
    # aceite silenciosamente uma EFI incompleta como se fosse a atual.
    try:
        help_result = subprocess.run(
            [str(util_bin), "opencore", "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if help_result.returncode != 0 or "--no-apv-arm64-port" not in help_result.stdout:
        with LOG_FILE.open("a", encoding="utf-8") as lf:
            lf.write(
                f"\n--- reims-util opencore ({Path(output_path).name}) ---\n"
                f"Erro: {util_bin} não oferece suporte aos kexts APV arm64-port.\n"
            )
        return False

    final_dest = Path(output_path).resolve()
    final_dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{final_dest.stem}.", suffix=final_dest.suffix, dir=final_dest.parent
    )
    os.close(fd)
    dest = Path(temporary_name)
    dest.unlink()

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
    if show_picker:
        cmd.append("--show-picker")

    log_file = LOG_FILE

    try:
        if job_context is not None:
            res = job_context.run_subprocess(cmd, timeout=120, check=False)
            command_stdout = ""
            command_stderr = ""
        else:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
            )
            command_stdout = res.stdout or ""
            command_stderr = res.stderr or ""
        with log_file.open("a", encoding="utf-8") as lf:
            lf.write(f"\n--- reims-util opencore ({dest.name}) ---\n")
            lf.write(f"Command: {' '.join(cmd)}\n")
            lf.write(f"Returncode: {res.returncode}\n")
            if command_stdout:
                lf.write(f"Stdout: {command_stdout.strip()}\n")
            if command_stderr:
                lf.write(f"Stderr: {command_stderr.strip()}\n")

        if res.returncode != 0:
            return False

        if not dest.is_file() or dest.stat().st_size == 0:
            return False
        qemu_img = shutil.which("qemu-img")
        if not qemu_img:
            with log_file.open("a", encoding="utf-8") as lf:
                lf.write("Erro: qemu-img não encontrado para validar a EFI gerada.\n")
            return False
        if job_context is not None:
            check = job_context.run_subprocess([qemu_img, "check", str(dest)], timeout=30, check=False)
            check_stderr = ""
        else:
            check = subprocess.run(
                [qemu_img, "check", str(dest)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            check_stderr = check.stderr or ""
        if check.returncode:
            with log_file.open("a", encoding="utf-8") as lf:
                lf.write(f"Erro: qemu-img check falhou: {check_stderr.strip()}\n")
            return False
        os.replace(dest, final_dest)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        with log_file.open("a", encoding="utf-8") as lf:
            lf.write(f"Exception during reims-util invocation: {exc}\n")
        return False
    finally:
        dest.unlink(missing_ok=True)
