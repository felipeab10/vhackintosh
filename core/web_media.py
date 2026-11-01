"""Safe import of validated private uploads into the managed media library."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class MediaImportError(Exception):
    """Base class for expected media import failures."""


class InvalidMediaTransfer(MediaImportError):
    pass


class MediaTransferNotFound(MediaImportError):
    pass


class MediaAlreadyExists(MediaImportError):
    pass


class MediaTooLarge(MediaImportError):
    pass


@dataclass(frozen=True)
class ImportedMedia:
    id: str
    name: str
    size_bytes: int
    format: str


class ManagedMediaImporter:
    """Copy one private transfer into the managed library without replacement."""

    _ID = re.compile(r"^[0-9a-f]{32}$")
    _NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}$")
    _FORMATS = frozenset({"iso", "qcow2", "dmg"})

    def __init__(
        self,
        transfer_root: str | Path,
        media_root: str | Path,
        *,
        max_file_bytes: int = 64 * 1024**3,
        chunk_bytes: int = 1024 * 1024,
        on_progress: Callable[[int, int], None] | None = None,
        check_cancelled: Callable[[], None] | None = None,
    ) -> None:
        if max_file_bytes < 1 or chunk_bytes < 1:
            raise ValueError("File and chunk limits must be positive.")
        self.transfer_root = Path(transfer_root)
        self.media_root = Path(media_root)
        self.max_file_bytes = int(max_file_bytes)
        self.chunk_bytes = min(int(chunk_bytes), 4 * 1024 * 1024)
        self.on_progress = on_progress
        self.check_cancelled = check_cancelled

    @staticmethod
    def _same_content(left_fd: int, right_fd: int, size: int, chunk_bytes: int) -> bool:
        """Compare bytes for safe retry after publish-before-checkpoint crashes."""
        left_hash = hashlib.sha256()
        right_hash = hashlib.sha256()
        offset = 0
        while offset < size:
            length = min(chunk_bytes, size - offset)
            left = os.pread(left_fd, length, offset)
            right = os.pread(right_fd, length, offset)
            if len(left) != length or len(right) != length:
                return False
            left_hash.update(left)
            right_hash.update(right)
            offset += length
        return left_hash.digest() == right_hash.digest()

    def _already_imported(self, source_fd: int, destination: Path, fmt: str, size: int) -> bool:
        try:
            before = destination.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            raise MediaAlreadyExists("A managed media file with this name already exists.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            destination_fd = os.open(destination, flags)
        except OSError as exc:
            raise MediaAlreadyExists("A managed media file with this name already exists.") from exc
        try:
            after = os.fstat(destination_fd)
            if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise MediaAlreadyExists("A managed media file with this name already exists.")
            self._validate_signature(destination_fd, fmt, size)
            if self._same_content(source_fd, destination_fd, size, self.chunk_bytes):
                return True
        finally:
            os.close(destination_fd)
        raise MediaAlreadyExists("A managed media file with this name already exists.")

    @staticmethod
    def _ensure_directory(path: Path, *, create: bool, secure_mode: bool) -> None:
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise InvalidMediaTransfer("Storage directory must be a real directory.")
        if secure_mode:
            try:
                path.chmod(0o700)
            except OSError:
                pass

    def _cleanup_staging_files(self) -> None:
        for entry in self.media_root.iterdir():
            if not re.fullmatch(r"\.import-[0-9a-f]{32}\.tmp", entry.name):
                continue
            try:
                info = entry.lstat()
                if stat.S_ISREG(info.st_mode):
                    entry.unlink()
            except FileNotFoundError:
                continue

    def _open_source(self, transfer_id: str) -> tuple[int, str, str, int]:
        try:
            root_info = self.transfer_root.lstat()
        except FileNotFoundError as exc:
            raise MediaTransferNotFound("Transfer not found.") from exc
        if not stat.S_ISDIR(root_info.st_mode):
            raise InvalidMediaTransfer("Transfer storage is not a real directory.")

        prefix = transfer_id + "--"
        for entry in self.transfer_root.iterdir():
            if not entry.name.startswith(prefix):
                continue
            name = entry.name[len(prefix):]
            if not self._NAME.fullmatch(name) or name in {".", ".."}:
                continue
            fmt = Path(name).suffix.lower().lstrip(".")
            if fmt not in self._FORMATS or Path(name).suffix.lower() != f".{fmt}":
                continue
            try:
                before = entry.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(before.st_mode):
                raise InvalidMediaTransfer("Transfer must be a regular file.")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            try:
                fd = os.open(entry, flags)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOENT}:
                    raise InvalidMediaTransfer("Transfer is not a regular file.") from exc
                raise
            after = os.fstat(fd)
            if (
                not stat.S_ISREG(after.st_mode)
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            ):
                os.close(fd)
                raise InvalidMediaTransfer("Transfer changed while it was being opened.")
            if after.st_size <= 0:
                os.close(fd)
                raise InvalidMediaTransfer("Empty media files are not accepted.")
            if after.st_size > self.max_file_bytes:
                os.close(fd)
                raise MediaTooLarge("Media file exceeds the configured limit.")
            return fd, name, fmt, after.st_size
        raise MediaTransferNotFound("Transfer not found.")

    @staticmethod
    def _validate_signature(fd: int, fmt: str, size: int) -> None:
        if fmt == "qcow2":
            if size < 72 or os.pread(fd, 4, 0) != b"QFI\xfb":
                raise InvalidMediaTransfer("File is not a supported QCOW2 image.")
        elif fmt == "iso":
            if size < 32775:
                raise InvalidMediaTransfer("File is too small to be an ISO9660 image.")
            descriptor = os.pread(fd, 7, 16 * 2048)
            if len(descriptor) != 7 or descriptor[0] != 1 or descriptor[1:6] != b"CD001" or descriptor[6] != 1:
                raise InvalidMediaTransfer("File is not a supported ISO9660 image.")
        elif fmt == "dmg":
            if size < 512 or os.pread(fd, 4, size - 512) != b"koly":
                raise InvalidMediaTransfer("File is not a supported UDIF disk image.")

    @staticmethod
    def _rename_noreplace(source: Path, destination: Path) -> None:
        """Publish with Linux renameat2(RENAME_NOREPLACE), with a safe fallback."""
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            renameat2.restype = ctypes.c_int
            result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
            if result == 0:
                return
            error = ctypes.get_errno()
            if error == errno.EEXIST:
                raise MediaAlreadyExists("A managed media file with this name already exists.")
            if error not in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                raise OSError(error, os.strerror(error), str(destination))

        # Hard-link creation is atomic and fails if the destination exists; both
        # paths are in the same media directory/filesystem.
        try:
            os.link(source, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise MediaAlreadyExists("A managed media file with this name already exists.") from exc
        source.unlink()

    def import_transfer(self, transfer_id: str) -> ImportedMedia:
        if not isinstance(transfer_id, str) or not self._ID.fullmatch(transfer_id):
            raise InvalidMediaTransfer("Transfer identifier is invalid.")
        # The upload spool belongs to the unprivileged HTTP service. The root
        # agent reads it, so it must never change the spool's owner permissions.
        self._ensure_directory(self.transfer_root, create=False, secure_mode=False)
        self._ensure_directory(self.media_root, create=True, secure_mode=True)
        self._cleanup_staging_files()

        source_fd, name, fmt, expected_size = self._open_source(transfer_id)
        self._validate_signature(source_fd, fmt, expected_size)
        stage = self.media_root / f".import-{secrets.token_hex(16)}.tmp"
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        stage_fd = -1
        published = False
        try:
            stage_fd = os.open(stage, flags, 0o600)
            copied = 0
            last_reported = 0
            while True:
                if self.check_cancelled is not None:
                    self.check_cancelled()
                chunk = os.read(source_fd, self.chunk_bytes)
                if not chunk:
                    break
                copied += len(chunk)
                if self.on_progress is not None and (copied - last_reported >= 64 * 1024**2 or copied == expected_size):
                    self.on_progress(copied, expected_size)
                    last_reported = copied
                if copied > self.max_file_bytes or copied > expected_size:
                    raise MediaTooLarge("Media file grew beyond the permitted size.")
                view = memoryview(chunk)
                while view:
                    written = os.write(stage_fd, view)
                    if written <= 0:
                        raise OSError("short write while importing media")
                    view = view[written:]
            if copied != expected_size:
                raise InvalidMediaTransfer("Transfer changed while it was being copied.")
            os.fsync(stage_fd)
            self._validate_signature(stage_fd, fmt, copied)
            os.close(stage_fd)
            stage_fd = -1

            destination = self.media_root / name
            # The final check is deliberately adjacent to the atomic publish:
            # a cancelled job must never leave a completed media file behind.
            if self.check_cancelled is not None:
                self.check_cancelled()
            try:
                self._rename_noreplace(stage, destination)
                published = True
            except MediaAlreadyExists:
                if not self._already_imported(source_fd, destination, fmt, expected_size):
                    raise
                stage.unlink(missing_ok=True)
                published = True
            directory_fd = os.open(self.media_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return ImportedMedia(transfer_id, name, copied, fmt)
        finally:
            os.close(source_fd)
            if stage_fd >= 0:
                os.close(stage_fd)
            if not published:
                try:
                    info = stage.lstat()
                    if stat.S_ISREG(info.st_mode):
                        stage.unlink()
                except FileNotFoundError:
                    pass
