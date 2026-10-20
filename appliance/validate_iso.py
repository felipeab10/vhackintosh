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
from typing import NamedTuple

# O perfil define (e o build-iso.sh já confere) os nomes de pacote que o
# install-ctl.sh exige; aqui provamos que os ARQUIVOS estão dentro da imagem.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_build_deps import missing_markers, parse_pacman_packages  # noqa: E402

EXPECTED_OPENCORE_VIRTUAL_SIZE = 384 * 1024 * 1024


def command(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, capture_output=True, text=True)


def validate_opencore_image(path: Path) -> None:
    """Reject the small native reims-util EFI in place of the ULTMOS image."""
    info = json.loads(command("qemu-img", "info", "--output=json", str(path)).stdout)
    virtual_size = info.get("virtual-size")
    if virtual_size != EXPECTED_OPENCORE_VIRTUAL_SIZE:
        raise RuntimeError(
            f"OpenCore image has virtual size {virtual_size!r}, expected "
            f"{EXPECTED_OPENCORE_VIRTUAL_SIZE} bytes (384 MiB Ultimate-macOS-KVM template)"
        )


class Provenance(NamedTuple):
    """De onde veio o reims-vgpu empacotado, já conferido.

    `prebuilt` distingue os dois contratos: no modo pré-compilado `head` é a
    RELEASE (ex.: `v1.1.1`) e `qemu` é o ativo do pacote; no modo fontes são os
    commits do reims-vgpu e do submódulo do QEMU.
    """

    head: str
    qemu: str
    branch: str | None
    dirty_allowed: bool
    prebuilt: bool


def confere_provenance(rootfs: Path, source: Path) -> Provenance:
    """Confere a provenance do reims-vgpu empacotado e devolve o que declarar.

    São DOIS contratos, e a ISO se declara num deles pelo `mode`:

    - `mode=prebuilt`: o rootfs não tem fonte nenhuma; tem o QEMU e a ROM de uma
      release publicada. Valida-se o que esse modo promete — a release anunciada
      é a empacotada e o QEMU/ROM estão dentro do rootfs, no mesmo prefixo.
    - qualquer outra coisa (`mode=source` ou ausente): o rootfs carrega as
      fontes de um commit exato, então a provenance tem de continuar batendo com
      o HEAD do repositório validado.

    O modo prebuilt não tem `commit`/`qemu` na provenance. Exigir essas chaves
    dele fazia o default `"HEAD"` virar `None != <sha do HEAD vivo>`, que é
    sempre verdadeiro: toda ISO pré-compilada era reprovada no fim da build,
    com uma mensagem que apontava para "commits" que nunca existiram no pacote.
    """
    provenance = (rootfs / "opt/reims-vgpu/SOURCE-PROVENANCE.txt").read_text().splitlines()
    provenance_data = dict(line.split("=", 1) for line in provenance if "=" in line)
    if provenance_data.get("mode") == "prebuilt":
        release = provenance_data.get("release")
        if not release:
            raise RuntimeError(
                "prebuilt ReimsVgpu provenance has no release: " + repr(provenance_data)
            )
        if provenance_data.get("repo") != "felipeab10/reims-vgpu":
            raise RuntimeError(
                "prebuilt ReimsVgpu provenance points at an unexpected repository: "
                f"{provenance_data.get('repo')!r}"
            )
        # O QEMU tem de estar DENTRO do rootfs, na forma que o appliance
        # instala. Sem ele a ISO não sobe VM alguma.
        qemu_in_iso = rootfs / "usr/local/lib/reims-vgpu-qemu/bin/qemu-system-x86_64"
        if not qemu_in_iso.is_file():
            raise RuntimeError(
                "prebuilt ISO has no packaged QEMU at "
                "usr/local/lib/reims-vgpu-qemu/bin/qemu-system-x86_64"
            )
        # E a ROM do vGPU AO LADO dele (mesmo prefixo), com o nome que o
        # binário pede pelo `romfile`.
        rom_in_iso = qemu_in_iso.parent.parent / "share/qemu/ReimsVgpu.rom"
        if not rom_in_iso.is_file():
            raise RuntimeError(
                "prebuilt ISO has no packaged ReimsVgpu.rom next to the QEMU"
            )
        # Se o `build-iso.sh` registrou a release de origem do pacote, ela tem
        # de ser a mesma que a provenance anuncia: um pacote de v1.1.1 com
        # provenance dizendo v1.0.0 é erro de empacotamento.
        origem = rootfs / "opt/reims-vgpu/.release"
        if origem.is_file():
            registrado = origem.read_text(encoding="utf-8").strip()
            if registrado and registrado != release:
                raise RuntimeError(
                    "packaged ReimsVgpu release does not match provenance: "
                    f"pacote={registrado!r} provenance={release!r}"
                )
        return Provenance(
            head=release,
            qemu=provenance_data.get("asset", "prebuilt"),
            branch=None,
            dirty_allowed=False,
            prebuilt=True,
        )
    expected_head = provenance_data.get("commit", "HEAD")
    expected_qemu = provenance_data.get("qemu", "HEAD")
    current_head = command("git", "-C", str(source), "rev-parse", expected_head).stdout.strip()
    current_qemu = command("git", "-C", str(source / "vendor/qemu"), "rev-parse", expected_qemu).stdout.strip()
    if provenance_data.get("commit") != current_head or provenance_data.get("qemu") != current_qemu:
        raise RuntimeError("packaged ReimsVgpu provenance does not match validated source commits")
    return Provenance(
        head=current_head,
        qemu=current_qemu,
        branch=provenance_data.get("branch"),
        dirty_allowed=provenance_data.get("dirty_allowed") == "1",
        prebuilt=False,
    )


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
            validate_opencore_image(oc)
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
                "opt/vhackintosh/core/reims_util.py",
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
            # O appliance compila o reims-vgpu na máquina do usuário, e é uma
            # imagem live: sem /var/lib/pacman/sync o `pacman -S` do
            # install-ctl.sh não instala nada em runtime. Logo, todo pacote que o
            # próprio script empacotado exige por nome tem de estar DENTRO do
            # squashfs (não basta estar declarado no perfil).
            packaged_ctl = rootfs / "opt/reims-vgpu/scripts/install-ctl.sh"
            if packaged_ctl.is_file():
                required_pkgs = parse_pacman_packages(packaged_ctl.read_text(encoding="utf-8"))
                absent = missing_markers(rootfs, required_pkgs)
                if absent:
                    detail = ", ".join(f"{name} ({marker})" for name, marker in absent)
                    raise RuntimeError(
                        "ISO rootfs cannot build reims-vgpu offline, missing package markers: " + detail
                    )
            payload = rootfs / "opt/vhackintosh/templates"
            external = Path(__file__).resolve().parents[1] / "templates/config.plist"
            embedded = payload / "config.plist"
            if not embedded.is_file() or embedded.read_bytes() != external.read_bytes():
                raise RuntimeError("embedded OpenCore config.plist differs from sanitized source")
            macserial = rootfs / "opt/vhackintosh/resources/macserial/macserial"
            if not macserial.stat().st_mode & 0o111:
                raise RuntimeError("bundled macserial is not executable in ISO rootfs")
            version = command(str(macserial), "-v").stdout + command(str(macserial), "-v").stderr
            if "2.1.8" not in version:
                raise RuntimeError("bundled macserial version differs from pinned 2.1.8")
            # O reims-ctl usa o reims-util em operações nativas de OpenCore.
            # A criação pelo wizard parte do template versionado derivado do
            # Ultimate-macOS-KVM e edita o SMBIOS in-place; não substitui essa EFI.
            reims_util = rootfs / "usr/local/bin/reims-util"
            if not reims_util.is_file():
                raise RuntimeError(
                    "ISO payload missing required file: usr/local/bin/reims-util "
                    "(required by reims-ctl's native OpenCore operations)"
                )
            if not reims_util.stat().st_mode & 0o111:
                raise RuntimeError(
                    "bundled reims-util is not executable in ISO rootfs "
                    "(o mkarchiso copia o airootfs com --no-preserve=mode e só reaplica os "
                    'modos listados em file_permissions: falta ["/usr/local/bin/reims-util"]'
                    '="0:0:755" em appliance/archiso/profiledef.sh)'
                )
            try:
                util_version = command(str(reims_util), "--version").stdout.strip()
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(f"bundled reims-util does not run: {exc}") from exc
            if "reims-util" not in util_version:
                raise RuntimeError(f"bundled reims-util reports no version: {util_version!r}")
            vars_file = rootfs / "opt/vhackintosh/templates/OVMF_VARS.fd"
            ovmf_vars_hash = hashlib.sha256(vars_file.read_bytes()).hexdigest()
            expected_vars = Path(__file__).resolve().parents[1] / "templates/OVMF_VARS.fd"
            if vars_file.read_bytes() != expected_vars.read_bytes():
                raise RuntimeError("bundled OVMF_VARS differs from neutral reviewed template")
            image = payload / "OpenCore.qcow2"
            if not image.is_file():
                raise RuntimeError("ISO has no OpenCore.qcow2 template")
            command("qemu-img", "check", str(image))
            validate_opencore_image(image)
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
            # Provenance conferida por uma função própria: o ramo prebuilt e o
            # ramo fontes têm contratos diferentes, e a checagem vivia no meio
            # deste `main()` (onde só dava para testá-la por `assertIn` no
            # texto, que foi como o bug do modo pré-compilado passou).
            proveniencia = confere_provenance(rootfs, source)
            current_head = proveniencia.head
            current_qemu = proveniencia.qemu
            cargo_lock_file = rootfs / "opt/reims-vgpu/Cargo.lock"
            if not cargo_lock_file.exists():
                cargo_lock_file = source / "Cargo.lock"
            manifest = {
                "iso_sha256": hashlib.sha256(iso.read_bytes()).hexdigest(),
                "reims_git_head": current_head,
                "reims_git_branch": proveniencia.branch,
                "source_dirty_allowed": proveniencia.dirty_allowed,
                "qemu_gitlink": current_qemu,
                "cargo_lock_sha256": hashlib.sha256(cargo_lock_file.read_bytes()).hexdigest(),
                "airootfs_sfs_sha256": hashlib.sha256(squashfs.read_bytes()).hexdigest(),
                "opencore_sha256": hashlib.sha256(opencore.read_bytes()).hexdigest(),
                "config_sha256": hashlib.sha256(embedded.read_bytes()).hexdigest(),
                "macserial_sha256": hashlib.sha256(macserial.read_bytes()).hexdigest(),
                "reims_util_sha256": hashlib.sha256(reims_util.read_bytes()).hexdigest(),
                "ovmf_vars_sha256": ovmf_vars_hash,
                "checks": ["squashfs-extracted", "opencore-qcow2", "plist-neutral", "source-complete", "macserial-pinned", "reims-util-packaged", "ovmf-vars-match", "reims-build-deps"],
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
