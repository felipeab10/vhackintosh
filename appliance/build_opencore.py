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


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, capture_output=True, text=True)


def build(util: Path, output: Path, config_output: Path) -> None:
    for tool in ("guestfish", "qemu-img"):
        if not shutil.which(tool):
            raise RuntimeError(f"Ferramenta obrigatória ausente: {tool}")
    if not util.is_file() or not os.access(util, os.X_OK):
        raise RuntimeError(f"reims-util não executável: {util}")

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
