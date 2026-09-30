"""
vHackintosh — Modern Terminal User Interface (TUI)
Interface rica em terminal construída com Python Rich para gestão de VMs macOS.
"""

from __future__ import annotations
import os
import sys
import uuid
import datetime
from pathlib import Path
from typing import Optional
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.layout import Layout
from rich.text import Text

from core.config import VMConfig, VMManagerStore, ExclusiveVMLock
from core.hardware import HardwareAdvisor
from core.gpu import GPUChecker
from core.smbios import GenSMBIOS
from core.downloader import MacOSDownloader, MACOS_PRODUCTS
from ui.banner import render_banner

console = Console()


class VMTUI:
    """Interface principal do gerenciador vHackintosh."""

    def __init__(self, store: VMManagerStore):
        self.store = store
        self.lock = ExclusiveVMLock()

    def run_main_loop(self) -> None:
        while True:
            console.clear()
            console.print(render_banner())

            running_vm = self.lock.get_running_vm_info()
            if running_vm:
                console.print(f"[bold red]● VM Ativa em Execução: {running_vm}[/bold red]\n")

            vms = self.store.list_vms()
            self._render_vm_table(vms)

            console.print("\n[bold cyan]Opções do Gerenciador:[/bold cyan]")
            console.print(" [bold green]1[/bold green] - Iniciar / Gerenciar uma VM")
            console.print(" [bold green]2[/bold green] - Criar Nova VM macOS (Assistente com Auto-Tuning)")
            console.print(" [bold green]3[/bold green] - Diagnóstico de Hardware & GPU Compatibility")
            console.print(" [bold green]4[/bold green] - Atualizar Componentes Upstream (reims-vgpu)")
            console.print(" [bold green]5[/bold green] - Baixar / Gerenciar Imagens do macOS (Apple Recovery)")
            console.print(" [bold red]0[/bold red] - Sair para o Terminal / Desligar")

            choice = Prompt.ask("\nEscolha uma opção", choices=["1", "2", "3", "4", "5", "0"], default="1")

            if choice == "1":
                self._menu_manage_vms(vms)
            elif choice == "2":
                self._wizard_create_vm()
            elif choice == "3":
                self._view_hardware_diagnostics()
            elif choice == "4":
                self._update_upstream()
            elif choice == "5":
                self._menu_download_images()
            elif choice == "0":
                console.print("[dim]Até logo![/dim]")
                break

    def _render_vm_table(self, vms: list[VMConfig]) -> None:
        table = Table(title="Máquinas Virtuais macOS Instaladas", border_style="cyan", header_style="bold magenta")
        table.add_column("#", justify="center", style="bold yellow", width=4)
        table.add_column("Nome da VM", style="bold white")
        table.add_column("Versão macOS", style="cyan")
        table.add_column("vCPUs", justify="center")
        table.add_column("RAM", justify="center")
        table.add_column("Disco", justify="center")
        table.add_column("Gráficos", justify="center")
        table.add_column("Auto-Start", justify="center")

        if not vms:
            table.add_row("-", "Nenhuma VM cadastrada ainda", "-", "-", "-", "-", "-", "-")
        else:
            for idx, vm in enumerate(vms, start=1):
                auto_str = "[bold green]★ SIM[/bold green]" if vm.auto_start else "[dim]NÃO[/dim]"
                table.add_row(
                    str(idx),
                    vm.name,
                    vm.macos_version.capitalize(),
                    f"{vm.vcpus} vCPUs",
                    f"{vm.ram_gb} GB",
                    f"{vm.disk_size_gb} GB",
                    vm.gpu_mode,
                    auto_str,
                )

        console.print(table)

    def _menu_manage_vms(self, vms: list[VMConfig]) -> None:
        if not vms:
            console.print("[yellow]Nenhuma VM cadastrada. Crie uma nova VM primeiro![/yellow]")
            Prompt.ask("Pressione Enter para continuar")
            return

        choices = [str(i) for i in range(1, len(vms) + 1)] + ["b"]
        idx_str = Prompt.ask("\nDigite o número da VM para gerenciar (ou [b] para voltar)", choices=choices)
        if idx_str == "b":
            return

        vm = vms[int(idx_str) - 1]
        self._vm_details_screen(vm)

    def _vm_details_screen(self, vm: VMConfig) -> None:
        while True:
            console.clear()
            console.print(render_banner())

            # Painel com todas as especificações da VM
            details_text = Text()
            details_text.append(f"Nome da VM: ", style="bold")
            details_text.append(f"{vm.name}\n", style="bold green")
            details_text.append(f"Versão do macOS: ", style="bold")
            details_text.append(f"{vm.macos_version.capitalize()}\n", style="cyan")
            details_text.append(f"Processamento: ", style="bold")
            details_text.append(f"{vm.vcpus} vCPUs\n", style="white")
            details_text.append(f"Memória RAM: ", style="bold")
            details_text.append(f"{vm.ram_gb} GB\n", style="white")
            details_text.append(f"Disco Virtual: ", style="bold")
            details_text.append(f"{vm.disk_size_gb} GB ({vm.disk_path or 'Padrão'})\n", style="white")
            details_text.append(f"Adaptador Gráfico: ", style="bold")
            details_text.append(f"{vm.gpu_mode} (X11 Backend)\n", style="magenta")
            details_text.append(f"Auto-Start no Boot: ", style="bold")
            details_text.append(f"{'ATIVADO' if vm.auto_start else 'DESATIVADO'}\n\n", style="bold green" if vm.auto_start else "dim")

            details_text.append(f"── OpenCore SMBIOS ──\n", style="dim cyan")
            details_text.append(f"Modelo: {vm.smbios.model}\n", style="dim")
            details_text.append(f"Serial: {vm.smbios.serial_number}\n", style="dim")
            details_text.append(f"MLB: {vm.smbios.board_serial}\n", style="dim")
            details_text.append(f"SmUUID: {vm.smbios.smuuid}\n", style="dim")

            panel = Panel(details_text, title=f"[bold green]Gerenciamento da VM: {vm.name}[/bold green]", border_style="green")
            console.print(panel)

            console.print("[bold cyan]Ações Disponíveis:[/bold cyan]")
            console.print(" [bold green]1[/bold green] - ▶ Iniciar esta VM")
            console.print(" [bold green]2[/bold green] - ★ Alternar Auto-Start no Boot")
            console.print(" [bold green]3[/bold green] - ⚙ Ajustar vCPUs e Memória RAM")
            console.print(" [bold green]4[/bold green] - 🔄 Regenerar Seriais GenSMBIOS")
            console.print(" [bold red]5[/bold red] - ✖ Excluir esta VM")
            console.print(" [bold yellow]0[/bold yellow] - Voltar ao Menu Principal")

            action = Prompt.ask("\nEscolha uma ação", choices=["1", "2", "3", "4", "5", "0"], default="1")

            if action == "1":
                self._launch_vm(vm)
                break
            elif action == "2":
                self.store.set_auto_start(vm.id, not vm.auto_start)
                vm.auto_start = not vm.auto_start
            elif action == "3":
                self._edit_vm_resources(vm)
            elif action == "4":
                new_smbios = GenSMBIOS.generate(vm.macos_version)
                vm.smbios = new_smbios
                self.store.update_vm(vm)
                console.print("[bold green]Novos seriais SMBIOS gerados com sucesso![/bold green]")
                Prompt.ask("Enter para continuar")
            elif action == "5":
                if Confirm.ask(f"[bold red]Tem certeza que deseja excluir a VM '{vm.name}'?[/bold red]"):
                    del_disk = Confirm.ask("Deseja apagar também o arquivo de disco virtual do SSD?")
                    self.store.delete_vm(vm.id, delete_disk=del_disk)
                    console.print("[green]VM excluída com sucesso.[/green]")
                    Prompt.ask("Enter para continuar")
                    break
            elif action == "0":
                break

    def _wizard_create_vm(self) -> None:
        console.clear()
        console.print(render_banner())
        console.print("[bold cyan]Assistente de Criação de VM macOS[/bold cyan]\n")

        # Análise automática de hardware
        profile = HardwareAdvisor.analyze()

        # 1. Nome da VM
        name = Prompt.ask("Digite um nome para a VM", default="macOS Workstation")
        vm_id = name.lower().replace(" ", "-") + "-" + str(uuid.uuid4())[:4]

        # 2. Versão do macOS
        console.print("\n[bold]Escolha a versão do macOS:[/bold]")
        console.print(" 1 - macOS Tahoe 26 (Experimental Reims vGPU)")
        console.print(" 2 - macOS Sequoia 15 (Estável)")
        console.print(" 3 - macOS Sonoma 14")
        ver_opt = Prompt.ask("Versão", choices=["1", "2", "3"], default="2")
        version_map = {"1": "tahoe", "2": "sequoia", "3": "sonoma"}
        macos_version = version_map[ver_opt]

        # 3. vCPUs recomendadas
        console.print(f"\n[cyan]Topologia de CPU detectada:[/cyan] {profile.cpu.model_name} ({profile.cpu.total_threads} threads)")
        vcpus = IntPrompt.ask("Quantidade de vCPUs", default=profile.cpu.suggested_vcpus)

        # 4. RAM recomendada
        console.print(f"[cyan]RAM Total do Host:[/cyan] {profile.ram.total_gb} GB")
        ram_gb = IntPrompt.ask("Quantidade de Memória RAM (GB)", default=profile.ram.suggested_vm_ram_gb)

        # 5. Tamanho do Disco
        disk_size_gb = IntPrompt.ask("Tamanho do Disco Virtual (GB)", default=64)

        # 6. Auto-Start
        auto_start = Confirm.ask("Definir esta VM para iniciar automaticamente no boot do host?", default=False)

        # 7. Geração automática de SMBIOS
        smbios = GenSMBIOS.generate(macos_version=macos_version)

        # 8. Download / Preparação do Instalador Apple
        cached_img = MacOSDownloader.get_cached_image_path(macos_version)
        installer_path = str(cached_img) if cached_img else ""
        if not cached_img:
            if Confirm.ask(f"\nDeseja baixar a imagem oficial de recuperação da Apple para {macos_version.capitalize()} agora?", default=True):
                img_result = MacOSDownloader.prepare_installer(macos_version)
                if img_result:
                    installer_path = str(img_result)

        vm = VMConfig(
            id=vm_id,
            name=name,
            macos_version=macos_version,
            vcpus=vcpus,
            ram_gb=ram_gb,
            disk_size_gb=disk_size_gb,
            disk_path=installer_path,
            auto_start=auto_start,
            smbios=smbios,
            created_at=datetime.datetime.now().isoformat(),
        )

        self.store.add_vm(vm)
        console.print(f"\n[bold green]✔ VM '{name}' criada e configurada com sucesso![/bold green]")
        Prompt.ask("Pressione Enter para voltar ao menu")

    def _edit_vm_resources(self, vm: VMConfig) -> None:
        console.print(f"\n[bold]Ajuste de Recursos para {vm.name}:[/bold]")
        vm.vcpus = IntPrompt.ask("vCPUs", default=vm.vcpus)
        vm.ram_gb = IntPrompt.ask("RAM (GB)", default=vm.ram_gb)
        self.store.update_vm(vm)
        console.print("[bold green]Recursos atualizados com sucesso![/bold green]")
        Prompt.ask("Enter para continuar")

    def _view_hardware_diagnostics(self, interactive: bool = True) -> None:
        if interactive and sys.stdin.isatty():
            console.clear()
        console.print(render_banner())

        profile = HardwareAdvisor.analyze()
        gpus = GPUChecker.list_gpus()

        console.print("[bold cyan]── Diagnóstico de Processamento & Memória ──[/bold cyan]")
        console.print(f"CPU: [bold white]{profile.cpu.model_name}[/bold white]")
        console.print(f"Threads Totais: [bold green]{profile.cpu.total_threads}[/bold green] | Físicos: {profile.cpu.physical_cores}")
        console.print(f"RAM Total: [bold green]{profile.ram.total_gb} GB[/bold green] | Disponível: {profile.ram.available_gb} GB")
        console.print(f"Recomendação de Alocação Segura: [bold cyan]{profile.cpu.suggested_vcpus} vCPUs / {profile.ram.suggested_vm_ram_gb} GB RAM[/bold cyan]\n")

        console.print("[bold cyan]── Diagnóstico de GPUs & Aceleração ──[/bold cyan]")
        for gpu in gpus:
            console.print(f"• [bold white]{gpu.vendor_name} {gpu.device_name}[/bold white] ({gpu.pci_slot})")
            console.print(f"  Compatibilidade Reims vGPU: {'[bold green]SIM (Vulkan 1.2+)[/bold green]' if gpu.reims_vgpu_compatible else '[red]NÃO[/red]'}")
            console.print(f"  IOMMU Group: {gpu.iommu_group or 'N/A'} | VFIO Stubbed: {gpu.is_vfio_stubbed}")
            console.print(f"  Recomendação: [dim]{gpu.recommendation}[/dim]\n")

        if interactive and sys.stdin.isatty():
            Prompt.ask("Pressione Enter para voltar")

    def _update_upstream(self) -> None:
        console.print("\n[bold cyan]Sincronizando novidades e reconstruindo componentes...[/bold cyan]")
        from core.builder import SilentBuilder
        reims_path = "/home/felipeab10/reims-vgpu"
        if os.path.exists(reims_path):
            SilentBuilder.build_reims_vgpu(reims_path)
            console.print("[bold green]Componentes atualizados e compilados com sucesso![/bold green]")
        else:
            console.print("[yellow]Diretório reims-vgpu não encontrado.[/yellow]")
        Prompt.ask("Enter para continuar")

    def _menu_download_images(self) -> None:
        console.clear()
        console.print(render_banner())
        console.print("[bold cyan]Central de Download de Imagens macOS (Apple Recovery)[/bold cyan]\n")

        versions = list(MACOS_PRODUCTS.keys())
        for idx, ver in enumerate(versions, start=1):
            info = MACOS_PRODUCTS[ver]
            cached = MacOSDownloader.get_cached_image_path(ver)
            status_str = f"[bold green]✔ BAIXADO[/bold green] ({cached})" if cached else "[dim]Não baixado[/dim]"
            console.print(f" [bold green]{idx}[/bold green] - {info['name']}: {status_str}")

        console.print(" [bold yellow]0[/bold yellow] - Voltar ao Menu")

        choice = Prompt.ask("\nEscolha uma versão para baixar/reparar", choices=[str(i) for i in range(len(versions) + 1)], default="0")
        if choice == "0":
            return

        chosen_ver = versions[int(choice) - 1]
        console.print(f"\n[bold green]Iniciando processo para {MACOS_PRODUCTS[chosen_ver]['name']}...[/bold green]")
        MacOSDownloader.prepare_installer(chosen_ver)
        Prompt.ask("\nPressione Enter para continuar")

    def _launch_vm(self, vm: VMConfig) -> None:
        console.clear()
        console.print(render_banner())

        if not self.lock.acquire(vm.name):
            console.print("[bold red]ERRO: Já existe outra máquina virtual em execução no momento![/bold red]")
            console.print("[yellow]Apenas 1 VM pode ser executada por vez no vHackintosh.[/yellow]")
            Prompt.ask("Enter para continuar")
            return

        try:
            console.print(f"[bold green]▶ Iniciando VM '{vm.name}' em Modo Kiosk Fullscreen...[/bold green]\n")
            # Executa script de boot correspondente
            reims_dir = "/home/felipeab10/reims-vgpu"
            script_path = os.path.join(reims_dir, "scripts", f"run-{vm.macos_version}.sh")
            if os.path.exists(script_path):
                cmd = f"FORCE_X11=1 CPUS={vm.vcpus} RAM={vm.ram_gb}G {script_path}"
                os.system(cmd)
            else:
                console.print(f"[yellow]Script {script_path} não encontrado, utilizando inicializador padrão...[/yellow]")
        finally:
            self.lock.release()
