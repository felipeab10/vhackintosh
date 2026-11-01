"""Safe offline cloning for the appliance's managed VM layout."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.config import AdditionalDiskConfig, SMBIOSConfig, VMConfig, VMManagerStore
from core.smbios import GenSMBIOS
from core.vm_backup import VMBackupConflict, VMBackupService, VMBackupUnsafe


class VMCloneService:
    """Clone one stopped managed VM without changing its source artifacts."""

    _VM_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

    def __init__(
        self,
        catalog_path: str | Path,
        *,
        vm_storage_root: str | Path,
        reservations_lock: str | Path | None = None,
        reservations_reader=None,
        running_reader=None,
        qcow_inspector=None,
        smbios_factory=None,
        mac_factory=None,
        opencore_rewriter=None,
        nvram_template: str | Path | None = None,
    ) -> None:
        self.catalog_path = Path(catalog_path)
        self.vm_storage_root = Path(vm_storage_root)
        kwargs = {}
        if reservations_lock is not None:
            kwargs["reservations_lock"] = reservations_lock
        if reservations_reader is not None:
            kwargs["reservations_reader"] = reservations_reader
        if running_reader is not None:
            kwargs["running_reader"] = running_reader
        if qcow_inspector is not None:
            kwargs["qcow_inspector"] = qcow_inspector
        self.guard = VMBackupService(
            self.catalog_path,
            self.vm_storage_root.parent / "backups",
            vm_storage_root=self.vm_storage_root,
            **kwargs,
        )
        self.smbios_factory = smbios_factory or self._generate_smbios
        self.mac_factory = mac_factory or self._generate_mac
        self.opencore_rewriter = opencore_rewriter or self._rewrite_opencore
        self.nvram_template = Path(nvram_template).expanduser() if nvram_template else None

    @staticmethod
    def _generate_smbios(vm: VMConfig) -> SMBIOSConfig:
        return GenSMBIOS.generate(vm.macos_version, model_override=vm.smbios.model)

    @staticmethod
    def _generate_mac() -> str:
        return "52:54:00:" + ":".join(f"{byte:02x}" for byte in secrets.token_bytes(3))

    @staticmethod
    def _rewrite_opencore(path: Path, vm: VMConfig) -> None:
        from core.disk import DiskProvisioner

        DiskProvisioner.inject_opencore_config(path, vm, show_picker=vm.opencore_show_picker)

    @staticmethod
    def _replace_toml_value(text: str, key: str, value: str, *, section: str | None = None) -> str:
        lines = text.splitlines(keepends=True)
        active_section = None
        matches = []
        for index, line in enumerate(lines):
            section_match = re.match(r"\s*\[([^]]+)\]\s*(?:#.*)?(?:\r?\n)?$", line)
            if section_match:
                active_section = section_match.group(1)
            if active_section == section and section is not None or section is None and active_section is None:
                if re.match(rf"\s*{re.escape(key)}\s*=", line):
                    matches.append(index)
        if len(matches) != 1:
            raise VMBackupUnsafe(f"vm.toml precisa conter exatamente um campo {section + '.' if section else ''}{key}.")
        index = matches[0]
        newline = "\r\n" if lines[index].endswith("\r\n") else "\n" if lines[index].endswith("\n") else ""
        lines[index] = f"{key} = {json.dumps(value, ensure_ascii=False)}{newline}"
        return "".join(lines)

    def _write_clone_journal(self, journal: Path, payload: dict) -> None:
        temporary = journal.with_name(f".{journal.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            self.guard._write_all(fd, json.dumps(payload, sort_keys=True).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, journal)
        parent = os.open(journal.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent)
        finally:
            os.close(parent)

    @staticmethod
    def _rename_noreplace(source: Path, destination: Path) -> None:
        libc = __import__("ctypes").CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise VMBackupUnsafe("Este sistema não oferece publicação atômica sem sobrescrita.")
        renameat2.argtypes = [__import__("ctypes").c_int, __import__("ctypes").c_char_p,
                              __import__("ctypes").c_int, __import__("ctypes").c_char_p,
                              __import__("ctypes").c_uint]
        renameat2.restype = __import__("ctypes").c_int
        if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
            error = __import__("ctypes").get_errno()
            raise OSError(error, os.strerror(error), str(destination))

    def _managed_disk_source(self, value: str) -> Path:
        """Resolve an additional disk only inside real managed storage roots."""
        if not isinstance(value, str) or not value:
            raise VMBackupUnsafe("Caminho de disco adicional inválido.")
        raw = Path(value).expanduser()
        if not raw.is_absolute() or any(part in {".", ".."} for part in raw.parts):
            raise VMBackupUnsafe("Discos adicionais precisam usar um caminho absoluto gerenciado.")
        config_root = self.vm_storage_root.parent
        try:
            config_root_info = config_root.lstat()
            config_real = config_root.resolve(strict=True)
        except OSError as exc:
            raise VMBackupUnsafe("Raiz de armazenamento gerenciado indisponível.") from exc
        if not stat.S_ISDIR(config_root_info.st_mode):
            raise VMBackupUnsafe("Raiz de armazenamento gerenciado inválida.")

        for dirname in ("vms", "media", "images"):
            root = config_root / dirname
            try:
                root_info = root.lstat()
                root_real = root.resolve(strict=True)
            except OSError:
                continue
            if not stat.S_ISDIR(root_info.st_mode) or root_real != root:
                continue
            lexical = Path(os.path.abspath(raw))
            if not lexical.is_relative_to(root):
                continue
            current = root
            safe = True
            for part in lexical.relative_to(root).parts[:-1]:
                current = current / part
                try:
                    if not stat.S_ISDIR(current.lstat().st_mode):
                        safe = False
                        break
                except OSError:
                    safe = False
                    break
            if not safe:
                raise VMBackupUnsafe("Caminho de disco adicional contém diretório inválido ou symlink.")
            try:
                leaf = lexical.lstat()
                resolved = lexical.resolve(strict=True)
            except OSError as exc:
                raise VMBackupUnsafe("Disco adicional ausente ou não resolvido.") from exc
            if (not stat.S_ISREG(leaf.st_mode) or resolved != lexical
                    or not resolved.is_relative_to(root_real)
                    or not resolved.is_relative_to(config_real)):
                raise VMBackupUnsafe("Discos adicionais precisam ser arquivos regulares dentro do armazenamento gerenciado.")
            return lexical
        raise VMBackupUnsafe("Disco adicional fora das raízes gerenciadas; clone recusado.")

    @staticmethod
    def _hash_regular_file(path: Path) -> tuple[int, str]:
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise VMBackupUnsafe("Artifact do clone não é arquivo regular.")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        except OSError as exc:
            raise VMBackupUnsafe("Não foi possível validar o arquivo do clone.") from exc
        digest = hashlib.sha256()
        size = 0
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise VMBackupUnsafe("Artifact do clone mudou durante a validação.")
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
            after = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
            ) or size != opened.st_size:
                raise VMBackupUnsafe("Artifact do clone foi alterado durante a validação.")
            return size, digest.hexdigest()
        finally:
            os.close(fd)

    @staticmethod
    def _qcow_virtual_size(path: Path) -> int:
        try:
            proc = subprocess.run(
                ["qemu-img", "info", "--output=json", "-U", str(path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=15, check=False, shell=False,
            )
            info = json.loads(proc.stdout) if proc.returncode == 0 else None
            size = info.get("virtual-size") if isinstance(info, dict) else None
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                raise ValueError("invalid virtual size")
            return size
        except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
            raise VMBackupUnsafe("Não foi possível estimar o espaço da cadeia QCOW2.") from exc

    @staticmethod
    def _flatten_qcow(source: Path, destination: Path, *, check_cancelled=None) -> None:
        """Materialize a QCOW2 backing chain as one independent QCOW2 file."""
        command = ["qemu-img", "convert", "-O", "qcow2", str(source), str(destination)]
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, shell=False, close_fds=True,
            )
        except OSError as exc:
            raise VMBackupUnsafe("qemu-img é necessário para materializar a cadeia QCOW2.") from exc
        deadline = time.monotonic() + 6 * 60 * 60
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise VMBackupUnsafe("qemu-img excedeu o limite de seis horas para materializar o disco.")
                if check_cancelled is not None:
                    try:
                        check_cancelled()
                    except Exception:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        raise
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode != 0:
                raise VMBackupUnsafe("qemu-img falhou ao materializar a cadeia QCOW2.")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    @staticmethod
    def _additional_disks(vm: VMConfig, vm_toml: dict) -> list[AdditionalDiskConfig]:
        raw = vm_toml.get("additional_disks", [])
        if not isinstance(raw, list) or len(raw) > 15:
            raise VMBackupUnsafe("Lista de discos adicionais inválida no vm.toml.")
        records: list[AdditionalDiskConfig] = []
        for order, item in enumerate(raw):
            if not isinstance(item, dict):
                raise VMBackupUnsafe("Entrada de disco adicional inválida no vm.toml.")
            try:
                disk = AdditionalDiskConfig(
                    path=item["path"],
                    format=item.get("format", "qcow2"),
                    controller=item.get("controller", "nvme"),
                    order=item.get("order", order),
                )
                disk.validate(expected_order=order)
            except (KeyError, TypeError, ValueError) as exc:
                raise VMBackupUnsafe("Metadados de disco adicional inválidos no vm.toml.") from exc
            records.append(disk)
        if vm.additional_disks is not None and records != vm.additional_disks:
            raise VMBackupUnsafe("Catálogo e vm.toml divergem nos discos adicionais; clone recusado.")
        if vm.additional_disks is not None and not raw and vm.additional_disks:
            raise VMBackupUnsafe("Catálogo lista discos adicionais ausentes do vm.toml.")
        return records

    @staticmethod
    def _replace_additional_disk_paths(text: str, disks: list[AdditionalDiskConfig], paths: list[Path]) -> str:
        if not disks:
            return text
        blocks = list(re.finditer(r"(?ms)^\[\[additional_disks\]\]\s*\n(.*?)(?=^\[|\Z)", text))
        if len(blocks) != len(disks) or len(paths) != len(disks):
            raise VMBackupUnsafe("vm.toml não contém a lista esperada de discos adicionais.")
        output = text
        for block, disk, target in reversed(list(zip(blocks, disks, paths))):
            body = block.group(1)
            matches = list(re.finditer(r"(?m)^(\s*path\s*=\s*)(?:\"(?:\\.|[^\"\\])*\"|'[^'\n]*')([ \t]*(?:#.*)?(?:\r?\n|$))", body))
            if len(matches) != 1:
                raise VMBackupUnsafe("Cada disco adicional precisa de exatamente um campo path no vm.toml.")
            match = matches[0]
            changed_body = body[:match.start()] + match.group(1) + json.dumps(str(target)) + match.group(2) + body[match.end():]
            output = output[:block.start(1)] + changed_body + output[block.end(1):]
        return output

    @staticmethod
    def _write_metadata(path: Path, record: dict) -> None:
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write in clone metadata")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    def _load_clean_nvram(self) -> Path:
        candidates: list[Path] = []
        if self.nvram_template is not None:
            candidates.append(self.nvram_template)
        else:
            from core.disk import DEFAULT_OVMF_VARS_TEMPLATES

            # The home-directory fallback may contain a user's mutable state;
            # cloning must always seed fresh NVRAM from the reviewed appliance
            # or repository template instead.
            candidates.extend(DEFAULT_OVMF_VARS_TEMPLATES[:2])
        for candidate in candidates:
            try:
                info = candidate.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_size > 0:
                    return candidate
            except OSError:
                continue
        raise VMBackupUnsafe("Template limpo OVMF_VARS.fd não está disponível; clone recusado.")

    def _recover_clone(self, vm_id: str) -> None:
        journal = self.vm_storage_root / f".{vm_id}.clone-journal.json"
        try:
            info = journal.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            raise VMBackupUnsafe("Journal de clonagem inválido.")
        try:
            fd = os.open(journal, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or opened.st_size > 64 * 1024:
                    raise ValueError("invalid clone journal")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    record = json.loads(stream.read().decode("utf-8"))
            finally:
                os.close(fd)
            stage_name = record.get("stage")
            if record.get("version") != 1 or record.get("vm_id") != vm_id or not isinstance(stage_name, str) or not re.fullmatch(rf"\.{re.escape(vm_id)}\.clone-[0-9a-f]{{32}}", stage_name):
                raise ValueError("invalid clone journal paths")
            stage = self.vm_storage_root / stage_name
            target = self.vm_storage_root / vm_id
            store = VMManagerStore(self.catalog_path)
            catalogued = store.get_vm(vm_id) is not None
            if record.get("phase") in {"published", "catalogued"} and catalogued:
                if stage.exists():
                    shutil.rmtree(stage)
            elif record.get("phase") == "published" and not catalogued:
                if target.exists():
                    shutil.rmtree(target)
                if stage.exists():
                    shutil.rmtree(stage)
            elif record.get("phase") == "prepared" and stage.exists():
                shutil.rmtree(stage)
            elif record.get("phase") == "prepared" and not catalogued and target.is_dir():
                # renameat2 is no-replace: if staging disappeared between the
                # prepared journal and the published journal, publication won.
                shutil.rmtree(target)
            else:
                raise ValueError("clone publication state is ambiguous")
            journal.unlink()
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise VMBackupUnsafe("Uma clonagem interrompida precisa de recuperação manual; os arquivos foram preservados.") from exc

    def clone(
        self,
        source_id: str,
        clone_id: str,
        name: str,
        *,
        check_cancelled=None,
        on_progress=None,
    ) -> dict[str, object]:
        if not isinstance(source_id, str) or not self._VM_ID.fullmatch(source_id):
            raise VMBackupUnsafe("Identificador da VM de origem inválido.")
        if not isinstance(clone_id, str) or not self._VM_ID.fullmatch(clone_id):
            raise VMBackupUnsafe("Identificador do clone inválido.")
        clean_name = name.strip() if isinstance(name, str) else ""
        if not 1 <= len(clean_name) <= 80:
            raise VMBackupUnsafe("Nome do clone deve ter entre 1 e 80 caracteres.")
        self.guard._ensure_real_directory(self.vm_storage_root)
        if source_id == clone_id:
            raise VMBackupConflict("A VM de destino precisa ter outro identificador.")
        with self.guard._vm_lock(source_id), self.guard._vm_lock(clone_id), self.guard._offline_guard(source_id):
            self._recover_clone(clone_id)
            store = VMManagerStore(self.catalog_path)
            source = store.get_vm(source_id)
            if source is None:
                raise VMBackupConflict("VM de origem não encontrada no catálogo.")
            if store.get_vm(clone_id) is not None or (self.vm_storage_root / clone_id).exists():
                raise VMBackupConflict("Já existe uma VM ou diretório com este identificador.")
            source_dir, artifacts, _catalog_additional_paths = self.guard._source_artifacts(source, source_id)
            source_toml = self.guard._validate_vm_toml(source_dir / "vm.toml", source_id)
            additional_disks = self._additional_disks(source, source_toml)
            main_disk = source_dir / "macos.qcow2"
            main_chain = self.guard._safe_qcow_files(main_disk)
            self.guard._safe_qcow_files(source_dir / "OpenCore.qcow2")
            disk_jobs = [{
                "role": "primary", "source": main_disk, "format": "qcow2", "controller": None,
                "order": None, "filename": "macos.qcow2", "chain": main_chain,
            }]
            seen_disk_files = {
                (path.lstat().st_dev, path.lstat().st_ino) for path in main_chain
            }
            for order, disk in enumerate(additional_disks):
                source_path = self._managed_disk_source(disk.path)
                if source_path == main_disk:
                    raise VMBackupUnsafe("O disco principal não pode ser repetido como disco adicional.")
                if disk.format == "qcow2":
                    chain = self.guard._safe_qcow_files(source_path)
                else:
                    chain = [source_path]
                for chain_file in chain:
                    info = chain_file.lstat()
                    if info.st_size <= 0:
                        raise VMBackupUnsafe("Disco adicional vazio; clone recusado.")
                    identity = (info.st_dev, info.st_ino)
                    if identity in seen_disk_files:
                        raise VMBackupUnsafe("Dois discos adicionais compartilham arquivo ou backing; clone recusado.")
                    seen_disk_files.add(identity)
                suffix = "qcow2" if disk.format == "qcow2" else "raw"
                disk_jobs.append({
                    "role": "additional", "source": source_path, "format": disk.format,
                    "controller": disk.controller, "order": order,
                    "filename": f"additional-disk-{order:02d}.{suffix}", "chain": chain,
                })

            identities = [vm.smbios for vm in store.list_vms()]
            smbios = self.smbios_factory(source)
            GenSMBIOS.validate(smbios)
            if any(GenSMBIOS._identities_conflict(smbios, old) for old in identities):
                raise VMBackupConflict("A identidade SMBIOS gerada já pertence a outra VM.")
            existing_macs = set()
            for vm in store.list_vms():
                if vm.id == source_id or vm.disk_path:
                    try:
                        candidate = self.vm_storage_root / vm.id / "vm.toml"
                        data = self.guard._validate_vm_toml(candidate, vm.id)
                        network = data.get("network")
                        if isinstance(network, dict) and isinstance(network.get("mac"), str):
                            existing_macs.add(network["mac"].lower())
                    except VMBackupUnsafe:
                        if vm.id == source_id or vm.disk_path:
                            raise VMBackupUnsafe("Não foi possível confirmar todos os MACs cadastrados com segurança; clone recusado.")
                config = vm.extra_config if isinstance(vm.extra_config, dict) else {}
                network = config.get("network") if isinstance(config.get("network"), dict) else {}
                legacy_mac = network.get("mac")
                if isinstance(legacy_mac, str):
                    existing_macs.add(legacy_mac.lower())
            mac = ""
            for _attempt in range(16):
                candidate_mac = self.mac_factory().lower()
                if re.fullmatch(r"52:54:00(?::[0-9a-f]{2}){3}", candidate_mac) and candidate_mac not in existing_macs:
                    mac = candidate_mac
                    break
            if not mac:
                raise VMBackupConflict("Não foi possível gerar um endereço MAC novo e válido.")

            clone = VMConfig.from_dict(source.to_dict())
            clone.id = clone_id
            clone.name = clean_name
            clone.disk_path = str(self.vm_storage_root / clone_id / "macos.qcow2")
            if source.additional_disks is not None:
                clone.additional_disks = [
                    AdditionalDiskConfig(
                        path=str(self.vm_storage_root / clone_id / disk_jobs[index + 1]["filename"]),
                        format=disk.format,
                        controller=disk.controller,
                        order=disk.order,
                    )
                    for index, disk in enumerate(additional_disks)
                ]
            clone.smbios = smbios
            clone.auto_start = False
            clone.network_ssh_port = 0
            clone.created_at = datetime.now(timezone.utc).isoformat()
            clone.last_booted_at = None
            stage = self.vm_storage_root / f".{clone_id}.clone-{uuid.uuid4().hex}"
            target = self.vm_storage_root / clone_id
            journal = self.vm_storage_root / f".{clone_id}.clone-journal.json"
            nvram = self._load_clean_nvram()
            for job in disk_jobs:
                job["materialized"] = job["format"] == "qcow2" and len(job["chain"]) > 1
                if job["materialized"]:
                    job["estimated_bytes"] = self._qcow_virtual_size(job["source"])
                else:
                    job["estimated_bytes"] = job["source"].stat(follow_symlinks=False).st_size
            total_bytes = sum(
                (disk_jobs[0]["estimated_bytes"] if path == main_disk else path.stat(follow_symlinks=False).st_size)
                for path in artifacts if path.name != "OVMF_VARS.fd"
            )
            total_bytes += sum(job["estimated_bytes"] for job in disk_jobs[1:]) + nvram.stat().st_size
            if shutil.disk_usage(self.vm_storage_root).free < total_bytes + 16 * 1024 * 1024:
                raise VMBackupConflict("Espaço livre insuficiente para criar o clone sem alterar a origem.")
            stage.mkdir(mode=0o700)
            try:
                copied = 0
                completed_disk_records = []
                main_job = disk_jobs[0]
                for path in artifacts:
                    if check_cancelled is not None:
                        check_cancelled()
                    if path.name == "OVMF_VARS.fd":
                        continue
                    flattened_main = path == main_disk and main_job["materialized"]
                    if flattened_main:
                        destination = stage / main_job["filename"]
                        self._flatten_qcow(path, destination, check_cancelled=check_cancelled)
                        os.chmod(destination, 0o600)
                        size, digest = self._hash_regular_file(destination)
                        copied += size
                        if on_progress is not None:
                            on_progress(copied, total_bytes)
                        completed_disk_records.append({
                            "role": "primary", "file": destination.name, "format": "qcow2",
                            "backing_chain_materialized": True, "size_bytes": size, "sha256": digest,
                        })
                    else:
                        size, digest = self.guard._copy_hash(
                            path,
                            stage / path.name,
                            check_cancelled=check_cancelled,
                            already_copied=copied,
                            on_progress=(lambda amount: on_progress(amount, total_bytes)) if on_progress else None,
                        )
                        if path == main_disk:
                            completed_disk_records.append({
                                "role": "primary", "file": path.name, "format": "qcow2",
                                "backing_chain_materialized": False, "size_bytes": size, "sha256": digest,
                            })
                        copied += size

                clone_disk_paths = []
                for job in disk_jobs[1:]:
                    if check_cancelled is not None:
                        check_cancelled()
                    destination = stage / job["filename"]
                    if job["materialized"]:
                        self._flatten_qcow(job["source"], destination, check_cancelled=check_cancelled)
                        os.chmod(destination, 0o600)
                        size, digest = self._hash_regular_file(destination)
                        copied += size
                        if on_progress is not None:
                            on_progress(copied, total_bytes)
                    else:
                        size, digest = self.guard._copy_hash(
                            job["source"], destination,
                            check_cancelled=check_cancelled,
                            already_copied=copied,
                            on_progress=(lambda amount: on_progress(amount, total_bytes)) if on_progress else None,
                        )
                        copied += size
                    if job["format"] == "qcow2":
                        copied_chain = self.guard._safe_qcow_files(destination)
                        if job["materialized"] and len(copied_chain) != 1:
                            raise VMBackupUnsafe("A cadeia QCOW2 do clone não foi materializada por completo.")
                    completed_disk_records.append({
                        "role": "additional", "file": destination.name, "format": job["format"],
                        "controller": job["controller"], "order": job["order"],
                        "backing_chain_materialized": bool(job["materialized"]),
                        "size_bytes": size, "sha256": digest,
                    })
                    clone_disk_paths.append(self.vm_storage_root / clone_id / job["filename"])
                vm_toml_path = stage / "vm.toml"
                toml_text = vm_toml_path.read_text(encoding="utf-8")
                toml_text = self._replace_toml_value(toml_text, "name", clone_id)
                toml_text = self._replace_toml_value(toml_text, "mac", mac, section="network")
                toml_text = self._replace_additional_disk_paths(toml_text, additional_disks, clone_disk_paths)
                vm_toml_path.write_text(toml_text, encoding="utf-8")
                self.guard._validate_vm_toml(vm_toml_path, clone_id)
                self.opencore_rewriter(stage / "OpenCore.qcow2", clone)
                self.guard._copy_hash(nvram, stage / "OVMF_VARS.fd")
                if on_progress is not None:
                    on_progress(total_bytes, total_bytes)
                os.chmod(stage / "OVMF_VARS.fd", 0o600)
                cloned_main_chain = self.guard._safe_qcow_files(stage / "macos.qcow2")
                if main_job["materialized"] and len(cloned_main_chain) != 1:
                    raise VMBackupUnsafe("A cadeia QCOW2 principal não foi materializada por completo.")
                self.guard.qcow_inspector(stage / "OpenCore.qcow2")
                completed_files = []
                for artifact in sorted(stage.iterdir(), key=lambda item: item.name):
                    if artifact.name == "clone-manifest.json":
                        continue
                    size, digest = self._hash_regular_file(artifact)
                    completed_files.append({"name": artifact.name, "size_bytes": size, "sha256": digest})
                self._write_metadata(stage / "clone-manifest.json", {
                    "format": "vhackintosh-vm-clone",
                    "version": 1,
                    "source_vm_id": source_id,
                    "vm_id": clone_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "files": completed_files,
                    "disks": completed_disk_records,
                })
                if check_cancelled is not None:
                    check_cancelled()
                stage_fd = os.open(stage, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(stage_fd)
                finally:
                    os.close(stage_fd)
                record = {"version": 1, "vm_id": clone_id, "stage": stage.name, "phase": "prepared"}
                self._write_clone_journal(journal, record)
                self._rename_noreplace(stage, target)
                record["phase"] = "published"
                self._write_clone_journal(journal, record)
                try:
                    store.add_vm(clone, expected_revision=store.revision)
                except Exception:
                    try:
                        shutil.rmtree(target)
                        journal.unlink(missing_ok=True)
                    except OSError:
                        # Keep the published journal so the next attempt can
                        # remove an unregistered clone after catalog recovery.
                        pass
                    raise
                record["phase"] = "catalogued"
                self._write_clone_journal(journal, record)
                journal.unlink(missing_ok=True)
                return {"source_vm_id": source_id, "vm_id": clone_id, "name": clean_name, "mac": mac, "created": True}
            except Exception:
                if stage.exists() and not journal.exists():
                    shutil.rmtree(stage, ignore_errors=True)
                raise
