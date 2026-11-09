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

BOOT_ARGS = "keepsyms=1 tlbto_us=0 vti=9"
PICKER_TIMEOUT = 45
NEUTRAL_SERIAL = "C02VHA000001"
NEUTRAL_MLB = "C02VHA00000000001"
NEUTRAL_UUID = "00000000-0000-0000-0000-000000000000"
NEUTRAL_ROM = "000000000000"
PLATFORM_GUID = "7C436110-AB2A-4BBB-A880-FE41995C9F82"
REQUIRED_KEXTS = {
    "Lilu.kext": "25.0.0",
    "ReimsVgpu.kext": "25.0.0",
    "AppleGraphicsDeviceControlBridge.kext": "25.4.0",
    "AppleParavirtGPULoader.kext": "25.4.0",
    "AppleParavirtIOSurface.kext": "25.4.0",
    "IOGPUFamilyBridge.kext": "25.4.0",
    "AppleParavirtGPUIOGPUFamily.kext": "25.4.0",
    "IOGraphicsFamilyBridge.kext": "25.4.0",
    "APVDisplayPort.kext": "25.4.0",
}
KEXT_MAX_KERNEL = "25.99.99"

# Lilu ainda vem embutido no binário do reims-util; os 8 restantes são vendorizados
# aqui e injetados com `--add-kext` quando o binário não os traz.
EMBEDDED_KEXTS = frozenset({"Lilu.kext"})
VENDORED_KEXTS = tuple(
    (name, minimum) for name, minimum in REQUIRED_KEXTS.items() if name not in EMBEDDED_KEXTS
)
# Ordem de carga: ReimsVgpu primeiro, os bridges depois. O reims-util recusa um kext que
# linke contra outro dado mais tarde, então a ordem de REQUIRED_KEXTS (dict ordenado) vale.
VENDORED_KEXTS_DIR = Path(__file__).resolve().parent.parent / "resources" / "reims-kexts"

# Flag do binário novo (master >= fe9bfe1) que aceita kexts externos.
_ADD_KEXT_FLAG = "--add-kext"

# ATENÇÃO — bomba-relógio. Os 8 kexts abaixo são os do `apple-gfx-pci`, o default do
# reims-ctl instalado hoje (~/.cargo/bin de 30/out; core/reims_ctl.py omite --graphics e
# core/reims_release.py exige `apple-gfx-pci` no binário). Quando o reims-ctl da frota
# migrar para `reims-graphics-pci` (default do origin/master do fork, commit 2cc59c93e),
# o conjunto certo passa a ser os 5 `Rgfx*` de guest/prebuilt/ e estes 8 ficam errados.
# A escolha depende do binário do reims-ctl instalado, NÃO de uma constante eterna.


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, capture_output=True, text=True)

def kext_injection(util: Path) -> list[str]:
    """Argumentos `--add-kext` que o binário precisa, ou [] se ele já os embute.

    Duas gerações de reims-util são aceitas:
      - master (>= fe9bfe1): não embute os kexts de guest; expõe `--add-kext` e nós
        passamos os bundles vendorizados com os gates de kernel de REQUIRED_KEXTS;
      - pin antigo (c717e69): embute os 9 kexts na própria imagem; não passamos nada.
    Rejeita binário que não entregue os kexts por nenhum dos dois caminhos.
    """
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
        # Binário antigo: embute os kexts. A validação de REQUIRED_KEXTS logo abaixo
        # confirma, na imagem gerada, que todos os 9 chegaram.
        return []

    missing = [name for name, _ in VENDORED_KEXTS if not (VENDORED_KEXTS_DIR / name).is_dir()]
    if missing:
        raise RuntimeError(
            f"reims-util exige --add-kext, mas faltam kexts em {VENDORED_KEXTS_DIR}: "
            f"{', '.join(missing)}"
        )
    args: list[str] = []
    for name, minimum_kernel in VENDORED_KEXTS:
        # MIN explícito: o default do `--add-kext` é 25.0.0, e os 7 ports exigem 25.4.0.
        args += ["--add-kext", f"{VENDORED_KEXTS_DIR / name},{minimum_kernel},{KEXT_MAX_KERNEL}"]
    return args


def build(util: Path, output: Path, config_output: Path) -> None:
    for tool in ("guestfish", "qemu-img"):
        if not shutil.which(tool):
            raise RuntimeError(f"Ferramenta obrigatória ausente: {tool}")
    if not util.is_file() or not os.access(util, os.X_OK):
        raise RuntimeError(f"reims-util não executável: {util}")

    kext_args = kext_injection(util)

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
        for name, minimum_kernel in REQUIRED_KEXTS.items():
            entry = add_kexts.get(name)
            if not entry or not entry.get("Enabled"):
                raise RuntimeError(f"reims-util não habilitou {name}")
            if entry.get("MinKernel") != minimum_kernel or entry.get("MaxKernel") != "25.99.99":
                raise RuntimeError(f"{name} não está delimitada ao Kernel macOS 26")
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
    args = parser.parse_args()
    try:
        build(args.reims_util, args.output, args.config_output)
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        print(f"Erro ao gerar OpenCore: {exc}", file=__import__("sys").stderr)
        return 1
    print(f"EFI OpenCore criada com reims-util: {args.output}")
    print(f"config.plist conferido: {args.config_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
