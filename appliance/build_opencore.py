#!/usr/bin/env python3
"""Gera e valida a EFI neutra da ISO com os kexts do reims-util oficial."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path
import sys

# Este arquivo também é executado diretamente em builds de ISO (`python appliance/...`),
# onde sys.path começa em appliance/ e não encontra o pacote core/ do checkout.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.graphics_profiles import (
    EMBEDDED_KEXTS,
    GRAPHICS_PROFILES,
    KEXT_MAX_KERNEL,
    LEGACY_PROFILE,
    VENDORED_KEXTS,
    VENDORED_KEXTS_DIR,
    profile_kext_dir,
    verify_kext_manifest,
)

BOOT_ARGS = "keepsyms=1 tlbto_us=0 vti=9"
PICKER_TIMEOUT = 45
NEUTRAL_SERIAL = "C02VHA000001"
NEUTRAL_MLB = "C02VHA00000000001"
NEUTRAL_UUID = "00000000-0000-0000-0000-000000000000"
NEUTRAL_ROM = "000000000000"
PLATFORM_GUID = "7C436110-AB2A-4BBB-A880-FE41995C9F82"
# O conjunto legado permanece como alias histórico para chamadores que esperam o
# contrato completo (incluindo Lilu embutido). A tabela e os diretórios vêm do módulo
# compartilhado `core.graphics_profiles`, também usado por `core.reims_util`.
REQUIRED_KEXTS = {
    "Lilu.kext": "25.0.0",
    **dict(GRAPHICS_PROFILES[LEGACY_PROFILE]),
}

# Flag do binário novo (master >= fe9bfe1) que aceita kexts externos.
_ADD_KEXT_FLAG = "--add-kext"

# A bomba-relógio registrada antes virou mecanismo: a escolha do perfil não é mais uma
# constante. `detect_graphics_profile` mede o device compilado no binário do QEMU da
# release (o mesmo detector que `core/reims_release.py:_device_of_this_binary` usa) e
# traduz para o nome do perfil. Quando a frota migrar para `reims-graphics-pci`, a EFI
# passa a sair com os 5 `Rgfx*`/`ReimsGraphics*` sem editar este arquivo.


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, capture_output=True, text=True)

def _candidate_qemu_binaries() -> list[Path]:
    """Onde procurar o binário do QEMU quando `--qemu` não é dado.

    A ordem segue a mesma convenção de `core.reims_build.qemu_binary()` (o
    `qemu/current/bin/qemu-system-x86_64` que o `install_prebuilt` instala) e depois o
    PATH. O `build-iso.sh` chama este gerador ANTES de resolver o QEMU da release, então
    exigir `--qemu` ali travaria o build; a detecção cai nesses candidatos.
    """
    candidates: list[Path] = []
    try:
        from core.reims_build import qemu_binary  # type: ignore

        candidates.append(qemu_binary())
    except Exception:  # pacote ausente no contexto da ISO: segue para o PATH
        pass
    on_path = shutil.which("qemu-system-x86_64")
    if on_path:
        candidates.append(Path(on_path))
    return candidates

def detect_graphics_profile(qemu: Path | None, *, explicit: str | None = None) -> str:
    """Qual perfil gráfico usar: o nome do device, medido no binário do QEMU da release.

    A fonte é o NOME DO DEVICE compilado no binário do QEMU — o mesmo sinal positivo que
    `core/reims_release.py:_device_of_this_binary` já mede (docstring dela em
    `core/reims_release.py:125-152`). Não é uma flag manual livre nem uma preferência do
    operador: é o device que o runtime instalado realmente traz. Quando a frota migrar,
    a EFI passa a sair com o conjunto novo sozinha.

    Tradução do device para o nome do perfil (mesma precedência de
    `core/reims_release.py:_device_of_this_binary`):
      `reims-graphics-pci` presente -> "reims-graphics-pci". Esse nome identifica o
      device novo por si só; a branch development SOMA os dois devices (mantém
      `apple-gfx-pci` para compatibilidade), então o novo tem de prevalecer.
      somente `apple-gfx-pci` presente -> "apple-gfx-pci"
      nenhum device conhecido ou binário ilegível -> recusa (sem fallback silencioso)

    `explicit` só serve para testes de mutação e para um override consciente de quem chama:
    tem de NOMEAR um perfil conhecido; qualquer outro valor levanta, nunca cai em default.
    """
    if explicit is not None:
        if explicit not in GRAPHICS_PROFILES:
            raise RuntimeError(
                f"perfil gráfico desconhecido: {explicit!r}; conhecidos: "
                f"{', '.join(sorted(GRAPHICS_PROFILES))}"
            )
        return explicit

    if qemu is None:
        looked = _candidate_qemu_binaries()
        qemu = next((c for c in looked if c.is_file()), None)
        if qemu is None:
            raise RuntimeError(
                "não foi possível detectar o perfil gráfico: nenhum binário de QEMU "
                "encontrado; passe --qemu com o qemu-system-x86_64 da release"
            )
    try:
        dados = Path(qemu).read_bytes()
    except OSError as exc:
        raise RuntimeError(
            f"não foi possível ler o binário do QEMU {qemu} para detectar o perfil: {exc}"
        ) from exc

    tem_graphics = b"reims-graphics-pci" in dados
    tem_apple_gfx = b"apple-gfx-pci" in dados
    if tem_graphics:
        return "reims-graphics-pci"
    if tem_apple_gfx:
        return "apple-gfx-pci"
    raise RuntimeError(
        "não foi possível detectar o perfil gráfico do binário do QEMU "
        f"{qemu}: nenhum device conhecido presente"
    )

def profile_kextexts(profile: str) -> tuple[tuple[str, str], ...]:
    """Os bundles do perfil, sem o Lilu (que é embutido). Recusa perfil desconhecido."""
    try:
        return GRAPHICS_PROFILES[profile]
    except KeyError as exc:
        raise RuntimeError(
            f"perfil gráfico desconhecido: {profile!r}; conhecidos: "
            f"{', '.join(sorted(GRAPHICS_PROFILES))}"
        ) from exc

def kext_injection(util: Path, profile: str) -> list[str]:
    """Argumentos `--add-kext` que o binário precisa, ou [] se ele já os embute.

    Duas gerações de reims-util são aceitas:
      - master (>= fe9bfe1): não embute os kexts de guest; expõe `--add-kext` e nós
        passamos os bundles do PERFIL vendorizado com os gates de kernel do perfil;
      - pin antigo (c717e69): embute os kexts na própria imagem; não passamos nada.
    Rejeita binário que não entregue os kexts por nenhum dos dois caminhos.
    """
    bundles = profile_kextexts(profile)  # valida antes de acessar o binário externo
    try:
        help_result = subprocess.run(
            [str(util), "opencore", "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"reims-util opencore --help falhou: {exc}") from exc
    help_text = help_result.stdout or ""
    if help_result.returncode != 0:
        raise RuntimeError(f"reims-util opencore --help falhou (rc={help_result.returncode})")

    if _ADD_KEXT_FLAG not in help_text:
        # Binário antigo: embute os kexts. A validação do perfil logo abaixo confirma,
        # na imagem gerada, que todos chegaram.
        return []

    profile_dir = profile_kext_dir(profile, VENDORED_KEXTS_DIR)
    missing = [name for name, _ in bundles if not (profile_dir / name).is_dir()]
    if missing:
        raise RuntimeError(
            f"o perfil {profile} exige --add-kext, mas faltam kexts em {profile_dir}: "
            f"{', '.join(missing)}"
        )
    # Integridade antes de injetar: set.toml é a fonte da verdade dos executáveis.
    verify_kext_manifest(profile, VENDORED_KEXTS_DIR)
    args: list[str] = []
    for name, minimum_kernel in bundles:
        # MIN explícito: o default do `--add-kext` é 25.0.0, e os ports exigem 25.4.0.
        args += ["--add-kext", f"{profile_dir / name},{minimum_kernel},{KEXT_MAX_KERNEL}"]
    return args


def build(util: Path, output: Path, config_output: Path, *, graphics_profile: str | None = None, qemu: Path | None = None) -> None:
    for tool in ("guestfish", "qemu-img"):
        if not shutil.which(tool):
            raise RuntimeError(f"Ferramenta obrigatória ausente: {tool}")
    if not util.is_file() or not os.access(util, os.X_OK):
        raise RuntimeError(f"reims-util não executável: {util}")

    profile = detect_graphics_profile(qemu, explicit=graphics_profile)
    profile_kextexts(profile)  # recusa perfil desconhecido antes de gastar um build
    kext_args = kext_injection(util, profile)

    output.parent.mkdir(parents=True, exist_ok=True)
    config_output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vhackintosh-opencore-") as td:
        staging = Path(td) / "OpenCore.qcow2"
        config = Path(td) / "config.plist"
        verified_config = Path(td) / "config-verified.plist"

        run(
            str(util), "opencore", "-o", str(staging), "--no-ovmf",
            "--model", "iMac20,1", "--serial", NEUTRAL_SERIAL,
            "--mlb", NEUTRAL_MLB, "--uuid", NEUTRAL_UUID,
            "--rom", NEUTRAL_ROM, "--timeout", str(PICKER_TIMEOUT),
            "--boot-args", BOOT_ARGS, "--show-picker",
            *kext_args,
        )
        run("qemu-img", "check", str(staging))
        image_info = json.loads(run("qemu-img", "info", "--output=json", str(staging)).stdout)
        if image_info.get("virtual-size") != 128 * 1024 * 1024:
            raise RuntimeError("reims-util não gerou a imagem OpenCore esperada de 128 MiB")
        run(
            "guestfish", "--ro", "-a", str(staging), "-m", "/dev/sda1",
            "download", "/EFI/OC/config.plist", str(config),
        )
        data = plistlib.loads(config.read_bytes())
        platform = data.get("PlatformInfo")
        generic = platform.get("Generic") if isinstance(platform, dict) else None
        if not isinstance(generic, dict):
            raise RuntimeError("reims-util gerou config.plist sem PlatformInfo.Generic")

        # IDs neutros na ISO; DiskProvisioner injeta uma identidade por VM.
        generic.update({
            "SystemProductName": "iMac20,1",
            "SystemSerialNumber": "",
            "MLB": "",
            "SystemUUID": NEUTRAL_UUID,
            "ROM": bytes(6),
        })
        for section_name in ("DataHub", "PlatformNVRAM", "SMBIOS"):
            section = platform.get(section_name)
            if isinstance(section, dict):
                for key in ("SystemSerialNumber", "SystemUUID", "MLB", "ROM", "BoardSerialNumber"):
                    section.pop(key, None)

        add_kexts = {
            entry.get("BundlePath"): entry
            for entry in data.get("Kernel", {}).get("Add", [])
            if isinstance(entry, dict)
        }
        # Valida contra o PERFIL detectado, não contra uma constante: o conjunto esperado
        # (Lilu embutido + os bundles do perfil) tem de ser IGUALDADE EXATA na imagem,
        # com os gates certos — um kext do outro perfil sobrando também é erro.
        expected = {"Lilu.kext": "25.0.0", **dict(profile_kextexts(profile))}
        extras = sorted(set(add_kexts) - set(expected))
        if extras:
            raise RuntimeError(
                f"a imagem tem kexts fora do perfil {profile}: {', '.join(extras)}"
            )
        for name, minimum_kernel in expected.items():
            entry = add_kexts.get(name)
            if not entry or not entry.get("Enabled"):
                raise RuntimeError(f"reims-util não habilitou {name} (perfil {profile})")
            if entry.get("MinKernel") != minimum_kernel or entry.get("MaxKernel") != "25.99.99":
                raise RuntimeError(
                    f"{name} não está delimitada ao Kernel macOS 26 no perfil {profile}"
                )
            listing = run(
                "guestfish", "--ro", "-a", str(staging), "-m", "/dev/sda1",
                "find", f"/EFI/OC/Kexts/{name}",
            ).stdout
            if not listing.strip():
                raise RuntimeError(f"reims-util não incluiu os arquivos de {name}")

        nvram = data.setdefault("NVRAM", {}).setdefault("Add", {}).setdefault(PLATFORM_GUID, {})
        nvram["boot-args"] = BOOT_ARGS
        boot = data.setdefault("Misc", {}).setdefault("Boot", {})
        boot["ShowPicker"] = True
        boot["Timeout"] = PICKER_TIMEOUT
        with config.open("wb") as stream:
            plistlib.dump(data, stream, sort_keys=False)
        run(
            "guestfish", "-a", str(staging), "-m", "/dev/sda1",
            "upload", str(config), "/EFI/OC/config.plist",
        )
        run(
            "guestfish", "--ro", "-a", str(staging), "-m", "/dev/sda1",
            "download", "/EFI/OC/config.plist", str(verified_config),
        )
        if verified_config.read_bytes() != config.read_bytes():
            raise RuntimeError("config.plist não persistiu na imagem OpenCore")
        run("qemu-img", "check", str(staging))

        # Publica os dois arquivos juntos somente após a EFI passar pela validação.
        staged_output = output.with_name(f".{output.name}.new")
        staged_config = config_output.with_name(f".{config_output.name}.new")
        shutil.copyfile(staging, staged_output)
        shutil.copyfile(config, staged_config)
        os.replace(staged_output, output)
        os.replace(staged_config, config_output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reims-util", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config-output", type=Path, required=True)
    # O perfil normalmente é detectado do binário do QEMU (--qemu). --graphics-profile só
    # sobrepõe de forma consciente; um nome desconhecido falha, nunca cai no legado.
    parser.add_argument(
        "--graphics-profile",
        choices=sorted(GRAPHICS_PROFILES),
        default=None,
        help="Sobrepõe o perfil gráfico; o padrão é detectá-lo do binário do QEMU (--qemu).",
    )
    parser.add_argument(
        "--qemu",
        type=Path,
        default=None,
        help="Binário do QEMU da release, de onde o perfil gráfico é detectado.",
    )
    args = parser.parse_args()
    try:
        build(
            args.reims_util, args.output, args.config_output,
            graphics_profile=args.graphics_profile, qemu=args.qemu,
        )
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        print(f"Erro ao gerar OpenCore: {exc}", file=__import__("sys").stderr)
        return 1
    print(f"EFI OpenCore criada com reims-util: {args.output}")
    print(f"config.plist conferido: {args.config_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
