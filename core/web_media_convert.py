"""Cancellable, staged conversion of managed raw/QCOW2 media."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class MediaConversionError(Exception):
    """Base class for media conversion failures."""


class InvalidMediaConversion(MediaConversionError):
    pass


class MediaConversionCollision(MediaConversionError):
    pass


class MediaConversionCancelled(MediaConversionError):
    pass


@dataclass(frozen=True)
class ConvertedMedia:
    name: str
    source_format: str
    format: str
    size_bytes: int


class ManagedMediaConverter:
    """Convert only files contained in one managed media directory.

    QCOW2 inputs must be standalone. This avoids silently following a backing
    file outside the managed library. The temporary output is private and on
    the destination filesystem; Linux renameat2 publishes it without replacing
    an existing library item.
    """

    _NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}$")
    _FORMAT_EXTENSIONS = {"raw": {".raw", ".img"}, "qcow2": {".qcow2"}}
    _PROGRESS = re.compile(rb"\(([0-9]+(?:\.[0-9]+)?)/100(?:\.0+)?%\)")
    _CHUNK = 64 * 1024

    def __init__(
        self,
        media_root: str | Path,
        *,
        qemu_img: str = "qemu-img",
        timeout_seconds: float = 6 * 60 * 60,
        max_virtual_bytes: int = 512 * 1024**3,
        check_cancelled: Callable[[], None] | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> None:
        if timeout_seconds <= 0 or max_virtual_bytes < 1:
            raise ValueError("timeout and virtual size limits must be positive")
        self.media_root = Path(media_root)
        self.qemu_img = qemu_img
        self.timeout_seconds = float(timeout_seconds)
        self.max_virtual_bytes = int(max_virtual_bytes)
        self.check_cancelled = check_cancelled
        self.on_progress = on_progress

    @staticmethod
    def _rename_noreplace(source: Path, destination: Path) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise InvalidMediaConversion("Sistema sem publicação atômica sem sobrescrita.")
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) == 0:
            return
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise MediaConversionCollision("Já existe uma mídia com esse nome.")
        raise OSError(error, os.strerror(error), str(destination))

    def _check_cancelled(self) -> None:
        if self.check_cancelled is not None:
            self.check_cancelled()

    def _validate_name(self, name: str, fmt: str) -> None:
        if not isinstance(name, str) or not self._NAME.fullmatch(name) or name in {".", ".."}:
            raise InvalidMediaConversion("Nome de mídia inválido.")
        if Path(name).name != name or "/" in name or "\\" in name:
            raise InvalidMediaConversion("Caminhos externos não são permitidos.")
        if fmt not in self._FORMAT_EXTENSIONS or Path(name).suffix.lower() not in self._FORMAT_EXTENSIONS[fmt]:
            raise InvalidMediaConversion("A extensão do arquivo não corresponde ao formato declarado.")

    def _source_fd(self, path: Path) -> int:
        absolute_root = Path(os.path.normpath(self.media_root.absolute()))
        cursor = Path(absolute_root.anchor)
        for component in absolute_root.parts[1:]:
            cursor = cursor / component
            try:
                component_info = cursor.lstat()
            except FileNotFoundError as exc:
                raise InvalidMediaConversion("A biblioteca gerenciada não existe.") from exc
            if stat.S_ISLNK(component_info.st_mode):
                raise InvalidMediaConversion("A biblioteca gerenciada não pode atravessar symlinks.")
        root_info = self.media_root.lstat()
        if not stat.S_ISDIR(root_info.st_mode):
            raise InvalidMediaConversion("A biblioteca gerenciada deve ser um diretório real.")
        source_info = path.lstat()
        if not stat.S_ISREG(source_info.st_mode) or source_info.st_size <= 0:
            raise InvalidMediaConversion("A origem deve ser um arquivo regular não vazio.")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (source_info.st_dev, source_info.st_ino):
            os.close(fd)
            raise InvalidMediaConversion("A origem mudou durante a validação.")
        return fd

    def _run_capture(self, argv: list[str], *, pass_fds: tuple[int, ...] = (), check_cancelled: bool = True) -> bytes:
        if check_cancelled:
            self._check_cancelled()
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                close_fds=True,
                start_new_session=True,
                pass_fds=pass_fds,
            )
        except OSError as exc:
            raise InvalidMediaConversion("qemu-img não conseguiu inspecionar a imagem.") from exc
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        result = bytearray()
        deadline = time.monotonic() + min(self.timeout_seconds, 30)
        try:
            while True:
                if check_cancelled:
                    self._check_cancelled()
                if time.monotonic() >= deadline:
                    raise TimeoutError("qemu-img inspection exceeded its timeout")
                for key, _ in selector.select(timeout=0.1):
                    chunk = os.read(key.fd, 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if len(result) + len(chunk) > 1024 * 1024:
                        raise InvalidMediaConversion("qemu-img retornou metadados acima do limite.")
                    result.extend(chunk)
                returncode = process.poll()
                if returncode is not None and not selector.get_map():
                    break
            if returncode != 0:
                raise InvalidMediaConversion("qemu-img rejeitou o formato da imagem.")
        except BaseException:
            self._terminate_group(process)
            raise
        finally:
            selector.close()
            process.stdout.close()
        if returncode != 0:
            raise InvalidMediaConversion("qemu-img rejeitou o formato da imagem.")
        return bytes(result)

    @staticmethod
    def _terminate_group(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    def _inspect_source(self, path: str, fmt: str, *, pass_fds: tuple[int, ...] = ()) -> dict:
        raw = self._run_capture([
            self.qemu_img, "info", "--backing-chain", "--output=json", "-f", fmt, path,
        ], pass_fds=pass_fds)
        try:
            rows = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise InvalidMediaConversion("qemu-img retornou metadados inválidos.") from exc
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise InvalidMediaConversion("Não é permitido converter QCOW2 com backing chain.")
        row = rows[0]
        if row.get("format") != fmt or row.get("backing-filename"):
            raise InvalidMediaConversion("Formato incompatível ou backing chain não suportada.")
        virtual_size = row.get("virtual-size")
        if isinstance(virtual_size, bool) or not isinstance(virtual_size, int) or not 0 < virtual_size <= self.max_virtual_bytes:
            raise InvalidMediaConversion("Tamanho virtual inválido ou acima do limite da conversão.")
        try:
            if not pass_fds and Path(row["filename"]).resolve(strict=True) != Path(path).resolve(strict=True):
                raise InvalidMediaConversion("qemu-img inspecionou um arquivo diferente da origem.")
        except (KeyError, OSError) as exc:
            raise InvalidMediaConversion("qemu-img não identificou a origem esperada.") from exc
        return row

    def _convert_process(self, argv: list[str], *, pass_fds: tuple[int, ...] = ()) -> None:
        self._check_cancelled()
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                start_new_session=True,
                pass_fds=pass_fds,
            )
        except OSError as exc:
            raise InvalidMediaConversion("Não foi possível iniciar qemu-img convert.") from exc
        assert process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stderr, selectors.EVENT_READ)
        deadline = time.monotonic() + self.timeout_seconds
        pending = bytearray()
        reported = -1.0
        try:
            while True:
                self._check_cancelled()
                if time.monotonic() >= deadline:
                    raise TimeoutError("qemu-img convert excedeu o tempo limite")
                for key, _ in selector.select(timeout=0.1):
                    chunk = os.read(key.fd, self._CHUNK)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    pending.extend(chunk)
                    for match in self._PROGRESS.finditer(pending):
                        value = min(100.0, max(0.0, float(match.group(1))))
                        if value > reported:
                            reported = value
                            if self.on_progress is not None:
                                self.on_progress(value)
                    if len(pending) > 256:
                        del pending[:-64]
                returncode = process.poll()
                if returncode is not None and not selector.get_map():
                    break
            if returncode != 0:
                raise InvalidMediaConversion("qemu-img não conseguiu converter a imagem.")
            if self.on_progress is not None and reported < 100:
                self.on_progress(100.0)
        except BaseException:
            self._terminate_group(process)
            raise
        finally:
            selector.close()
            process.stderr.close()

    def convert(self, source_name: str, destination_name: str, source_format: str, output_format: str) -> ConvertedMedia:
        if (not isinstance(source_format, str) or not isinstance(output_format, str)
                or source_format not in self._FORMAT_EXTENSIONS or output_format not in self._FORMAT_EXTENSIONS):
            raise InvalidMediaConversion("Formato de entrada ou saída não permitido.")
        self._validate_name(source_name, source_format)
        self._validate_name(destination_name, output_format)
        if source_name == destination_name:
            raise InvalidMediaConversion("A origem e o destino precisam ser diferentes.")

        source = self.media_root / source_name
        destination = self.media_root / destination_name
        source_fd = self._source_fd(source)
        source_stat = os.fstat(source_fd)
        stage_dir = self.media_root / f".convert-{uuid.uuid4().hex}"
        stage_file = stage_dir / destination_name
        published = False
        try:
            fd_source = f"/proc/self/fd/{source_fd}"
            self._inspect_source(fd_source, source_format, pass_fds=(source_fd,))
            try:
                destination.lstat()
            except FileNotFoundError:
                pass
            else:
                raise MediaConversionCollision("Já existe uma mídia com esse nome.")
            os.mkdir(stage_dir, 0o700)
            self._convert_process([
                self.qemu_img, "convert", "-p", "-f", source_format, "-O", output_format,
                fd_source, str(stage_file),
            ], pass_fds=(source_fd,))
            self._check_cancelled()
            final_source = os.fstat(source_fd)
            if (final_source.st_dev, final_source.st_ino, final_source.st_size, final_source.st_mtime_ns,
                final_source.st_ctime_ns) != (source_stat.st_dev, source_stat.st_ino, source_stat.st_size,
                                               source_stat.st_mtime_ns, source_stat.st_ctime_ns):
                raise InvalidMediaConversion("A origem foi alterada durante a conversão.")
            output_info = stage_file.lstat()
            if not stat.S_ISREG(output_info.st_mode) or output_info.st_size <= 0:
                raise InvalidMediaConversion("qemu-img não produziu um arquivo regular válido.")
            output_raw = self._run_capture([
                self.qemu_img, "info", "--output=json", "-f", output_format, str(stage_file),
            ])
            try:
                output_info_json = json.loads(output_raw)
            except (ValueError, TypeError) as exc:
                raise InvalidMediaConversion("qemu-img retornou metadados inválidos para a saída.") from exc
            if not isinstance(output_info_json, dict) or output_info_json.get("format") != output_format:
                raise InvalidMediaConversion("Formato da imagem convertida não corresponde ao solicitado.")
            if output_format == "qcow2" and output_info_json.get("backing-filename"):
                raise InvalidMediaConversion("A saída QCOW2 não pode depender de uma backing chain.")
            stage_fd = os.open(stage_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fchmod(stage_fd, 0o600)
                os.fsync(stage_fd)
            finally:
                os.close(stage_fd)
            stage_dir_fd = os.open(stage_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(stage_dir_fd)
            finally:
                os.close(stage_dir_fd)
            self._rename_noreplace(stage_file, destination)
            directory_fd = os.open(self.media_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return ConvertedMedia(destination_name, source_format, output_format, output_info.st_size)
        finally:
            os.close(source_fd)
            if stage_dir.exists():
                shutil.rmtree(stage_dir)
