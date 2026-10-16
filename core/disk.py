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

VM_STORAGE_BASE = CONFIG_DIR / "vms"
# O template canônico vem de templates/ neste repositório (versionado): EFI
# genérica com ReimsVgpu habilitada para Tahoe 26. Não implica que runtime e host
# estejam na configuração GPU já testada (ver templates/README.md). A ISO o instala
# em /opt/vhackintosh/templates; ~/.config é o override do usuário.
DEFAULT_OPENCORE_TEMPLATES = [
    Path("/opt/vhackintosh/templates/OpenCore.qcow2"),
    Path(__file__).resolve().parent.parent / "templates" / "OpenCore.qcow2",
    Path(os.path.expanduser("~/.config/vhackintosh/templates/OpenCore.qcow2")),
]

DEFAULT_OVMF_VARS_TEMPLATES = [
    Path("/opt/vhackintosh/templates/OVMF_VARS.fd"),
    Path(__file__).resolve().parent.parent / "templates" / "OVMF_VARS.fd",
    Path(os.path.expanduser("~/.config/vhackintosh/templates/OVMF_VARS.fd")),
]

# O par do OVMF_VARS de 540672 bytes: um build 4M (o template do repo, ou o do
# sistema como fallback). O reims-ctl exige firmware_code/firmware_vars no
# diretório da VM — ver core/reims_ctl.py.
DEFAULT_OVMF_CODE_TEMPLATES = [
    Path("/opt/vhackintosh/templates/OVMF_CODE.fd"),
    Path(__file__).resolve().parent.parent / "templates" / "OVMF_CODE_4M.fd",
    Path(os.path.expanduser("~/.config/vhackintosh/templates/OVMF_CODE.fd")),
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
    def get_template_opencore_path(cls) -> Optional[Path]:
        for candidate in DEFAULT_OPENCORE_TEMPLATES:
            if candidate.exists() and candidate.stat().st_size > 1024 * 1024:
                return candidate
        return None

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
    def provision(cls, vm: VMConfig, installer_img: Optional[str | Path] = None) -> VMDiskBundle:
        """
        Cria a pasta da VM, o disco principal QCOW2 e a imagem OpenCore personalizada.
        """
        vm_dir = VM_STORAGE_BASE / vm.id
        vm_dir.mkdir(parents=True, exist_ok=True)
        macos_path = vm_dir / "macos.qcow2"
        hdd_path = vm_dir / "hdd.qcow2"
        opencore_path = vm_dir / "OpenCore.qcow2"
        ovmf_vars_path = vm_dir / "OVMF_VARS.fd"

        with Progress(
            SpinnerColumn(spinner_name="dots"),
            TextColumn("[bold cyan]{task.description}[/bold cyan]"),
            console=console,
        ) as progress:
            # 1. Criação do disco virtual macOS QCOW2
            task1 = progress.add_task(f"Criando disco virtual de {vm.disk_size_gb} GB...", total=None)
            if not macos_path.exists():
                cls._create_qcow2_disk(macos_path, vm.disk_size_gb)
            if not hdd_path.exists():
                try:
                    hdd_path.symlink_to("macos.qcow2")
                except Exception:
                    pass
            progress.update(task1, description=f"[bold green]✔ Disco de {vm.disk_size_gb} GB criado (macos.qcow2)[/bold green]")

            # 2. Cópia e customização ou geração nativa do OpenCore.qcow2
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
            cls.ensure_ovmf_code(vm_dir)

            # 4. Geração do OpenCore (reims-util preferencial, fallback para template + guestfish)
            if new_opencore:
                reims_util_ready = reims_util.is_available()
                generated = False
                if reims_util_ready:
                    progress.update(task2, description="Gerando bootloader OpenCore EFI via reims-util...")
                    try:
                        generated = reims_util.generate_opencore(
                            opencore_path,
                            vm.smbios,
                            no_ovmf=True,
                        )
                    except Exception:
                        generated = False

                if not generated:
                    template_oc = cls.get_template_opencore_path()
                    if not template_oc:
                        raise FileNotFoundError("Imagem base OpenCore.qcow2 não encontrada e reims-util indisponível.")
                    if not shutil.which("guestfish"):
                        opencore_path.unlink(missing_ok=True)
                        raise RuntimeError("guestfish ou reims-util é necessário para provisionar o OpenCore EFI com segurança")
                    # Dizer POR QUE caiu no fallback: sem isto a TUI mostra
                    # "via guestfish" e ninguém sabe que o binário nativo falta.
                    if not reims_util_ready:
                        motive = f"reims-util ausente (procurei em {reims_util.search_summary()})"
                    else:
                        motive = f"reims-util falhou (veja {reims_util.LOG_FILE})"
                    shutil.copyfile(template_oc, opencore_path)
                    try:
                        progress.update(task2, description=f"[yellow]{motive}: aplicando identidade SMBIOS à EFI via guestfish...[/yellow]")
                        cls._inject_smbios_to_opencore_qcow2(opencore_path, vm)
                    except Exception:
                        opencore_path.unlink(missing_ok=True)
                        raise
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
    def _create_qcow2_disk(cls, disk_path: Path, size_gb: int) -> None:
        cmd = [
            "qemu-img", "create",
            "-f", "qcow2",
            "-o", "cluster_size=2M",
            str(disk_path),
            f"{size_gb}G",
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Falha ao criar disco QCOW2: {res.stderr}")

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
        fd, staged_name = tempfile.mkstemp(prefix=f".{original_image.name}.", suffix=".staged", dir=original_image.parent)
        os.close(fd)
        staged_image = Path(staged_name)
        backup_name = None
        try:
            native_generated = False
            if reims_util.is_available():
                native_generated = reims_util.generate_opencore(
                    staged_image,
                    vm.smbios,
                    no_ovmf=True,
                    timeout=5 if show_picker else None,
                )

            if not native_generated:
                if not original_image.exists():
                    raise FileNotFoundError(f"OpenCore image not found: {original_image}")
                if not shutil.which("guestfish"):
                    raise RuntimeError("guestfish ou reims-util é necessário para atualizar o OpenCore EFI")
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
