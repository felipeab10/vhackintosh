"""A ISO precisa nascer com as dependências de build que o reims-vgpu exige.

Regressão de 16/10 (ISO 1.1.0): o `install-ctl.sh` do fork tentou
`sudo pacman -S --needed cmake libxss libdecor` dentro do appliance e o pacman
respondeu "database file for 'core' does not exist" + "target not found" — uma
imagem live do archiso não leva `/var/lib/pacman/sync`, e o primeiro boot pode
estar offline. O build morria antes de compilar uma linha.

A regra que estes testes guardam: todo nome em `pacman_pkgs` (o array que o
script confere) está declarado em `archiso/packages.x86_64`, e todo nome tem um
marcador de arquivo para o `validate_iso.py` provar que está dentro do squashfs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "appliance"))

import check_build_deps as deps  # noqa: E402

PROFILE_DIR = ROOT / "appliance" / "archiso"
REIMS_SRC = Path.home() / "reims-vgpu"
REIMS_CTL = REIMS_SRC / "scripts" / "install-ctl.sh"

SAMPLE = """#!/usr/bin/env bash
set -euo pipefail
if true; then
  apt_pkgs=(build-essential cmake libxss-dev)
  # pacman_pkgs=(this one is commented out)
  pacman_pkgs=(base-devel git curl cmake
    libxss libdecor gtk3)
fi
"""


def _declared() -> set[str]:
    return set(deps.parse_declared_packages((PROFILE_DIR / "packages.x86_64").read_text()))


# --------------------------------------------------------------------------- parsers

def test_parses_pacman_array_across_lines():
    assert deps.parse_pacman_packages(SAMPLE) == [
        "base-devel",
        "git",
        "curl",
        "cmake",
        "libxss",
        "libdecor",
        "gtk3",
    ]


def test_parses_only_the_pacman_array_not_the_apt_one():
    names = deps.parse_pacman_packages(SAMPLE)
    assert "build-essential" not in names
    assert "libxss-dev" not in names


def test_missing_array_is_empty_not_an_error():
    assert deps.parse_pacman_packages("#!/bin/sh\necho hi\n") == []
    assert deps.parse_pacman_packages("pacman_pkgs=( # nothing\n") == []


def test_declared_packages_ignores_comments_and_blanks():
    declared = deps.parse_declared_packages("cmake\n\n# nota\nlibxss  # inline\n")
    assert declared == ["cmake", "libxss"]


def test_missing_packages_keeps_order_and_drops_duplicates():
    assert deps.missing_packages(["a", "b", "a", "c"], ["a"]) == ["b", "c"]


# --------------------------------------------------------------------------- o perfil real

def test_profile_declares_the_packages_that_broke_the_appliance():
    declared = _declared()
    for name in ("cmake", "libxss", "libdecor"):
        assert name in declared, f"{name} precisa estar em archiso/packages.x86_64"


def test_profile_declares_every_package_install_ctl_checks():
    if not REIMS_CTL.is_file():
        pytest.skip("árvore do reims-vgpu ausente neste host")
    missing, unmapped = deps.check(REIMS_SRC, PROFILE_DIR)
    assert missing == [], f"o perfil não declara: {' '.join(missing)}"
    assert unmapped == [], f"sem marcador em PACMAN_MARKERS: {' '.join(unmapped)}"


def test_install_ctl_does_not_rely_on_a_transitive_dependency():
    """Nenhum nome do script pode ficar de fora só por "já vem de carona"."""
    if not REIMS_CTL.is_file():
        pytest.skip("árvore do reims-vgpu ausente neste host")
    required = deps.parse_pacman_packages(REIMS_CTL.read_text())
    assert len(required) > 20
    assert deps.missing_packages(required, _declared()) == []


# --------------------------------------------------------------------------- marcadores da ISO

def test_every_required_package_has_a_rootfs_marker():
    if not REIMS_CTL.is_file():
        pytest.skip("árvore do reims-vgpu ausente neste host")
    required = deps.parse_pacman_packages(REIMS_CTL.read_text())
    assert deps.unmapped_packages(required) == []


def test_missing_markers_finds_absent_file(tmp_path):
    assert deps.missing_markers(tmp_path, ["cmake"]) == [("cmake", "usr/bin/cmake")]


def test_missing_markers_accepts_matching_glob(tmp_path):
    lib = tmp_path / "usr" / "lib"
    lib.mkdir(parents=True)
    (lib / "libXss.so.1").write_bytes(b"")
    assert deps.missing_markers(tmp_path, ["libxss"]) == []
    assert deps.missing_markers(tmp_path, ["libdecor"]) == [("libdecor", "usr/lib/libdecor-0.so.0")]


def test_missing_markers_reports_a_package_without_marker(tmp_path):
    assert deps.missing_markers(tmp_path, ["pacote-novo"]) == [
        ("pacote-novo", "<sem marcador em PACMAN_MARKERS>")
    ]


# --------------------------------------------------------------------------- fiação no build

def test_build_iso_checks_the_dependencies_before_mkarchiso():
    text = (ROOT / "appliance" / "build-iso.sh").read_text()
    assert "check_build_deps.py" in text
    assert text.index("check_build_deps.py") < text.index("mkarchiso -v -w")
    assert '--reims-src "${REIMS_SRC}"' in text


def test_validate_iso_proves_the_markers_inside_the_image():
    text = (ROOT / "appliance" / "validate_iso.py").read_text()
    assert "missing_markers" in text
    assert "parse_pacman_packages" in text
    assert "reims-build-deps" in text


# ------------------------------------------------------- reims-util dentro da ISO
#
# Sem o binário na imagem o OpenCore é provisionado por template + guestfish: o
# appliance compila o reims-vgpu offline, mas o `reims-ctl __record-install`
# instala o reims-util com `cargo install --git` (rede), e o primeiro boot pode
# estar offline. Estes testes executam o resolvedor de verdade, com HOME e PATH
# isolados, e guardam a fiação do build e da validação.

RESOLVER = ROOT / "appliance" / "find_reims_util.sh"


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def _resolve(tmp_path: Path, **env: str):
    import os
    import shutil
    import subprocess

    complete = {"HOME": str(tmp_path / "home"), "PATH": str(tmp_path / "empty")}
    complete.update(env)
    return subprocess.run(
        [shutil.which("bash") or "bash", str(RESOLVER)],
        env=complete,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )


def test_resolver_prefers_the_binary_installed_beside_reims_ctl(tmp_path):
    fresh = _executable(tmp_path / "cargo" / "bin" / "reims-util")
    _executable(tmp_path / "usr-local-bin" / "reims-util")
    result = _resolve(
        tmp_path,
        CARGO_HOME=str(tmp_path / "cargo"),
        PATH=str(tmp_path / "usr-local-bin"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(fresh)


def test_resolver_falls_back_to_the_path(tmp_path):
    baked = _executable(tmp_path / "usr-local-bin" / "reims-util")
    result = _resolve(tmp_path, PATH=str(tmp_path / "usr-local-bin"))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(baked)


def test_resolver_fails_loudly_when_the_host_has_none(tmp_path):
    result = _resolve(tmp_path)
    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_resolver_honours_an_explicit_override(tmp_path):
    explicit = _executable(tmp_path / "explicit-reims-util")
    result = _resolve(tmp_path, REIMS_UTIL_HOST_BIN=str(explicit))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(explicit)


def test_resolver_rejects_a_broken_override(tmp_path):
    result = _resolve(tmp_path, REIMS_UTIL_HOST_BIN=str(tmp_path / "nao-existe"))
    assert result.returncode == 1
    assert "REIMS_UTIL_HOST_BIN" in result.stderr


def test_resolver_finds_the_binary_under_the_invoking_users_home_after_sudo(tmp_path):
    """A ISO 1.1.0 saiu sem o binário porque o build roda como root.

    Com `sudo`, `$HOME` é `/root`; o reims-util estava em
    `/home/felipeab10/.cargo/bin/reims-util`. O registro de SUDO_USER resolve.
    """
    user_home = tmp_path / "home" / "felipe"
    installed = _executable(user_home / ".cargo" / "bin" / "reims-util")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    getent = fake_bin / "getent"
    getent.write_text(f"#!/bin/sh\necho 'felipe:x:1000:1000::{user_home}:/bin/bash'\n")
    getent.chmod(0o755)

    result = _resolve(
        tmp_path,
        HOME=str(tmp_path / "root"),
        PATH=f"{fake_bin}:/usr/bin",
        SUDO_USER="felipe",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(installed)


def test_resolver_without_sudo_user_does_not_look_into_other_homes(tmp_path):
    """Guarda a regressão: sem SUDO_USER o home de quem chamou não é visto."""
    user_home = tmp_path / "home" / "felipe"
    _executable(user_home / ".cargo" / "bin" / "reims-util")

    result = _resolve(tmp_path, HOME=str(tmp_path / "root"))

    assert result.returncode == 1


def test_build_iso_packages_reims_util_or_refuses_to_build():
    text = (ROOT / "appliance" / "build-iso.sh").read_text()
    assert "find_reims_util.sh" in text
    assert 'install -m 0755 "${REIMS_UTIL_HOST_BIN}"' in text
    assert "airootfs/usr/local/bin/reims-util" in text
    # A cópia só acontece depois de o resolvedor ter encontrado o binário.
    assert text.index("find_reims_util.sh") < text.index('install -m 0755 "${REIMS_UTIL_HOST_BIN}"')
    assert "exit 1" in text[text.index("find_reims_util.sh") :]


def test_validate_iso_requires_the_baked_reims_util():
    text = (ROOT / "appliance" / "validate_iso.py").read_text()
    assert "usr/local/bin/reims-util" in text
    assert "reims_util_sha256" in text
    assert "reims-util-packaged" in text
