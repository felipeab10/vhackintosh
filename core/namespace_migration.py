"""Opt-in migration of legacy vHackintosh metadata into an explicit namespace.

This module is deliberately not called at startup and is not exposed through
the web API. It only copies a small allowlist of metadata. VM disks, EFI,
NVRAM, media, caches, logs, and external paths are never moved or rewritten.
The source is retained as the rollback copy.
"""

from __future__ import annotations

import hashlib
import ctypes
import dataclasses
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal


_MARKER = ".vhackintosh-namespace-migration.json"
_BACKUP_DIR = ".vhackintosh-migration-backup"
_MAX_METADATA_BYTES = 32 * 1024 * 1024


class NamespaceMigrationError(RuntimeError):
    """The requested offline namespace migration cannot be performed safely."""


@dataclass(frozen=True)
class MigrationFile:
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class MigrationPreview:
    source: str
    destination: str
    files: tuple[MigrationFile, ...]
    total_bytes: int
    source_fingerprint: str
    reims_registry_source: str | None = None
    already_migrated: bool = False


RuntimeState = Literal["stopped", "running", "unknown"]


def _safe_root(path: str | Path) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute():
        raise NamespaceMigrationError("Os caminhos de origem e destino precisam ser absolutos.")
    if value.is_symlink():
        raise NamespaceMigrationError(f"Diretório de namespace é link simbólico: {value}")
    absolute = Path(os.path.abspath(value))
    if value.resolve(strict=False) != absolute:
        raise NamespaceMigrationError(f"O caminho de namespace atravessa um link simbólico: {value}")
    return absolute


def _registry_path(path: str | Path | None) -> Path | None:
    if path is None:
        return None
    value = Path(path).expanduser()
    if not value.is_absolute():
        raise NamespaceMigrationError("O caminho do registro Reims precisa ser absoluto.")
    return _safe_root(value.parent) / value.name


def _metadata_sources(root: Path, reims_registry_source: str | Path | None) -> list[tuple[str, Path]]:
    """Allowlisted metadata map: destination-relative name -> source file."""
    candidates: list[tuple[str, Path]] = [
        ("vms.json", root / "vms.json"),
        ("settings.json", root / "settings.json"),
    ]
    vm_root = root / "vms"
    if vm_root.is_symlink():
        raise NamespaceMigrationError("A pasta de VMs é um link simbólico.")
    if vm_root.exists():
        if not vm_root.is_dir():
            raise NamespaceMigrationError("A pasta de VMs não é um diretório.")
        for child in sorted(vm_root.iterdir()):
            if child.is_symlink():
                raise NamespaceMigrationError(f"Diretório de VM é link simbólico: {child}")
            if child.is_dir():
                vm_toml = child / "vm.toml"
                if vm_toml.is_symlink():
                    raise NamespaceMigrationError(f"Metadata é um link simbólico: {vm_toml}")
                if vm_toml.exists():
                    candidates.append((vm_toml.relative_to(root).as_posix(), vm_toml))

    registry_path: Path | None = None
    if reims_registry_source is not None:
        # Reject symlinks in the file and every path component, then preserve
        # the caller's actual file path as the source of the immutable copy.
        registry_path = _registry_path(reims_registry_source)
        assert registry_path is not None
        if registry_path.is_symlink():
            raise NamespaceMigrationError("O registro Reims não pode ser link simbólico.")
        if not registry_path.exists():
            raise NamespaceMigrationError("O caminho informado para o registro Reims não existe.")
        if root == registry_path or root in registry_path.parents:
            raise NamespaceMigrationError("O registro Reims deve estar fora do namespace legado.")
        candidates.append(("reims-ctl/vms.toml", registry_path))

    files: list[tuple[str, Path]] = []
    total = 0
    for relative_path, candidate in candidates:
        # Path.exists() follows links and returns False for a broken symlink;
        # reject both kinds before deciding that a legacy entry is absent.
        if candidate.is_symlink():
            raise NamespaceMigrationError(f"Metadata é um link simbólico: {candidate}")
        if not candidate.exists():
            continue
        info = candidate.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise NamespaceMigrationError(f"Metadata não é um arquivo regular: {candidate}")
        total += info.st_size
        if total > _MAX_METADATA_BYTES:
            raise NamespaceMigrationError("Metadata excede o limite seguro de 32 MiB.")
        files.append((relative_path, candidate))
    if not files:
        raise NamespaceMigrationError("Nenhum arquivo de metadata conhecido foi encontrado na origem.")
    return files


def _manifest(files: list[tuple[str, Path]]) -> tuple[tuple[MigrationFile, ...], str]:
    entries: list[MigrationFile] = []
    for relative_path, path in files:
        digest = hashlib.sha256()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise NamespaceMigrationError(f"Metadata deixou de ser arquivo regular: {path}")
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        entries.append(MigrationFile(relative_path, info.st_size, digest.hexdigest()))
    canonical = json.dumps([entry.__dict__ for entry in entries], sort_keys=True, separators=(",", ":"))
    return tuple(entries), hashlib.sha256(canonical.encode()).hexdigest()


def _publish_without_replace(stage: Path, destination: Path) -> None:
    """Use Linux renameat2 so a concurrent destination cannot be replaced."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise NamespaceMigrationError("O kernel não oferece renameat2; publicação segura indisponível.")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    at_fdcwd, rename_noreplace = -100, 1
    result = renameat2(at_fdcwd, os.fsencode(stage), at_fdcwd, os.fsencode(destination), rename_noreplace)
    if result != 0:
        error = ctypes.get_errno()
        if error in (17, 39):  # EEXIST / ENOTEMPTY
            raise NamespaceMigrationError("O destino surgiu durante a migração; nada foi sobrescrito.")
        raise OSError(error, os.strerror(error), str(destination))


def _sync_tree_directories(root: Path) -> None:
    directories = [path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()]
    for directory in sorted([*directories, root], key=lambda item: len(item.parts), reverse=True):
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _mkdir_private(path: Path, boundary: Path) -> None:
    missing: list[Path] = []
    current = path
    while current != boundary:
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if directory.is_symlink() or not directory.is_dir():
                raise NamespaceMigrationError(f"Diretório inseguro durante a cópia: {directory}")


def preview_namespace_migration(
    source: str | Path,
    destination: str | Path,
    *,
    reims_registry_source: str | Path | None = None,
) -> MigrationPreview:
    """Read-only preview. It never creates directories, probes QEMU, or edits data."""
    src, dst = _safe_root(source), _safe_root(destination)
    if src == dst or src in dst.parents or dst in src.parents:
        raise NamespaceMigrationError("Origem e destino precisam ser diretórios separados.")
    if not src.is_dir():
        raise NamespaceMigrationError(f"A origem não existe ou não é diretório: {src}")
    registry_source = _registry_path(reims_registry_source)
    files = _metadata_sources(src, registry_source)
    manifest, fingerprint = _manifest(files)
    already = False
    marker = dst / _MARKER
    if marker.exists():
        if marker.is_symlink() or not marker.is_file():
            raise NamespaceMigrationError("Marcador de migração inválido; nenhum arquivo foi alterado.")
        try:
            document = json.loads(marker.read_text(encoding="utf-8"))
            already = (
                document.get("schema") == 1
                and document.get("source") == str(src)
                and document.get("reims_registry_source") == (str(registry_source) if registry_source else None)
                and document.get("source_fingerprint") == fingerprint
            )
        except (OSError, ValueError, AttributeError):
            already = False
        if not already:
            raise NamespaceMigrationError("Marcador existente não corresponde a esta origem e metadata.")
        for entry in manifest:
            target_file = dst / entry.relative_path
            if not target_file.is_file() or target_file.is_symlink():
                raise NamespaceMigrationError("Migração marcada, mas há arquivo ausente ou inseguro no destino.")
            _, actual = _manifest([(entry.relative_path, target_file)])
            expected = hashlib.sha256(
                json.dumps([entry.__dict__], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if actual != expected:
                raise NamespaceMigrationError("Migração marcada, mas metadata do destino diverge.")
    elif dst.exists():
        raise NamespaceMigrationError("O destino já existe sem marcador; não será mesclado nem sobrescrito.")
    return MigrationPreview(
        source=str(src),
        destination=str(dst),
        files=manifest,
        total_bytes=sum(item.size for item in manifest),
        source_fingerprint=fingerprint,
        reims_registry_source=str(registry_source) if registry_source else None,
        already_migrated=already,
    )


def migrate_namespace_offline(
    source: str | Path,
    destination: str | Path,
    *,
    runtime_state: RuntimeState,
    services_stopped: bool,
    preview: MigrationPreview | None = None,
    runtime_state_reader: Callable[[], dict[str, int] | None] | None = None,
    reims_registry_source: str | Path | None = None,
) -> MigrationPreview:
    """Copy metadata into a new namespace with backup, verification and marker.

    Callers must explicitly confirm offline maintenance. When a runtime reader
    is supplied it must report a complete empty snapshot; `None` means unknown.
    This function is never invoked automatically by the appliance.
    """
    if not services_stopped:
        raise NamespaceMigrationError("Pare o serviço web/agente e entre em manutenção offline antes de migrar.")
    if runtime_state != "stopped":
        raise NamespaceMigrationError("Migração recusada: o estado das VMs precisa ser confirmado como parado.")
    reader = runtime_state_reader
    if reader is None:
        # Resolve only when the operator explicitly calls the migration; import
        # and preview remain independent of the runtime and side-effect free.
        from core.reims_ctl import vm_runtime_snapshot

        reader = vm_runtime_snapshot
    observed = reader()
    if observed is None or observed:
        raise NamespaceMigrationError("Migração recusada: o inventário Reims está indisponível ou há VMs ativas.")

    if preview is not None:
        if reims_registry_source is not None:
            requested_registry = _registry_path(reims_registry_source)
            assert requested_registry is not None
            if preview.reims_registry_source != str(requested_registry):
                raise NamespaceMigrationError("A prévia usa outro caminho de registro Reims.")
        else:
            reims_registry_source = preview.reims_registry_source
    plan = preview or preview_namespace_migration(source, destination, reims_registry_source=reims_registry_source)
    if plan.source != str(_safe_root(source)) or plan.destination != str(_safe_root(destination)):
        raise NamespaceMigrationError("A prévia não corresponde à origem/destino informados.")
    if plan.already_migrated:
        return plan
    src, dst = Path(plan.source), Path(plan.destination)
    # Re-read immediately before copying to reject a stale preview.
    current = preview_namespace_migration(src, dst, reims_registry_source=plan.reims_registry_source)
    if current.source_fingerprint != plan.source_fingerprint:
        raise NamespaceMigrationError("A origem mudou depois da prévia; gere uma nova prévia.")

    dst.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{dst.name}.migration-", dir=dst.parent))
    try:
        backup_root = stage / _BACKUP_DIR
        backup_root.mkdir(mode=0o700)
        for entry in current.files:
            source_file = (
                Path(current.reims_registry_source)
                if entry.relative_path == "reims-ctl/vms.toml" and current.reims_registry_source
                else src / entry.relative_path
            )
            target_file = stage / entry.relative_path
            backup_file = backup_root / entry.relative_path
            _mkdir_private(target_file.parent, stage)
            _mkdir_private(backup_file.parent, backup_root)
            shutil.copy2(source_file, target_file, follow_symlinks=False)
            shutil.copy2(source_file, backup_file, follow_symlinks=False)

        # Verify source snapshot, target copy and backup before publication.
        # Re-enumerate the allowlist as well as hashing old paths so a newly
        # added settings/vm.toml file cannot be silently omitted mid-copy.
        source_after, source_fingerprint_after = _manifest(
            _metadata_sources(src, current.reims_registry_source)
        )
        if source_fingerprint_after != current.source_fingerprint or source_after != current.files:
            raise NamespaceMigrationError("A origem mudou durante a cópia; destino não publicado.")
        for entry in current.files:
            for copied in (stage / entry.relative_path, backup_root / entry.relative_path):
                if copied.is_symlink() or not copied.is_file() or copied.stat().st_size != entry.size:
                    raise NamespaceMigrationError(f"Cópia incompleta ou insegura: {entry.relative_path}")
                digest_state = hashlib.sha256()
                with copied.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest_state.update(chunk)
                    os.fsync(handle.fileno())
                digest = digest_state.hexdigest()
                if digest != entry.sha256:
                    raise NamespaceMigrationError(f"Verificação SHA-256 falhou: {entry.relative_path}")

        marker = {
            "schema": 1,
            "source": str(src),
            "destination": str(dst),
            "reims_registry_source": current.reims_registry_source,
            "source_fingerprint": current.source_fingerprint,
            "files": [entry.__dict__ for entry in current.files],
            "backup_directory": _BACKUP_DIR,
            "source_retained": True,
        }
        marker_path = stage / _MARKER
        marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with marker_path.open("rb") as handle:
            os.fsync(handle.fileno())
        _sync_tree_directories(stage)
        # Atomic publication; a pre-existing destination is never replaced.
        _publish_without_replace(stage, dst)
        parent_fd = os.open(dst.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        # Keep staging files for forensic recovery; never delete potentially
        # useful metadata after an interrupted offline migration.
        raise
    return dataclasses.replace(
        preview_namespace_migration(src, dst, reims_registry_source=current.reims_registry_source),
        already_migrated=False,
    )
