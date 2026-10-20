"""
vHackintosh — Build sob demanda do reims-vgpu a partir do fork

A ISO passa a levar as **fontes** do reims-vgpu (crates + vendor/qemu + deps
vendorizadas), e o appliance compila na máquina do usuário em vez de carregar um
binário que envelhece em silêncio. O objetivo é o binário em uso **sempre
corresponder** ao fonte:

* se nunca foi compilado  → compila (barra de progresso);
* se já foi compilado e o fonte mudou → recompila;
* se já foi compilado e nada mudou → **não** recompila.

A decisão é tomada por um *stamp* de conteúdo (hash do fonte), não por data: a
ISO não leva o `.git` do reims-vgpu (732 MB) e o do `vendor/qemu` é um gitfile
apontando para fora. O hash dos arquivos é mais confiável que data e não depende
de metadados VCS.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from core.config import LOGS_DIR, CONFIG_DIR

# ---------------------------------------------------------------------------
# Localização do fonte
# ---------------------------------------------------------------------------

# Caminhos onde a árvore do reims-vgpu pode estar, na ordem de preferência.
REIMS_SEARCH_PATHS = (
    "/opt/reims-vgpu",
    "/home/felipeab10/reims-vgpu",
    "~/reims-vgpu",
)

BUILD_STAMP_NAME = ".vhackintosh-build-stamp"
# O build in-tree do QEMU deixa o binário direto em build/; as releases novas
# usam o layout do prefixo (bin/ + share/qemu/), coberto por prebuilt_qemu().
QEMU_BIN_REL = Path("vendor/qemu/build/qemu-system-x86_64")
STATICLIB_REL = Path("target/release/libreims_vgpu.a")

# A árvore nova do fork (libreims_vgpu + reims-ctl) compila num passo só, pelo
# `scripts/install-ctl.sh`: biblioteca (cargo xtask dist, ROM incluída), QEMU
# sobre ela em ~/.cache/reims-ctl, e o reims-ctl instalado em ~/.cargo/bin. O
# QEMU fica em `$XDG_DATA_HOME/reims-ctl/qemu/<build>` com o symlink `current`.
INSTALL_CTL_REL = Path("scripts/install-ctl.sh")
QEMU_BUILD_LEGACY_REL = Path("scripts/qemu-build/qemu-build.sh")

# Modo pré-compilado: a ISO nasce com o reims-vgpu já compilado, vindo de uma
# release do GitHub (`vendor/qemu/build/qemu-system-x86_64` + a marca
# `.release`/`SOURCE-PROVENANCE.txt` gravada pelo `appliance/build-iso.sh`).
# Nesse caso não há o que compilar no primeiro boot — e o appliance é uma imagem
# live, onde compilar o fork do QEMU custava horas sem garantia de rede.
PREBUILT_QEMU_REL = Path("vendor/qemu/build/qemu-system-x86_64")
# O layout novo espelha o prefixo do QEMU instalado (bin/ + share/qemu/), que é
# o que o `release.sh` publica. O caminho plano acima continua aceito para não
# invalidar releases já publicadas.
PREBUILT_QEMU_RELS = (
    Path("vendor/qemu/build/bin/qemu-system-x86_64"),
    PREBUILT_QEMU_REL,
)
# A ROM do vGPU: o binário a procura por este nome, relativo ao próprio prefixo.
PREBUILT_ROM_NAME = "ReimsVgpu.rom"
PREBUILT_MARKER_REL = Path(".release")
PREBUILT_PROVENANCE_REL = Path("SOURCE-PROVENANCE.txt")

# O **motor**: é o `reims-ctl` que sobe o QEMU, e o appliance não tem motor de
# reserva (`ui.tui.decide_reimsctl`). Sem ele a ISO pré-compilada instala o QEMU
# e ainda assim não lança VM nenhuma. Ele viaja no bundle da release, ao lado da
# árvore do QEMU, e é instalado no mesmo lugar em que `core.reims_ctl` procura.
PREBUILT_REIMS_CTL_RELS = (
    Path("bin/reims-ctl"),
    Path("reims-ctl"),
)


def prebuilt_qemu(reims_dir: Path) -> Optional[Path]:
    """O binário do QEMU dentro da árvore pré-compilada, em qualquer layout."""
    for rel in PREBUILT_QEMU_RELS:
        candidato = reims_dir / rel
        if candidato.is_file():
            return candidato
    return None


def prebuilt_rom(reims_dir: Path) -> Optional[Path]:
    """A ROM do vGPU na árvore pré-compilada, em qualquer layout conhecido.

    Só devolve a ROM com o nome atual (`ReimsVgpu.rom`), que é a que o QEMU
    procura pelo `romfile`. Releases antigas traziam uma ROM diferente, a GOP em
    `crates/reims-vgpu-efi/out/reims-vgpu-gop.rom` (26112 bytes contra 5632 da
    atual, sha256 distinto) — instalá-la renomeada faria o QEMU carregar o
    firmware errado em silêncio, então ela é ignorada de propósito. Quem chama
    deve tratar `None` como "a release não trouxe a ROM do vGPU".
    """
    candidatos = [
        reims_dir / "vendor" / "qemu" / "build" / "share" / "qemu" / PREBUILT_ROM_NAME,
        reims_dir / "vendor" / "qemu" / "pc-bios" / PREBUILT_ROM_NAME,
        reims_dir / "vendor" / "qemu" / "build" / PREBUILT_ROM_NAME,
    ]
    for candidato in candidatos:
        if candidato.is_file():
            return candidato
    # Último recurso: qualquer arquivo com o nome atual sob a árvore.
    try:
        for achado in sorted(reims_dir.rglob(PREBUILT_ROM_NAME)):
            if achado.is_file():
                return achado
    except OSError:
        pass
    return None


def legacy_gop_rom(reims_dir: Path) -> Optional[Path]:
    """A ROM GOP das releases antigas, que NÃO serve como ReimsVgpu.rom.

    Existe para o diagnóstico poder explicar a diferença em vez de o usuário
    ver só "release sem ROM" quando na verdade veio uma ROM com outro nome.
    """
    antiga = reims_dir / "crates" / "reims-vgpu-efi" / "out" / "reims-vgpu-gop.rom"
    return antiga if antiga.is_file() else None


def prebuilt_release(reims_dir: Path) -> Optional[str]:
    """Devolve a tag da release empacotada na ISO, ou None se veio das fontes.

    Só considera pré-compilado quando há **marca** e **binário executável**: um
    diretório com as fontes tem `vendor/qemu/` mas não necessariamente o binário
    (que só existe depois do build), e um binário sem marca é ambíguo.
    """
    marker = reims_dir / PREBUILT_MARKER_REL
    if marker.is_file() and prebuilt_qemu(reims_dir) is not None:
        return marker.read_text(encoding="utf-8", errors="replace").strip() or "desconhecida"
    return None


def prebuilt_provenance(reims_dir: Path) -> dict[str, str]:
    """Lê o SOURCE-PROVENANCE.txt do modo pré-compilado (vazio se não houver)."""
    prov = reims_dir / PREBUILT_PROVENANCE_REL
    if not prov.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        for line in prov.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                out[key.strip()] = value.strip()
    except OSError:
        return {}
    return out


def prebuilt_reims_ctl(reims_dir: Path) -> Optional[Path]:
    """O motor (`reims-ctl`) dentro da árvore pré-compilada, se a release o trouxer.

    `None` significa "este bundle não tem motor" — que é o caso das releases
    antigas e do modo `--from-source` (onde o `install-ctl.sh` o compila). Não é
    fatal por si só, mas `decide_reimsctl` vai recusar o lançamento se não houver
    nenhum `reims-ctl` utilizável, então quem chama deve avisar em vez de seguir
    calado.
    """
    for rel in PREBUILT_REIMS_CTL_RELS:
        candidato = reims_dir / rel
        if candidato.is_file():
            return candidato
    return None


def reims_ctl_install_dir() -> Path:
    """O diretório em que `core.reims_ctl.reims_ctl_bin()` procura o motor.

    As duas implementações **têm de concordar**: `reims_ctl_bin()` olha o PATH e
    depois `~/.cargo/bin/reims-ctl` (que como root é `/root/.cargo/bin`).
    Instalar em outro lugar deixaria o motor recém-instalado invisível.
    """
    return Path(os.path.expanduser("~/.cargo/bin"))


def prebuilt_current_release() -> Optional[str]:
    """O nome do diretório apontado pelo symlink `qemu/current`, ou `None`.

    É o que responde "qual compilação a VM vai usar". Devolve só o nome (não o
    caminho resolvido): o alvo é relativo de propósito, para a árvore poder ser
    movida junto.
    """
    link = qemu_data_dir() / "qemu" / "current"
    if not link.is_symlink():
        return None
    try:
        alvo = os.readlink(link)
    except OSError:
        return None
    return Path(alvo).name or None


def qemu_data_dir() -> Path:
    if os.geteuid() == 0:
        base = "/root/.local/share"
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "reims-ctl"


def appliance_build_environment() -> dict[str, str]:
    """Keep root-triggered appliance builds in the runtime root user's paths."""
    env = os.environ.copy()
    if os.geteuid() == 0:
        env["HOME"] = "/root"
        env["CARGO_HOME"] = str(Path(env["HOME"]) / ".cargo")
        env["XDG_DATA_HOME"] = str(Path(env["HOME"]) / ".local" / "share")
    return env


def qemu_binary() -> Path:
    """O QEMU que o install-ctl.sh instala (symlink `current`)."""
    return qemu_data_dir() / "qemu" / "current" / "bin" / "qemu-system-x86_64"


def is_valid_reims_tree(cand: Path) -> bool:
    """
    Este diretório serve como a árvore do reims-vgpu?

    Duas formas válidas, e as duas PRECISAM valer aqui:

      1. **Fontes**, que é o caso de um checkout: tem `scripts/install-ctl.sh`
         ou o antigo `scripts/qemu-build/qemu-build.sh`.
      2. **Pré-compilada**, que é o caso do payload que a própria ISO instala em
         `/opt/reims-vgpu`: tem `.release` + `bin/reims-ctl` + o QEMU do bundle.

    A segunda estava de fora, e o resultado foi medido numa ISO real: a VM
    recusava o lançamento com "sem diretório do reims-vgpu" enquanto o menu
    "Atualizar reims-vgpu" dizia, na mesma sessão, que a v1.2.0 estava
    instalada — porque o menu caía em `DEFAULT_REIMS_DIR` e o lançamento não.
    Exigir um script de COMPILAÇÃO para reconhecer uma instalação BINÁRIA é
    pedir à release uma coisa que ela não tem e não deve ter.

    A checagem é deliberadamente frouxa sobre qual binário veio: quem valida a
    integridade do payload é `core.reims_release` (`_verify_tree`), não aqui.
    Este predicado só responde "existe algo instalado neste caminho?".
    """
    if (cand / INSTALL_CTL_REL).is_file() or (cand / QEMU_BUILD_LEGACY_REL).is_file():
        return True

    tem_motor = any((cand / rel).is_file() for rel in PREBUILT_REIMS_CTL_RELS)
    return bool(tem_motor and prebuilt_qemu(cand) is not None)


def find_reims_dir(explicit: Optional[str | Path] = None) -> Optional[Path]:
    """Devolve a árvore do reims-vgpu — de fontes ou pré-compilada."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("REIMS_SRC") or os.environ.get("REIMS_VGPU_DIR")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(Path(p).expanduser() for p in REIMS_SEARCH_PATHS)

    for cand in candidates:
        if is_valid_reims_tree(cand):
            return cand
    return None


def has_sources(reims_dir: Path) -> bool:
    """A árvore precisa das fontes para poder compilar (não só do binário)."""
    return (
        (reims_dir / "Cargo.toml").exists()
        and (reims_dir / "crates").is_dir()
        and (reims_dir / "vendor" / "qemu" / "meson.build").exists()
    )


# ---------------------------------------------------------------------------
# Bit de execução
# ---------------------------------------------------------------------------

# O `mkarchiso` copia o airootfs com `cp -af --no-preserve=ownership,mode`
# (linha 323 do script do Arch) e depois só reaplica os modos listados em
# `file_permissions`, que **não aceita glob**. Resultado: todo arquivo fora
# dessa lista chega na ISO como 0644 — inclusive `vendor/qemu/configure`, que o
# `qemu-build.sh` invoca como `./configure`, produzindo
# `./configure: Permission denied`.
#
# A correção tem duas camadas: aqui (restauramos o bit em tempo de execução, o
# que também cobre ISOs já gravadas) e no `profiledef.sh` (para as próximas).
# Só tocamos em arquivos que *começam* com `#!`: assim o bit nunca é dado a
# dados de teste, `.rom` ou `.so`.
_EXEC_SCAN_DIRS = ("vendor/qemu", "scripts", "vm", "crates")
_EXEC_SKIP_PARTS = {"build", "target", ".git", "tests", "docs", "roms", "subprojects"}


def _has_shebang(path: Path) -> bool:
    """Um `#!` de verdade, não um atributo interno do Rust.

    `crates/**/tests.rs` começa com `#![allow(...)]`, que é `#!` seguido de
    `[`. Um shebang real aponta para um interpretador (`/bin/sh`, `env python`).
    """
    try:
        with open(path, "rb") as fh:
            first = fh.readline(256)
    except OSError:
        return False
    if not first.startswith(b"#!"):
        return False
    rest = first[2:].lstrip()
    return bool(rest) and rest[:1] not in (b"[", b"\n")


# O `configure` do QEMU se recusa a usar um `build/` que ele não criou; o
# contrato é este arquivo-marker. A ISO pode trazer `vendor/qemu/build/`
# contendo só o binário pré-compilado (fallback para quem recusa compilar), e
# nesse estado o build falha com
#   ERROR: ./build dir already exists and was not previously created by configure
CONFIGURE_MARKER = Path("vendor/qemu/build/auto-created-by-configure")


def prepare_build_dir(reims_dir: Path, log: Optional[Callable[[str], None]] = None) -> None:
    """Deixa o `build/` num estado em que o `configure` do QEMU o aceite.

    Só age quando o `build/` existe **sem** `build.ninja` — isto é, não é um
    build de verdade, e sim o diretório com o binário enviado pela ISO. Nesse
    caso o `configure` vai apagá-lo de qualquer forma; o marker apenas o
    autoriza a fazê-lo.
    """
    build = reims_dir / "vendor" / "qemu" / "build"
    marker = reims_dir / CONFIGURE_MARKER
    if not build.is_dir() or marker.exists():
        return
    if (build / "build.ninja").exists():
        return  # árvore já configurada: o incremental funciona, não mexer
    try:
        marker.touch()
        if log:
            log("build/ pré-existente marcado como descartável (exigência do configure)")
    except OSError:
        pass


def ensure_executable_bits(reims_dir: Path, log: Optional[Callable[[str], None]] = None) -> int:
    """Restaura o bit +x em scripts com shebang. Devolve quantos corrigiu.

    Idempotente e barato: numa árvore já correta não altera nada.
    """
    fixed = 0
    for rel in _EXEC_SCAN_DIRS:
        root = reims_dir / rel
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                if any(part in _EXEC_SKIP_PARTS for part in path.parts):
                    continue
                mode = path.stat().st_mode
                if mode & 0o111:
                    continue  # já executável
                if not _has_shebang(path):
                    continue
                path.chmod(mode | 0o755)
                fixed += 1
            except OSError:
                continue
    if log:
        if fixed:
            log(f"{fixed} script(s) de build recuperaram o bit de execução")
        else:
            log("bits de execução já estavam corretos")
    return fixed


# ---------------------------------------------------------------------------
# Stamp por conteúdo
# ---------------------------------------------------------------------------

# Árvores cujo conteúdo entra no hash. O binário compilado depende de todas.
# `firmware/` (a ROM da árvore nova) e `scripts/` (o próprio install-ctl.sh)
# também mudam o que sai do build.
_STAMP_ROOTS = ("crates", "vendor/qemu", "firmware", "scripts")
_STAMP_FILES = ("Cargo.toml", "Cargo.lock")
# Derivações que não afetam o binário (e que, por conteúdo, seriam enormes).
_STAMP_SKIP_DIRS = {
    "build", "target", ".git", "out", "node_modules", "__pycache__", "tests",
    "docs", ".github", "subprojects", "roms",
}
_STAMP_SKIP_SUFFIXES = (".o", ".a", ".so", ".d", ".pyc", ".img", ".qcow2", ".iso")


def _iter_stamp_files(reims_dir: Path) -> Iterable[Path]:
    seen: set[Path] = set()
    for rel in _STAMP_FILES:
        p = reims_dir / rel
        if p.is_file():
            seen.add(p)
            yield p
    for root_rel in _STAMP_ROOTS:
        root = reims_dir / root_rel
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path in seen or not path.is_file():
                continue
            if any(part in _STAMP_SKIP_DIRS for part in path.parts):
                continue
            if path.name.endswith(_STAMP_SKIP_SUFFIXES):
                continue
            seen.add(path)
            yield path


def source_fingerprint(reims_dir: Path) -> str:
    """Hash do conteúdo do fonte. Muda quando o fonte muda, nunca por data."""
    digest = hashlib.sha256()
    for path in sorted(_iter_stamp_files(reims_dir)):
        rel = path.relative_to(reims_dir).as_posix()
        digest.update(rel.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(1 << 20):
                    digest.update(chunk)
        except OSError:
            # Arquivo desapareceu durante a varredura (build concorrente): o
            # hash do resto ainda é válido e o próximo cálculo o pegará.
            continue
        digest.update(b"\0")
    return digest.hexdigest()


def read_stamp(reims_dir: Path) -> Optional[str]:
    try:
        p = reims_dir / BUILD_STAMP_NAME
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        fallback = CONFIG_DIR / "stamps" / f"{reims_dir.name}{BUILD_STAMP_NAME}"
        if fallback.is_file():
            return fallback.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return None


def write_stamp(reims_dir: Path, fingerprint: str) -> None:
    try:
        (reims_dir / BUILD_STAMP_NAME).write_text(fingerprint + "\n", encoding="utf-8")
    except OSError:
        pass
    try:
        stamps_dir = CONFIG_DIR / "stamps"
        stamps_dir.mkdir(parents=True, exist_ok=True)
        (stamps_dir / f"{reims_dir.name}{BUILD_STAMP_NAME}").write_text(fingerprint + "\n", encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Estado do build
# ---------------------------------------------------------------------------

class BuildState:
    """O que precisa ser feito, decidido sem compilar nada."""

    def __init__(
        self,
        reims_dir: Optional[Path],
        *,
        reason: str,
        needs_build: bool,
        binary_path: Optional[Path] = None,
        sources_present: bool = False,
    ) -> None:
        self.reims_dir = reims_dir
        self.reason = reason
        self.needs_build = needs_build
        self.binary_path = binary_path
        self.sources_present = sources_present

    def __repr__(self) -> str:  # pragma: no cover - diagnóstico
        return (
            f"BuildState(needs_build={self.needs_build}, "
            f"reason={self.reason!r}, dir={self.reims_dir})"
        )


def evaluate(
    reims_dir: Optional[Path] = None,
    *,
    explicit: Optional[str | Path] = None,
    log: Optional[Callable[[str], None]] = None,
) -> BuildState:
    """Decide se o binário precisa ser (re)compilado, sem compilar."""
    emit = log or (lambda _msg: None)
    directory = reims_dir or find_reims_dir(explicit)
    if directory is None:
        return BuildState(None, reason="reims-vgpu não encontrado", needs_build=False)

    # O binário em uso é o instalado pelo install-ctl.sh (árvore nova); a árvore
    # antiga mantém o build in-tree em vendor/qemu/build como fallback.
    #
    # `prebuilt_qemu()` cobre os **dois** layouts da árvore (o prefixo
    # `build/bin/...` que a release publica e o plano `build/...` do build
    # in-tree). Sem ele, uma ISO pré-compilada com o layout novo se declarava sem
    # binário — `binary_path=None` — e a TUI recusava a VM com o QEMU ali do lado.
    installed = qemu_binary()
    binary = installed if installed.exists() else (prebuilt_qemu(directory) or directory / QEMU_BIN_REL)
    if not has_sources(directory):
        # Árvore só com binário (ISO pré-compilada de uma release do GitHub): não
        # há o que compilar. O binário já está utilizável em qualquer host x64
        # com Vulkan 1.2+; a atualização é trocar o asset da release, não
        # recompilar (ver core/reims_release.py).
        release = prebuilt_release(directory)
        reason = (
            f"pré-compilado da release {release} (nada a compilar)"
            if release
            else "árvore sem fontes (somente binário pré-compilado)"
        )
        return BuildState(
            directory,
            reason=reason,
            needs_build=False,
            binary_path=binary if binary.exists() else None,
            sources_present=False,
        )

    if not binary.exists():
        return BuildState(
            directory,
            reason="binário do QEMU ainda não foi compilado",
            needs_build=True,
            binary_path=binary,
            sources_present=True,
        )

    current = source_fingerprint(directory)
    recorded = read_stamp(directory)
    if recorded is None:
        return BuildState(
            directory,
            reason="sem registro de build (primeira vez com fontes)",
            needs_build=True,
            binary_path=binary,
            sources_present=True,
        )
    if recorded != current:
        return BuildState(
            directory,
            reason="o fonte do reims-vgpu mudou desde o último build",
            needs_build=True,
            binary_path=binary,
            sources_present=True,
        )

    emit("fonte e binário coincidem — build não é necessário")
    return BuildState(
        directory,
        reason="binário atualizado (fonte inalterado)",
        needs_build=False,
        binary_path=binary,
        sources_present=True,
    )


# ---------------------------------------------------------------------------
# Execução do build
# ---------------------------------------------------------------------------

class BuildError(RuntimeError):
    pass


def install_prebuilt(reims_dir: Path) -> bool:
    """Instala o QEMU pré-compilado da release no prefixo que o appliance usa.

    Espelha o que o `install-ctl.sh` produz na árvore nova:
    `$XDG_DATA_HOME/reims-ctl/qemu/<release>/bin/qemu-system-x86_64` mais o
    symlink `current`. Assim o resto do appliance (`qemu_binary()`) encontra o
    binário sem saber de onde ele veio.

    Devolve False — sem levantar — quando o binário empacotado não existe, para
    o chamador poder cair no modo fontes.

    É **idempotente**: chamar de novo com a mesma árvore já instalada não recopia
    nada. O "Iniciar VM" chama esta função a cada lançamento, e a árvore tem
    ~100 MB que iriam para o tmpfs da ISO live toda vez.
    """
    release = prebuilt_release(reims_dir)
    # Sem árvore de origem (ou sem `.release`) o prefixo já instalado continua
    # válido: usar a tag que o `current` aponta faz o caso "origem sumiu, prefixo
    # íntegro" ser reconhecido como pronto em vez de recusado.
    atual = prebuilt_current_release()
    release = release or atual or "prebuilt"
    # A tag pode trazer caracteres que não valem como nome de diretório.
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in release)
    base = qemu_data_dir() / "qemu"
    target_dir = base / safe
    dest = target_dir / "bin" / "qemu-system-x86_64"

    # ------------------------------------------------------------------
    # Idempotência, ANTES de exigir a árvore de origem: o prefixo já instalado é
    # utilizável mesmo que a origem tenha sido removida, e recusar aqui trocaria
    # um "já está pronto" por um erro. A condição olha o `current` (a tag que a
    # VM vai usar) **e** o binário no disco: um `current` correto com o arquivo
    # apagado ainda precisa ser reinstalado.
    # ------------------------------------------------------------------
    ctl_src = prebuilt_reims_ctl(reims_dir)
    ctl_dest = reims_ctl_install_dir() / "reims-ctl"
    ctl_ok = ctl_src is None or (ctl_dest.is_file() and os.access(ctl_dest, os.X_OK))
    if (
        atual == safe
        and dest.is_file()
        and os.access(dest, os.X_OK)
        and ctl_ok
    ):
        return True

    src = prebuilt_qemu(reims_dir)
    if src is None or not src.is_file():
        print(f"  binário pré-compilado ausente: {reims_dir / PREBUILT_QEMU_RELS[0]}")
        return False

    try:
        if not (dest.is_file() and os.access(dest, os.X_OK)):
            (target_dir / "bin").mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            dest.chmod(0o755)

            # O vGPU procura a ROM pelo `romfile ReimsVgpu.rom`, relativo ao
            # prefixo do QEMU: ela tem de ficar em share/qemu/, ao lado do
            # binário. Sem isto a VM sobe sem a GPU virtual e o sintoma é tela
            # preta.
            rom_src = prebuilt_rom(reims_dir)
            if rom_src is not None:
                rom_dest = target_dir / "share" / "qemu"
                rom_dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(rom_src, rom_dest / "ReimsVgpu.rom")
            else:
                # Releases antigas traziam outra ROM, com outro nome. Dizer só
                # "sem ROM" mandaria o usuário procurar no lugar errado.
                antiga = legacy_gop_rom(reims_dir)
                if antiga is not None:
                    print(
                        f"  aviso: a release traz {antiga.name}, que não é a ROM do "
                        "vGPU; o QEMU precisa de ReimsVgpu.rom (romfile). "
                        "A VM vai subir sem GPU virtual."
                    )
                else:
                    print("  aviso: a release não trouxe a ROM do vGPU (ReimsVgpu.rom)")

            # As demais ROMs do QEMU (pxe, vgabios) acompanham a do vGPU.
            for origem, destino in (
                (reims_dir / "vendor" / "qemu" / "build" / "share" / "qemu", target_dir / "share" / "qemu"),
                (reims_dir / "vendor" / "qemu" / "pc-bios", target_dir / "pc-bios"),
            ):
                if origem.is_dir():
                    for item in origem.iterdir():
                        alvo = destino / item.name
                        if alvo.exists():
                            continue
                        if item.is_symlink() or item.is_file():
                            destino.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(item, alvo, follow_symlinks=False)
                        elif item.is_dir():
                            shutil.copytree(item, alvo, symlinks=True)

        # O motor. Fica no diretório em que `core.reims_ctl.reims_ctl_bin()`
        # procura: o PATH do usuário pode não incluir ~/.cargo/bin, mas aquela
        # função checa o caminho literal, então o motor aparece do mesmo jeito.
        if ctl_src is not None and not (ctl_dest.is_file() and os.access(ctl_dest, os.X_OK)):
            ctl_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ctl_src, ctl_dest)
            ctl_dest.chmod(0o755)

        link = base / "current"
        if link.is_symlink() or link.exists():
            if link.is_dir() and not link.is_symlink():
                print(f"  {link} é um diretório; não vou substituí-lo.")
                return False
            link.unlink()
        link.symlink_to(target_dir.name)
    except OSError as exc:
        print(f"  falha ao instalar o QEMU pré-compilado: {exc}")
        return False

    prov = prebuilt_provenance(reims_dir)
    origin = prov.get("release") or release
    if not (dest.is_file() and os.access(dest, os.X_OK)):
        print(f"  falha: {dest} não ficou executável.")
        return False
    print(f"  QEMU pré-compilado instalado em {dest} (release {origin})")
    if ctl_src is not None:
        print(f"  motor reims-ctl instalado em {ctl_dest} (release {origin})")
    else:
        # Sem motor no bundle E sem nenhum utilizável no sistema a ISO instala o
        # QEMU e ainda assim não sobe VM (o appliance não tem motor de reserva).
        # Avisar aqui é a diferença entre um diagnóstico e uma tela preta.
        try:
            from core.reims_ctl import reims_ctl_bin

            encontrado = reims_ctl_bin()
        except Exception:  # pragma: no cover - diagnóstico best-effort
            encontrado = None
        if encontrado is None:
            print(
                "  aviso: a release não trouxe o motor reims-ctl e não há nenhum "
                "instalado; o appliance não tem motor de reserva e não vai "
                "conseguir iniciar a VM assim."
            )
    return True


def build(
    reims_dir: Path,
    *,
    on_start: Optional[Callable[[str, float], None]] = None,
    on_done: Optional[Callable[[str, bool], None]] = None,
    progress: Optional[Callable[[float], None]] = None,
) -> bool:
    """Compila reims-vgpu + QEMU (Vulkan). Devolve True em sucesso.

    ``on_start(nome, segundos_estimados)`` é chamado antes de cada etapa para a
    TUI montar a barra; ``progress(0..100)`` recebe o avanço estimado.
    """
    # Modo pré-compilado: a ISO já trouxe o QEMU/reims-vgpu compilado de uma
    # release do GitHub. Não há `scripts/install-ctl.sh` para rodar nem nada a
    # compilar; reinstalar o binário é o trabalho todo.
    release = prebuilt_release(reims_dir)
    if release is not None:
        print(f"  reims-vgpu pré-compilado detectado (release {release}); "
              "nada a compilar.")
        ok = install_prebuilt(reims_dir)
        if on_start is not None:
            on_start("Instalando reims-vgpu pré-compilado", 0.0)
        if on_done is not None:
            on_done("Instalando reims-vgpu pré-compilado", ok)
        return ok

    # A árvore nova compila tudo pelo install-ctl.sh (biblioteca + ROM + QEMU +
    # reims-ctl, prefixo FHS em ~/.local/share); a antiga usa o qemu-build.sh.
    new_tree = (reims_dir / INSTALL_CTL_REL).is_file()
    if new_tree:
        script = reims_dir / INSTALL_CTL_REL
        cmd = ["bash", str(script), "--yes"]
    else:
        script = reims_dir / QEMU_BUILD_LEGACY_REL
        cmd = ["bash", str(script), "--target", "x86_64", "--backend", "vulkan"]
    if not script.exists():
        raise BuildError(f"script de build ausente: {script}")

    # A ISO entrega a árvore em 0644 (ver `ensure_executable_bits`); sem isto o
    # `./configure` do QEMU falha com "Permission denied" logo na primeira
    # etapa. Restauramos antes de qualquer coisa.
    ensure_executable_bits(reims_dir, log=lambda msg: print(f"  {msg}"))

    # A ISO leva o cache git (metal2vulkan) porque `cargo vendor` não cobre
    # dependências git. Restaurá-lo aqui é o que permite o build offline.
    seed_cargo_git_cache(
        appliance_build_environment(), log=lambda msg: print(f"  {msg}")
    )

    # Só a árvore antiga builda in-tree: o build/ que a ISO envia (só com o
    # binário) precisa do marker do próprio QEMU, senão o configure aborta.
    # Ver `prepare_build_dir`.
    if not new_tree:
        prepare_build_dir(reims_dir, log=lambda msg: print(f"  {msg}"))

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. ROM UEFI GOP, quando houver script próprio (só a árvore antiga; a nova
    # compila a ReimsVgpu.rom dentro do próprio xtask dist).
    rom = reims_dir / "crates" / "reims-vgpu-efi" / "build.sh"
    if rom.exists():
        ok = _run_stage(
            name="ROM UEFI GOP",
            cmd=["bash", str(rom)],
            cwd=rom.parent,
            log_name="build-rom.log",
            expected=15.0,
            on_start=on_start,
            progress=progress,
        )
        if on_done:
            on_done("ROM UEFI GOP", ok)
        if not ok:
            return False

    # 2. Biblioteca + QEMU (+ reims-ctl na árvore nova).
    ok = _run_stage(
        name="reims-vgpu & QEMU (Vulkan)",
        cmd=cmd,
        cwd=reims_dir,
        log_name="build-qemu-reims.log",
        expected=900.0,
        on_start=on_start,
        progress=progress,
    )
    if on_done:
        on_done("reims-vgpu & QEMU (Vulkan)", ok)

    if ok and new_tree and not qemu_binary().exists():
        # O install-ctl.sh saiu 0 mas o prefixo não recebeu o QEMU: tratar como
        # falha para o estado "binário presente" nunca mentir.
        print("  install-ctl.sh concluiu mas o QEMU instalado não foi encontrado "
              f"em {qemu_binary()}")
        ok = False

    if ok:
        # Só grava o stamp com o build comprovadamente de pé.
        write_stamp(reims_dir, source_fingerprint(reims_dir))
    return ok


# ---------------------------------------------------------------------------
# Política de rede do Cargo
# ---------------------------------------------------------------------------
#
# O appliance promete compilar offline, e `cargo vendor` só cobre o **registry**
# do crates.io. O `metal2vulkan` não vem do crates.io: é dependência **git**
# (`Cargo.toml` do fork, `git = "https://github.com/steelbrain/metal2vulkan"`,
# `rev = "29d3ebb…"`), e `vendor/cargo/` não o contém.
#
# Por isso a heurística antiga — "existe `vendor/cargo/` ⟹ `--offline`" —
# garantia a falha em vez de evitá-la:
#
#   error: failed to get `metal2vulkan` as a dependency of package
#          `reims-vgpu-vulkan v0.1.0`
#     Caused by: unable to update https://github.com/steelbrain/metal2vulkan?rev=29d3ebb…
#     Caused by: can't checkout from 'https://github.com/steelbrain/metal2vulkan':
#                you are in the offline mode (--offline)
#
# O cargokit sempre teve de baixar esse commit uma vez; o que a ISO pode fazer é
# levar a **cópia já em cache** (`~/.cargo/git/db` + `checkouts`) junto com o
# fonte, para que o primeiro boot não precise de rede. Enquanto essa cópia não
# estiver no payload, forçar `--offline` transforma "precisa de rede" em "nunca
# compila". A decisão correta é:
#
#   * `REIMS_CARGO_OFFLINE=1`  → offline forçado (o usuário pediu; útil para
#                                provar que o payload offline está completo);
#   * `REIMS_CARGO_OFFLINE=0`  → rede liberada (build de desenvolvimento);
#   * automático → offline **só** quando o cache git do próprio CARGO_HOME já
#                  tem o repositório que as deps git exigem; senão online.
#
# O automático é o que destrava o build: sem o cache a única saída honesta é
# rede, e o cargokit já a usa quando precisa.

# Repositórios git exigidos pelo `Cargo.toml` do fork. O cargo guarda o cache
# em `$CARGO_HOME/git/db/<nome>-<hash>`; o hash vem da URL normalizada, então
# casamos pelo prefixo do nome em vez de fixar o sufixo.
_REQUIRED_GIT_DEPS = ("metal2vulkan",)

# Teto do download inicial (só para tornar o modo online visível no log).
_GIT_CACHE_ENV_HINT = (
    "Dica: a ISO precisa levar o cache git do Cargo (~/.cargo/git/db) para "
    "compilar offline no primeiro boot."
)


def _looks_like_bare_repo(path: Path) -> bool:
    """Um `git/db/<nome>` de verdade? Não basta o diretório existir.

    Um `mkdir -p` seguido de `rsync` interrompido (Ctrl-C, disco cheio, OOM)
    deixa um `metal2vulkan-*` **vazio** para trás. Aceitá-lo por nome fazia a
    política decidir `CARGO_NET_OFFLINE=true` e o cargo voltar exatamente ao erro
    original (`can't checkout ... offline mode`), então o cache envenenado se
    perpetuava: `seed_cargo_git_cache` também não recopiava por já ver o
    diretório. Exigimos a forma de um repositório bare — `objects/` e `refs/` —
    que é o mínimo que o cargo precisa para resolver a revisão.
    """
    return (path / "objects").is_dir() and (path / "refs").is_dir()


def _git_dep_cache_present(cargo_home: Path) -> bool:
    """O cache git do Cargo já tem as deps git que o build exige?

    Só assim `--offline` é uma promessa e não uma armadilha. O repositório bare
    em `git/db` basta: o Cargo materializa o `checkouts/` a partir dele, offline
    (verificado neste host). Por isso o teste é sobre `db`, e não `checkouts`.
    """
    db = cargo_home / "git" / "db"
    if not db.is_dir():
        return False
    for dep in _REQUIRED_GIT_DEPS:
        found = False
        for entry in db.iterdir():
            if entry.is_dir() and entry.name.startswith(dep):
                if _looks_like_bare_repo(entry):
                    found = True
                    break
        if not found:
            return False
    return True


# Onde a ISO deposita o cache git que empacotou (ver `appliance/build-iso.sh`).
GIT_CACHE_PAYLOAD = Path("/opt/reims-vgpu-cargo-git")
# Onde a ISO deposita o cache do registry do crates.io.
REGISTRY_CACHE_PAYLOAD = Path("/opt/reims-vgpu-cargo-registry")


def _registry_cache_present(cargo_home: Path) -> bool:
    """O registry do crates.io já tem índice e cache de .crate baixados?"""
    return (cargo_home / "registry" / "index").is_dir() and (
        cargo_home / "registry" / "cache"
    ).is_dir()


def _count_entries(path: Path) -> int:
    """Quantas entradas `path` tem, sem explodir se algo sumiu no meio."""
    try:
        return sum(1 for _ in path.iterdir())
    except OSError:
        return 0


def _unique_aside(path: Path) -> Path:
    """Um nome livre ao lado de `path` para guardar o que não pode ser apagado."""
    for i in range(1000):
        cand = path.with_name(f"{path.name}.resquicio-vhackintosh-{i}")
        if not cand.exists() and not cand.is_symlink():
            return cand
    return path.with_name(f"{path.name}.resquicio-vhackintosh-{id(path)}")


def _clear_blocking_files(origin: Path, dest: Path, say: Callable[[str], None]) -> None:
    """Remove, em `dest`, o que bloqueia a cópia fiel de `origin`.

    `shutil.copytree(dirs_exist_ok=True)` faz merge de diretórios, mas tem dois
    modos de falha silenciosa/ruidosa quando origem e destino discordam no TIPO:

    * payload traz **diretório**, destino tem **arquivo/symlink** → estoura
      `EEXIST` no meio do merge e deixa o destino pela metade (ex.:
      `git/db/<nome>/objects` como arquivo de 0 byte);
    * payload traz **arquivo**, destino tem **diretório** → o arquivo é
      simplesmente PULADO, sem erro nenhum. O merge "termina com sucesso" com o
      repositório incompleto — pior, porque a falha é invisível.

    Os dois casos são resquício de cópia interrompida, não dado do usuário:
    o diretório de destino só existe ali por acidente. Removemos o bloqueador
    para o merge sempre reproduzir o payload.
    """
    if not dest.exists() and not dest.is_symlink():
        return
    for entry in sorted(origin.iterdir()):
        target = dest / entry.name
        if not target.exists() and not target.is_symlink():
            continue
        # `entry.is_dir()` SEGUE symlinks: um symlink-para-diretório no payload
        # se comportaria como diretório e a recursão andaria pelo link,
        # materializando no destino uma árvore fantasma que o `copytree`
        # (`symlinks=True`) deveria ter reproduzido como link. Tratamos symlink
        # de origem como folha, para a cópia ser fiel.
        if entry.is_dir() and not entry.is_symlink():
            if target.is_dir() and not target.is_symlink():
                _clear_blocking_files(entry, target, say)
            else:
                say(f"removendo resquício de arquivo em {target}")
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
        elif target.is_dir() and not target.is_symlink():
            # Direção reversa: o payload tem ARQUIVO (ou symlink) e o destino um
            # DIRETÓRIO. Sem remover, o `copytree` pula o arquivo sem avisar.
            #
            # CUIDADO: o payload traz arquivos de nome `HEAD`, `config`,
            # `FETCH_HEAD` e `description` na raiz de todo repositório bare. Um
            # diretório com um desses nomes pode conter trabalho do usuário, e um
            # `rmtree` cego apagaria tudo — inclusive num cache que já estava
            # válido. Só removemos o que é claramente resquício: diretório vazio,
            # ou que não tem forma de repositório bare. Caso contrário movemos
            # para o lado e avisamos, para nada ser perdido em silêncio.
            entries = _count_entries(target)
            if not _looks_like_bare_repo(target) and entries == 0:
                say(f"removendo diretório vazio que bloqueia o arquivo em {target}")
                target.rmdir()
            else:
                aside = _unique_aside(target)
                say(
                    f"AVISO: {target} é um diretório com {entries} entrada(s) onde o "
                    f"cache empacotado tem um arquivo; movendo para {aside} em vez "
                    "de apagar (nada foi perdido)"
                )
                target.rename(aside)


def _seed_cache_tree(origin: Path, dest: Path, say: Callable[[str], None]) -> bool:
    """Copia o conteúdo de `origin` para `dest`, sem sobrescrever o existente.

    Copia arquivos **e** diretórios: `registry/cache` guarda os `.crate` soltos
    ao lado dos subdiretórios do índice, e ignorar arquivos deixaria o registry
    incompleto (foi o que um teste pegou).

    Reparação de cache parcial: um `rsync` interrompido (Ctrl-C, disco cheio,
    OOM) ou um payload corrompido pode deixar um `git/db/<nome>` **vazio**,
    **pela metade**, ou com um **arquivo** onde deveria haver um diretório
    (`objects` como arquivo de 0 byte). Um diretório que não tem a forma de
    repositório bare (`objects/` + `refs/`) é resquício, não cache do usuário:
    fazemos merge por cima (`copytree(dirs_exist_ok=True)`) em vez de pular.
    Sem isso o cache envenenado persistia e o build voltava ao erro original de
    `offline mode`.
    """
    ok = True
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(origin.iterdir()):
        target = dest / entry.name
        # Se o payload traz um DIRETÓRIO e o destino já tem algo que não é um
        # diretório comum (arquivo, symlink), isso é resquício e precisa sair:
        # `copytree` morreria com EEXIST. Um diretório que não tem forma de repo
        # bare é resquício; mas mesmo um que PARECE bare pode ter um arquivo
        # bloqueando mais fundo (ex.: `objects/pack` como arquivo), então
        # limpamos os bloqueios sempre que houver merge de diretório.
        if target.exists() or target.is_symlink():
            if entry.is_dir() and not (target.is_dir() and not target.is_symlink()):
                say(f"removendo resquício em {target}")
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            elif not entry.is_dir():
                # Payload é arquivo: nunca sobrescrevemos conteúdo já existente.
                continue
            elif not _looks_like_bare_repo(target) and not any(target.iterdir()):
                target.rmdir()
                say(f"reparando cache parcial em {target}")
        try:
            if entry.is_dir():
                _clear_blocking_files(entry, target, say)
                # `origin` é a raiz do payload desta chamada: propagá-la permite
                # que links absolutos apontando para FORA de `entry` (irmãos,
                # avô) sejam reconhecidos como internos e reescritos.
                _merge_tree(entry, target, say, origin)
            else:
                # Nunca sobrescrevemos um arquivo já existente: se já está lá, o
                # cache do usuário é ao menos tão bom quanto o empacotado.
                if not target.exists():
                    shutil.copy2(entry, target)
        except OSError as exc:
            say(f"não foi possível copiar {entry} para {target}: {exc}")
            ok = False
    return ok


def _merge_tree(
    origin: Path,
    dest: Path,
    say: Callable[[str], None],
    payload_root: Optional[Path] = None,
) -> None:
    """Mescla `origin` em `dest` copiando **apenas o que falta**.

    `payload_root` é a raiz do payload, usada para reescrever links absolutos que
    apontam para dentro dele. Precisa ser propagada: `origin` muda a cada nível
    da recursão e um link pode apontar para fora do diretório corrente (irmão,
    avô), então a raiz não pode ser deduzida de `origin`.

    `shutil.copytree(dirs_exist_ok=True)` sobrescreve arquivos existentes. Isso
    violaria a promessa de não destruir o cache de quem já tem um: um usuário com
    um `git/db/<repo>` completo e atualizado teria o `HEAD`/`config`/refs
    substituídos pela versão empacotada na ISO (mais antiga). Aqui só criamos o
    que está ausente, então um cache real do usuário é preservado intacto e um
    cache incompleto é preenchido.
    """
    if payload_root is None:
        payload_root = origin
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(origin.iterdir()):
        target = dest / entry.name
        # `entry.is_dir()` SEGUE symlinks. Recursar num symlink-para-diretório do
        # payload andaria pelo link e materializaria no destino uma árvore
        # fantasma (medido: 41 entradas em vez de 1), em vez de reproduzir o
        # link como o `copytree(symlinks=True)` faria. Symlink de origem é folha.
        if entry.is_dir() and not entry.is_symlink():
            _merge_tree(entry, target, say, payload_root)
        elif not target.exists() and not target.is_symlink():
            if entry.is_symlink():
                _copy_symlink(entry, payload_root, target)
            else:
                shutil.copy2(entry, target, follow_symlinks=False)


def _copy_symlink(origin: Path, origin_root: Path, target: Path) -> None:
    """Reproduz um symlink do payload, sem seguir o alvo.

    Um `readlink` absoluto costuma apontar para dentro do PAYLOAD (ex.: um link
    que volta para o próprio diretório de origem). Copiado cru para o destino,
    ele apontaria para o payload — que na ISO é descartável e no guest pode nem
    existir. Quando o alvo cai dentro do payload, reescrevemos o link de forma
    relativa, para ele resolver no destino. Link que não puder ser reproduzido é
    simplesmente omitido: um link quebrado no cache é pior que a ausência dele.
    """
    try:
        raw = os.readlink(origin)
    except OSError:
        return
    link = raw
    if os.path.isabs(raw):
        try:
            inside = Path(raw).relative_to(origin_root)
        except ValueError:
            inside = None
        if inside is not None:
            # O alvo, dentro do destino, é o caminho do link relativo à raiz do
            # payload, trocando a raiz pela raiz do destino. O relativo sai do
            # DIRETÓRIO DO LINK até esse ponto.
            link_dir_rel = origin.parent.relative_to(origin_root)
            dest_target = target.parent / os.path.relpath(inside, link_dir_rel)
            link = os.path.relpath(dest_target, target.parent)
    try:
        target.symlink_to(link)
    except OSError:
        # Destino sem suporte a symlink: melhor omitir do que deixar link podre.
        pass


def seed_cargo_git_cache(env: dict[str, str], log: Optional[Callable[[str], None]] = None) -> bool:
    """Restaura no CARGO_HOME os caches que a ISO empacotou.

    São dois, e os dois são necessários para o build offline:

    * `git/db` — a dependência `metal2vulkan` é **git** e não existe no
      `vendor/cargo`; sem ela o Cargo tenta o GitHub e morre sob `--offline`;
    * `registry/{index,cache}` — o fork não usa `cargo vendor`, então as crates
      do `Cargo.lock` vêm do crates.io.

    Devolve True se o cache git ficou disponível. É idempotente e barato: só
    copia o que ainda falta, para não repetir o trabalho a cada build.

    Sem isto o `--offline` automático nunca liga (ver `_git_dep_cache_present`) e
    a promessa de "compila offline" continua falsa — o payload existe na ISO mas
    o Cargo não olha para ele.
    """
    say = log or (lambda _m: None)
    cargo_home = Path(env.get("CARGO_HOME") or (Path(env.get("HOME", "~")) / ".cargo"))

    git_src = GIT_CACHE_PAYLOAD
    if git_src.is_dir():
        db = git_src / "git" / "db"
        if db.is_dir():
            # Não basta checar `_git_dep_cache_present`: um `rsync` interrompido
            # pode deixar o repositório com a FORMA de bare (`objects/` + `refs/`
            # são diretórios) mas com um arquivo no lugar de um diretório mais
            # fundo (ex.: `objects/pack` como arquivo). Nesse caso o predicado
            # passa e o Cargo falha depois. Sempre mesclamos: é idempotente e só
            # copia o que falta.
            _seed_cache_tree(db, cargo_home / "git" / "db", say)

    registry_src = REGISTRY_CACHE_PAYLOAD
    if registry_src.is_dir():
        for sub in ("cache", "index"):
            origin = registry_src / "registry" / sub
            if origin.is_dir():
                _seed_cache_tree(origin, cargo_home / "registry" / sub, say)

    ok = _git_dep_cache_present(cargo_home)
    if ok:
        say(f"caches do Cargo restaurados em {cargo_home} "
            "(o Cargo extrai o checkout sozinho, offline)")
    elif git_src.is_dir():
        say(f"cache git empacotado não cobre {', '.join(_REQUIRED_GIT_DEPS)}")
    return ok


def _apply_cargo_offline_policy(env: dict[str, str], cwd: Path) -> dict[str, str]:
    """Decide `CARGO_NET_OFFLINE` para uma etapa de build, com motivo explícito.

    Nunca sobrescreve uma escolha já feita pelo chamador (`env.setdefault`), para
    que `REIMS_CARGO_OFFLINE` e afins continuem mandando.

    `cwd` é o diretório da etapa; hoje não decide nada (a presença de
    `vendor/cargo` deixou de ser critério — ela só cobria o crates.io e por isso
    *garantia* a falha com a dep git), mas fica no contrato porque é o contexto
    natural caso a política volte a depender da árvore.
    """
    if "CARGO_NET_OFFLINE" in env:
        return env

    requested = os.environ.get("REIMS_CARGO_OFFLINE", "").strip().lower()
    if requested in ("1", "true", "yes", "on"):
        env["CARGO_NET_OFFLINE"] = "true"
        return env
    if requested in ("0", "false", "no", "off"):
        env["CARGO_NET_OFFLINE"] = "false"
        return env

    # Automático: offline só quando o CARGO_HOME tem *tudo* que o build exige.
    # Exigir o registry além do git evita prometer offline e morrer no meio da
    # resolução, que foi exatamente o modo de falha original.
    cargo_home = Path(env.get("CARGO_HOME") or (Path(env.get("HOME", "~")) / ".cargo"))
    git_ok = _git_dep_cache_present(cargo_home)
    registry_ok = _registry_cache_present(cargo_home)
    if git_ok and registry_ok:
        env["CARGO_NET_OFFLINE"] = "true"
    else:
        # `false` (e não a ausência da variável) deixa o estado visível no log e
        # desarma qualquer `CARGO_NET_OFFLINE` herdado do ambiente.
        env["CARGO_NET_OFFLINE"] = "false"
        faltando = []
        if not git_ok:
            faltando.append(f"git das deps ({', '.join(_REQUIRED_GIT_DEPS)})")
        if not registry_ok:
            faltando.append("registry do crates.io")
        print(f"  cargo: sem cache de {', '.join(faltando)} em {cargo_home}; "
              "build usará rede.")
        print(f"  {_GIT_CACHE_ENV_HINT}")
    return env


def _run_stage(
    *,
    name: str,
    cmd: list[str],
    cwd: Path,
    log_name: str,
    expected: float,
    on_start: Optional[Callable[[str, float], None]],
    progress: Optional[Callable[[float], None]],
) -> bool:
    if on_start:
        on_start(name, expected)
    log_file = LOGS_DIR / log_name

    env = appliance_build_environment()
    env = _apply_cargo_offline_policy(env, cwd)

    with open(log_file, "w", encoding="utf-8") as lf:
        lf.write(f"$ {' '.join(cmd)}\n(cwd={cwd})\n\n")
        lf.flush()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=lf,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
        except OSError as exc:
            lf.write(f"\n[erro ao iniciar: {exc}]\n")
            return False

        started = time.time()
        while proc.poll() is None:
            if progress:
                elapsed = time.time() - started
                # Assíntota em 95%: o último trecho só fecha com o sucesso.
                progress(min(95.0, (elapsed / expected) * 100.0))
            time.sleep(0.1)

    if progress:
        progress(100.0 if proc.returncode == 0 else 0.0)
    return proc.returncode == 0


def tail_log(log_name: str, lines: int = 15) -> str:
    """Últimas linhas do log de build, para mostrar o motivo da falha."""
    path = LOGS_DIR / log_name
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])
