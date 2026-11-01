"""Private, bounded streaming storage for explicitly recognized transfer formats.

This module is deliberately not a VM media library: it accepts only content
whose small format signature can be checked and stores opaque transfer IDs.
Importing a transfer into a VM remains a separate, reviewed operation.
"""

from __future__ import annotations

import os
import fcntl
import re
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO


class TransferError(Exception):
    """Base class for expected transfer failures."""


class InvalidTransfer(TransferError):
    pass


class TransferTooLarge(TransferError):
    pass


class TransferQuotaExceeded(TransferError):
    pass


class TransferNotFound(TransferError):
    pass


@dataclass(frozen=True)
class Transfer:
    id: str
    filename: str
    size_bytes: int
    format: str


class TransferWriter:
    def __init__(self, store: "PrivateTransferStore", transfer_id: str, filename: str, fmt: str):
        self.store = store
        self.transfer_id = transfer_id
        self.filename = filename
        self.format = fmt
        self.stage = store.root / f".{transfer_id}.part"
        self.owner_lock = store.root / f".{transfer_id}.active"
        lock_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        self.owner_fd = os.open(self.owner_lock, lock_flags, 0o600)
        try:
            fcntl.flock(self.owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.owner_fd)
            self.owner_fd = -1
            try:
                self.owner_lock.unlink()
            except OSError:
                pass
            raise
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.stage, flags, 0o600)
        except BaseException:
            self._release_owner()
            raise
        self.file: BinaryIO = os.fdopen(fd, "wb")
        self.size = 0
        self.closed = False

    def _release_owner(self) -> None:
        if getattr(self, "owner_fd", -1) < 0:
            return
        try:
            fcntl.flock(self.owner_fd, fcntl.LOCK_UN)
        finally:
            os.close(self.owner_fd)
            self.owner_fd = -1
        try:
            info = self.owner_lock.lstat()
            if stat.S_ISREG(info.st_mode):
                self.owner_lock.unlink()
        except OSError:
            pass

    def write(self, chunk: bytes) -> None:
        if self.closed:
            raise InvalidTransfer("Transfer is already closed.")
        if not isinstance(chunk, bytes):
            raise InvalidTransfer("Transfer chunks must be bytes.")
        if not chunk:
            return
        with self.store._serialized():
            if self.size + len(chunk) > self.store.max_file_bytes:
                raise TransferTooLarge("Transfer exceeds the per-file limit.")
            used = self.store._used_bytes_locked()
            if used + len(chunk) > self.store.max_total_bytes:
                raise TransferQuotaExceeded("Transfer storage quota is exhausted.")
            if shutil.disk_usage(self.store.root).free - len(chunk) < self.store.min_free_bytes:
                raise TransferQuotaExceeded("Transfer would consume reserved free space.")
            written = self.file.write(chunk)
            if written != len(chunk):
                raise OSError("short write")
            self.size += written

    def commit(self) -> Transfer:
        if self.closed:
            raise InvalidTransfer("Transfer is already closed.")
        try:
            if self.size == 0:
                raise InvalidTransfer("Empty transfers are not accepted.")
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()
            self.store._validate_format(self.stage, self.format, self.size)
            destination = self.store.root / f"{self.transfer_id}--{self.filename}"
            with self.store._serialized():
                if destination.exists() or destination.is_symlink():
                    raise InvalidTransfer("Transfer identifier collision.")
                os.replace(self.stage, destination)
                self.store._fsync_directory()
            self.closed = True
            self._release_owner()
            return Transfer(self.transfer_id, self.filename, self.size, self.format)
        except BaseException:
            self.abort()
            raise

    def abort(self) -> None:
        if self.closed:
            return
        try:
            if not self.file.closed:
                self.file.close()
        finally:
            with self.store._serialized():
                try:
                    info = self.stage.lstat()
                    if stat.S_ISREG(info.st_mode):
                        self.stage.unlink()
                except FileNotFoundError:
                    pass
            self.closed = True
            self._release_owner()


class PrivateTransferStore:
    """A private spool with content signature checks and serialized quotas."""

    _FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}$")
    _ID = re.compile(r"^[0-9a-f]{32}$")
    ALLOWED_FORMATS = frozenset({"iso", "qcow2", "dmg"})
    MEDIA_TYPES = {
        "iso": "application/x-iso9660-image",
        "qcow2": "application/x-qemu-disk",
        "dmg": "application/x-apple-diskimage",
    }

    def __init__(self, root: str | Path, *, max_file_bytes: int = 64 * 1024**3, max_total_bytes: int = 128 * 1024**3, min_free_bytes: int = 1024**3):
        if max_file_bytes < 1 or max_total_bytes < max_file_bytes or min_free_bytes < 0:
            raise ValueError("Transfer quotas must be positive and total quota must cover one maximum file.")
        self.root = Path(root)
        self.max_file_bytes = int(max_file_bytes)
        self.max_total_bytes = int(max_total_bytes)
        self.min_free_bytes = int(min_free_bytes)
        self._lock = threading.RLock()
        self._initialized = False
        self._last_reconcile_at = 0.0

    def _ensure_initialized(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.is_symlink() or not self.root.is_dir():
                raise ValueError("Transfer directory must be a real directory, not a symlink.")
            try:
                self.root.chmod(0o700)
            except OSError:
                pass
            self._initialized = True

    def _used_bytes_locked(self) -> int:
        total = 0
        for entry in self.root.iterdir():
            try:
                info = entry.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
        return total

    def _cleanup_abandoned_stages_locked(self) -> None:
        """Remove only staged uploads whose per-transfer owner lock is free.

        The quota lock serializes begin/cleanup across processes. A writer keeps
        its separate advisory lock for its entire lifetime, so another API
        worker cannot mistake an active upload for a crash remnant.
        """
        changed = False
        for entry in self.root.iterdir():
            match = re.fullmatch(r"\.([0-9a-f]{32})\.part", entry.name)
            if not match:
                continue
            try:
                stage_info = entry.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(stage_info.st_mode):
                continue
            owner = self.root / f".{match.group(1)}.active"
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            try:
                owner_fd = os.open(owner, flags, 0o600)
            except OSError:
                # A malformed owner marker is not authority to remove data.
                continue
            try:
                try:
                    fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue
                try:
                    current = entry.lstat()
                    if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (stage_info.st_dev, stage_info.st_ino):
                        entry.unlink()
                        changed = True
                except FileNotFoundError:
                    pass
                try:
                    owner_info = owner.lstat()
                    if stat.S_ISREG(owner_info.st_mode):
                        owner.unlink()
                        changed = True
                except FileNotFoundError:
                    pass
            finally:
                fcntl.flock(owner_fd, fcntl.LOCK_UN)
                os.close(owner_fd)
        if changed:
            self._fsync_directory()

    @contextmanager
    def _serialized(self):
        """Serialize quota checks across threads and API worker processes."""
        self._ensure_initialized()
        with self._lock:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.root / ".quota.lock", flags, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                now = time.monotonic()
                if now - self._last_reconcile_at >= 60:
                    self._cleanup_abandoned_stages_locked()
                    self._last_reconcile_at = now
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def begin(self, filename: str, fmt: str) -> TransferWriter:
        self._ensure_initialized()
        if fmt not in self.ALLOWED_FORMATS:
            raise InvalidTransfer("Transfer format is not allowed.")
        if not isinstance(filename, str) or not self._FILENAME.fullmatch(filename) or filename in {".", ".."}:
            raise InvalidTransfer("Filename must be a simple basename using safe ASCII characters.")
        if Path(filename).name != filename or "/" in filename or "\\" in filename:
            raise InvalidTransfer("Path components are not allowed in filenames.")
        if Path(filename).suffix.lower() != f".{fmt}":
            raise InvalidTransfer("Filename extension must match the declared format.")
        with self._serialized():
            if self._used_bytes_locked() >= self.max_total_bytes:
                raise TransferQuotaExceeded("Transfer storage quota is exhausted.")
            return TransferWriter(self, uuid.uuid4().hex, filename, fmt)

    def _validate_format(self, path: Path, fmt: str, size: int) -> None:
        with path.open("rb") as stream:
            if fmt == "qcow2":
                if size < 72 or stream.read(4) != b"QFI\xfb":
                    raise InvalidTransfer("File is not a supported QCOW2 image.")
            elif fmt == "iso":
                # Require a primary volume descriptor at the ISO9660-defined LBA 16.
                if size < 32775:
                    raise InvalidTransfer("File is too small to be an ISO9660 image.")
                stream.seek(16 * 2048)
                descriptor = stream.read(7)
                if len(descriptor) != 7 or descriptor[0] != 1 or descriptor[1:6] != b"CD001" or descriptor[6] != 1:
                    raise InvalidTransfer("File is not a supported ISO9660 image.")
            elif fmt == "dmg":
                if size < 512:
                    raise InvalidTransfer("File is too small to be a UDIF disk image.")
                stream.seek(size - 512)
                if stream.read(4) != b"koly":
                    raise InvalidTransfer("File is not a supported UDIF disk image.")

    def _transfer_path(self, transfer_id: str) -> tuple[Path, Transfer]:
        if not self._ID.fullmatch(transfer_id):
            raise TransferNotFound("Transfer not found.")
        prefix = f"{transfer_id}--"
        for entry in self.root.iterdir():
            if not entry.name.startswith(prefix):
                continue
            try:
                info = entry.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            filename = entry.name[len(prefix):]
            fmt = Path(filename).suffix.lower().lstrip(".")
            if fmt not in self.ALLOWED_FORMATS:
                continue
            return entry, Transfer(transfer_id, filename, info.st_size, fmt)
        raise TransferNotFound("Transfer not found.")

    def list(self) -> list[Transfer]:
        self._ensure_initialized()
        result: list[Transfer] = []
        with self._serialized():
            for entry in self.root.iterdir():
                match = re.match(r"^([0-9a-f]{32})--(.+)$", entry.name)
                if not match:
                    continue
                try:
                    info = entry.lstat()
                except FileNotFoundError:
                    continue
                fmt = Path(match.group(2)).suffix.lower().lstrip(".")
                if stat.S_ISREG(info.st_mode) and fmt in self.ALLOWED_FORMATS:
                    result.append(Transfer(match.group(1), match.group(2), info.st_size, fmt))
        return sorted(result, key=lambda item: item.id)

    def open_download(self, transfer_id: str) -> tuple[Transfer, BinaryIO]:
        self._ensure_initialized()
        path, item = self._transfer_path(transfer_id)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError as exc:
            raise TransferNotFound("Transfer not found.") from exc
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise TransferNotFound("Transfer not found.")
        return item, os.fdopen(fd, "rb")

    def delete(self, transfer_id: str) -> None:
        self._ensure_initialized()
        with self._serialized():
            path, _ = self._transfer_path(transfer_id)
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                path.unlink()
                self._fsync_directory()

    def _fsync_directory(self) -> None:
        fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
