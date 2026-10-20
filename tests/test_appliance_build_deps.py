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

import re
import shutil
import subprocess
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


def _bloco_do_reims_util(ate_o_install: bool) -> str:
    """Recorte CONTÍGUO do bloco do reims-util no build-iso.sh.

    Contíguo de propósito: um recorte fatiado mediria uma região diferente da
    que o script executa, e uma mutação fora do corte mudaria o arquivo com o
    instrumento verde (foi o que aconteceu no achado H do verifier).
    """
    linhas = (ROOT / "appliance" / "build-iso.sh").read_text(encoding="utf-8").splitlines()
    inicio = next(
        i for i, ln in enumerate(linhas)
        if ln.strip().startswith("if ! REIMS_UTIL_HOST_BIN=")
    )
    if ate_o_install:
        fim = next(
            i for i in range(inicio, len(linhas))
            if linhas[i].strip().startswith('install -m 0755 "${REIMS_UTIL_HOST_BIN}"')
        )
    else:
        fim = next(i for i in range(inicio + 1, len(linhas)) if linhas[i].strip() == "fi")
    return "\n".join(linhas[inicio : fim + 1])


def test_the_reims_util_install_block_actually_installs_it(tmp_path):
    """Roda o bloco de verdade e confere que o reims-util CHEGA ao airootfs.

    O teste acima checa o PREFIXO de uma linha literal
    (`'install -m 0755 "${REIMS_UTIL_HOST_BIN}"' in text`). Isso prende um diff,
    não um comportamento, e foi medido: envolver o `install` num `if` — o pior
    caso, porque o teste segue verde enquanto o reims-util some da ISO e o
    OpenCore volta a ser provisionado por template+guestfish no primeiro boot —
    passa despercebido. Idem quebrar a linha com `\\` de continuação.

    Então aqui o bloco é executado: do `if ! REIMS_UTIL_HOST_BIN=` até o
    `install`, com o `find_reims_util.sh` dublando sucesso, e o destino é
    conferido em disco. Se o `install` for removido, comentado ou escondido
    atrás de um `if`, o destino fica vazio e isto falha.
    """
    import os
    import stat
    import subprocess

    bloco = _bloco_do_reims_util(ate_o_install=True)

    # Precondições do recorte: se um marcador sumir, o teste mediria um trecho
    # qualquer e passaria sem exercitar nada.
    assert "find_reims_util.sh" in bloco, "o recorte perdeu o resolvedor"
    assert "install -m 0755" in bloco, "o recorte perdeu o install"
    assert "reims-util" in bloco, "o recorte não nomeia o destino"

    fonte = tmp_path / "reims-util"
    fonte.write_text("#!/bin/sh\necho reims-util\n", encoding="utf-8")
    fonte.chmod(0o755)

    perfil = tmp_path / "perfil"
    (perfil / "airootfs" / "usr" / "local" / "bin").mkdir(parents=True)

    # Resolvedor dublado. O bloco o invoca como `bash "${SCRIPT_DIR}/..."` e usa
    # o stdout como caminho. Dublar aqui é o certo: o resolvedor tem testes
    # próprios, e o que este teste isola é a INSTALAÇÃO.
    script_dir = tmp_path / "appliance"
    script_dir.mkdir()
    (script_dir / "find_reims_util.sh").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "{fonte}"\n', encoding="utf-8"
    )

    harness = tmp_path / "t.sh"
    harness.write_text(
        "set -euo pipefail\n"
        # Toda variável que o bloco lê, senão `set -u` falha pelo motivo errado
        # e a falha passa por cobertura.
        f'SCRIPT_DIR="{script_dir}"\n'
        f'ARCHISO_PROFILE="{perfil}"\n'
        'RED=""; BLUE=""; CYAN=""; NC=""\n'
        + bloco
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    assert proc.returncode == 0, f"o bloco falhou:\n{proc.stdout}{proc.stderr}"

    instalado = perfil / "airootfs" / "usr" / "local" / "bin" / "reims-util"
    assert instalado.is_file(), (
        "o reims-util não chegou ao airootfs — a ISO sairia sem ele e o "
        f"OpenCore cairia para o caminho de template. stdout={proc.stdout!r}"
    )
    assert os.access(instalado, os.X_OK), (
        "instalado sem bit de execução; o mkarchiso copia com "
        "--no-preserve=mode e o validate_iso reprova"
    )
    assert instalado.read_bytes() == fonte.read_bytes(), "conteúdo diferente da fonte"
    assert stat.S_IMODE(instalado.stat().st_mode) == 0o755


def test_the_reims_util_block_refuses_when_the_resolver_fails(tmp_path):
    """O contraponto: sem o reims-util o build PARA, não segue sem ele.

    Sem isto, apagar o `exit 1` passaria no teste acima — e a ISO sairia sem o
    utilitário enquanto o build dizia "pronto".
    """
    import subprocess

    bloco = _bloco_do_reims_util(ate_o_install=False)
    assert "exit 1" in bloco, "o recorte perdeu a recusa"

    script_dir = tmp_path / "appliance"
    script_dir.mkdir()
    # Falha como o resolvedor real falha quando não acha nada.
    (script_dir / "find_reims_util.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    harness = tmp_path / "t.sh"
    harness.write_text(
        "set -euo pipefail\n"
        f'SCRIPT_DIR="{script_dir}"\n'
        f'ARCHISO_PROFILE="{tmp_path / "perfil"}"\n'
        'RED=""; BLUE=""; CYAN=""; NC=""\n'
        + bloco
        + '\necho "NAO DEVERIA CHEGAR AQUI"\n',
        encoding="utf-8",
    )

    proc = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    assert proc.returncode == 1, (
        f"sem reims-util o build tinha de parar com 1:\n{proc.stdout}{proc.stderr}"
    )
    assert "NAO DEVERIA" not in proc.stdout, "o script seguiu sem o reims-util"


def test_validate_iso_requires_the_baked_reims_util():
    text = (ROOT / "appliance" / "validate_iso.py").read_text()
    assert "usr/local/bin/reims-util" in text
    assert "reims_util_sha256" in text
    assert "reims-util-packaged" in text


# --------------------------------------------------------------------------- modos na ISO
#
# Regressão de 16/10 (primeira ISO com o provisionamento nativo): o build
# terminou, o mkarchiso gerou a imagem e o validate_iso reprovou com
# "bundled reims-util is not executable in ISO rootfs". O mkarchiso copia o
# airootfs com `cp -af --no-preserve=ownership,mode` — o que zera os bits de
# execução de TODO arquivo — e só reaplica os modos declarados em
# `file_permissions` do profiledef.sh. O reims-util não estava na lista.
# A lista não aceita glob, então cada arquivo precisa da sua linha.

def _declared_permission_paths() -> set[str]:
    text = (PROFILE_DIR / "profiledef.sh").read_text()
    return set(re.findall(r'\["(/[^"]+)"\]\s*=', text))


def _paths_landing_in_usr_local_bin() -> set[str]:
    """Os arquivos do perfil e os que o build-iso.sh instala em tempo de build."""
    source = PROFILE_DIR / "airootfs" / "usr" / "local" / "bin"
    names = {path.name for path in source.iterdir() if path.is_file()}
    build = (ROOT / "appliance" / "build-iso.sh").read_text()
    names |= set(re.findall(r"airootfs/usr/local/bin/([A-Za-z0-9._+-]+)", build))
    return names


def test_every_file_in_usr_local_bin_declares_its_mode():
    declared = _declared_permission_paths()
    missing = sorted(
        name
        for name in _paths_landing_in_usr_local_bin()
        if f"/usr/local/bin/{name}" not in declared
    )
    assert not missing, (
        "sem entrada em file_permissions estes arquivos chegam 0644 na ISO: "
        + ", ".join(missing)
    )


MKARCHISO_PERMISSIONS_LOOP = r"""
set -euo pipefail
profile_airootfs="$1"
pacstrap_dir="$2"
profiledef="$3"
umask 022
# Mesma linha do _prepare_airootfs do /usr/bin/mkarchiso.
cp -af --no-preserve=ownership,mode -- "${profile_airootfs}/." "${pacstrap_dir}"
# Como no mkarchiso (linha 48), o array é associativo antes de o perfil preenchê-lo.
declare -A file_permissions=()
source "${profiledef}"
for filename in "${!file_permissions[@]}"; do
    IFS=':' read -ra permissions <<<"${file_permissions[${filename}]}"
    if [ "${filename}" = "${filename%/}" ]; then
        chmod -f -- "${permissions[2]}" "${pacstrap_dir}${filename}" 2>/dev/null || true
    else
        chmod -fR -- "${permissions[2]}" "${pacstrap_dir}${filename}" 2>/dev/null || true
    fi
done
stat -c '%a' "${pacstrap_dir}/usr/local/bin/reims-util"
"""


def _emulate_mkarchiso(tmp_path: Path, profiledef: Path) -> str:
    airootfs = tmp_path / "profile" / "airootfs" / "usr" / "local" / "bin"
    airootfs.mkdir(parents=True)
    # O conteúdo é irrelevante: o alvo da emulação é o bit de execução.
    baked = airootfs / "reims-util"
    shutil.copyfile(PROFILE_DIR / "airootfs" / "usr" / "local" / "bin" / "vhackintosh", baked)
    baked.chmod(0o755)

    pacstrap = tmp_path / "pacstrap"
    pacstrap.mkdir()
    result = subprocess.run(
        ["bash", "-c", MKARCHISO_PERMISSIONS_LOOP, "emul", str(tmp_path / "profile" / "airootfs"), str(pacstrap), str(profiledef)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_mkarchiso_loop_restores_the_exec_bit_of_the_baked_reims_util(tmp_path):
    assert _emulate_mkarchiso(tmp_path, PROFILE_DIR / "profiledef.sh") == "755"


def test_without_the_entry_the_binary_would_reach_the_iso_as_0644(tmp_path):
    """Prova que o teste acima não passa à toa: sem a linha, o loop não salva."""
    stripped = tmp_path / "profiledef.sh"
    stripped.write_text(
        (PROFILE_DIR / "profiledef.sh")
        .read_text()
        .replace('  ["/usr/local/bin/reims-util"]="0:0:755"\n', "")
    )
    assert _emulate_mkarchiso(tmp_path, stripped) == "644"


# ------------------------------------- provenance do reims-vgpu dentro da ISO
#
# A ISO tem DOIS modos e cada um promete uma coisa diferente. O modo fontes
# carrega o código de um commit exato, e aí a provenance tem de bater com o HEAD
# do repositório. O modo pré-compilado não carrega fonte nenhuma: carrega o QEMU
# e a ROM de uma RELEASE publicada.
#
# O validador exigia `commit`/`qemu` da provenance nos dois casos. Como a
# provenance prebuilt não tem essas chaves, ele caía no default "HEAD" e
# comparava `None != <sha do HEAD vivo>` — sempre verdadeiro. Resultado: TODA
# ISO em modo pré-compilado era reprovada no fim da build, com a mensagem
# "packaged ReimsVgpu provenance does not match validated source commits", e o
# `.sha256`/manifesto nunca saíam. Não dependia de release, tag nem commit: era
# determinístico.
#
# Estes testes rodam a FUNÇÃO de verdade, com a árvore do rootfs montada em
# disco. `assertIn` sobre o texto do validador não distingue "confere a release"
# de "confere o HEAD vivo", que era exatamente o bug.

import importlib.util  # noqa: E402

VALIDATE = ROOT / "appliance" / "validate_iso.py"


def _carrega_validador():
    spec = importlib.util.spec_from_file_location("_validate_iso_under_test", VALIDATE)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _raiz_prebuilt(tmp_path: Path, *, prov: dict[str, str], release_no_pacote: str | None = None,
                   com_qemu: bool = True, com_rom: bool = True) -> Path:
    """Monta o mínimo de rootfs que o ramo prebuilt do validador lê."""
    rootfs = tmp_path / "rootfs"
    (rootfs / "opt/reims-vgpu").mkdir(parents=True)
    linhas = "\n".join(f"{k}={v}" for k, v in prov.items())
    (rootfs / "opt/reims-vgpu/SOURCE-PROVENANCE.txt").write_text(linhas + "\n")
    if release_no_pacote is not None:
        (rootfs / "opt/reims-vgpu/.release").write_text(release_no_pacote + "\n")
    if com_qemu:
        qemu = rootfs / "usr/local/lib/reims-vgpu-qemu/bin/qemu-system-x86_64"
        qemu.parent.mkdir(parents=True)
        qemu.write_bytes(b"\x7fELF qemu do pacote")
    if com_rom:
        rom = rootfs / "usr/local/lib/reims-vgpu-qemu/share/qemu/ReimsVgpu.rom"
        rom.parent.mkdir(parents=True)
        rom.write_bytes(b"\x55\xaa rom do pacote")
    return rootfs


def _confere_prebuilt(tmp_path: Path, **kwargs):
    """Chama a mesma rotina que o `main()` usa para decidir a provenance."""
    modulo = _carrega_validador()
    rootfs = _raiz_prebuilt(tmp_path, **kwargs)
    return modulo.confere_provenance(rootfs, Path("/tmp/reims-vgpu-inexistente"))


def test_prebuilt_iso_is_not_compared_against_the_live_head(tmp_path):
    """O bug: reprovava SEMPRE, porque `None != sha` é sempre verdadeiro."""
    resultado = _confere_prebuilt(
        tmp_path,
        prov={"mode": "prebuilt", "release": "v1.1.1", "repo": "felipeab10/reims-vgpu",
              "asset": "reims-vgpu-linux-x86_64.tar.gz"},
        release_no_pacote="v1.1.1",
    )
    assert resultado.head == "v1.1.1", (
        "o modo prebuilt tem de se identificar pela RELEASE, não pelo HEAD do "
        f"checkout vivo; veio {resultado.head!r}"
    )


def test_prebuilt_iso_without_commit_fields_is_accepted(tmp_path):
    """A provenance prebuilt não tem `commit` nem `qemu`: isso NÃO é erro."""
    prov = {"mode": "prebuilt", "release": "v1.1.0", "repo": "felipeab10/reims-vgpu",
            "asset": "reims-vgpu-linux-x86_64.tar.gz"}
    assert "commit" not in prov and "qemu" not in prov
    resultado = _confere_prebuilt(tmp_path, prov=prov)
    assert resultado.head == "v1.1.0"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo_de_mentira(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Um repositório com `vendor/qemu` de verdade, para o modo fontes.

    O validador resolve os commits por `git rev-parse` no repositório real: um
    sha inventado faria o `CalledProcessError` do git aparecer no lugar da
    checagem que se quer medir. Com um repo próprio o teste não depende nem do
    remoto nem de existir um `vendor/qemu` neste checkout.
    """
    repo = tmp_path / "reims-src"
    (repo / "vendor/qemu").mkdir(parents=True)
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "HOME": str(tmp_path),
    }
    import os as _os

    def commita(caminho: Path, texto: str) -> str:
        (caminho / "arquivo").write_text(texto)
        subprocess.run(["git", "-C", str(caminho), "add", "-A"], check=True,
                       capture_output=True, env={**_os.environ, **env})
        subprocess.run(["git", "-C", str(caminho), "commit", "-qm", texto],
                       check=True, capture_output=True, env={**_os.environ, **env})
        return _git(caminho, "rev-parse", "HEAD")

    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True,
                   capture_output=True, env={**_os.environ, **env})
    subprocess.run(["git", "-C", str(repo / "vendor/qemu"), "init", "-q"],
                   check=True, capture_output=True, env={**_os.environ, **env})
    qemu = commita(repo / "vendor/qemu", "qemu")
    head = commita(repo, "reims")
    return repo, repo / "vendor/qemu", head, qemu


def test_source_mode_still_demands_the_commits(tmp_path):
    """Contraprova: o modo fontes continua exigindo o casamento com o HEAD."""
    modulo = _carrega_validador()
    repo, _, head, qemu = _repo_de_mentira(tmp_path)
    # O build-iso.sh grava o SHA já resolvido na provenance, não a palavra HEAD.
    rootfs = _raiz_prebuilt(
        tmp_path,
        prov={"source": str(repo), "branch": "master", "commit": head,
              "qemu": qemu, "dirty_allowed": "0"},
    )
    resultado = modulo.confere_provenance(rootfs, repo)
    assert resultado.head == head and resultado.qemu == qemu
    assert resultado.prebuilt is False


def test_source_mode_refuses_a_commit_that_does_not_match(tmp_path):
    """E o casamento do modo fontes é mesmo exigido, não decorativo."""
    modulo = _carrega_validador()
    repo, _, head, _qemu = _repo_de_mentira(tmp_path)
    # A provenance declara um commit que NÃO é o HEAD do repositório: é
    # exatamente o caso que o modo fontes tem de reprovar.
    rootfs = _raiz_prebuilt(
        tmp_path,
        prov={"source": str(repo), "branch": "master", "commit": head,
              "qemu": "HEAD", "dirty_allowed": "0"},
    )
    (repo / "arquivo").write_text("mudou sem commitar")
    with pytest.raises(RuntimeError, match="does not match validated source commits"):
        modulo.confere_provenance(rootfs, repo)


def test_a_package_from_another_release_is_refused(tmp_path):
    """Pacote v1.1.1 com provenance dizendo v1.0.0 é erro de empacotamento."""
    with pytest.raises(RuntimeError, match="release does not match provenance"):
        _confere_prebuilt(
            tmp_path,
            prov={"mode": "prebuilt", "release": "v1.0.0", "repo": "felipeab10/reims-vgpu",
                  "asset": "reims-vgpu-linux-x86_64.tar.gz"},
            release_no_pacote="v1.1.1",
        )


def test_prebuilt_iso_without_a_release_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="has no release"):
        _confere_prebuilt(tmp_path, prov={"mode": "prebuilt", "repo": "felipeab10/reims-vgpu"})


def test_prebuilt_iso_from_an_unexpected_repo_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="unexpected repository"):
        _confere_prebuilt(
            tmp_path,
            prov={"mode": "prebuilt", "release": "v1.1.1", "repo": "outro/reims-vgpu"},
        )


def test_prebuilt_iso_without_the_qemu_is_refused(tmp_path):
    """Sem QEMU dentro do rootfs a ISO não sobe VM: é erro, não aviso."""
    with pytest.raises(RuntimeError, match="has no packaged QEMU"):
        _confere_prebuilt(
            tmp_path,
            prov={"mode": "prebuilt", "release": "v1.1.1", "repo": "felipeab10/reims-vgpu"},
            com_qemu=False,
        )


def test_prebuilt_iso_without_the_rom_is_refused(tmp_path):
    """Sem a ROM ao lado do QEMU o binário não acha `romfile ReimsVgpu.rom`."""
    with pytest.raises(RuntimeError, match="ReimsVgpu.rom"):
        _confere_prebuilt(
            tmp_path,
            prov={"mode": "prebuilt", "release": "v1.1.1", "repo": "felipeab10/reims-vgpu"},
            com_rom=False,
        )


def test_the_qemu_and_rom_must_be_in_the_same_prefix(tmp_path):
    """QEMU num prefixo e ROM em outro passaria se a checagem fosse por nome."""
    modulo = _carrega_validador()
    rootfs = _raiz_prebuilt(
        tmp_path,
        prov={"mode": "prebuilt", "release": "v1.1.1", "repo": "felipeab10/reims-vgpu"},
    )
    # Move a ROM para longe do binário.
    rom = rootfs / "usr/local/lib/reims-vgpu-qemu/share/qemu/ReimsVgpu.rom"
    outro = rootfs / "outro/lugar/share/qemu"
    outro.mkdir(parents=True)
    rom.rename(outro / "ReimsVgpu.rom")
    with pytest.raises(RuntimeError, match="ReimsVgpu.rom"):
        modulo.confere_provenance(rootfs, Path("/tmp/reims-vgpu-inexistente"))


# ---------------------------------------------------------------------------
# Portabilidade do QEMU empacotado
# ---------------------------------------------------------------------------

# O bundle é compilado numa estação de trabalho e executado dentro da ISO live,
# cujo conjunto de pacotes é bem menor que o de um desktop. O `configure` do
# QEMU **auto-detecta** várias bibliotecas opcionais e as liga quando as acha no
# host de BUILD — então uma máquina com `brltty` instalado produzia um bundle
# que não iniciava em lugar nenhum.
#
# Medido na v1.2.0 publicada: o binário saía com 62 NEEDED e morria com
# `libbrlapi.so.0.8: cannot open shared object file`. Cruzando com as
# bibliotecas da ISO, faltavam exatamente três de 62: libbrlapi, libiscsi e
# libsndio. As outras 59 já estavam lá.
#
# Estes testes travam o que importa: os backends que a ISO não fornece ficam
# DESABILITADOS explicitamente. Não travam a lista inteira de flags — a geração
# do QEMU muda e um teste preso a "--enable-gtk" só daria manutenção falsa.
_BACKENDS_QUE_A_ISO_NAO_TEM = {
    "brlapi": "libbrlapi.so.0.8 (brltty): display braille, não usado pelo vGPU",
    "libiscsi": "libiscsi.so.11: transporte SCSI remoto, não usado pelo vGPU",
    "sndio": "libsndio.so.7: backend de áudio do BSD, não usado pelo vGPU",
}


def _ramo_linux_do_install_ctl() -> str:
    """O texto do ramo `Linux/x86_64`, onde vive o QEMU_CONFIGURE do appliance."""
    if not REIMS_CTL.is_file():
        pytest.skip("árvore do reims-vgpu ausente neste host")
    texto = REIMS_CTL.read_text(encoding="utf-8")
    inicio = texto.find("Linux/x86_64)")
    if inicio < 0:
        pytest.fail("install-ctl.sh não tem mais o ramo Linux/x86_64")
    fim = texto.find(";;", inicio)
    return texto[inicio:fim if fim > 0 else len(texto)]


@pytest.mark.parametrize("backend", sorted(_BACKENDS_QUE_A_ISO_NAO_TEM))
def test_o_qemu_do_appliance_nao_liga_backend_ausente_na_iso(backend):
    """Cada backend ausente tem de estar desabilitado explicitamente.

    Sem o `--disable-*`, o `configure` liga o backend sempre que a lib existir
    no host de build — e o artefato publicado passa a depender do que estava
    instalado na máquina de quem empacotou.
    """
    ramo = _ramo_linux_do_install_ctl()
    assert f"--disable-{backend}" in ramo, (
        f"o QEMU do appliance pode voltar a exigir {_BACKENDS_QUE_A_ISO_NAO_TEM[backend]}; "
        f"faltou --disable-{backend} no QEMU_CONFIGURE do ramo Linux/x86_64. "
        "O sintoma no usuário é 'error while loading shared libraries' e a VM "
        "não inicia, mesmo com a ISO recém-instalada."
    )


def test_as_tres_ausencias_medidas_continuam_cobertas():
    """Trava o conjunto medido, não só cada flag isolada.

    Este teste falha se alguém remover uma das três — inclusive por reescrever a
    lista de flags e perder uma sem perceber.
    """
    ramo = _ramo_linux_do_install_ctl()
    faltando = [b for b in _BACKENDS_QUE_A_ISO_NAO_TEM if f"--disable-{b}" not in ramo]
    assert not faltando, (
        "backends que a ISO não fornece e voltaram a ser auto-detectados: "
        + ", ".join(sorted(faltando))
    )


def test_o_binario_construido_nao_pede_lib_fora_da_iso():
    """Mede o ARTEFATO, não só a flag: nenhuma NEEDED pode estar fora da ISO.

    A checagem de flag acima é barata e roda sempre; esta prova o resultado no
    binário de verdade. Mede o build ATUAL do `install-ctl.sh` — o prefixo
    rotulado por data em `~/.local/share/reims-ctl/qemu/`, e o symlink
    `current` que o appliance executa.

    De propósito NÃO mede uma release publicada antiga: uma v1.2.0 já no
    GitHub carrega o defeito para sempre e reprovaria a suíte por um artefato
    que o código atual não pode mais corrigir. Quem impede a republicação de um
    bundle ruim é a checagem de flag, que roda sobre o fonte.
    """
    candidatos = [Path.home() / ".local/share/reims-ctl/qemu" / "current" / "bin"
                  / "qemu-system-x86_64"]
    raiz_builds = Path.home() / ".local/share/reims-ctl/qemu"
    if raiz_builds.is_dir():
        # O symlink `current` pode não existir; os prefixos rotulados existem.
        rotulados = sorted(
            (d / "bin" / "qemu-system-x86_64" for d in raiz_builds.iterdir()
             if d.is_dir() and (d / "bin" / "qemu-system-x86_64").is_file()),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        candidatos.extend(rotulados)

    binario = next((c for c in candidatos if c.is_file()), None)
    if binario is None:
        pytest.skip("nenhum binário do QEMU construído neste host para medir")

    objdump = shutil.which("objdump")
    if objdump is None:
        pytest.skip("objdump ausente; não dá para listar NEEDED")

    saida = subprocess.run(
        [objdump, "-p", str(binario)], capture_output=True, text=True, check=True
    ).stdout
    needed = sorted({l.split()[1] for l in saida.splitlines() if "NEEDED" in l})
    assert needed, "objdump não listou nenhuma NEEDED; a medição não é válida"

    # Os três nomes medidos não podem voltar, em nenhuma versão do artefato.
    proibidas = {soname for soname in needed
                 if soname.split(".so")[0] in
                 ("libbrlapi", "libiscsi", "libsndio")}
    assert not proibidas, (
        f"o binário em {binario} ainda pede {sorted(proibidas)}, que a ISO não "
        "tem: a VM morreria com 'error while loading shared libraries'"
    )
