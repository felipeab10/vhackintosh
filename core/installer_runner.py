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
from rich.prompt import Prompt, Confirm, IntPrompt

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
        console.print("[bold cyan]── Teste de Criação e Instalação de VM macOS do Zero ──[/bold cyan]\n")
        console.print("[white]Este assistente provisiona uma VM limpa com disco zerado, injeta novos seriais SMBIOS[/white]")
        console.print("[white]e inicializa o instalador oficial da Apple (BaseSystem) para validar o comportamento.[/white]")
        console.print("[dim]Áudio corrigido ativado por padrão: ich9-intel-hda + hda-duplex (ultimate-macOS-KVM standard).[/dim]\n")

        # 1. Escolha da versão
        console.print("[bold]1. Escolha a versão do macOS:[/bold]")
        console.print("  1 - macOS Sequoia 15 (Recomendado)")
        console.print("  2 - macOS Tahoe 26 (Experimental)")
        console.print("  3 - macOS Ventura 13")
        v_opt = Prompt.ask("Versão", choices=["1", "2", "3"], default="1")
        v_map = {"1": "sequoia", "2": "tahoe", "3": "ventura"}
        macos_version = v_map[v_opt]

        # 2. Imagem de Instalação/Recuperação da Apple (Automático)
        console.print(f"\n[bold]2. Imagem de Instalação/Recuperação da Apple ({macos_version.capitalize()}):[/bold]")
        installer_img = MacOSDownloader.get_cached_image_path(macos_version)
        if installer_img:
            console.print(f"  [bold green]✔ Imagem oficial vinculada automaticamente:[/bold green] {installer_img.name}")
        else:
            console.print(f"  [bold cyan]▶ Baixando imagem oficial dos servidores da Apple...[/bold cyan]")
            installer_img = MacOSDownloader.prepare_installer(macos_version)

        if not installer_img:
            console.print("[red]Erro ao obter o instalador da Apple. Teste abortado.[/red]")
            Prompt.ask("Enter para sair")
            return

        # 3. Tamanho do Disco Virtual
        console.print("\n[bold]3. Tamanho do Disco Virtual macOS:[/bold]")
        disk_size_gb = IntPrompt.ask("Tamanho do Disco (GB)", default=64)

        # 4. Modo de visualização
        console.print("\n[bold]4. Modo de Exibição:[/bold]")
        console.print("  1 - Tela Cheia (Fullscreen Kiosk)")
        console.print("  2 - Modo Janela (Windowed)")
        disp_choice = Prompt.ask("Modo", choices=["1", "2"], default="1")
        fullscreen = "1" if disp_choice == "1" else "0"

        # Áudio corrigido definitivo (ich9-intel-hda)
        audio_device = "ich9-intel-hda"

        # 5. Provisionamento da VM de teste (limpeza automática)
        test_vm_id = f"test-ab-{macos_version}"
        test_vm_dir = Path(os.path.expanduser("~/.config/vhackintosh/vms")) / test_vm_id
        if test_vm_dir.exists():
            import shutil
            try:
                shutil.rmtree(test_vm_dir)
            except Exception as e:
                console.print(f"[yellow]Aviso ao limpar diretório antigo: {e}[/yellow]")

        console.print(f"\n[bold cyan]▶ Provisionando armazenamento ({disk_size_gb} GB) e OpenCore EFI para '{test_vm_id}'...[/bold cyan]")
        smbios = GenSMBIOS.generate(macos_version)
        vm = VMConfig(
            id=test_vm_id,
            name=f"macOS {macos_version.capitalize()} (Teste A/B)",
            macos_version=macos_version,
            vcpus=12,
            ram_gb=8,
            disk_size_gb=disk_size_gb,
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
            env["REIMS_VGPU_GUEST_IMPORT"] = "off"
            env["REIMS_VGPU_ACQUIRE_TIMEOUT_MS"] = "100"
            env["CPUS"] = str(vm.vcpus)
            env["RAM"] = f"{vm.ram_gb}G"
            env["AUDIO_DEVICE"] = audio_device
            env["INSTALL_MEDIA"] = str(installer_img)
            env["PERSISTENT_DIR"] = str(bundle.vm_dir)
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
