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
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from core.config import LOGS_DIR

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
QEMU_BIN_REL = Path("vendor/qemu/build/qemu-system-x86_64")
STATICLIB_REL = Path("target/release/libreims_vgpu.a")


def find_reims_dir(explicit: Optional[str | Path] = None) -> Optional[Path]:
    """Devolve a árvore do reims-vgpu que tenha o script de build."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("REIMS_SRC") or os.environ.get("REIMS_VGPU_DIR")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(Path(p).expanduser() for p in REIMS_SEARCH_PATHS)

    for cand in candidates:
        if (cand / "scripts" / "qemu-build" / "qemu-build.sh").exists():
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
_STAMP_ROOTS = ("crates", "vendor/qemu")
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
        return (reims_dir / BUILD_STAMP_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def write_stamp(reims_dir: Path, fingerprint: str) -> None:
    try:
        (reims_dir / BUILD_STAMP_NAME).write_text(fingerprint + "\n", encoding="utf-8")
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

    binary = directory / QEMU_BIN_REL
    if not has_sources(directory):
        # Árvore só com binário (ISO de release): não há o que compilar.
        return BuildState(
            directory,
            reason="árvore sem fontes (somente binário pré-compilado)",
            needs_build=False,
            binary_path=binary if binary.exists() else None,
            sources_present=False,
        )

    if not binary.exists():
        return BuildState(
            directory,
            reason="binário do QEMU ainda não foi compilado",
            needs_build=True,
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
    script = reims_dir / "scripts" / "qemu-build" / "qemu-build.sh"
    if not script.exists():
        raise BuildError(f"script de build ausente: {script}")

    # A ISO entrega a árvore em 0644 (ver `ensure_executable_bits`); sem isto o
    # `./configure` do QEMU falha com "Permission denied" logo na primeira
    # etapa. Restauramos antes de qualquer coisa.
    ensure_executable_bits(reims_dir, log=lambda msg: print(f"  {msg}"))

    # E o `build/` que a ISO envia (só com o binário) precisa do marker do
    # próprio QEMU, senão o configure aborta. Ver `prepare_build_dir`.
    prepare_build_dir(reims_dir, log=lambda msg: print(f"  {msg}"))

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. ROM UEFI GOP, quando houver script próprio.
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

    # 2. QEMU + staticlib do reims-vgpu.
    ok = _run_stage(
        name="reims-vgpu & QEMU (Vulkan)",
        cmd=["bash", str(script), "--target", "x86_64", "--backend", "vulkan"],
        cwd=reims_dir,
        log_name="build-qemu-reims.log",
        expected=300.0,
        on_start=on_start,
        progress=progress,
    )
    if on_done:
        on_done("reims-vgpu & QEMU (Vulkan)", ok)

    if ok:
        # Só grava o stamp com o build comprovadamente de pé.
        write_stamp(reims_dir, source_fingerprint(reims_dir))
    return ok


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

    env = os.environ.copy()
    # Build offline quando as deps estão vendorizadas ao lado do fonte.
    vendor = cwd / "vendor" / "cargo"
    if (cwd / ".cargo" / "config.toml").exists() or vendor.is_dir():
        env.setdefault("CARGO_NET_OFFLINE", "true")

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
