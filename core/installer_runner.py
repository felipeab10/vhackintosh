"""
vHackintosh — Teste A/B de Instalação do Zero
Permite provisionar uma VM temporária ou de teste limpa e iniciar o processo
de boot do instalador oficial do macOS com opções comparativas A/B de áudio e vídeo.
"""

from __future__ import annotations
import os
import sys
import subprocess
from pathlib import Path
from rich.console import Console
from rich.prompt import Prompt, Confirm

from core.config import VMConfig, SMBIOSConfig, ExclusiveVMLock
from core.smbios import GenSMBIOS
from core.disk import DiskProvisioner
from core.downloader import MacOSDownloader, MACOS_PRODUCTS
from ui.banner import render_banner

console = Console()


class ABInstallerTest:
    """Orquestrador do teste A/B de instalação do zero."""

    @classmethod
    def run_interactive(cls) -> None:
        console.clear()
        console.print(render_banner())
        console.print("[bold cyan]── Teste A/B: Criação e Instalação de VM do Zero ──[/bold cyan]\n")
        console.print("[white]Este teste cria uma VM limpa com disco zerado, injeta novos seriais SMBIOS[/white]")
        console.print("[white]e inicializa o instalador oficial da Apple (BaseSystem) para validar o comportamento.[/white]\n")

        # 1. Escolha da versão
        console.print("[bold]1. Escolha a versão do macOS:[/bold]")
        console.print("  1 - macOS Sequoia 15 (Recomendado)")
        console.print("  2 - macOS Tahoe 26 (Experimental)")
        console.print("  3 - macOS Ventura 13")
        v_opt = Prompt.ask("Versão", choices=["1", "2", "3"], default="1")
        v_map = {"1": "sequoia", "2": "tahoe", "3": "ventura"}
        macos_version = v_map[v_opt]

        # 2. Teste A/B de Áudio
        console.print("\n[bold]2. Selecione o modelo de áudio para o Teste A/B:[/bold]")
        console.print("  [bold green]A (Recomendado)[/bold green] - ultimate-macOS-KVM standard (ich9-intel-hda + hda-duplex)")
        console.print("  [bold red]B (Legado)[/bold red]     - OSX-KVM padrão (usb-audio - conhecido por estalos e lag)")
        audio_choice = Prompt.ask("Opção de Áudio", choices=["A", "B", "a", "b"], default="A").upper()
        audio_device = "ich9-intel-hda" if audio_choice == "A" else "usb-audio"

        # 3. Modo de visualização
        console.print("\n[bold]3. Modo de Exibição:[/bold]")
        console.print("  1 - Tela Cheia (Fullscreen Kiosk)")
        console.print("  2 - Modo Janela (Windowed)")
        disp_choice = Prompt.ask("Modo", choices=["1", "2"], default="1")
        fullscreen = "1" if disp_choice == "1" else "0"

        # 4. Checagem / Preparação do Instalador
        installer_img = MacOSDownloader.get_cached_image_path(macos_version)
        if not installer_img:
            console.print(f"\n[yellow]A imagem do instalador para {macos_version} não está no cache.[/yellow]")
            if Confirm.ask("Deseja baixar e converter agora dos servidores da Apple?", default=True):
                installer_img = MacOSDownloader.prepare_installer(macos_version)
                if not installer_img:
                    console.print("[red]Erro ao preparar instalador. Teste abortado.[/red]")
                    Prompt.ask("Enter para sair")
                    return
            else:
                console.print("[red]Instalador necessário para continuar. Teste abortado.[/red]")
                Prompt.ask("Enter para sair")
                return

        # 5. Provisionamento da VM de teste
        test_vm_id = f"test-ab-{macos_version}"
        console.print(f"\n[bold cyan]▶ Provisionando armazenamento e OpenCore EFI para '{test_vm_id}'...[/bold cyan]")
        smbios = GenSMBIOS.generate(macos_version)
        vm = VMConfig(
            id=test_vm_id,
            name=f"macOS {macos_version.capitalize()} (Teste A/B)",
            macos_version=macos_version,
            vcpus=12,
            ram_gb=8,
            disk_size_gb=64,
            audio_device=audio_device,
            smbios=smbios,
        )

        bundle = DiskProvisioner.provision(vm, installer_img=installer_img)

        # 6. Execução do Boot
        reims_dir = Path("/home/felipeab10/reims-vgpu")
        boot_script = reims_dir / "vm" / "boot-x86.sh"

        if not boot_script.exists():
            console.print(f"[red]Erro: Script {boot_script} não encontrado.[/red]")
            Prompt.ask("Enter para sair")
            return

        lock = ExclusiveVMLock()
        if not lock.acquire(vm.name):
            console.print("[bold red]ERRO: Outra VM já está em execução no momento![/bold red]")
            Prompt.ask("Enter para voltar")
            return

        try:
            console.print(f"\n[bold green]▶ Inicializando VM de Teste com o instalador da Apple...[/bold green]")
            console.print(f"  • Áudio: [bold cyan]{audio_device}[/bold cyan]")
            console.print(f"  • Tela: [bold cyan]{'Fullscreen' if fullscreen == '1' else 'Janela'}[/bold cyan]")
            console.print(f"  • Instalador: [bold cyan]{installer_img}[/bold cyan]\n")

            env = os.environ.copy()
            env["FORCE_X11"] = "1"
            env["REIMS_VGPU_FULLSCREEN"] = fullscreen
            env["REIMS_VGPU_BACKEND"] = "vulkan"
            env["REIMS_VGPU_WINDOW"] = "1"
            env["CPUS"] = str(vm.vcpus)
            env["RAM"] = f"{vm.ram_gb}G"
            env["AUDIO_DEVICE"] = audio_device
            env["INSTALL_MEDIA"] = str(installer_img)
            env["DISK_MASTER"] = str(bundle.hdd_path)
            env["OPENCORE_MASTER"] = str(bundle.opencore_path)
            env["QEMU_REBOOT_ACTION"] = "reset"

            cmd = [
                "bash", str(boot_script),
                "--rail", macos_version,
                "--persistent",
                "--device", "reims-vgpu-pci",
            ]
            subprocess.run(cmd, env=env)
            console.print("\n[bold yellow]Sessão de teste finalizada.[/bold yellow]")
        finally:
            lock.release()

        Prompt.ask("Pressione Enter para retornar")
