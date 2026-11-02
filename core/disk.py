"""
vHackintosh — Virtual Disk & OpenCore EFI Provisioner
Gerencia o provisionamento automático de discos virtuais QCOW2 (qemu-img)
e injeção de parâmetros/seriais GenSMBIOS diretamente na partição EFI do OpenCore.
"""

from __future__ import annotations
import os
import shutil
import tempfile
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from core.config import VMConfig, CONFIG_DIR
from core.smbios import GenSMBIOS
from core import reims_util

console = Console()


class _SilentProgress:
    """Progress-compatible no-op used by supervised, headless jobs."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def add_task(self, *_args, **_kwargs):
        return 0

    def update(self, *_args, **_kwargs):
        return None

VM_STORAGE_BASE = CONFIG_DIR / "vms"

DEFAULT_OVMF_VARS_TEMPLATES = [
    Path("/opt/vhackintosh/templates/OVMF_VARS.fd"),
    Path(__file__).resolve().parent.parent / "templates" / "OVMF_VARS.fd",
    CONFIG_DIR / "templates/OVMF_VARS.fd",
]

# O par do OVMF_VARS de 540672 bytes: um build 4M (o template do repo, ou o do
# sistema como fallback). O reims-ctl exige firmware_code/firmware_vars no
# diretório da VM — ver core/reims_ctl.py.
DEFAULT_OVMF_CODE_TEMPLATES = [
    Path("/opt/vhackintosh/templates/OVMF_CODE.fd"),
    Path(__file__).resolve().parent.parent / "templates" / "OVMF_CODE_4M.fd",
    CONFIG_DIR / "templates/OVMF_CODE.fd",
    Path("/usr/share/edk2/x64/OVMF_CODE.4m.fd"),
]


@dataclass
class VMDiskBundle:
    vm_dir: Path
    hdd_path: Path
    opencore_path: Path
    installer_path: Optional[Path] = None


class DiskProvisioner:
    """Provisiona discos e configura o OpenCore para cada VM criada."""

    @classmethod
    def ensure_ovmf_code(cls, dest_dir: Path) -> bool:
        """Garante o `OVMF_CODE.fd` (par 4M do OVMF_VARS) no diretório da VM.

        Idempotente: já existindo, não faz nada. Devolve False só quando não há
        template algum para copiar.
        """
        dest = Path(dest_dir) / "OVMF_CODE.fd"
        if dest.exists():
            return True
        for cand in DEFAULT_OVMF_CODE_TEMPLATES:
            if cand.exists():
                try:
                    shutil.copyfile(cand, dest)
                    return True
                except OSError:
                    continue
        return False

    @classmethod
    def provision(
        cls,
        vm: VMConfig,
        installer_img: Optional[str | Path] = None,
        *,
        job_context=None,
    ) -> VMDiskBundle:
        """
        Cria a pasta da VM, o disco principal QCOW2 e a imagem OpenCore personalizada.
        """
        vm_dir = VM_STORAGE_BASE / vm.id
        vm_dir.mkdir(parents=True, exist_ok=True)
        macos_path = vm_dir / "macos.qcow2"
        hdd_path = vm_dir / "hdd.qcow2"
        opencore_path = vm_dir / "OpenCore.qcow2"
        ovmf_vars_path = vm_dir / "OVMF_VARS.fd"

        progress_context = (
            Progress(
                SpinnerColumn(spinner_name="dots"),
                TextColumn("[bold cyan]{task.description}[/bold cyan]"),
                console=console,
            )
            if job_context is None
            else _SilentProgress()
        )
        with progress_context as progress:
            # 1. Criação do disco virtual macOS QCOW2
            task1 = progress.add_task(f"Criando disco virtual de {vm.disk_size_gb} GB...", total=None)
            if macos_path.exists() and not cls._check_qcow2_disk(macos_path, job_context=job_context):
                # A pre-existing output may be from an older interrupted
                # provision attempt. Never mistake an invalid QCOW2 for a
                # completed checkpoint.
                macos_path.unlink(missing_ok=True)
            if not macos_path.exists():
                cls._create_qcow2_disk(macos_path, vm.disk_size_gb, job_context=job_context)
            if not hdd_path.exists():
                try:
                    hdd_path.symlink_to("macos.qcow2")
                except Exception:
                    pass
            progress.update(task1, description=f"[bold green]✔ Disco de {vm.disk_size_gb} GB criado (macos.qcow2)[/bold green]")

            # 2. Cópia e customização ou geração nativa do OpenCore.qcow2
            if opencore_path.exists() and not cls._check_qcow2_disk(opencore_path, job_context=job_context):
                opencore_path.unlink(missing_ok=True)
            task2 = progress.add_task("Provisionando bootloader OpenCore EFI...", total=None)
            new_opencore = not opencore_path.exists()

            # Existing VMs retain their identity; new VMs always get a fresh SMBIOS.
            if new_opencore:
                if not vm.smbios or not all((vm.smbios.serial_number, vm.smbios.board_serial, vm.smbios.smuuid, vm.smbios.rom)):
                    vm.smbios = GenSMBIOS.generate(vm.macos_version)
                GenSMBIOS.validate(vm.smbios)

            # 3. Provisionamento de OVMF_VARS.fd se não existir
            if not ovmf_vars_path.exists():
                for v_cand in DEFAULT_OVMF_VARS_TEMPLATES:
                    if v_cand.exists():
                        shutil.copyfile(v_cand, ovmf_vars_path)
                        break
            # E o código do OVMF ao lado (par do VARS; o reims-ctl pede os dois
            # no diretório da VM — ver core/reims_ctl.py).
            if not cls.ensure_ovmf_code(vm_dir):
                raise FileNotFoundError(
                    "OVMF_CODE.fd personalizado não encontrado; a logo do vHackintosh não pode ser preservada."
                )

            # 4. Gere a EFI para esta VM com o reims-util oficial. Isso garante
            # que VMs criadas pelo checkout e pela ISO recebam os kexts APV
            # atuais; copiar o fallback versionado do checkout pode omiti-los.
            # --no-ovmf deixa o OVMF personalizado acima intacto, incluindo a
            # logo do vHackintosh.
            if new_opencore:
                progress.update(
                    task2,
                    description=(
                        "Gerando OpenCore com reims-util oficial, kexts APV e SMBIOS exclusivo..."
                    ),
                )
                generated = reims_util.generate_opencore(
                    opencore_path,
                    vm.smbios,
                    no_ovmf=True,
                    timeout=reims_util.OPENCORE_PICKER_TIMEOUT,
                    boot_args=reims_util.OPENCORE_BOOT_ARGS,
                    show_picker=bool(getattr(vm, "opencore_show_picker", False)),
                    job_context=job_context,
                )
                if not generated:
                    raise RuntimeError(
                        "Não foi possível gerar uma EFI com os kexts APV pelo reims-util oficial. "
                        "Atualize o pacote vHackintosh Control para instalar o reims-util APV validado e tente novamente."
                    )
            progress.update(task2, description="[bold green]✔ OpenCore EFI pronto para boot![/bold green]")

        # 5. Link/cópia do instalador se fornecido
        inst_target: Optional[Path] = None
        if installer_img and Path(installer_img).exists():
            inst_target = Path(installer_img)

        # Atualiza o VMConfig com os caminhos criados
        vm.disk_path = str(macos_path)

        return VMDiskBundle(
            vm_dir=vm_dir,
            hdd_path=macos_path,
            opencore_path=opencore_path,
            installer_path=inst_target,
        )

    @classmethod
    def _create_qcow2_disk(cls, disk_path: Path, size_gb: int, *, job_context=None) -> None:
        disk_path = Path(disk_path)
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{disk_path.stem}.", suffix=disk_path.suffix or ".qcow2", dir=disk_path.parent
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        temporary_path.unlink()
        cmd = [
            "qemu-img", "create",
            "-f", "qcow2",
            "-o", "cluster_size=2M",
            str(temporary_path),
            f"{size_gb}G",
        ]
        try:
            if job_context is not None:
                res = job_context.run_subprocess(cmd, check=False)
                if res.returncode != 0:
                    raise RuntimeError(f"Falha ao criar disco QCOW2 (qemu-img retornou {res.returncode})")
                check = job_context.run_subprocess(["qemu-img", "check", str(temporary_path)], check=False)
                if check.returncode != 0:
                    raise RuntimeError(f"qemu-img check falhou para o disco criado (código {check.returncode})")
            else:
                res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if res.returncode != 0:
                    raise RuntimeError(f"Falha ao criar disco QCOW2: {res.stderr}")
                check = subprocess.run(
                    ["qemu-img", "check", str(temporary_path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
                )
                if check.returncode != 0:
                    raise RuntimeError(f"qemu-img check falhou: {check.stderr}")
            if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
                raise RuntimeError("qemu-img não produziu um arquivo de disco válido")
            os.replace(temporary_path, disk_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _check_qcow2_disk(disk_path: Path, *, job_context=None) -> bool:
        argv = ["qemu-img", "check", str(disk_path)]
        if job_context is not None:
            return job_context.run_subprocess(argv, check=False).returncode == 0
        try:
            return subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    @classmethod
    def inject_opencore_config(
        cls,
        opencore_path: Path,
        vm: VMConfig,
        show_picker: Optional[bool] = None,
    ) -> None:
        """
        Extrai o config.plist de dentro do OpenCore.qcow2 via guestfish,
        injeta os seriais gerados pelo GenSMBIOS, ajustes de áudio e comportamento de boot,
        e faz o upload de volta.
        """
        if show_picker is None:
            show_picker = getattr(vm, "opencore_show_picker", False)

        original_image = Path(opencore_path)
        if shutil.which("qemu-img"):
            check = subprocess.run(["qemu-img", "check", str(original_image)], capture_output=True, text=True)
            if check.returncode:
                raise RuntimeError("OpenCore image failed integrity check before injection")
        if not original_image.is_file():
            raise FileNotFoundError(f"OpenCore image not found: {original_image}")
        if not shutil.which("guestfish"):
            raise RuntimeError("guestfish é necessário para atualizar a configuração da EFI OpenCore")

        fd, staged_name = tempfile.mkstemp(prefix=f".{original_image.name}.", suffix=".staged", dir=original_image.parent)
        os.close(fd)
        staged_image = Path(staged_name)
        backup_name = None
        try:
            # Preserve a EFI inteira (drivers, recursos e kexts); alteramos
            # somente o config.plist para manter a identidade por VM.
            shutil.copy2(original_image, staged_image)
            cls._inject_opencore_config_in_place(staged_image, vm, show_picker)

            backup_fd, backup_name = tempfile.mkstemp(prefix=f".{original_image.name}.", suffix=".backup", dir=original_image.parent)
            os.close(backup_fd)
            shutil.copy2(original_image, backup_name)
            os.replace(staged_image, original_image)
            try:
                if shutil.which("qemu-img"):
                    check = subprocess.run(["qemu-img", "check", str(original_image)], capture_output=True, text=True)
                    if check.returncode:
                        raise RuntimeError("OpenCore image failed integrity check after identity injection")
            except Exception:
                os.replace(backup_name, original_image)
                backup_name = None
                raise
        except Exception:
            staged_image.unlink(missing_ok=True)
            raise
        finally:
            if backup_name:
                Path(backup_name).unlink(missing_ok=True)

    @classmethod
    def _inject_opencore_config_in_place(cls, opencore_path: Path, vm: VMConfig, show_picker: Optional[bool]) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_plist = Path(tmp_dir) / "config.plist"

            # 1. Download do config.plist da partição EFI
            cmd_download = [
                "guestfish",
                "-a", str(opencore_path),
                "-m", "/dev/sda1",
                "download", "/EFI/OC/config.plist", str(tmp_plist),
            ]
            res_dl = subprocess.run(cmd_download, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res_dl.returncode != 0:
                # Tenta partição raiz /dev/sda se não for particionado.
                cmd_download[4] = "/dev/sda"
                subprocess.run(cmd_download, check=True)

            # 2. Injeta os seriais e ajustes de áudio no plist
            GenSMBIOS.inject_into_config_plist(tmp_plist, vm.smbios, show_picker=show_picker)

            # 3. Upload do config.plist atualizado de volta para a imagem
            cmd_upload = [
                "guestfish",
                "-a", str(opencore_path),
                "-m", "/dev/sda1",
                "upload", str(tmp_plist), "/EFI/OC/config.plist",
            ]
            res_up = subprocess.run(cmd_upload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res_up.returncode != 0:
                cmd_upload[4] = "/dev/sda"
                subprocess.run(cmd_upload, check=True)
            else:
                cmd_upload[4] = "/dev/sda1"
            verify = subprocess.run(
                ["guestfish", "--ro", "-a", str(opencore_path), "-m", "/dev/sda1",
                 "download", "/EFI/OC/config.plist", str(tmp_plist)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            if verify.returncode:
                verify = subprocess.run(
                    ["guestfish", "--ro", "-a", str(opencore_path), "-m", "/dev/sda",
                     "download", "/EFI/OC/config.plist", str(tmp_plist)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
            if verify.returncode:
                raise RuntimeError("Unable to verify OpenCore EFI after identity injection")
            import plistlib
            with tmp_plist.open("rb") as fh:
                generic = plistlib.load(fh).get("PlatformInfo", {}).get("Generic", {})
            if not (
                generic.get("SystemProductName") == vm.smbios.model
                and generic.get("SystemSerialNumber") == vm.smbios.serial_number
                and generic.get("MLB") == vm.smbios.board_serial
                and generic.get("SystemUUID") == vm.smbios.smuuid
                and generic.get("ROM") == bytes.fromhex(vm.smbios.rom)
            ):
                raise RuntimeError("OpenCore EFI identity verification failed")

    _inject_smbios_to_opencore_qcow2 = inject_opencore_config

    @classmethod
    def get_disk_info(cls, disk_path: str | Path) -> dict:
        """Retorna informações de tamanho virtual e tamanho em disco."""
        path = Path(disk_path)
        if not path.exists():
            return {"virtual_gb": 0, "disk_gb": 0}

        try:
            cmd = ["qemu-img", "info", "--output=json", str(path)]
            out = subprocess.check_output(cmd, text=True)
            import json
            data = json.loads(out)
            virtual_gb = round(data.get("virtual-size", 0) / (1024**3), 1)
            disk_gb = round(data.get("actual-size", 0) / (1024**3), 1)
            return {"virtual_gb": virtual_gb, "disk_gb": disk_gb}
        except Exception:
            return {"virtual_gb": 0, "disk_gb": 0}
