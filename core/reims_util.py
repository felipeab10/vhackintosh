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
from core.graphics_profiles import (
    GRAPHICS_PROFILES,
    KEXT_MAX_KERNEL,
    VENDORED_KEXTS,
    VENDORED_KEXTS_DIR,
    profile_kext_dir,
    verify_kext_manifest,
)

# Onde cada chamada ao reims-util é registrada (com stdout/stderr e returncode).
LOG_FILE = LOGS_DIR / "reims-util-opencore.log"
OPENCORE_BOOT_ARGS = "keepsyms=1 tlbto_us=0 vti=9"
OPENCORE_PICKER_TIMEOUT = 45

# Profile bundles, kernel bounds, and paths are shared with appliance/build_opencore.
# Includes `reims-graphics-pci`; do not duplicate its bundle table here. This module's
# `generate_opencore(graphics_profile=...)` supports both `apple-gfx-pci` and
# `reims-graphics-pci`.
# Keep these imported names as public aliases for existing callers and tests.
# Nome da flag aposentada que gateava a EFI: o master a aceita e ignora (`hide = true`),
# e binários antigos a expõem no `--help`. Serve só para reconhecer o binário velho.
_LEGACY_APV_FLAG = "--no-apv-arm64-port"

# Flag do binário novo que prova que ele aceita kexts externos.
_ADD_KEXT_FLAG = "--add-kext"


REIMS_UTIL_CANDIDATES = (
    # Linux distro packages ship the exact upstream revision validated for APV.
    # Prefer it over user-installed Cargo/PATH copies, which may predate APV.
    Path("/opt/vhackintosh/resources/reims-util/reims-util"),
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

    O pacote do Control vem primeiro: ele carrega a revisão oficial testada com
    APV. Em um checkout de desenvolvimento, seguimos com o binário em
    CARGO_HOME, o diretório ao lado de reims-ctl, PATH e por último a cópia da
    ISO; uma cópia antiga do host não pode substituir o utilitário validado.
    """
    which_path = shutil.which("reims-util")
    raw: tuple[Optional[Path], ...] = (
        *REIMS_UTIL_CANDIDATES[:1],
        _cargo_home_bin(),
        *REIMS_UTIL_CANDIDATES[1:2],
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


def _detect_profile_or_none(output_path: Path | str) -> str | None:
    """Perfil quando o chamador não passa um: detecta pelo QEMU; `None` se não der para saber.

    Reusa `appliance.build_opencore.detect_graphics_profile` (mesma regra de
    `core.reims_release._device_of_this_binary`): `reims-graphics-pci` presente prevalece
    (a branch development soma os dois), só `apple-gfx-pci` = legado.

    IMPORTANTE: sem evidência devolve `None` e registra a razão no log — NÃO assume o legado.
    `None` é "não sei", não "legado": o ponto de provisionamento (`core/disk.py`) chama sem
    perfil, e assumir legado num host cujo QEMU espera `reims-graphics-pci` entrega a VM sem
    aceleração, com o sintoma aparecendo no guest. Recusar aqui mantém o mesmo sentido do
    builder (`detect_graphics_profile` levanta `RuntimeError`). O legado continua disponível
    quando o chamador PEDE explicitamente (`graphics_profile="apple-gfx-pci"`).
    """
    try:
        from appliance.build_opencore import detect_graphics_profile

        return detect_graphics_profile(None)
    except Exception as exc:  # import indisponível ou QEMU ilegível/ausente
        with LOG_FILE.open("a", encoding="utf-8") as lf:
            lf.write(
                f"\n--- reims-util opencore ({Path(output_path).name}) ---\n"
                f"Perfil não informado e não detectável ({exc}); nenhuma imagem foi gerada.\n"
            )
        return None

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
    graphics_profile: str | None = None,
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
        graphics_profile: Perfil de kexts gráficos a injetar. `None` autodetecta o perfil
            pelo device do QEMU; sem evidência a geração é RECUSADA (não assume legado).
            Passe `"apple-gfx-pci"` explicitamente para forçar o perfil legado. Um nome
            desconhecido falha, nunca cai em default silencioso.

    Returns:
        True se a imagem foi gerada com sucesso e é válida.
    """
    util_bin = find_reims_util()
    if not util_bin:
        return False
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Valida o perfil ANTES de decidir pela geração do binário: um nome desconhecido tem de
    # falhar mesmo quando o binário escolhido embute os kexts e ignoraria o argumento.
    # `None` NÃO é "legado silencioso": tenta detectar pelo device do QEMU da release e, sem
    # evidência, RECUSA (mesmo sentido do builder). O legado só quando o chamador o pede.
    if graphics_profile is None:
        profile = _detect_profile_or_none(output_path)
    else:
        profile = graphics_profile
    if profile not in GRAPHICS_PROFILES:
        with LOG_FILE.open("a", encoding="utf-8") as lf:
            lf.write(
                f"\n--- reims-util opencore ({Path(output_path).name}) ---\n"
                f"Erro: perfil gráfico desconhecido {profile!r}; conhecidos: "
                f"{', '.join(sorted(GRAPHICS_PROFILES))}.\n"
            )
        return False

    # Os kexts de guest definem se a EFI sobe o desktop. São DUAS gerações de binário,
    # e as duas são aceitas:
    #   - master (>= fe9bfe1): não embute os kexts; expõe `--add-kext` e nós passamos os
    #     kexts vendorizados;
    #   - antigo (pin c717e69): embute os 9 kexts na própria imagem e não conhece
    #     `--add-kext`; não passamos nada.
    # O `--no-apv-arm64-port` NÃO serve de critério: o master o aceita e ignora, e o
    # esconderam do `--help` — um gate por essa flag reprovaria o binário novo.
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
    help_text = help_result.stdout or ""
    if help_result.returncode != 0:
        with LOG_FILE.open("a", encoding="utf-8") as lf:
            lf.write(
                f"\n--- reims-util opencore ({Path(output_path).name}) ---\n"
                f"Erro: {util_bin} opencore --help falhou (rc={help_result.returncode}).\n"
            )
        return False

    supports_add_kext = _ADD_KEXT_FLAG in help_text
    embeds_kexts = _LEGACY_APV_FLAG in help_text

    # Rejeita binário que não sabe entregar os kexts por NENHUM dos dois caminhos.
    if not supports_add_kext and not embeds_kexts:
        with LOG_FILE.open("a", encoding="utf-8") as lf:
            lf.write(
                f"\n--- reims-util opencore ({Path(output_path).name}) ---\n"
                f"Erro: {util_bin} não oferece nem kexts embutidos nem {_ADD_KEXT_FLAG}.\n"
            )
        return False

    # No caminho `--add-kext` os kexts têm de estar vendorizados, senão a EFI sai sem eles.
    kext_args: list[str] = []
    if supports_add_kext:
        bundles = GRAPHICS_PROFILES[profile]
        profile_dir = profile_kext_dir(profile, VENDORED_KEXTS_DIR)
        missing = [name for name, _ in bundles if not (profile_dir / name).is_dir()]
        if missing:
            with LOG_FILE.open("a", encoding="utf-8") as lf:
                lf.write(
                    f"\n--- reims-util opencore ({Path(output_path).name}) ---\n"
                    f"Erro: o perfil {profile} exige --add-kext, mas faltam kexts em "
                    f"{profile_dir}: {', '.join(missing)}.\n"
                )
            return False
        # Integridade antes de injetar: set.toml é a fonte da verdade dos executáveis.
        try:
            verify_kext_manifest(profile, VENDORED_KEXTS_DIR)
        except RuntimeError as exc:
            with LOG_FILE.open("a", encoding="utf-8") as lf:
                lf.write(
                    f"\n--- reims-util opencore ({Path(output_path).name}) ---\n"
                    f"Erro de integridade dos kexts: {exc}.\n"
                )
            return False
        for name, minimum_kernel in bundles:
            kext_args.append("--add-kext")
            kext_args.append(f"{profile_dir / name},{minimum_kernel},{KEXT_MAX_KERNEL}")

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

    cmd.extend(kext_args)

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
