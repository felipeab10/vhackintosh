"""
vHackintosh — Modern Terminal User Interface (TUI)
Interface rica em terminal construída com Python Rich para gestão de VMs macOS.
"""

from __future__ import annotations
import os
import sys
import uuid
import datetime
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.layout import Layout
from rich.text import Text

from core.config import VMConfig, VMManagerStore, ExclusiveVMLock, CONFIG_DIR
from core.hardware import HardwareAdvisor
from core.gpu import GPUChecker
from core.readiness import SystemReadinessChecker
from core.smbios import GenSMBIOS
from core.downloader import MacOSDownloader, MACOS_PRODUCTS
from core.disk import DiskProvisioner
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

            # Checagem de prontidão do sistema (Virtualização BIOS e Internet)
            readiness = SystemReadinessChecker.check_all()
            if not readiness.can_run_vms:
                console.print("[bold red]⚠ ATENÇÃO: Virtualização por hardware (VT-x / AMD SVM) DESATIVADA na BIOS![/bold red]")
                console.print("[yellow]Acesse o setup da placa-mãe (BIOS) e ative a virtualização para poder iniciar VMs.[/yellow]\n")
            elif not readiness.has_internet:
                console.print("[yellow]⚠ Aviso de Rede: Sem conexão com a Internet (downloads Apple e AI indisponíveis).[/yellow]\n")

            vms = self.store.list_vms()
            self._render_vm_table(vms)

            console.print("\n[bold cyan]Opções do Gerenciador:[/bold cyan]")
            console.print(" [bold green]1[/bold green] - Iniciar / Gerenciar uma VM")
            console.print(" [bold green]2[/bold green] - Criar Nova VM macOS (Assistente com Auto-Tuning)")
            console.print(" [bold green]3[/bold green] - Diagnóstico de Hardware & GPU Compatibility")
            console.print(" [bold green]4[/bold green] - Atualizar Componentes Upstream (reims-vgpu)")
            console.print(" [bold green]5[/bold green] - Baixar / Gerenciar Imagens do macOS (Apple Recovery)")
            console.print(" [bold green]6[/bold green] - Teste A/B de Instalação do Zero (Apple Recovery)")
            console.print(" [bold green]7[/bold green] - Gerador de Imagem ISO Bootável (Appliance Live USB com AI Harnesses)")
            is_live_media = os.path.exists("/run/archiso/bootmnt")
            if is_live_media:
                console.print(" [bold magenta]I[/bold magenta] - [bold yellow]★ INSTALAR vHackintosh OS no SSD/Disco deste Computador[/bold yellow]")

            console.print(" [bold red]0[/bold red] - Sair para o Terminal / Desligar")

            choices = ["1", "2", "3", "4", "5", "6", "7", "0"]
            if is_live_media:
                choices.extend(["I", "i"])

            choice = Prompt.ask("\nEscolha uma opção", choices=choices, default="1")

            if choice in ("I", "i"):
                if shutil.which("vhackintosh-install"):
                    cmd = ["sudo", "vhackintosh-install"] if os.geteuid() != 0 else ["vhackintosh-install"]
                    subprocess.run(cmd)
                else:
                    console.print("[red]Instalador 'vhackintosh-install' não encontrado.[/red]")
                    Prompt.ask("Enter para continuar")
            elif choice == "1":
                self._menu_manage_vms(vms)
            elif choice == "2":
                self._wizard_create_vm()
            elif choice == "3":
                self._view_hardware_diagnostics()
            elif choice == "4":
                self._update_upstream()
            elif choice == "5":
                self._menu_download_images()
            elif choice == "6":
                from core.installer_runner import ABInstallerTest
                ABInstallerTest.run_interactive()
            elif choice == "7":
                self._menu_build_iso()
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
                gpu_str = vm.selected_gpu_name or vm.gpu_mode
                if len(gpu_str) > 24:
                    gpu_str = gpu_str[:21] + "..."
                table.add_row(
                    str(idx),
                    vm.name,
                    vm.macos_version.capitalize(),
                    f"{vm.vcpus} vCPUs",
                    f"{vm.ram_gb} GB",
                    f"{vm.disk_size_gb} GB",
                    gpu_str,
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
            # Informações de disco
            disk_info = DiskProvisioner.get_disk_info(vm.disk_path) if vm.disk_path else {"virtual_gb": vm.disk_size_gb, "disk_gb": 0}
            details_text.append(f"Disco Virtual: ", style="bold")
            details_text.append(f"{vm.disk_size_gb} GB alocados (Uso real no host: {disk_info['disk_gb']} GB)\n", style="white")
            details_text.append(f"Arquivo de Disco: ", style="bold")
            details_text.append(f"{vm.disk_path or 'Padrão'}\n", style="dim")
            details_text.append(f"Adaptador Gráfico: ", style="bold")
            gpu_display = vm.selected_gpu_name or vm.gpu_mode
            pci_suffix = f" [PCI: {vm.selected_gpu}]" if vm.selected_gpu else ""
            details_text.append(f"{gpu_display}{pci_suffix}\n", style="magenta")
            details_text.append(f"Auto-Start no Boot: ", style="bold")
            details_text.append(f"{'ATIVADO' if vm.auto_start else 'DESATIVADO'}\n\n", style="bold green" if vm.auto_start else "dim")

            details_text.append(f"── OpenCore SMBIOS ──\n", style="dim cyan")
            details_text.append(f"Modelo: {vm.smbios.model}\n", style="dim")
            details_text.append(f"Serial: {vm.smbios.serial_number}\n", style="dim")
            details_text.append(f"MLB: {vm.smbios.board_serial}\n", style="dim")
            details_text.append(f"SmUUID: {vm.smbios.smuuid}\n", style="dim")

            panel = Panel(details_text, title=f"[bold green]Gerenciamento da VM: {vm.name}[/bold green]", border_style="green")
            console.print(panel)

            gpus = GPUChecker.list_gpus()
            has_multiple_gpus = len(gpus) > 1

            console.print("[bold cyan]Ações Disponíveis:[/bold cyan]")
            console.print(" [bold green]1[/bold green] - ▶ Iniciar esta VM")
            console.print(" [bold green]2[/bold green] - ★ Alternar Auto-Start no Boot")
            console.print(" [bold green]3[/bold green] - ⚙ Ajustar vCPUs e Memória RAM")
            console.print(" [bold green]4[/bold green] - 🔄 Regenerar Seriais GenSMBIOS")
            if has_multiple_gpus:
                console.print(" [bold green]5[/bold green] - 🎮 Alterar Placa de Vídeo Vinculada")
                console.print(" [bold red]6[/bold red] - ✖ Excluir esta VM")
                allowed_actions = ["1", "2", "3", "4", "5", "6", "0"]
            else:
                console.print(" [bold red]5[/bold red] - ✖ Excluir esta VM")
                allowed_actions = ["1", "2", "3", "4", "5", "0"]
            console.print(" [bold yellow]0[/bold yellow] - Voltar ao Menu Principal")

            action = Prompt.ask("\nEscolha uma ação", choices=allowed_actions, default="1")

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
            elif has_multiple_gpus and action == "5":
                self._choose_gpu_for_vm(vm, gpus)
                self.store.update_vm(vm)
                console.print(f"[bold green]Placa de vídeo atualizada para: {vm.selected_gpu_name}![/bold green]")
                Prompt.ask("Enter para continuar")
            elif (has_multiple_gpus and action == "6") or (not has_multiple_gpus and action == "5"):
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
        console.print(" 2 - macOS Sequoia 15 (Recomendada / Estável)")
        console.print(" 3 - macOS Ventura 13 (Legado Estável)")
        ver_opt = Prompt.ask("Versão", choices=["1", "2", "3"], default="2")
        version_map = {"1": "tahoe", "2": "sequoia", "3": "ventura"}
        macos_version = version_map[ver_opt]

        # 3. vCPUs recomendadas
        console.print(f"\n[cyan]Topologia de CPU detectada:[/cyan] {profile.cpu.model_name} ({profile.cpu.total_threads} threads)")
        vcpus = IntPrompt.ask("Quantidade de vCPUs", default=profile.cpu.suggested_vcpus)

        # 4. RAM recomendada
        console.print(f"[cyan]RAM Total do Host:[/cyan] {profile.ram.total_gb} GB")
        ram_gb = IntPrompt.ask("Quantidade de Memória RAM (GB)", default=profile.ram.suggested_vm_ram_gb)

        # 5. Tamanho do Disco
        disk_size_gb = IntPrompt.ask("Tamanho do Disco Virtual (GB)", default=64)

        # 6. Seleção de Placa de Vídeo (somente se houver mais de 1 GPU no host)
        gpus = GPUChecker.list_gpus()
        selected_gpu_slot = None
        selected_gpu_name = None

        if len(gpus) > 1:
            console.print("\n[bold cyan]Placas de Vídeo Detectadas no Host:[/bold cyan]")
            default_choice = "1"
            for idx, g in enumerate(gpus, start=1):
                if g.vendor_name == "AMD":
                    tipo = "[bold green]Dedicada (Suporte Nativo Apple / Metal)[/bold green]"
                elif g.vendor_name == "NVIDIA":
                    tipo = "[bold magenta]Dedicada (Reims vGPU / Vulkan)[/bold magenta]"
                else:
                    tipo = "[bold cyan]Integrada (Reims vGPU / Vulkan)[/bold cyan]"

                if g.vendor_name in ["NVIDIA", "AMD"]:
                    default_choice = str(idx)
                console.print(f"  [bold yellow]{idx}[/bold yellow] - {g.vendor_name} {g.device_name} ([dim]{g.pci_slot}[/dim]) [{tipo}]")

            gpu_choices = [str(i) for i in range(1, len(gpus) + 1)]
            chosen_idx = Prompt.ask(
                "Selecione a GPU para vinculação e aceleração da VM",
                choices=gpu_choices,
                default=default_choice,
            )
            chosen_gpu = gpus[int(chosen_idx) - 1]
            selected_gpu_slot = chosen_gpu.pci_slot
            selected_gpu_name = f"{chosen_gpu.vendor_name} {chosen_gpu.device_name}"
        elif len(gpus) == 1:
            selected_gpu_slot = gpus[0].pci_slot
            selected_gpu_name = f"{gpus[0].vendor_name} {gpus[0].device_name}"

        # 7. Auto-Start
        auto_start = Confirm.ask("Definir esta VM para iniciar automaticamente no boot do host?", default=False)

        # 8. Geração automática de SMBIOS
        smbios = GenSMBIOS.generate(macos_version=macos_version)

        # 9. Download / Preparação do Instalador Apple
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
            disk_path="",
            auto_start=auto_start,
            selected_gpu=selected_gpu_slot,
            selected_gpu_name=selected_gpu_name,
            smbios=smbios,
            created_at=datetime.datetime.now().isoformat(),
        )

        console.print("\n[bold cyan]▶ Provisionando armazenamento e partição EFI OpenCore...[/bold cyan]")
        try:
            bundle = DiskProvisioner.provision(vm, installer_img=installer_path)
            vm.disk_path = str(bundle.hdd_path)
        except Exception as e:
            console.print(f"[bold red]Aviso no provisionamento de disco:[/bold red] {e}")

        self.store.add_vm(vm)
        console.print(f"\n[bold green]✔ VM '{name}' criada e provisionada com sucesso![/bold green]\n")

        if Confirm.ask("Deseja INICIAR a instalação do macOS nesta VM agora?", default=True):
            self._launch_vm(vm)
        else:
            Prompt.ask("Pressione Enter para voltar ao menu")

    def _edit_vm_resources(self, vm: VMConfig) -> None:
        console.print(f"\n[bold]Ajuste de Recursos para {vm.name}:[/bold]")
        vm.vcpus = IntPrompt.ask("vCPUs", default=vm.vcpus)
        vm.ram_gb = IntPrompt.ask("RAM (GB)", default=vm.ram_gb)

        gpus = GPUChecker.list_gpus()
        if len(gpus) > 1:
            if Confirm.ask("Deseja alterar a placa de vídeo vinculada a esta VM?", default=False):
                self._choose_gpu_for_vm(vm, gpus)

        self.store.update_vm(vm)
        console.print("[bold green]Recursos atualizados com sucesso![/bold green]")
        Prompt.ask("Enter para continuar")

    def _choose_gpu_for_vm(self, vm: VMConfig, gpus: list) -> None:
        console.print("\n[bold cyan]Placas de Vídeo Detectadas no Host:[/bold cyan]")
        for idx, g in enumerate(gpus, start=1):
            if g.vendor_name == "AMD":
                tipo = "[bold green]Dedicada (Suporte Nativo Apple / Metal)[/bold green]"
            elif g.vendor_name == "NVIDIA":
                tipo = "[bold magenta]Dedicada (Reims vGPU / Vulkan)[/bold magenta]"
            else:
                tipo = "[bold cyan]Integrada (Reims vGPU / Vulkan)[/bold cyan]"

            selected_mark = " ★ (Atual)" if vm.selected_gpu == g.pci_slot else ""
            console.print(f"  [bold yellow]{idx}[/bold yellow] - {g.vendor_name} {g.device_name} ([dim]{g.pci_slot}[/dim]) [{tipo}]{selected_mark}")

        gpu_choices = [str(i) for i in range(1, len(gpus) + 1)]
        g_choice = Prompt.ask("Selecione a nova GPU para a VM", choices=gpu_choices, default="1")
        chosen_gpu = gpus[int(g_choice) - 1]
        vm.selected_gpu = chosen_gpu.pci_slot
        vm.selected_gpu_name = f"{chosen_gpu.vendor_name} {chosen_gpu.device_name}"

    def _view_hardware_diagnostics(self, interactive: bool = True) -> None:
        if interactive and sys.stdin.isatty():
            console.clear()
        console.print(render_banner())

        # Matriz completa de prontidão e compatibilidade
        readiness = SystemReadinessChecker.check_all()
        console.print(SystemReadinessChecker.render_panel(readiness))
        console.print()

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

        readiness = SystemReadinessChecker.check_all()
        if not readiness.has_internet:
            console.print("[bold red]✖ Erro: Nenhuma conexão com a Internet detectada.[/bold red]")
            console.print("[yellow]O download de instaladores oficiais da Apple requer conectividade ativa.[/yellow]\n")
            if shutil.which("nmtui"):
                if Confirm.ask("Deseja abrir o utilitário de rede 'nmtui' para conectar agora?", default=True):
                    subprocess.run(["nmtui"])
            Prompt.ask("Pressione Enter para continuar")
            return

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
            console.print(f"[bold green]▶ Iniciando VM '{vm.name}' (macOS {vm.macos_version.capitalize()})...[/bold green]\n")

            reims_dir = None
            for cand in ["/opt/reims-vgpu", "/home/felipeab10/reims-vgpu", os.path.expanduser("~/reims-vgpu")]:
                if os.path.exists(os.path.join(cand, "vm", "boot-x86.sh")):
                    reims_dir = cand
                    break

            # Se a VM possui disco e OpenCore próprios no diretório
            custom_dir = None
            if vm.disk_path and os.path.exists(vm.disk_path):
                disk_parent = Path(vm.disk_path).parent
                if (disk_parent / "OpenCore.qcow2").exists():
                    custom_dir = disk_parent

            # Busca mídia de instalação (BaseSystem.img) se disponível
            installer_img = None
            for cand_img in [
                custom_dir / "BaseSystem.img" if custom_dir else None,
                CONFIG_DIR / "images" / vm.macos_version / "BaseSystem.img",
                Path(os.path.expanduser(f"~/.config/vhackintosh/images/{vm.macos_version}/BaseSystem.img")),
            ]:
                if cand_img and cand_img.exists():
                    installer_img = str(cand_img)
                    break

            rail_dir = Path(reims_dir) / "vm" / "disks" / "rails" / vm.macos_version if reims_dir else None
            use_reims_rail = bool(reims_dir and rail_dir and rail_dir.exists() and os.path.exists(os.path.join(reims_dir, "vm", "boot-x86.sh")))

            env_vars = os.environ.copy()
            env_vars["FORCE_X11"] = "1"
            env_vars["REIMS_VGPU_FULLSCREEN"] = "1"
            env_vars["REIMS_VGPU_BACKEND"] = "vulkan"
            env_vars["REIMS_VGPU_WINDOW"] = "1"
            env_vars["REIMS_VGPU_GUEST_IMPORT"] = "off"
            env_vars["REIMS_VGPU_ACQUIRE_TIMEOUT_MS"] = "100"
            env_vars["CPUS"] = str(vm.vcpus)
            env_vars["RAM"] = f"{vm.ram_gb}G"
            env_vars["AUDIO_DEVICE"] = vm.audio_device or "ich9-intel-hda"
            env_vars["QEMU_REBOOT_ACTION"] = "reset"

            # Aplica diretivas de aceleração gráfica para a GPU vinculada
            self._apply_gpu_environment(vm, env_vars)

            if use_reims_rail:
                boot_script = os.path.join(reims_dir, "vm", "boot-x86.sh")
                if installer_img:
                    env_vars["INSTALL_MEDIA"] = installer_img

                if custom_dir:
                    env_vars["PERSISTENT_DIR"] = str(custom_dir)
                    env_vars["DISK_MASTER"] = str(vm.disk_path)
                    env_vars["OPENCORE_MASTER"] = str(custom_dir / "OpenCore.qcow2")

                subprocess.run([
                    "bash", boot_script,
                    "--rail", vm.macos_version,
                    "--persistent",
                    "--device", "reims-vgpu-pci",
                ], env=env_vars)
            else:
                self._launch_vm_native_qemu(vm, custom_dir, installer_img, env_vars=env_vars)

            console.print("\n[bold yellow]VM finalizada.[/bold yellow]")
            if sys.stdin.isatty():
                Prompt.ask("Pressione Enter para retornar ao gerenciador")
        finally:
            self.lock.release()

    def _apply_gpu_environment(self, vm: VMConfig, env_vars: dict) -> None:
        if not vm.selected_gpu_name and not vm.selected_gpu:
            return
        gpu_name_upper = (vm.selected_gpu_name or "").upper()
        if "NVIDIA" in gpu_name_upper:
            env_vars["__NV_PRIME_RENDER_OFFLOAD"] = "1"
            env_vars["__GLX_VENDOR_LIBRARY_NAME"] = "nvidia"
            env_vars["__VK_LAYER_NV_optimus"] = "NVIDIA_only"
            env_vars["DRI_PRIME"] = "1"
            if os.path.exists("/usr/share/vulkan/icd.d/nvidia_icd.json"):
                env_vars["VK_DRIVER_FILES"] = "/usr/share/vulkan/icd.d/nvidia_icd.json"
        elif "INTEL" in gpu_name_upper:
            env_vars["DRI_PRIME"] = "0"
            for intel_icd in [
                "/usr/share/vulkan/icd.d/intel_icd.x86_64.json",
                "/usr/share/vulkan/icd.d/intel_hasvk_icd.x86_64.json",
            ]:
                if os.path.exists(intel_icd):
                    env_vars["VK_DRIVER_FILES"] = intel_icd
                    break
        elif "AMD" in gpu_name_upper:
            env_vars["DRI_PRIME"] = "1"
            if os.path.exists("/usr/share/vulkan/icd.d/radeon_icd.x86_64.json"):
                env_vars["VK_DRIVER_FILES"] = "/usr/share/vulkan/icd.d/radeon_icd.x86_64.json"

        if vm.selected_gpu:
            env_vars["REIMS_VGPU_PCI_SLOT"] = vm.selected_gpu

    def _launch_vm_native_qemu(
        self,
        vm: VMConfig,
        custom_dir: Optional[Path],
        installer_img: Optional[str],
        env_vars: Optional[dict] = None,
    ) -> None:
        console.print("[cyan]Inicializando motor KVM nativo para macOS...[/cyan]")

        # Localiza OVMF_CODE e OVMF_VARS
        ovmf_code = None
        for cand in [
            Path("/opt/vhackintosh/templates/OVMF_CODE_4M.fd"),
            Path(__file__).resolve().parent.parent / "templates" / "OVMF_CODE_4M.fd",
            Path("/usr/share/edk2/x64/OVMF_CODE.4m.fd"),
            Path("/usr/share/edk2-ovmf/x64/OVMF_CODE.4m.fd"),
            Path("/home/felipeab10/reims-vgpu/vm/ovmf/OVMF_CODE_4M.fd"),
        ]:
            if cand.exists():
                ovmf_code = str(cand)
                break

        ovmf_vars = None
        if custom_dir and (custom_dir / "OVMF_VARS.fd").exists():
            ovmf_vars = str(custom_dir / "OVMF_VARS.fd")
        else:
            for cand in [
                Path("/opt/vhackintosh/templates/OVMF_VARS.fd"),
                Path(__file__).resolve().parent.parent / "templates" / "OVMF_VARS.fd",
                Path("/usr/share/edk2/x64/OVMF_VARS.4m.fd"),
            ]:
                if cand.exists():
                    ovmf_vars = str(cand)
                    break

        opencore = None
        if custom_dir and (custom_dir / "OpenCore.qcow2").exists():
            opencore = str(custom_dir / "OpenCore.qcow2")
        else:
            for cand in [
                Path("/opt/vhackintosh/templates/OpenCore.qcow2"),
                Path(__file__).resolve().parent.parent / "templates" / "OpenCore.qcow2",
            ]:
                if cand.exists():
                    opencore = str(cand)
                    break

        if not opencore or not ovmf_code:
            console.print("[bold red]Erro: Componentes essenciais de boot (OpenCore ou OVMF) não encontrados![/bold red]")
            return

        qemu_bin = "qemu-system-x86_64"
        for qcand in [
            "/opt/reims-vgpu/vendor/qemu/build/qemu-system-x86_64",
            "/home/felipeab10/reims-vgpu/vendor/qemu/build/qemu-system-x86_64",
            shutil.which("qemu-system-x86_64"),
        ]:
            if qcand and os.path.exists(qcand) and os.access(qcand, os.X_OK):
                qemu_bin = qcand
                break

        cmd = [
            qemu_bin,
            "-enable-kvm",
            "-m", f"{vm.ram_gb}G",
            "-smp", f"cpus={vm.vcpus},sockets=1,cores={vm.vcpus},threads=1",
            "-cpu", "host,kvm=on,vendor=GenuineIntel,+invtsc,+hypervisor,vmx=on",
            "-machine", "q35,accel=kvm",
            "-drive", f"if=pflash,format=raw,readonly=on,file={ovmf_code}",
        ]
        if ovmf_vars:
            cmd.extend(["-drive", f"if=pflash,format=raw,file={ovmf_vars}"])

        cmd.extend([
            "-device", "ich9-ahci,id=sata",
            "-drive", f"id=OpenCoreBoot,if=none,format=qcow2,file={opencore}",
            "-device", "ide-hd,bus=sata.2,drive=OpenCoreBoot",
        ])

        if installer_img and os.path.exists(installer_img):
            cmd.extend([
                "-drive", f"id=InstallMedia,if=none,format=raw,file={installer_img}",
                "-device", "ide-hd,bus=sata.3,drive=InstallMedia",
            ])

        if vm.disk_path and os.path.exists(vm.disk_path):
            cmd.extend([
                "-drive", f"id=MacHDD,if=none,format=qcow2,file={vm.disk_path}",
                "-device", "ide-hd,bus=sata.4,drive=MacHDD",
            ])

        cmd.extend([
            "-netdev", "user,id=net0",
            "-device", "virtio-net-pci,netdev=net0,mac=52:54:00:12:34:56",
            "-device", "usb-ehci,id=ehci",
            "-device", "usb-kbd,bus=ehci.0",
            "-device", "usb-tablet,bus=ehci.0",
            "-device", "ich9-intel-hda",
            "-device", "hda-output",
        ])

        # Suporte a Passthrough VFIO direto para GPUs AMD compatíveis nativamente com macOS
        if vm.gpu_mode == "vfio-passthrough" and vm.selected_gpu:
            clean_pci = vm.selected_gpu.replace("0000:", "")
            cmd.extend(["-device", f"vfio-pci,host={clean_pci},multifunction=on"])
        else:
            cmd.extend(["-vga", "std"])

        # Se estiver no console TTY puro sem display X11/Wayland ativo, inicializa via xinit
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            if shutil.which("xinit"):
                cmd = ["xinit"] + cmd + ["--", ":0"]

        if env_vars is None:
            env_vars = os.environ.copy()
            self._apply_gpu_environment(vm, env_vars)

        subprocess.run(cmd, env=env_vars)

    def _menu_build_iso(self) -> None:
        console.clear()
        console.print(render_banner())
        console.print("[bold cyan]Gerador de Imagem ISO Bootável (vHackintosh Appliance Live USB)[/bold cyan]\n")
        console.print("O Appliance vHackintosh é um sistema operacional Linux dedicado e minimalista,")
        console.print("projetado especificamente para rodar máquinas virtuais macOS com aceleração gráfica.\n")
        console.print("[bold green]Recursos integrados na ISO:[/bold green]")
        console.print("  • Kernel Linux com otimizações KVM para macOS (MSRs ignorados, nested virtualization)")
        console.print("  • Pilha gráfica Vulkan + X11 otimizada para o driver reims-vgpu")
        console.print("  • Subsistema de áudio PipeWire de baixa latência (Intel ICH9 HDA)")
        console.print("  • Auto-login no console com interface Kiosk do vHackintosh")
        console.print("  • [bold magenta]AI Coding Harnesses CLI integrados:[/bold magenta]")
        console.print("      - [cyan]claude[/cyan]   (@anthropic-ai/claude-code)")
        console.print("      - [cyan]codex[/cyan]    (@openai/codex)")
        console.print("      - [cyan]opencode[/cyan] (opencode-cli)")
        console.print("  • Tema customizado do GRUB 2 com identidade visual Apple Silicon / Minimalist\n")

        builder_script = Path(__file__).resolve().parent.parent / "appliance" / "build-iso.sh"
        if not shutil.which("mkarchiso"):
            console.print("[bold yellow]Aviso: 'mkarchiso' não está instalado neste computador host.[/bold yellow]")
            console.print("Para instalar o Archiso no Arch / CachyOS, execute:")
            console.print("  [bold green]sudo pacman -S --needed archiso[/bold green]\n")

        console.print(f"Script de compilação: [dim]{builder_script}[/dim]\n")
        console.print(" [bold green]1[/bold green] - Instruções para compilar a ISO (requer sudo no terminal)")
        console.print(" [bold green]2[/bold green] - Instalar / Atualizar Harnesses CLI neste sistema host agora")
        console.print(" [bold yellow]0[/bold yellow] - Voltar ao Menu Principal")

        choice = Prompt.ask("\nEscolha uma opção", choices=["1", "2", "0"], default="0")
        if choice == "1":
            console.print("\n[bold yellow]Para compilar a imagem ISO completa, execute no seu terminal:[/bold yellow]")
            console.print(f"  [bold green]sudo bash {builder_script}[/bold green]\n")
            Prompt.ask("Pressione Enter para continuar")
        elif choice == "2":
            setup_script = Path(__file__).resolve().parent.parent / "appliance" / "archiso" / "airootfs" / "usr" / "local" / "bin" / "setup-harness-tools.sh"
            if setup_script.exists():
                subprocess.run(["bash", str(setup_script)])
            Prompt.ask("\nPressione Enter para continuar")
