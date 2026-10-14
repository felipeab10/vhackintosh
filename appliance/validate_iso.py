#!/usr/bin/env python3
"""Validate a built appliance ISO before its checksum is published."""
from __future__ import annotations
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def command(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, capture_output=True, text=True)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: validate_iso.py ISO REIMS_SOURCE", file=sys.stderr)
        return 2
    iso, source = Path(sys.argv[1]), Path(sys.argv[2])
    if not iso.is_file() or not source.is_dir():
        print("ISO or source directory missing", file=sys.stderr)
        return 1
    for required in ("Cargo.toml", "Cargo.lock", "crates", "vendor/qemu/meson.build", "scripts/install-ctl.sh"):
        if not (source / required).exists():
            print(f"Missing required ReimsVgpu input: {required}", file=sys.stderr)
            return 1
    try:
        for tool in ("xorriso", "unsquashfs", "qemu-img", "guestfish"):
            if not shutil.which(tool):
                raise RuntimeError(f"required validation tool unavailable: {tool}")
        if shutil.which("qemu-img"):
            oc = Path(__file__).resolve().parents[1] / "templates/OpenCore.qcow2"
            command("qemu-img", "check", str(oc))
        tmp_parent = Path(os.environ.get("TMPDIR", "/var/tmp"))
        if not tmp_parent.is_dir() or not os.access(tmp_parent, os.W_OK):
            tmp_parent = None
        with tempfile.TemporaryDirectory(dir=tmp_parent) as td:
            temp = Path(td)
            # Check El Torito catalog includes a UEFI boot entry before extraction.
            catalog = command("xorriso", "-indev", str(iso), "-report_el_torito", "as_mkisofs").stdout
            if "-e" not in catalog or not any(token in catalog for token in ("appended_partition_", "efiboot.img", "eltorito.img")):
                raise RuntimeError("ISO El Torito catalog has no UEFI boot entry")
            # Extract the complete ISO tree; paths returned by xorriso are absolute in ISO.
            iso_root = temp / "iso"
            iso_root.mkdir()
            command("xorriso", "-osirrox", "on", "-indev", str(iso), "-extract", "/", str(iso_root))
            root = temp / "iso"
            squashfs = next(root.rglob("airootfs.sfs"), None)
            if squashfs is None:
                raise RuntimeError("ISO has no airootfs.sfs")
            command("unsquashfs", "-no-progress", "-no-xattrs", "-d", str(temp / "rootfs"), str(squashfs))
            rootfs = temp / "rootfs"
            for required_path in (
                "opt/vhackintosh/core/smbios.py",
                "opt/vhackintosh/resources/macserial/macserial",
                "opt/vhackintosh/resources/macserial/LICENSE.txt",
                "opt/vhackintosh/resources/macserial/README.md",
                "opt/vhackintosh/templates/config.plist",
                "opt/vhackintosh/templates/OpenCore.qcow2",
                "opt/vhackintosh/templates/OVMF_CODE_4M.fd",
                "opt/vhackintosh/templates/OVMF_VARS.fd",
            ):
                if not (rootfs / required_path).is_file():
                    raise RuntimeError(f"ISO payload missing required file: {required_path}")
            payload = rootfs / "opt/vhackintosh/templates"
            external = Path(__file__).resolve().parents[1] / "templates/config.plist"
            embedded = payload / "config.plist"
            if not embedded.is_file() or embedded.read_bytes() != external.read_bytes():
                raise RuntimeError("embedded OpenCore config.plist differs from sanitized source")
            macserial = rootfs / "opt/vhackintosh/resources/macserial/macserial"
            # Garante permissão de execução localmente para validação, pois unsquashfs/xorriso podem perder o bit em alguns ambientes
            if not macserial.stat().st_mode & 0o111:
                macserial.chmod(macserial.stat().st_mode | 0o111)
            version = command(str(macserial), "-v").stdout + command(str(macserial), "-v").stderr
            if "2.1.8" not in version:
                raise RuntimeError("bundled macserial version differs from pinned 2.1.8")
            vars_file = rootfs / "opt/vhackintosh/templates/OVMF_VARS.fd"
            ovmf_vars_hash = hashlib.sha256(vars_file.read_bytes()).hexdigest()
            expected_vars = Path(__file__).resolve().parents[1] / "templates/OVMF_VARS.fd"
            if vars_file.read_bytes() != expected_vars.read_bytes():
                raise RuntimeError("bundled OVMF_VARS differs from neutral reviewed template")
            image = payload / "OpenCore.qcow2"
            if not image.is_file():
                raise RuntimeError("ISO has no OpenCore.qcow2 template")
            command("qemu-img", "check", str(image))
            with tempfile.TemporaryDirectory() as efi_tmp:
                embedded_plist = Path(efi_tmp) / "config.plist"
                command("guestfish", "--ro", "-a", str(image), "-m", "/dev/sda1", "download", "/EFI/OC/config.plist", str(embedded_plist))
                if embedded_plist.read_bytes() != external.read_bytes():
                    raise RuntimeError("OpenCore.qcow2 internal plist differs from external template")
            parsed = plistlib.loads(embedded.read_bytes())
            kexts = {entry.get("BundlePath"): entry for entry in parsed.get("Kernel", {}).get("Add", [])}
            for required_kext, minimum in (("Lilu.kext", "25.0.0"), ("ReimsVgpu.kext", "25.0.0")):
                entry = kexts.get(required_kext)
                if not entry or not entry.get("Enabled") or entry.get("MinKernel") != minimum or entry.get("MaxKernel") != "25.99.99":
                    raise RuntimeError(f"OpenCore {required_kext} is missing, disabled, or has unsupported kernel bounds")
                listing = command("guestfish", "--ro", "-a", str(image), "-m", "/dev/sda1", "find", f"/EFI/OC/Kexts/{required_kext}").stdout
                if not listing.strip():
                    raise RuntimeError(f"OpenCore image is missing {required_kext}")
            for section_name in ("DataHub", "Generic", "PlatformNVRAM", "SMBIOS"): 
                section = parsed["PlatformInfo"][section_name]
                for key in ("SystemSerialNumber", "BoardSerialNumber", "MLB", "ROM", "SystemUUID"):
                    if key not in section:
                        continue
                    value = section[key]
                    if key == "SystemUUID" and value != "00000000-0000-0000-0000-000000000000":
                        raise RuntimeError("non-generic SystemUUID in ISO OpenCore config")
                    if key == "ROM" and value and any(value):
                        raise RuntimeError("non-neutral ROM in ISO OpenCore config")
                    if key not in ("SystemUUID", "ROM") and value:
                        raise RuntimeError("non-empty serial field in ISO OpenCore config")
            opencore = payload / "OpenCore.qcow2"
            provenance = (rootfs / "opt/reims-vgpu/SOURCE-PROVENANCE.txt").read_text().splitlines()
            provenance_data = dict(line.split("=", 1) for line in provenance if "=" in line)
            current_head = command("git", "-C", str(source), "rev-parse", "HEAD").stdout.strip()
            current_qemu = command("git", "-C", str(source / "vendor/qemu"), "rev-parse", "HEAD").stdout.strip()
            if provenance_data.get("commit") != current_head or provenance_data.get("qemu") != current_qemu:
                raise RuntimeError("packaged ReimsVgpu provenance does not match validated source commits")
            manifest = {
                "iso_sha256": hashlib.sha256(iso.read_bytes()).hexdigest(),
                "reims_git_head": current_head,
                "reims_git_branch": provenance_data.get("branch"),
                "source_dirty_allowed": provenance_data.get("dirty_allowed") == "1",
                "qemu_gitlink": current_qemu,
                "cargo_lock_sha256": hashlib.sha256((source / "Cargo.lock").read_bytes()).hexdigest(),
                "airootfs_sfs_sha256": hashlib.sha256(squashfs.read_bytes()).hexdigest(),
                "opencore_sha256": hashlib.sha256(opencore.read_bytes()).hexdigest(),
                "config_sha256": hashlib.sha256(embedded.read_bytes()).hexdigest(),
                "macserial_sha256": hashlib.sha256(macserial.read_bytes()).hexdigest(),
                "ovmf_vars_sha256": ovmf_vars_hash,
                "checks": ["squashfs-extracted", "opencore-qcow2", "plist-neutral", "source-complete", "macserial-pinned", "ovmf-vars-match"],
            }
        # Atomic metadata publication only after all checks complete.
        target = iso.with_suffix(iso.suffix + ".manifest.json")
        temp_target = target.with_name(target.name + f".{os.getpid()}.tmp")
        temp_target.write_text(json.dumps(manifest, indent=2) + "\n")
        temp_target.replace(target)
        print(json.dumps(manifest, indent=2))
        return 0
    except Exception as exc:
        print(f"ISO validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
