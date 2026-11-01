"""Offline, atomic backups of the vHackintosh VM layout."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from core.config import CONFIG_DIR, VM_RESERVATIONS_LOCK, VMConfig, VMManagerStore


class VMBackupError(Exception):
    """Base class for backup failures."""


class VMBackupUnsafe(VMBackupError):
    pass


class VMBackupRunning(VMBackupError):
    pass


class VMBackupConflict(VMBackupError):
    pass


class VMBackupService:
    """Create a self-contained snapshot from the appliance's known VM layout.

    The process-wide reservation lock is held for the whole copy. This both
    excludes another vHackintosh launch and lets us fail closed on existing
    reservations/runtime state. Direct external Reims launches are detected by
    the authoritative `reims-ctl list --json` probe.
    """

    _VM_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
    _ARTIFACTS = ("macos.qcow2", "OpenCore.qcow2", "OVMF_CODE.fd", "OVMF_VARS.fd", "vm.toml")
    _MAX_QCOW_CHAIN = 16
    _MAX_BUNDLE_FILES = 300

    def __init__(
        self,
        catalog_path: str | Path,
        backup_root: str | Path,
        *,
        vm_storage_root: str | Path | None = None,
        reservations_lock: str | Path = VM_RESERVATIONS_LOCK,
        reservations_reader: Callable[[], list[dict]] | None = None,
        running_reader: Callable[[], tuple[dict, bool]] | None = None,
        qcow_inspector: Callable[[Path], list[Path] | None] | None = None,
    ) -> None:
        self.catalog_path = Path(catalog_path)
        self.backup_root = Path(backup_root)
        self.vm_storage_root = Path(vm_storage_root) if vm_storage_root else CONFIG_DIR / "vms"
        self.reservations_lock = Path(reservations_lock)
        self.reservations_reader = reservations_reader or self._read_reservations
        self.running_reader = running_reader or self._read_running_vms
        self.qcow_inspector = qcow_inspector or self._inspect_qcow

    @staticmethod
    def _read_reservations() -> list[dict]:
        from core.config import ExclusiveVMLock

        return ExclusiveVMLock._read_reservations()

    @staticmethod
    def _read_running_vms() -> tuple[dict, bool]:
        from core.config import ExclusiveVMLock

        return ExclusiveVMLock._running_reims_vms()

    @staticmethod
    def _inspect_qcow(path: Path) -> list[Path]:
        try:
            proc = subprocess.run(
                ["qemu-img", "info", "--backing-chain", "--output=json", "-U", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=15,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise VMBackupUnsafe("Não foi possível inspecionar a cadeia QCOW2 com segurança.") from exc
        if proc.returncode != 0:
            raise VMBackupUnsafe("qemu-img não conseguiu validar a cadeia QCOW2.")
        try:
            rows = json.loads(proc.stdout)
        except (ValueError, TypeError) as exc:
            raise VMBackupUnsafe("qemu-img retornou uma cadeia QCOW2 inválida.") from exc
        if not isinstance(rows, list) or not 1 <= len(rows) <= VMBackupService._MAX_QCOW_CHAIN:
            raise VMBackupUnsafe("A cadeia QCOW2 está vazia ou excede o limite suportado.")
        chain: list[Path] = []
        seen: set[Path] = set()
        try:
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or not isinstance(row.get("filename"), str):
                    raise VMBackupUnsafe("qemu-img retornou uma cadeia QCOW2 inválida.")
                filename = Path(row["filename"])
                resolved = filename.resolve(strict=True)
                if resolved in seen:
                    raise VMBackupUnsafe("A cadeia QCOW2 contém um ciclo.")
                if index:
                    previous = rows[index - 1]
                    backing = previous.get("full-backing-filename") or previous.get("backing-filename")
                    if not isinstance(backing, str) or not backing:
                        raise VMBackupUnsafe("qemu-img retornou uma cadeia QCOW2 inconsistente.")
                    backing_path = Path(backing)
                    if not backing_path.is_absolute():
                        backing_path = Path(previous["filename"]).parent / backing_path
                    if backing_path.resolve(strict=True) != resolved:
                        raise VMBackupUnsafe("A referência de backing QCOW2 não corresponde à cadeia detectada.")
                seen.add(resolved)
                chain.append(filename)
            if rows[-1].get("backing-filename"):
                raise VMBackupUnsafe("A cadeia QCOW2 excede os arquivos retornados pelo qemu-img.")
            expected = path.resolve(strict=True)
        except VMBackupError:
            raise
        except (KeyError, OSError, TypeError) as exc:
            raise VMBackupUnsafe("qemu-img não identificou a cadeia QCOW2 esperada.") from exc
        if chain[0] != expected:
            raise VMBackupUnsafe("qemu-img informou um arquivo diferente do disco configurado.")
        return chain

    def _safe_qcow_files(self, root: Path) -> list[Path]:
        """Return a QCOW root and its direct-child backing files, fail closed otherwise."""
        try:
            reported = self.qcow_inspector(root)
        except VMBackupError:
            raise
        except Exception as exc:
            raise VMBackupUnsafe("Não foi possível validar a cadeia QCOW2.") from exc
        # Keep existing injected inspectors usable: None means that the test or
        # caller validated only the requested standalone file.
        files = reported if isinstance(reported, (list, tuple)) else [root]
        if not 1 <= len(files) <= self._MAX_QCOW_CHAIN:
            raise VMBackupUnsafe("A cadeia QCOW2 excede o limite suportado.")
        try:
            parent = root.parent.resolve(strict=True)
            expected_root = root.resolve(strict=True)
        except OSError as exc:
            raise VMBackupUnsafe("Disco QCOW2 ausente ou não resolvido.") from exc
        result: list[Path] = []
        seen: set[Path] = set()
        for candidate in files:
            candidate = Path(candidate)
            try:
                info = candidate.lstat()
                resolved = candidate.resolve(strict=True)
            except OSError as exc:
                raise VMBackupUnsafe("Arquivo da cadeia QCOW2 ausente ou não resolvido.") from exc
            lexical = Path(os.path.normpath(os.path.abspath(candidate)))
            if (not stat.S_ISREG(info.st_mode) or lexical.parent != parent or resolved.parent != parent
                    or candidate.name in {"", ".", ".."}):
                raise VMBackupUnsafe("Backing files QCOW2 devem ser arquivos regulares no diretório gerenciado da VM.")
            if resolved in seen:
                raise VMBackupUnsafe("A cadeia QCOW2 contém um ciclo ou arquivo duplicado.")
            seen.add(resolved)
            result.append(resolved)
        if result[0] != expected_root:
            raise VMBackupUnsafe("A cadeia QCOW2 não começa no disco configurado.")
        return result

    @staticmethod
    def _ensure_real_directory(path: Path, *, create: bool = False) -> None:
        absolute = Path(os.path.normpath(path.absolute()))
        cursor = Path(absolute.anchor)
        for component in absolute.parts[1:]:
            cursor = cursor / component
            try:
                component_info = cursor.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(component_info.st_mode):
                raise VMBackupUnsafe("Caminhos de backup/VM não podem atravessar symlinks.")
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise VMBackupUnsafe("Diretório necessário não existe.") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise VMBackupUnsafe("Symlinks e diretórios inválidos não são aceitos no backup.")

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write in VM backup metadata")
            view = view[written:]

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @contextmanager
    def _vm_lock(self, vm_id: str) -> Iterator[None]:
        lock_dir = self.backup_root / ".locks"
        self._ensure_real_directory(lock_dir, create=True)
        lock_path = lock_dir / f"{vm_id}.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise VMBackupUnsafe("Lock do backup não é um arquivo regular.")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def _offline_guard(self, vm_id: str) -> Iterator[None]:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.reservations_lock, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise VMBackupUnsafe("Lock global de execução não é regular.")
            fcntl.flock(fd, fcntl.LOCK_EX)
            reservations = self.reservations_reader()
            if any(str(row.get("vm_id") or "") == vm_id for row in reservations):
                raise VMBackupRunning("A VM possui uma reserva ativa; backup offline recusado.")
            running, probe_ok = self.running_reader()
            if not probe_ok:
                raise VMBackupUnsafe("Não foi possível confirmar o estado offline pelo Reims.")
            if vm_id in running:
                raise VMBackupRunning("A VM está em execução; backup offline recusado.")
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _load_vm_and_catalog(self, vm_id: str) -> tuple[VMConfig, bytes, str]:
        try:
            self._ensure_real_directory(self.catalog_path.parent)
            fd = os.open(self.catalog_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
            try:
                catalog_info = os.fstat(fd)
                if not stat.S_ISREG(catalog_info.st_mode):
                    raise VMBackupUnsafe("O catálogo deve ser um arquivo regular, sem symlink.")
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    catalog_bytes = handle.read()
                after = os.fstat(fd)
                if (catalog_info.st_dev, catalog_info.st_ino, catalog_info.st_size, catalog_info.st_mtime_ns,
                    catalog_info.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
                ):
                    raise VMBackupConflict("O catálogo mudou durante a leitura; tente novamente.")
            finally:
                os.close(fd)
            if not stat.S_ISREG(catalog_info.st_mode):
                raise VMBackupUnsafe("O catálogo deve ser um arquivo regular, sem symlink.")
            raw = json.loads(catalog_bytes.decode("utf-8"))
            rows = raw.get("vms") if isinstance(raw, dict) else None
            if not isinstance(rows, dict):
                raise VMBackupUnsafe("O catálogo não tem o formato esperado.")
            vm_data = rows.get(vm_id)
            vm = VMConfig.from_dict(vm_data) if isinstance(vm_data, dict) else None
            if vm is None:
                raise VMBackupUnsafe("VM não encontrada no catálogo.")
            return vm, catalog_bytes, hashlib.sha256(catalog_bytes).hexdigest()
        except VMBackupError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise VMBackupUnsafe("Não foi possível ler a identidade do catálogo da VM.") from exc

    def _source_artifacts(self, vm: VMConfig, vm_id: str) -> tuple[Path, list[Path], list[Path]]:
        vm_dir = self.vm_storage_root / vm_id
        self._ensure_real_directory(self.vm_storage_root)
        self._ensure_real_directory(vm_dir)
        expected_disk = vm_dir / "macos.qcow2"
        if not vm.disk_path or Path(vm.disk_path).expanduser() != expected_disk:
            raise VMBackupUnsafe("O caminho do disco não corresponde ao layout gerenciado desta VM.")

        artifacts: list[Path] = []
        for name in self._ARTIFACTS:
            path = vm_dir / name
            try:
                info = path.lstat()
            except FileNotFoundError as exc:
                raise VMBackupUnsafe(f"Artifact obrigatório ausente: {name}.") from exc
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise VMBackupUnsafe(f"Artifact não regular ou vazio: {name}.")
            artifacts.append(path)
        additional_paths = self._validated_additional_paths(vm, vm_id, require_files=True)
        return vm_dir, artifacts, additional_paths

    def _validated_additional_paths(
        self, vm: VMConfig, vm_id: str, *, require_files: bool,
    ) -> list[Path]:
        vm_dir = self.vm_storage_root / vm_id
        config_root = self.vm_storage_root.parent
        self._ensure_real_directory(config_root)
        config_root = config_root.resolve(strict=True)
        vm_dir_real = vm_dir.resolve(strict=False)
        additional_paths: list[Path] = []
        main_artifacts = {(vm_dir / name).resolve(strict=False) for name in self._ARTIFACTS}
        for disk in vm.additional_disks or []:
            path = Path(disk.path).expanduser()
            if not path.is_absolute():
                raise VMBackupUnsafe("O caminho de disco adicional precisa ser absoluto e resolvido.")
            if any(part in {".", ".."} for part in path.parts):
                raise VMBackupUnsafe("O caminho de disco adicional contém componentes ambíguos.")
            lexical = Path(os.path.normpath(path))
            try:
                lexical.relative_to(config_root)
            except ValueError as exc:
                raise VMBackupUnsafe("Discos adicionais fora do armazenamento gerenciado não entram no backup.") from exc
            relative = lexical.relative_to(config_root)
            if not relative.parts or relative.parts[0] not in {"vms", "media", "images"}:
                raise VMBackupUnsafe("Discos adicionais devem estar em vms/, media/ ou images/ gerenciados.")
            if relative.parts[0] == "vms" and not lexical.is_relative_to(vm_dir_real):
                raise VMBackupUnsafe("Um backup não pode incluir discos pertencentes a outra VM.")
            self._ensure_real_directory(lexical.parent)
            try:
                info = lexical.lstat()
            except FileNotFoundError:
                if require_files:
                    raise VMBackupUnsafe("Disco adicional ausente ou inacessível.")
                info = None
            except OSError as exc:
                raise VMBackupUnsafe("Disco adicional inacessível.") from exc
            if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_size <= 0):
                raise VMBackupUnsafe("Disco adicional precisa ser um arquivo regular e não vazio.")
            resolved = lexical.resolve(strict=require_files)
            if resolved.parent != lexical.parent.resolve(strict=True):
                raise VMBackupUnsafe("Disco adicional não pode resolver para fora do seu diretório gerenciado.")
            if resolved in main_artifacts or resolved in additional_paths:
                raise VMBackupUnsafe("Discos adicionais não podem duplicar outro disco ou artifact da VM.")
            if disk.format not in {"qcow2", "raw"}:
                raise VMBackupUnsafe("Formato de disco adicional não suportado para backup.")
            additional_paths.append(resolved)
        return additional_paths

    @staticmethod
    def _safe_path_component(part: str) -> bool:
        return (
            bool(part) and part not in {".", ".."} and "/" not in part and "\\" not in part
            and "\x00" not in part and len(os.fsencode(part)) <= 255
            and not any(ord(char) < 0x20 for char in part)
        )

    @staticmethod
    def _bundle_file_path(bundle: Path, name: str) -> Path:
        """Resolve a v1 manifest file name without allowing path traversal or links."""
        if not isinstance(name, str) or name.startswith("/") or "\\" in name:
            raise VMBackupUnsafe("Nome de arquivo inválido no manifesto.")
        parts = name.split("/")
        if any(not VMBackupService._safe_path_component(part) for part in parts):
            raise VMBackupUnsafe("Caminho inválido no manifesto.")
        if len(parts) == 1:
            return bundle / parts[0]
        if len(parts) != 3 or parts[0] != "additional-disks" or not re.fullmatch(r"(?:0[0-9]|1[0-4])", parts[1]):
            raise VMBackupUnsafe("Caminho de disco adicional inválido no manifesto.")
        parent = bundle
        for part in parts[:-1]:
            parent = parent / part
            try:
                info = parent.lstat()
            except OSError as exc:
                raise VMBackupUnsafe("Diretório do disco não pôde ser lido com segurança.") from exc
            if not stat.S_ISDIR(info.st_mode):
                raise VMBackupUnsafe("Diretório do disco no bundle não é regular.")
        return parent / parts[-1]

    @staticmethod
    def _bundle_name(path: Path, bundle: Path) -> str:
        try:
            return path.relative_to(bundle).as_posix()
        except ValueError as exc:
            raise VMBackupUnsafe("Arquivo da cadeia não está confinado ao bundle.") from exc

    @classmethod
    def _bundle_inventory(cls, bundle: Path) -> tuple[set[str], int]:
        names: set[str] = set()
        total_bytes = 0
        for entry in bundle.iterdir():
            info = entry.lstat()
            if entry.name == "additional-disks":
                if not stat.S_ISDIR(info.st_mode):
                    raise VMBackupUnsafe("Diretório de discos adicionais inválido no bundle.")
                cls._ensure_real_directory(entry)
                for disk_dir in entry.iterdir():
                    disk_info = disk_dir.lstat()
                    if (not re.fullmatch(r"(?:0[0-9]|1[0-4])", disk_dir.name)
                            or not stat.S_ISDIR(disk_info.st_mode)):
                        raise VMBackupUnsafe("Diretório de disco adicional inválido no bundle.")
                    cls._ensure_real_directory(disk_dir)
                    for file_path in disk_dir.iterdir():
                        file_info = file_path.lstat()
                        if not stat.S_ISREG(file_info.st_mode):
                            raise VMBackupUnsafe("O bundle contém arquivo adicional não regular.")
                        name = file_path.relative_to(bundle).as_posix()
                        cls._bundle_file_path(bundle, name)
                        names.add(name)
                        total_bytes += file_info.st_size
            elif stat.S_ISREG(info.st_mode):
                names.add(entry.name)
                total_bytes += info.st_size
            else:
                raise VMBackupUnsafe("O bundle contém diretório ou arquivo não permitido.")
        return names, total_bytes

    def _additional_disk_files(
        self, vm: VMConfig, target_paths: list[Path], bundle: Path,
        expected_hashes: dict[str, str] | None = None,
    ) -> list[tuple[str, Path]]:
        """Map each archived additional disk-chain file to its managed restore path."""
        mapped: list[tuple[str, Path]] = []
        seen_targets: dict[Path, str] = {}
        for order, (disk, target) in enumerate(zip(vm.additional_disks or [], target_paths)):
            archive_root = bundle / "additional-disks" / f"{order:02d}" / target.name
            chain = self._safe_qcow_files(archive_root) if disk.format == "qcow2" else [archive_root]
            for archived_path in chain:
                archive_name = self._bundle_name(archived_path, bundle)
                destination = target.parent / archived_path.name
                prior = seen_targets.get(destination)
                if prior is not None and prior != archive_name:
                    # Shared backings are copied once only if the bytes agree.
                    if (expected_hashes is None or expected_hashes.get(prior) is None
                            or expected_hashes.get(prior) != expected_hashes.get(archive_name)):
                        raise VMBackupUnsafe("Cadeias adicionais compartilham um backing file com conteúdos diferentes.")
                else:
                    seen_targets[destination] = archive_name
                mapped.append((archive_name, destination))
        return mapped

    def _restore_external_paths(
        self, vm_id: str, targets: list[tuple[str, Path]], token: str,
    ) -> list[dict[str, object]]:
        config_root = self.vm_storage_root.parent.resolve(strict=True)
        operations: list[dict[str, object]] = []
        seen: set[Path] = set()
        for _archive_name, target in targets:
            target = Path(os.path.normpath(os.path.abspath(target)))
            if target.parent.resolve(strict=True) == (self.vm_storage_root / vm_id).resolve(strict=True):
                continue
            self._ensure_real_directory(target.parent)
            resolved_parent = target.parent.resolve(strict=True)
            if target.parent != resolved_parent:
                raise VMBackupUnsafe("O diretório do disco adicional mudou durante a restauração.")
            try:
                relative = target.relative_to(config_root)
            except ValueError as exc:
                raise VMBackupUnsafe("Destino de disco adicional fora do appliance.") from exc
            if not relative.parts or relative.parts[0] not in {"media", "images"}:
                raise VMBackupUnsafe("Restauração externa aceita apenas media/ e images/ gerenciados.")
            if target in seen:
                continue
            seen.add(target)
            try:
                current = target.lstat()
                if not stat.S_ISREG(current.st_mode):
                    raise VMBackupUnsafe("Destino de disco adicional deixou de ser um arquivo regular.")
                had_target = True
            except FileNotFoundError:
                had_target = False
            index = len(operations)
            stage = target.with_name(f".{vm_id}.restore-{token}-{index}.new")
            rollback = target.with_name(f".{vm_id}.restore-{token}-{index}.rollback")
            try:
                stage.lstat()
                stage_exists = True
            except FileNotFoundError:
                stage_exists = False
            try:
                rollback.lstat()
                rollback_exists = True
            except FileNotFoundError:
                rollback_exists = False
            if stage_exists or rollback_exists:
                raise VMBackupConflict("Já existe um arquivo temporário de restauração neste armazenamento.")
            operations.append({
                "target": relative.as_posix(),
                "stage": stage.name,
                "rollback": rollback.name,
                "had_target": had_target,
                "archive_name": _archive_name,
                "_target_path": target,
                "_stage_path": stage,
                "_rollback_path": rollback,
            })
        return operations

    def _journal_external_paths(self, vm_id: str, payload: dict[str, object]) -> list[dict[str, object]]:
        if payload.get("version") != 2:
            return []
        token = payload.get("token")
        entries = payload.get("external_files")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ValueError("invalid external restore token")
        if not isinstance(entries, list) or len(entries) > self._MAX_BUNDLE_FILES:
            raise ValueError("invalid external restore file list")
        config_root = self.vm_storage_root.parent.resolve(strict=True)
        result: list[dict[str, object]] = []
        seen: set[Path] = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError("invalid external restore row")
            target_rel = entry.get("target")
            if not isinstance(target_rel, str) or "\\" in target_rel or target_rel.startswith("/"):
                raise ValueError("invalid external restore target")
            parts = target_rel.split("/")
            if (len(parts) < 2 or any(part in {"", ".", ".."} for part in parts)
                    or parts[0] not in {"media", "images"}
                    or any(not self._safe_path_component(part) for part in parts)):
                raise ValueError("external restore target escaped managed roots")
            stage_name = f".{vm_id}.restore-{token}-{index}.new"
            rollback_name = f".{vm_id}.restore-{token}-{index}.rollback"
            if entry.get("stage") != stage_name or entry.get("rollback") != rollback_name or not isinstance(entry.get("had_target"), bool):
                raise ValueError("invalid external restore temporary names")
            target = config_root.joinpath(*parts)
            self._ensure_real_directory(target.parent)
            if target.parent.resolve(strict=True) != target.parent:
                raise ValueError("external restore parent changed")
            if target in seen:
                raise ValueError("duplicate external restore target")
            seen.add(target)
            stage = target.with_name(stage_name)
            rollback = target.with_name(rollback_name)
            for candidate in (target, stage, rollback):
                try:
                    info = candidate.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("external restore path is not a regular file")
            result.append({
                "target_path": target, "stage_path": stage, "rollback_path": rollback,
                "had_target": entry["had_target"],
            })
        return result

    def _rollback_external_paths(self, entries: list[dict[str, object]]) -> None:
        for entry in reversed(entries):
            target = Path(entry["target_path"])
            stage = Path(entry["stage_path"])
            rollback = Path(entry["rollback_path"])
            if rollback.exists():
                if target.exists():
                    target.unlink()
                os.replace(rollback, target)
            elif not entry["had_target"] and not stage.exists() and target.exists():
                target.unlink()
            stage.unlink(missing_ok=True)
            self._fsync_directory(target.parent)

    @staticmethod
    def _cleanup_external_paths(entries: list[dict[str, object]]) -> None:
        for entry in entries:
            rollback = entry.get("_rollback_path", entry.get("rollback_path"))
            stage = entry.get("_stage_path", entry.get("stage_path"))
            parent = Path(rollback).parent
            Path(rollback).unlink(missing_ok=True)
            Path(stage).unlink(missing_ok=True)
            VMBackupService._fsync_directory(parent)

    @staticmethod
    def _validate_vm_toml(
        path: Path, vm_id: str, vm: VMConfig | None = None, *, reject_unowned_disks: bool = False,
    ) -> dict:
        try:
            data = __import__("tomllib").loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise VMBackupUnsafe("vm.toml não pôde ser validado.") from exc
        if data.get("name") != vm_id:
            raise VMBackupUnsafe("vm.toml não identifica a VM do catálogo.")
        if data.get("installer"):
            raise VMBackupUnsafe("Backup de uma VM ainda vinculada a um instalador externo não é suportado.")
        expected_values = {
            "disk": "macos.qcow2",
            "opencore": "OpenCore.qcow2",
            "firmware_code": "OVMF_CODE.fd",
            "firmware_vars": "OVMF_VARS.fd",
        }
        q35 = data.get("q35")
        if not isinstance(q35, dict):
            raise VMBackupUnsafe("Configuração q35/EFI ausente em vm.toml.")
        for key, value in expected_values.items():
            actual = data.get(key) if key == "disk" else q35.get(key)
            if actual != value:
                raise VMBackupUnsafe(f"Artifact {key} não corresponde ao layout conhecido.")
        if vm is not None:
            raw_disks = data.get("additional_disks", [])
            if not isinstance(raw_disks, list):
                raise VMBackupUnsafe("Lista de discos adicionais inválida em vm.toml.")
            if vm.additional_disks is None and raw_disks and reject_unowned_disks:
                raise VMBackupUnsafe("vm.toml contém discos adicionais sem identidade correspondente no catálogo.")
            expected_disks = [
                {"path": disk.path, "format": disk.format, "controller": disk.controller, "order": disk.order}
                for disk in (vm.additional_disks or [])
            ]
            if vm.additional_disks is not None and raw_disks != expected_disks:
                raise VMBackupUnsafe("vm.toml e catálogo divergem na configuração dos discos adicionais.")
        return data

    @staticmethod
    def _copy_hash(
        source: Path,
        destination: Path,
        *,
        check_cancelled: Callable[[], None] | None = None,
        on_progress: Callable[[int], None] | None = None,
        already_copied: int = 0,
    ) -> tuple[int, str]:
        source_info = source.lstat()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        source_fd = os.open(source, flags)
        dest_fd = -1
        try:
            opened = os.fstat(source_fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (source_info.st_dev, source_info.st_ino):
                raise VMBackupUnsafe("Artifact mudou ou deixou de ser regular durante o backup.")
            dest_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            digest = hashlib.sha256()
            size = 0
            while True:
                if check_cancelled is not None:
                    check_cancelled()
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(dest_fd, view)
                    if written <= 0:
                        raise OSError("short write in VM backup")
                    view = view[written:]
                if on_progress is not None:
                    on_progress(already_copied + size)
            if size != opened.st_size:
                raise VMBackupUnsafe("Artifact mudou de tamanho durante o backup.")
            final = os.fstat(source_fd)
            if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns) != (
                opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns,
            ):
                raise VMBackupUnsafe("Artifact foi alterado durante a cópia.")
            os.fsync(dest_fd)
            return size, digest.hexdigest()
        finally:
            os.close(source_fd)
            if dest_fd >= 0:
                os.close(dest_fd)

    def create(
        self,
        vm_id: str,
        *,
        check_cancelled: Callable[[], None] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        if not isinstance(vm_id, str) or not self._VM_ID.fullmatch(vm_id):
            raise VMBackupUnsafe("Identificador da VM inválido.")
        self._ensure_real_directory(self.vm_storage_root)
        self._ensure_real_directory(self.backup_root, create=True)
        try:
            self.backup_root.chmod(0o700)
        except OSError:
            pass

        with self._vm_lock(vm_id), self._offline_guard(vm_id):
            vm, catalog_bytes, catalog_sha = self._load_vm_and_catalog(vm_id)
            vm_dir, artifacts, additional_paths = self._source_artifacts(vm, vm_id)
            vm_toml = vm_dir / "vm.toml"
            self._validate_vm_toml(vm_toml, vm_id, vm, reject_unowned_disks=True)
            archive_sources: dict[str, Path] = {path.name: path for path in artifacts}
            # Resolve and validate backing files before copying anything. Only
            # direct siblings of the managed VM directory are accepted, so a
            # relative backing name remains valid in both the bundle and restore.
            for disk in (vm_dir / "macos.qcow2", vm_dir / "OpenCore.qcow2"):
                for backing_file in self._safe_qcow_files(disk):
                    archive_name = backing_file.name
                    prior = archive_sources.get(archive_name)
                    if prior is not None and prior.resolve(strict=True) != backing_file.resolve(strict=True):
                        raise VMBackupUnsafe("Duas cadeias QCOW2 usam o mesmo nome para arquivos diferentes.")
                    archive_sources[archive_name] = backing_file
            for order, (disk_config, disk_path) in enumerate(zip(vm.additional_disks or [], additional_paths)):
                archive_dir = f"additional-disks/{order:02d}"
                chain = self._safe_qcow_files(disk_path) if disk_config.format == "qcow2" else [disk_path]
                for chain_file in chain:
                    archive_name = f"{archive_dir}/{chain_file.name}"
                    if archive_name in archive_sources:
                        raise VMBackupUnsafe("Caminho duplicado no bundle de discos adicionais.")
                    archive_sources[archive_name] = chain_file

            backup_vm_root = self.backup_root / vm_id
            self._ensure_real_directory(backup_vm_root, create=True)
            try:
                backup_vm_root.chmod(0o700)
            except OSError:
                pass
            stage = Path(tempfile.mkdtemp(prefix=".backup-", dir=backup_vm_root))
            os.chmod(stage, 0o700)
            published: Path | None = None
            try:
                records = []
                total_bytes = sum(path.stat(follow_symlinks=False).st_size for path in archive_sources.values())
                copied_bytes = 0
                for archive_name, source in sorted(archive_sources.items()):
                    if check_cancelled is not None:
                        check_cancelled()
                    if archive_name.startswith("additional-disks/"):
                        stage.joinpath(*archive_name.split("/")[:-1]).mkdir(parents=True, exist_ok=True, mode=0o700)
                    target = self._bundle_file_path(stage, archive_name)
                    size, digest = self._copy_hash(
                        source,
                        target,
                        check_cancelled=check_cancelled,
                        already_copied=copied_bytes,
                        on_progress=(lambda copied: on_progress(copied, total_bytes)) if on_progress else None,
                    )
                    copied_bytes += size
                    records.append({"name": archive_name, "size_bytes": size, "sha256": digest})
                self._validate_vm_toml(stage / "vm.toml", vm_id)
                self._safe_qcow_files(stage / "macos.qcow2")
                self._safe_qcow_files(stage / "OpenCore.qcow2")
                for order, disk_config in enumerate(vm.additional_disks or []):
                    if disk_config.format == "qcow2":
                        self._safe_qcow_files(stage / f"additional-disks/{order:02d}/{Path(disk_config.path).name}")
                config_file = stage / "vm-config.json"
                config_bytes = json.dumps({
                    "vm_id": vm.id,
                    "config": vm.to_dict(),
                    "catalog_sha256": catalog_sha,
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                fd = os.open(config_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    self._write_all(fd, config_bytes)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                records.append({"name": "vm-config.json", "size_bytes": len(config_bytes), "sha256": hashlib.sha256(config_bytes).hexdigest()})
                manifest = {
                    "format": "vhackintosh-vm-backup",
                    "version": 1,
                    "vm_id": vm_id,
                    "catalog_sha256": catalog_sha,
                    "files": records,
                }
                manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
                manifest_path = stage / "manifest.json"
                fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    self._write_all(fd, manifest_bytes)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.utime(stage, None, follow_symlinks=False)
                for child in sorted(stage.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                    if child.is_dir() and not child.is_symlink():
                        self._fsync_directory(child)
                self._fsync_directory(stage)
                if check_cancelled is not None:
                    check_cancelled()
                destination = backup_vm_root / f"{uuid.uuid4().hex}.vhb"
                libc = __import__("ctypes").CDLL(None, use_errno=True)
                renameat2 = getattr(libc, "renameat2", None)
                if renameat2 is None:
                    raise VMBackupUnsafe("Este sistema não oferece publicação atômica sem sobrescrita.")
                renameat2.argtypes = [__import__("ctypes").c_int, __import__("ctypes").c_char_p,
                                      __import__("ctypes").c_int, __import__("ctypes").c_char_p,
                                      __import__("ctypes").c_uint]
                renameat2.restype = __import__("ctypes").c_int
                if renameat2(-100, os.fsencode(stage), -100, os.fsencode(destination), 1) != 0:
                    error = __import__("ctypes").get_errno()
                    raise OSError(error, os.strerror(error), str(destination))
                published = destination
                root_fd = os.open(backup_vm_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(root_fd)
                finally:
                    os.close(root_fd)
                backup_root_fd = os.open(self.backup_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(backup_root_fd)
                finally:
                    os.close(backup_root_fd)
                return published
            finally:
                if published is None and stage.exists():
                    shutil.rmtree(stage)

    def list_backups(self, vm_id: str) -> list[dict[str, object]]:
        """List published backup bundles without reading large disk contents.

        A metadata-valid entry has a well-formed manifest whose declared files
        all exist as regular files at the declared sizes. SHA-256 verification
        is intentionally deferred to an explicit verification job so refreshing
        this view remains cheap.
        """
        if not isinstance(vm_id, str) or not self._VM_ID.fullmatch(vm_id):
            raise VMBackupUnsafe("Identificador da VM inválido.")
        try:
            root_info = self.backup_root.lstat()
        except FileNotFoundError:
            return []
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise VMBackupUnsafe("Diretório de backup inválido.")
        self._ensure_real_directory(self.backup_root)
        vm_root = self.backup_root / vm_id
        try:
            vm_root_info = vm_root.lstat()
        except FileNotFoundError:
            return []
        if stat.S_ISLNK(vm_root_info.st_mode) or not stat.S_ISDIR(vm_root_info.st_mode):
            raise VMBackupUnsafe("Diretório de backup da VM inválido.")
        self._ensure_real_directory(vm_root)

        items: list[dict[str, object]] = []
        for directory in sorted(vm_root.iterdir(), key=lambda item: item.name, reverse=True):
            if not re.fullmatch(r"[0-9a-f]{32}\.vhb", directory.name):
                continue
            try:
                directory_info = directory.lstat()
                if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
                    continue
                self._ensure_real_directory(directory)
                names, total_bytes = self._bundle_inventory(directory)
                manifest_path = directory / "manifest.json"
                fd = os.open(manifest_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 128 * 1024:
                        raise VMBackupUnsafe("Manifesto inválido.")
                    with os.fdopen(fd, "rb", closefd=False) as handle:
                        manifest = json.loads(handle.read().decode("utf-8"))
                finally:
                    os.close(fd)
                manifest = manifest if isinstance(manifest, dict) else {}
                records = manifest.get("files")
                valid = (
                    manifest.get("format") == "vhackintosh-vm-backup"
                    and manifest.get("version") == 1
                    and manifest.get("vm_id") == vm_id
                    and isinstance(records, list)
                    and 1 <= len(records) <= VMBackupService._MAX_BUNDLE_FILES
                )
                declared: set[str] = set()
                if valid:
                    for record in records:
                        if not isinstance(record, dict):
                            valid = False
                            break
                        name, size, digest = record.get("name"), record.get("size_bytes"), record.get("sha256")
                        if (
                            not isinstance(name, str)
                            or name == "manifest.json"
                            or name in declared
                            or not isinstance(size, int)
                            or size <= 0
                            or not isinstance(digest, str)
                            or not re.fullmatch(r"[0-9a-f]{64}", digest)
                        ):
                            valid = False
                            break
                        try:
                            file_path = self._bundle_file_path(directory, name)
                        except VMBackupUnsafe:
                            valid = False
                            break
                        declared.add(name)
                        try:
                            file_info = file_path.lstat()
                        except FileNotFoundError:
                            valid = False
                            break
                        if not stat.S_ISREG(file_info.st_mode) or file_info.st_size != size:
                            valid = False
                            break
                    if valid and names != declared | {"manifest.json"}:
                        valid = False
                items.append({
                    "backup_id": directory.name,
                    "created_at": datetime.fromtimestamp(directory_info.st_mtime, timezone.utc).isoformat(),
                    "size_bytes": total_bytes,
                    "file_count": len(names),
                    "metadata_valid": bool(valid),
                    "integrity_note": (
                        "Manifesto e tamanhos conferem; SHA-256 ainda não foi verificado."
                        if valid else "Manifesto ou arquivos não correspondem; este backup não deve ser restaurado."
                    ),
                })
            except (OSError, ValueError, TypeError, VMBackupError):
                # Do not follow or expose paths from entries that cannot be
                # inspected safely.
                continue
        return sorted(items, key=lambda item: str(item["created_at"]), reverse=True)

    def verify_backup(
        self,
        vm_id: str,
        backup_id: str,
        *,
        check_cancelled: Callable[[], None] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, object]:
        """Recheck all file hashes and QCOW2 chains in a published backup."""
        if not isinstance(vm_id, str) or not self._VM_ID.fullmatch(vm_id):
            raise VMBackupUnsafe("Identificador da VM inválido.")
        if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9a-f]{32}\.vhb", backup_id):
            raise VMBackupUnsafe("Identificador do backup inválido.")
        entry = next((item for item in self.list_backups(vm_id) if item["backup_id"] == backup_id), None)
        if entry is None or entry["metadata_valid"] is not True:
            raise VMBackupUnsafe("Manifesto ou arquivos não correspondem; este backup não pode ser verificado.")
        bundle = self.backup_root / vm_id / backup_id
        manifest_path = bundle / "manifest.json"
        try:
            fd = os.open(manifest_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 128 * 1024:
                    raise VMBackupUnsafe("Manifesto inválido.")
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    manifest = json.loads(handle.read().decode("utf-8"))
            finally:
                os.close(fd)
        except (OSError, ValueError, TypeError) as exc:
            raise VMBackupUnsafe("Manifesto não pôde ser lido com segurança.") from exc
        if (not isinstance(manifest, dict) or manifest.get("format") != "vhackintosh-vm-backup"
                or manifest.get("version") != 1 or manifest.get("vm_id") != vm_id):
            raise VMBackupUnsafe("Manifesto não corresponde a este backup.")
        required = set(self._ARTIFACTS) | {"vm-config.json"}
        records = manifest.get("files")
        if not isinstance(records, list) or not len(required) <= len(records) <= self._MAX_BUNDLE_FILES:
            raise VMBackupUnsafe("Manifesto não contém um conjunto válido de arquivos.")
        by_name: dict[str, dict] = {}
        for record in records:
            if not isinstance(record, dict):
                raise VMBackupUnsafe("Manifesto contém um registro inválido.")
            name, size, digest = record.get("name"), record.get("size_bytes"), record.get("sha256")
            if (not isinstance(name, str) or name == "manifest.json" or name in by_name
                    or isinstance(size, bool) or not isinstance(size, int) or size <= 0
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise VMBackupUnsafe("Manifesto contém nome, tamanho ou checksum inválido.")
            try:
                self._bundle_file_path(bundle, name)
            except VMBackupUnsafe as exc:
                raise VMBackupUnsafe("Manifesto contém nome, tamanho ou checksum inválido.") from exc
            by_name[name] = record
        if not required <= set(by_name):
            raise VMBackupUnsafe("Manifesto está incompleto.")
        total_bytes = sum(int(row["size_bytes"]) for row in by_name.values())
        checked = 0
        for name in sorted(by_name):
            if check_cancelled is not None:
                check_cancelled()
            path = self._bundle_file_path(bundle, name)
            try:
                before = path.lstat()
                if not stat.S_ISREG(before.st_mode) or before.st_size != by_name[name]["size_bytes"]:
                    raise VMBackupUnsafe("Arquivo do backup ausente, alterado ou não regular.")
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
            except OSError as exc:
                raise VMBackupUnsafe("Arquivo do backup não pôde ser aberto com segurança.") from exc
            digest = hashlib.sha256()
            size = 0
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise VMBackupUnsafe("Arquivo mudou durante a verificação.")
                while True:
                    if check_cancelled is not None:
                        check_cancelled()
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    if on_progress is not None:
                        on_progress(checked + size, total_bytes)
                final = os.fstat(fd)
                if size != opened.st_size or (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns) != (
                    opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns,
                ):
                    raise VMBackupUnsafe("Arquivo mudou durante a verificação.")
            finally:
                os.close(fd)
            if digest.hexdigest() != by_name[name]["sha256"]:
                raise VMBackupUnsafe(f"Checksum não confere para {name}.")
            checked += size
            if on_progress is not None:
                on_progress(checked, total_bytes)

        if check_cancelled is not None:
            check_cancelled()
        config_path = bundle / "vm-config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise VMBackupUnsafe("Configuração armazenada no backup é inválida.") from exc
        if (not isinstance(config, dict) or config.get("vm_id") != vm_id
                or not isinstance(config.get("config"), dict)
                or config["config"].get("id") != vm_id
                or config.get("catalog_sha256") != manifest.get("catalog_sha256")):
            raise VMBackupUnsafe("Configuração no backup não corresponde à identidade declarada.")
        try:
            saved_vm = VMConfig.from_dict(config["config"])
            additional_paths = self._validated_additional_paths(saved_vm, vm_id, require_files=False)
        except (ValueError, TypeError, RuntimeError) as exc:
            raise VMBackupUnsafe("Discos adicionais no backup não apontam para o armazenamento gerenciado.") from exc
        self._validate_vm_toml(bundle / "vm.toml", vm_id, saved_vm)
        if check_cancelled is not None:
            check_cancelled()
        expected_files = set(required)
        for root_name in ("macos.qcow2", "OpenCore.qcow2"):
            chain = self._safe_qcow_files(bundle / root_name)
            expected_files.update(path.name for path in chain)
        additional_files = self._additional_disk_files(
            saved_vm, additional_paths, bundle,
            {name: str(row["sha256"]) for name, row in by_name.items()},
        )
        expected_files.update(name for name, _target in additional_files)
        if set(by_name) != expected_files:
            raise VMBackupUnsafe("Manifesto contém arquivos de disco ausentes, extras ou não referenciados.")
        if check_cancelled is not None:
            check_cancelled()
        self._safe_qcow_files(bundle / "OpenCore.qcow2")
        if check_cancelled is not None:
            check_cancelled()
        return {"vm_id": vm_id, "backup_id": backup_id, "verified": True, "file_count": len(by_name), "size_bytes": checked}

    def restore(
        self,
        vm_id: str,
        backup_id: str,
        *,
        check_cancelled: Callable[[], None] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, object]:
        """Restore a verified backup over the same managed VM, with rollback journal.

        The old directory is retained until the catalog has been published. A
        small journal lets the next restore attempt roll back an interrupted
        directory swap before doing any new work.
        """
        if not isinstance(vm_id, str) or not self._VM_ID.fullmatch(vm_id):
            raise VMBackupUnsafe("Identificador da VM inválido.")
        if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9a-f]{32}\.vhb", backup_id):
            raise VMBackupUnsafe("Identificador do backup inválido.")
        self._ensure_real_directory(self.vm_storage_root)
        self._ensure_real_directory(self.backup_root)
        with self._vm_lock(vm_id), self._offline_guard(vm_id):
            self._recover_restore(vm_id)
            verification = self.verify_backup(vm_id, backup_id, check_cancelled=check_cancelled)
            bundle = self.backup_root / vm_id / backup_id
            config_path = bundle / "vm-config.json"
            try:
                saved = json.loads(config_path.read_text(encoding="utf-8"))
                restored_config = VMConfig.from_dict(saved["config"])
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                raise VMBackupUnsafe("Configuração do backup não pôde ser restaurada.") from exc
            if restored_config.id != vm_id:
                raise VMBackupUnsafe("A identidade do backup não corresponde à VM de destino.")
            vm_dir = self.vm_storage_root / vm_id
            self._ensure_real_directory(vm_dir)
            expected_disk = vm_dir / "macos.qcow2"
            if Path(restored_config.disk_path or "").expanduser() != expected_disk:
                raise VMBackupUnsafe("O backup não aponta para o disco gerenciado esperado.")
            store = VMManagerStore(self.catalog_path)
            store.load()
            current = store.get_vm(vm_id)
            if current is None:
                raise VMBackupConflict("A VM de destino não existe mais no catálogo.")
            total_bytes = int(verification["size_bytes"])
            if shutil.disk_usage(self.vm_storage_root).free < total_bytes + 16 * 1024 * 1024:
                raise VMBackupConflict("Espaço livre insuficiente para preparar a restauração com segurança.")

            additional_targets = self._validated_additional_paths(restored_config, vm_id, require_files=False)
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            by_name = {entry["name"]: entry for entry in manifest["files"] if isinstance(entry, dict)}
            additional_files = self._additional_disk_files(
                restored_config, additional_targets, bundle,
                {name: str(row["sha256"]) for name, row in by_name.items()},
            )
            vm_dir_real = vm_dir.resolve(strict=True)
            local_additional: dict[str, str] = {}
            external_targets: list[tuple[str, Path]] = []
            external_by_target: dict[Path, str] = {}
            for archive_name, destination in additional_files:
                if destination.parent.resolve(strict=True) == vm_dir_real:
                    if destination.name in self._ARTIFACTS:
                        raise VMBackupUnsafe("Disco adicional conflita com artifact obrigatório da VM.")
                    prior = local_additional.get(destination.name)
                    if prior is not None and by_name[prior]["sha256"] != by_name[archive_name]["sha256"]:
                        raise VMBackupUnsafe("Duas cadeias adicionais conflitam no diretório da VM.")
                    local_additional[destination.name] = archive_name
                else:
                    previous_archive = external_by_target.get(destination)
                    if previous_archive is not None:
                        if by_name[previous_archive]["sha256"] != by_name[archive_name]["sha256"]:
                            raise VMBackupUnsafe("Cadeias adicionais compartilham um destino com conteúdos diferentes.")
                        continue
                    external_by_target[destination] = archive_name
                    external_targets.append((archive_name, destination))

            external_sizes: dict[int, tuple[Path, int]] = {}
            for archive_name, destination in external_targets:
                device = destination.parent.stat().st_dev
                prior = external_sizes.get(device)
                external_sizes[device] = (
                    prior[0] if prior else destination.parent,
                    (prior[1] if prior else 0) + int(by_name[archive_name]["size_bytes"]),
                )
            for parent, needed in external_sizes.values():
                if shutil.disk_usage(parent).free < needed + 16 * 1024 * 1024:
                    raise VMBackupConflict("Espaço livre insuficiente no armazenamento de um disco adicional.")

            # Refuse to rewrite an additional disk that another VM still
            # references in the saved catalog, even when that VM is stopped.
            other_vms = [vm for vm in store.list_vms() if vm.id != vm_id]
            claimed = set()
            for other in other_vms:
                for configured in ([other.disk_path] if other.disk_path else []) + [disk.path for disk in (other.additional_disks or [])]:
                    path_value = configured.path if hasattr(configured, "path") else configured
                    try:
                        claimed.add(Path(path_value).expanduser().resolve(strict=False))
                    except (OSError, RuntimeError):
                        continue
            if any(target.resolve(strict=False) in claimed for _name, target in external_targets):
                raise VMBackupConflict("Um disco adicional deste backup também está atribuído a outra VM.")

            token = uuid.uuid4().hex
            stage = self.vm_storage_root / f".{vm_id}.restore-{token}"
            rollback = self.vm_storage_root / f".{vm_id}.rollback-{token}"
            journal = self.vm_storage_root / f".{vm_id}.restore-journal.json"
            if rollback.exists() or journal.exists():
                raise VMBackupConflict("Já existe uma recuperação de restauração em andamento para esta VM.")
            stage.mkdir(mode=0o700)
            records = []
            copied = 0
            external_ops: list[dict[str, object]] = []
            try:
                external_ops = self._restore_external_paths(vm_id, external_targets, token)
                scheduled: dict[Path, str] = {}
                external_names = {name for name, target in additional_files if target.parent.resolve(strict=True) != vm_dir_real}
                local_archive_names = {name for name, target in additional_files if target.parent.resolve(strict=True) == vm_dir_real}
                for name in sorted(set(by_name) - {"vm-config.json"} - external_names - local_archive_names):
                    if name.startswith("additional-disks/"):
                        # Local-to-VM additional disks are materialized below
                        # at their original sibling names.
                        continue
                    scheduled[stage / name] = name
                for filename, archive_name in local_additional.items():
                    destination = stage / filename
                    if destination in scheduled and scheduled[destination] != archive_name:
                        raise VMBackupUnsafe("Disco adicional conflita com outro arquivo da VM.")
                    scheduled[destination] = archive_name
                copy_tasks = [(name, destination) for destination, name in scheduled.items()]
                copy_tasks.extend((str(row["archive_name"]), Path(row["_stage_path"])) for row in external_ops)
                total_copy = sum(int(by_name[name]["size_bytes"]) for name, _destination in copy_tasks)
                for name, destination in copy_tasks:
                    if check_cancelled is not None:
                        check_cancelled()
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    size, digest = self._copy_hash(
                        self._bundle_file_path(bundle, name),
                        destination,
                        check_cancelled=check_cancelled,
                        already_copied=copied,
                        on_progress=(lambda amount: on_progress(amount, total_copy)) if on_progress else None,
                    )
                    if size != by_name[name]["size_bytes"] or digest != by_name[name]["sha256"]:
                        raise VMBackupUnsafe(f"Checksum não confere ao preparar {name}.")
                    copied += size
                    records.append(name)
                self._validate_vm_toml(stage / "vm.toml", vm_id, restored_config)
                self._safe_qcow_files(stage / "macos.qcow2")
                self._safe_qcow_files(stage / "OpenCore.qcow2")
                for disk_config, target in zip(restored_config.additional_disks or [], additional_targets):
                    if (disk_config.format == "qcow2"
                            and target.parent.resolve(strict=True) == vm_dir_real):
                        self._safe_qcow_files(stage / target.name)
                if check_cancelled is not None:
                    check_cancelled()
                self._fsync_directory(stage)
                for parent in {Path(op["_stage_path"]).parent for op in external_ops}:
                    self._fsync_directory(parent)
                payload = {
                    "version": 2,
                    "vm_id": vm_id,
                    "stage": stage.name,
                    "rollback": rollback.name,
                    "token": token,
                    "backup_id": backup_id,
                    "previous_config": current.to_dict(),
                    "external_files": [
                        {key: value for key, value in op.items() if not key.startswith("_")}
                        for op in external_ops
                    ],
                    "phase": "prepared",
                }
                self._write_restore_journal(journal, payload)
                os.replace(vm_dir, rollback)
                payload["phase"] = "old_moved"
                self._write_restore_journal(journal, payload)
                os.replace(stage, vm_dir)
                payload["phase"] = "new_published"
                self._write_restore_journal(journal, payload)
                if external_ops:
                    payload["phase"] = "external_publishing"
                    self._write_restore_journal(journal, payload)
                    for op in external_ops:
                        target = Path(op["_target_path"])
                        backup_path = Path(op["_rollback_path"])
                        staged_path = Path(op["_stage_path"])
                        if op["had_target"]:
                            os.replace(target, backup_path)
                        os.replace(staged_path, target)
                        parent_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                        try:
                            os.fsync(parent_fd)
                        finally:
                            os.close(parent_fd)
                        self._write_restore_journal(journal, payload)
                try:
                    store.update_vm(restored_config)
                except Exception:
                    try:
                        self._recover_restore(vm_id)
                    except (OSError, VMBackupError):
                        # Keep the journal and whichever directories remain so
                        # the next attempt can perform a deterministic rollback.
                        pass
                    raise
                payload["phase"] = "catalog_published"
                self._write_restore_journal(journal, payload)
                self._cleanup_external_paths(self._journal_external_paths(vm_id, payload))
                shutil.rmtree(rollback)
                journal.unlink(missing_ok=True)
                parent_fd = os.open(self.vm_storage_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
                return {"vm_id": vm_id, "backup_id": backup_id, "restored": True, "file_count": len(records)}
            except Exception:
                # Before journaling, staging is disposable. After journaling,
                # leave recovery metadata intact if rollback itself fails.
                if journal.exists():
                    try:
                        self._recover_restore(vm_id)
                    except (OSError, VMBackupError):
                        pass
                else:
                    if stage.exists():
                        shutil.rmtree(stage, ignore_errors=True)
                    self._cleanup_external_paths(external_ops)
                raise

    def _write_restore_journal(self, path: Path, payload: dict[str, object]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            self._write_all(fd, json.dumps(payload, sort_keys=True).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    def _recover_restore(self, vm_id: str) -> None:
        journal = self.vm_storage_root / f".{vm_id}.restore-journal.json"
        try:
            info = journal.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            raise VMBackupUnsafe("Journal de restauração inválido.")
        try:
            fd = os.open(journal, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or opened.st_size > 64 * 1024:
                    raise ValueError("restore journal is not a small regular file")
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    payload = json.loads(handle.read().decode("utf-8"))
            finally:
                os.close(fd)
            stage_name, rollback_name = payload["stage"], payload["rollback"]
            valid_names = (
                isinstance(stage_name, str) and re.fullmatch(rf"\.{re.escape(vm_id)}\.restore-[0-9a-f]{{32}}", stage_name)
                and isinstance(rollback_name, str) and re.fullmatch(rf"\.{re.escape(vm_id)}\.rollback-[0-9a-f]{{32}}", rollback_name)
                and payload.get("vm_id") == vm_id and payload.get("version") in {1, 2}
            )
            if not valid_names:
                raise ValueError("invalid restore paths")
            external_paths = self._journal_external_paths(vm_id, payload)
            stage = self.vm_storage_root / stage_name
            rollback = self.vm_storage_root / rollback_name
            live = self.vm_storage_root / vm_id
            for candidate in (stage, rollback, live):
                try:
                    candidate_info = candidate.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(candidate_info.st_mode):
                    raise ValueError("restore path is not a real directory")
            if payload.get("phase") == "catalog_published":
                if rollback.exists():
                    shutil.rmtree(rollback)
                self._cleanup_external_paths(external_paths)
            elif rollback.exists():
                self._rollback_external_paths(external_paths)
                previous = payload.get("previous_config")
                if not isinstance(previous, dict) or previous.get("id") != vm_id:
                    raise ValueError("restore journal has no previous VM identity")
                store = VMManagerStore(self.catalog_path)
                store.load()
                current = store.get_vm(vm_id)
                if current is None or current.to_dict() != previous:
                    store.update_vm(VMConfig.from_dict(previous))
                if live.exists():
                    shutil.rmtree(live)
                os.replace(rollback, live)
            elif payload.get("phase") in {"old_moved", "new_published"}:
                self._rollback_external_paths(external_paths)
                raise ValueError("restore rollback directory is missing")
            else:
                self._rollback_external_paths(external_paths)
            if stage.exists():
                shutil.rmtree(stage)
            journal.unlink()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise VMBackupUnsafe("Uma restauração interrompida precisa de recuperação manual; os arquivos foram preservados.") from exc
