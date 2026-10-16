#!/usr/bin/env python3
"""A ISO precisa nascer com tudo o que o fork do reims-vgpu exige por nome.

**Por que este arquivo existe.** O `scripts/install-ctl.sh` do fork confere, antes
de gastar ~5 min compilando, uma lista de pacotes (`pacman_pkgs`) e instala o que
falta com `sudo pacman -S`. No appliance isso não funciona: a ISO é uma imagem
**live** do archiso, que não leva as bases de sincronização do pacman. O
resultado real (ISO 1.1.0, 16/10) foi

    ==> Missing packages: cmake libxss libdecor
    warning: database file for 'core' does not exist (use '-Sy' to download)
    error: target not found: cmake

isto é: sem rede (ou sem `-Sy`) não há como instalar nada em runtime — e o
appliance pode, por projeto, não ter internet no primeiro boot (as crates Rust
vão vendorizadas justamente por isso).

A correção tem duas pontas:

1. todo nome que o script pede está declarado em `archiso/packages.x86_64`;
2. este módulo prova isso **antes** do `mkarchiso` (chamado pelo `build-iso.sh`)
   e o `validate_iso.py` prova, no fim, que a imagem gerada tem os arquivos.

`PACMAN_MARKERS` é o mapa "pacote → arquivo que só ele instala", usado pelo
`validate_iso.py` sobre o rootfs extraído. Os caminhos foram conferidos num host
Arch/CachyOS; um marcador ausente falha o build com a mensagem exata do que
declarar, em vez de quebrar a compilação do usuário 20 minutos depois.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, Sequence

#: Nome do array em `scripts/install-ctl.sh` que lista os pacotes do Arch.
PACMAN_ARRAY = "pacman_pkgs"

_ARRAY_RE = re.compile(rf"^\s*{PACMAN_ARRAY}=\((.*?)\)\s*$", re.S | re.M)
_COMMENT_RE = re.compile(r"#[^\n]*")
_NAME_RE = re.compile(r"^[A-Za-z0-9@._+-]+$")

#: Um arquivo que **só** o pacote da chave instala, relativo ao rootfs da ISO.
#: Usado pelo `validate_iso.py` no squashfs extraído (não no host de build): a
#: pergunta que importa é "a imagem leva isso?", não "meu host tem isso?".
PACMAN_MARKERS: dict[str, str] = {
    "base-devel": "usr/bin/make",
    "git": "usr/bin/git",
    "curl": "usr/bin/curl",
    "python": "usr/bin/python",
    "ninja": "usr/bin/ninja",
    "pkgconf": "usr/bin/pkgconf",
    "flex": "usr/bin/flex",
    "bison": "usr/bin/bison",
    "bzip2": "usr/bin/bzip2",
    "cmake": "usr/bin/cmake",
    "clang": "usr/bin/clang",
    "lld": "usr/bin/ld.lld",
    "dtc": "usr/bin/dtc",
    "pipewire": "usr/bin/pipewire",
    "dbus": "usr/bin/dbus-daemon",
    "glib2": "usr/lib/libglib-2.0.so.0",
    "pixman": "usr/lib/libpixman-1.so.0",
    "libslirp": "usr/lib/libslirp.so.0",
    "zlib": "usr/lib/libz.so.1",
    "libpulse": "usr/lib/libpulse.so.0",
    "alsa-lib": "usr/lib/libasound.so.2",
    "vulkan-icd-loader": "usr/lib/libvulkan.so.1",
    "libx11": "usr/lib/libX11.so.6",
    "libxext": "usr/lib/libXext.so.6",
    "libxrandr": "usr/lib/libXrandr.so.2",
    "libxcursor": "usr/lib/libXcursor.so.1",
    "libxi": "usr/lib/libXi.so.6",
    "libxfixes": "usr/lib/libXfixes.so.3",
    "libxss": "usr/lib/libXss.so.1",
    "libxtst": "usr/lib/libXtst.so.6",
    "libxkbcommon": "usr/lib/libxkbcommon.so.0",
    "wayland": "usr/lib/libwayland-client.so.0",
    "libdecor": "usr/lib/libdecor-0.so.0",
    "mesa": "usr/lib/libGLX_mesa.so.0",
    "libdrm": "usr/lib/libdrm.so.2",
    "systemd-libs": "usr/lib/libsystemd.so.0",
    "gtk3": "usr/lib/libgtk-3.so.0",
}

DEFAULT_SOURCE_CANDIDATES = ("/opt/reims-vgpu", "~/reims-vgpu", "/home/felipeab10/reims-vgpu")

_REMEDY = (
    "Adicione os nomes em appliance/archiso/packages.x86_64: o appliance é uma "
    "imagem live, e sem /var/lib/pacman/sync o `pacman -S` do install-ctl.sh não "
    "instala nada em runtime (e pode não haver rede no primeiro boot)."
)


def parse_pacman_packages(script_text: str) -> list[str]:
    """Extrai `pacman_pkgs=(...)` de um `install-ctl.sh`, na ordem do arquivo.

    Devolve lista vazia quando o array não existe (árvore antiga, sem o script
    novo) — quem chama decide se isso é erro.
    """
    match = _ARRAY_RE.search(_COMMENT_RE.sub("", script_text))
    if match is None:
        return []
    return [token for token in match.group(1).split() if _NAME_RE.match(token)]


def parse_declared_packages(profile_text: str) -> list[str]:
    """Nomes declarados num `packages.x86_64` do archiso (ignora comentários)."""
    declared: list[str] = []
    for line in profile_text.splitlines():
        name = line.split("#", 1)[0].strip()
        if name:
            declared.append(name)
    return declared


def missing_packages(required: Iterable[str], declared: Iterable[str]) -> list[str]:
    """Os exigidos que não estão declarados, sem repetição e na ordem de origem."""
    known = set(declared)
    seen: set[str] = set()
    missing: list[str] = []
    for name in required:
        if name not in known and name not in seen:
            seen.add(name)
            missing.append(name)
    return missing


def unmapped_packages(packages: Iterable[str]) -> list[str]:
    """Exigidos sem marcador em `PACMAN_MARKERS` (o validate_iso não os provaria)."""
    return [name for name in dict.fromkeys(packages) if name not in PACMAN_MARKERS]


def missing_markers(rootfs: Path, packages: Iterable[str]) -> list[tuple[str, str]]:
    """Pacotes cujo marcador não existe no rootfs extraído da ISO."""
    absent: list[tuple[str, str]] = []
    for name in dict.fromkeys(packages):
        marker = PACMAN_MARKERS.get(name)
        if marker is None:
            absent.append((name, "<sem marcador em PACMAN_MARKERS>"))
            continue
        if not any(rootfs.glob(marker)):
            absent.append((name, marker))
    return absent


def find_source(explicit: str | None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend(Path(candidate).expanduser() for candidate in DEFAULT_SOURCE_CANDIDATES)
    for candidate in candidates:
        if (candidate / "scripts" / "install-ctl.sh").is_file():
            return candidate
    return None


def check(source: Path | None, profile: Path) -> tuple[list[str], list[str]]:
    """(faltando no perfil, exigidos sem marcador). Vazio/vazio = tudo certo."""
    profile_file = profile / "packages.x86_64" if profile.is_dir() else profile
    if not profile_file.is_file():
        raise FileNotFoundError(f"packages.x86_64 não encontrado: {profile_file}")
    declared = parse_declared_packages(profile_file.read_text(encoding="utf-8"))
    if source is None:
        return [], []
    script = source / "scripts" / "install-ctl.sh"
    if not script.is_file():
        return [], []
    required = parse_pacman_packages(script.read_text(encoding="utf-8"))
    return missing_packages(required, declared), unmapped_packages(required)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reims-src", default=None, help="árvore do reims-vgpu com scripts/install-ctl.sh")
    parser.add_argument("--profile", required=True, help="perfil do archiso (ou o packages.x86_64)")
    args = parser.parse_args(argv)

    profile = Path(args.profile)
    source = find_source(args.reims_src)
    if source is None:
        print("check_build_deps: nenhuma árvore do reims-vgpu encontrada; nada a verificar")
        return 0

    try:
        missing, unmapped = check(source, profile)
    except FileNotFoundError as exc:
        print(f"check_build_deps: {exc}", file=sys.stderr)
        return 1

    script = source / "scripts" / "install-ctl.sh"
    if not parse_pacman_packages(script.read_text(encoding="utf-8")):
        print(f"check_build_deps: {script} não declara {PACMAN_ARRAY}; nada a verificar")
        return 0

    failed = False
    if missing:
        failed = True
        print(
            f"check_build_deps: {script} exige {len(missing)} pacote(s) que o perfil não declara:",
            file=sys.stderr,
        )
        print(f"  {' '.join(missing)}", file=sys.stderr)
        print(f"  {_REMEDY}", file=sys.stderr)
    if unmapped:
        failed = True
        print(
            "check_build_deps: sem marcador em PACMAN_MARKERS (o validate_iso.py não "
            "conseguiria provar que a ISO os leva):",
            file=sys.stderr,
        )
        print(f"  {' '.join(unmapped)}", file=sys.stderr)
    if failed:
        return 1

    print(f"check_build_deps: perfil cobre todas as dependências de {source}/scripts/install-ctl.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
