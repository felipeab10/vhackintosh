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

console = Console()

VM_STORAGE_BASE = CONFIG_DIR / "vms"
DEFAULT_OPENCORE_TEMPLATES = [
    Path("/opt/vhackintosh/templates/OpenCore.qcow2"),
    Path(__file__).resolve().parent.parent / "templates" / "OpenCore.qcow2",
    Path(os.path.expanduser("~/.config/vhackintosh/templates/OpenCore.qcow2")),
    Path("/opt/reims-vgpu/templates/OpenCore.qcow2"),
    Path("/home/felipeab10/reims-vgpu/.local/installer/osx-kvm-tools/OpenCore/OpenCore.qcow2"),
    Path("/home/felipeab10/reims-vgpu/vm/disks/rails/sequoia/persistent/OpenCore.qcow2"),
    Path("/home/felipeab10/reims-vgpu/vm/disks/rails/tahoe/persistent/OpenCore.qcow2"),
]

DEFAULT_OVMF_VARS_TEMPLATES = [
    Path("/opt/vhackintosh/templates/OVMF_VARS.fd"),
    Path(__file__).resolve().parent.parent / "templates" / "OVMF_VARS.fd",
    Path("/home/felipeab10/reims-vgpu/.local/installer/osx-kvm-tools/OVMF_VARS-1920x1080.fd"),
    Path("/home/felipeab10/reims-vgpu/vm/disks/rails/sequoia/persistent/OVMF_VARS.fd"),
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

            # 2. Cópia e customização do OpenCore.qcow2
            task2 = progress.add_task("Provisionando bootloader OpenCore EFI...", total=None)
            template_oc = cls.get_template_opencore_path()
            if not template_oc:
                raise FileNotFoundError("Imagem base OpenCore.qcow2 não encontrada no sistema.")

            if not opencore_path.exists():
                shutil.copyfile(template_oc, opencore_path)

            # 3. Provisionamento de OVMF_VARS.fd se não existir
            if not ovmf_vars_path.exists():
                for v_cand in DEFAULT_OVMF_VARS_TEMPLATES:
                    if v_cand.exists():
                        shutil.copyfile(v_cand, ovmf_vars_path)
                        break

            # 4. Injeção dos seriais GenSMBIOS no config.plist do OpenCore
            progress.update(task2, description="Injetando seriais GenSMBIOS na partição EFI...")
            cls._inject_smbios_to_opencore_qcow2(opencore_path, vm)
            progress.update(task2, description="[bold green]✔ OpenCore EFI configurado com seriais da Apple![/bold green]")

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
    def _inject_smbios_to_opencore_qcow2(cls, opencore_path: Path, vm: VMConfig) -> None:
        """
        Extrai o config.plist de dentro do OpenCore.qcow2 via guestfish,
        injeta os seriais gerados pelo GenSMBIOS e faz o upload de volta.
        """
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
                # Tenta partição raiz /dev/sda se não for particionado
                cmd_download[4] = "/dev/sda"
                subprocess.run(cmd_download, check=True)

            # 2. Injeta os seriais e ajustes de áudio no plist
            GenSMBIOS.inject_into_config_plist(tmp_plist, vm.smbios)

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
