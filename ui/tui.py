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
from core.mode import detect_system_mode, has_installer_binary
from core.install_phase import (
    InstallPhase,
    InstallEvidence,
    InstallPhaseResolver,
    InstallProbe,
    LaunchPolicy,
    DiskProbe,
    classify_evidence,
    summarize_probe,
    phase_label,
    phase_short_label,
    normalize_phase,
)
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
        # Detectado uma única vez: o modo de execução não muda durante a sessão.
        self.system_mode = detect_system_mode()
        self.installer_available = has_installer_binary()

    def run_main_loop(self) -> None:
        self._backfill_install_phases()
        while True:
            console.clear()
            console.print(render_banner())

            running_vm = self.lock.get_running_vm_info()
            if running_vm:
                console.print(f"[bold red]● VM Ativa em Execução: {running_vm}[/bold red]\n")

            if self.system_mode == "live":
                console.print("[bold cyan]● Mídia Live (pendrive/ISO) — instale no disco pela opção [I] ou use em modo demonstração.[/bold cyan]\n")

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

            # O instalador bare-metal permanece sempre acessível quando presente.
            # Ele é copiado para o sistema definitivo por design, portanto a sua
            # existência NÃO indica Live USB (ver core.mode.detect_system_mode).
            installer_available = self.installer_available
            if installer_available:
                console.print(" [bold yellow]★ [I][/bold yellow] - [bold magenta]INSTALAR vHackintosh OS no SSD/Disco deste Computador (Instalador)[/bold magenta]")

            console.print(" [bold green]1[/bold green] - Iniciar / Gerenciar uma VM")
            console.print(" [bold green]2[/bold green] - Criar Nova VM macOS (Assistente com Auto-Tuning)")
            console.print(" [bold green]3[/bold green] - Diagnóstico de Hardware & GPU Compatibility")
            console.print(" [bold green]4[/bold green] - Atualizar Componentes Upstream (reims-vgpu)")
            console.print(" [bold green]5[/bold green] - Baixar / Gerenciar Imagens do macOS (Apple Recovery)")
            console.print(" [bold green]6[/bold green] - Teste A/B de Instalação do Zero (Apple Recovery)")
            console.print(" [bold green]7[/bold green] - Gerador de Imagem ISO Bootável (Appliance Live USB com AI Harnesses)")
            console.print(" [bold red]0[/bold red] - Sair para o Terminal / Desligar")

            choices = ["1", "2", "3", "4", "5", "6", "7", "0"]
            if installer_available:
                choices.extend(["I", "i", "8"])

            choice = Prompt.ask("\nEscolha uma opção", choices=choices, default="1")

            if choice in ("I", "i", "8"):
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

    def _backfill_install_phases(self) -> None:
        """
        Resolve a fase de VMs ainda não verificadas (``install_phase == pending``)
        consultando o disco uma única vez por sessão e persistindo o resultado.

        NUNCA promove para ``INSTALLED`` — apenas registra ``pending`` ou
        ``installing``. Isso mantém a sincronização de energia do host desativada
        por segurança até a confirmação explícita do usuário.
        """
        try:
            vms = self.store.list_vms()
        except Exception:
            return

        unverified = [
            vm for vm in vms
            if normalize_phase(getattr(vm, "install_phase", None)) is InstallPhase.PENDING
        ]
        if not unverified:
            return

        console.print("[dim]Verificando o estado de instalação das VMs cadastradas...[/dim]")
        for vm in unverified:
            probe = InstallProbe.probe(vm.disk_path) if vm.disk_path else DiskProbe()
            vm.install_evidence = classify_evidence(probe).value
            vm.install_phase = InstallPhaseResolver.resolve_phase(vm, probe=probe).value
            self.store.update_vm(vm)
            console.print(f"  [dim]{vm.name}: {summarize_probe(probe)} → {vm.install_phase}[/dim]")
        console.print()

    def _render_vm_table(self, vms: list[VMConfig]) -> None:
        table = Table(title="Máquinas Virtuais macOS Instaladas", border_style="cyan", header_style="bold magenta")
        table.add_column("#", justify="center", style="bold yellow", width=4)
        table.add_column("Nome da VM", style="bold white")
        table.add_column("Versão macOS", style="cyan")
        table.add_column("Status", justify="center")
        table.add_column("vCPUs", justify="center")
        table.add_column("RAM", justify="center")
        table.add_column("Disco", justify="center")
        table.add_column("Gráficos", justify="center")
        table.add_column("Auto-Start", justify="center")

        if not vms:
            table.add_row("-", "Nenhuma VM cadastrada ainda", "-", "-", "-", "-", "-", "-", "-")
        else:
            for idx, vm in enumerate(vms, start=1):
                auto_str = "[bold green]★ SIM[/bold green]" if vm.auto_start else "[dim]NÃO[/dim]"
                gpu_str = vm.selected_gpu_name or vm.gpu_mode
                if len(gpu_str) > 24:
                    gpu_str = gpu_str[:21] + "..."
                phase = normalize_phase(getattr(vm, "install_phase", None))
                table.add_row(
                    str(idx),
                    vm.name,
                    vm.macos_version.capitalize(),
                    phase_short_label(phase, getattr(vm, "install_evidence", None)),
                    f"{vm.vcpus} vCPUs",
                    f"{vm.ram_gb} GB",
                    f"{vm.disk_size_gb} GB",
                    gpu_str,
                    auto_str,
                )

        console.print(table)

    def _toggle_install_phase(self, vm: VMConfig) -> None:
        """Alterna manualmente a fase da instalação do macOS desta VM."""
        console.clear()
        console.print(render_banner())
        console.print("[bold cyan]Estado da Instalação do macOS[/bold cyan]\n")

        current = normalize_phase(getattr(vm, "install_phase", None))
        probe = InstallProbe.probe(vm.disk_path) if vm.disk_path else DiskProbe()
        vm.install_evidence = classify_evidence(probe).value
        self.store.update_vm(vm)

        console.print(f"Fase atual: {phase_label(current)}")
        console.print(f"Reboots registrados: {getattr(vm, 'install_reboots', 0)}")
        console.print(f"Sonda de disco: [dim]{summarize_probe(probe)}[/dim]\n")

        if probe.looks_like_complete_install:
            console.print("[bold green]Evidência: existe um sistema macOS instalado neste disco.[/bold green]")
        elif probe.has_any_system_data:
            console.print("[yellow]Evidência: o disco tem dados, mas a instalação parece incompleta.[/yellow]")
        else:
            console.print("[yellow]Evidência: nenhum sistema detectado no disco.[/yellow]")

        console.print("\n[bold]Efeito da fase na execução da VM:[/bold]")
        console.print("  [bold green]INSTALADO[/bold green]  → mídia de instalação desacoplada, [bold]sincronização de energia ATIVA[/bold]")
        console.print("                  (reiniciar/desligar no macOS reinicia/desliga o computador)")
        console.print("  [bold yellow]INSTALANDO[/bold yellow] → mídia de instalação anexada, reboots contidos na VM")
        console.print("                  ([bold]o computador físico NUNCA é reiniciado[/bold])\n")

        if current is InstallPhase.INSTALLED:
            if Confirm.ask("Voltar esta VM para o modo INSTALAÇÃO (desativa a sincronização de energia)?", default=False):
                vm.install_phase = InstallPhase.INSTALLING.value
                self.store.update_vm(vm)
                console.print("[bold yellow]✔ VM em modo INSTALAÇÃO. O host não será reiniciado.[/bold yellow]")
        else:
            if Confirm.ask("Marcar o macOS como INSTALADO (ativa a sincronização de energia do host)?", default=False):
                vm.install_phase = InstallPhase.INSTALLED.value
                self.store.update_vm(vm)
                console.print("[bold green]✔ VM marcada como INSTALADA. O host acompanhará reboots/desligamentos.[/bold green]")

        Prompt.ask("\nPressione Enter para continuar")

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
            details_text.append(f"{'ATIVADO' if vm.auto_start else 'DESATIVADO'}\n", style="bold green" if vm.auto_start else "dim")
            show_picker = getattr(vm, "opencore_show_picker", False)
            details_text.append(f"Boot do OpenCore: ", style="bold")
            details_text.append(f"{'EXIBIR MENU (10s)' if show_picker else 'DIRETO NO MACOS (Segure ESPAÇO para opções)'}\n", style="yellow" if show_picker else "bold cyan")
            current_phase = normalize_phase(getattr(vm, "install_phase", None))
            details_text.append(f"Instalação do macOS: ", style="bold")
            details_text.append(f"{phase_label(current_phase)}\n")
            details_text.append(f"Reboots da instalação: ", style="bold")
            details_text.append(f"{getattr(vm, 'install_reboots', 0)}\n")
            details_text.append(f"Sincronização de energia do host: ", style="bold")
            if current_phase is InstallPhase.INSTALLED:
                details_text.append("ATIVADA (o PC reinicia/desliga junto com o macOS)\n\n", style="bold green")
            else:
                details_text.append("DESATIVADA (o PC não reinicia durante a instalação)\n\n", style="bold yellow")

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
            console.print(" [bold green]3[/bold green] - ⚡ Alternar Boot do OpenCore (Direto vs Menu)")
            console.print(" [bold green]4[/bold green] - ⚙ Ajustar vCPUs e Memória RAM")
            console.print(" [bold green]5[/bold green] - 🔄 Regenerar Seriais GenSMBIOS")
            console.print(" [bold magenta]i[/bold magenta] - ⏳ Alternar Estado da Instalação do macOS (Instalando / Instalado)")
            if has_multiple_gpus:
                console.print(" [bold green]6[/bold green] - 🎮 Alterar Placa de Vídeo Vinculada")
                console.print(" [bold red]7[/bold red] - ✖ Excluir esta VM")
                allowed_actions = ["1", "2", "3", "4", "5", "6", "7", "i", "I", "0"]
            else:
                console.print(" [bold red]6[/bold red] - ✖ Excluir esta VM")
                allowed_actions = ["1", "2", "3", "4", "5", "6", "i", "I", "0"]
            console.print(" [bold yellow]0[/bold yellow] - Voltar ao Menu Principal")

            action = Prompt.ask("\nEscolha uma ação", choices=allowed_actions, default="1")

            if action == "1":
                self._launch_vm(vm)
                break
            elif action in ("i", "I"):
                self._toggle_install_phase(vm)
            elif action == "2":
                new_state = not vm.auto_start
                self.store.set_auto_start(vm.id, new_state)
                vm.auto_start = new_state
            elif action == "3":
                new_picker = not getattr(vm, "opencore_show_picker", False)
                vm.opencore_show_picker = new_picker
                self.store.update_vm(vm)
                console.print("\n[cyan]Sincronizando config.plist da partição EFI do OpenCore...[/cyan]")
                opencore_file = None
                if vm.disk_path:
                    cand = Path(vm.disk_path).parent / "OpenCore.qcow2"
                    if cand.exists():
                        opencore_file = cand
                if not opencore_file:
                    for c in [
                        Path(os.path.expanduser(f"~/.config/vhackintosh/vms/{vm.id}/OpenCore.qcow2")),
                        Path(f"/root/.config/vhackintosh/vms/{vm.id}/OpenCore.qcow2"),
                    ]:
                        if c.exists():
                            opencore_file = c
                            break
                if opencore_file:
                    try:
                        DiskProvisioner.inject_opencore_config(opencore_file, vm, show_picker=new_picker)
                        status_str = "EXIBIR MENU (10s)" if new_picker else "AUTO-BOOT DIRETO NO MACOS (Segure ESPAÇO no boot para opções)"
                        console.print(f"[bold green]✔ OpenCore atualizado com sucesso: {status_str}[/bold green]")
                    except Exception as e:
                        console.print(f"[bold red]Erro ao regravar OpenCore.qcow2:[/bold red] {e}")
                else:
                    console.print("[yellow]Aviso: Arquivo OpenCore.qcow2 da VM não encontrado para atualização imediata.[/yellow]")
                Prompt.ask("Pressione Enter para continuar")
            elif action == "4":
                self._edit_vm_resources(vm)
            elif action == "5":
                new_smbios = GenSMBIOS.generate(vm.macos_version)
                vm.smbios = new_smbios
                self.store.update_vm(vm)
                console.print("[bold green]Novos seriais SMBIOS gerados com sucesso![/bold green]")
                Prompt.ask("Enter para continuar")
            elif has_multiple_gpus and action == "6":
                self._choose_gpu_for_vm(vm, gpus)
                self.store.update_vm(vm)
                console.print(f"[bold green]Placa de vídeo atualizada para: {vm.selected_gpu_name}![/bold green]")
                Prompt.ask("Enter para continuar")
            elif (has_multiple_gpus and action == "7") or (not has_multiple_gpus and action == "6"):
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
            self._launch_vm(vm, is_installation=True)
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
        from core.updater import SystemUpdater
        SystemUpdater.run_full_update(force=False)
        Prompt.ask("\nPressione Enter para continuar")

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

    def _launch_vm(self, vm: VMConfig, is_installation: bool = False) -> None:
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

            # ------------------------------------------------------------------
            # Fase do ciclo de vida da instalação do macOS.
            # Esta decisão governa TRÊS comportamentos críticos:
            #   1. acoplamento da mídia de instalação (BaseSystem.img);
            #   2. a flag -no-reboot do QEMU;
            #   3. a Sincronização de Energia do Host (reboot/poweroff do PC).
            # Enquanto a instalação não estiver concluída, o host NUNCA é
            # reiniciado — reboots intermediários da Apple ficam contidos no QEMU.
            # ------------------------------------------------------------------
            probe = InstallProbe.probe(vm.disk_path) if vm.disk_path else DiskProbe()
            policy = InstallPhaseResolver.build_policy(
                vm, force_installation=is_installation, probe=probe
            )
            vm.install_phase = policy.phase.value
            vm.install_evidence = classify_evidence(probe).value
            vm.last_booted_at = datetime.datetime.now().isoformat(timespec="seconds")
            self.store.update_vm(vm)

            console.print(f"[cyan]Estado da instalação:[/cyan] {phase_label(policy.phase)}")
            console.print(f"[dim]{policy.reason}[/dim]")
            if policy.is_installing:
                console.print("[dim]Reboots do instalador serão absorvidos pela VM. O computador físico não será reiniciado.[/dim]\n")

            # Mídia de instalação só é anexada enquanto a instalação não terminou.
            installer_img = None
            if policy.attach_installer:
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
            # Sem X11: a sessão gráfica do appliance é Wayland (sway kiosk) e a
            # janela do reims-vgpu (winit) fala Wayland nativo. Não exportamos
            # mais FORCE_X11 — o backend de janelamento passa a ser decidido pelo
            # próprio winit a partir da sessão em que a VM é iniciada.
            env_vars["REIMS_VGPU_FULLSCREEN"] = "1"
            env_vars["REIMS_VGPU_BACKEND"] = "vulkan"
            env_vars["REIMS_VGPU_WINDOW"] = "1"
            env_vars["REIMS_VGPU_GUEST_IMPORT"] = "off"
            env_vars["REIMS_VGPU_ACQUIRE_TIMEOUT_MS"] = "100"
            env_vars["CPUS"] = str(vm.vcpus)
            env_vars["RAM"] = f"{vm.ram_gb}G"
            env_vars["AUDIO_DEVICE"] = vm.audio_device or "ich9-intel-hda"
            # O harness rail traduz esta variável em `-action reboot=shutdown`.
            # - "reset" durante a instalação: o QEMU absorve os reboots da Apple.
            # - "exit" após instalado: o reboot do guest encerra o QEMU e o host
            #   pode acompanhar (mesma semântica do -no-reboot do motor nativo).
            env_vars["QEMU_REBOOT_ACTION"] = "reset" if policy.is_installing else "exit"

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

                # Fixa o RUN_DIR do rail para sabermos exatamente onde o socket QMP
                # é publicado (`$RUN_DIR/qmp.path`), permitindo monitorar os eventos
                # de energia do guest também pelo caminho rail.
                rail_run_dir = os.path.join(reims_dir, "vm", "disks", "run")
                env_vars["RUN_DIR"] = rail_run_dir

                from core.power import QMPPowerMonitor

                rail_monitor = QMPPowerMonitor(
                    sock_path=os.path.join(rail_run_dir, "qmp.sock"),
                    path_file=os.path.join(rail_run_dir, "qmp.path"),
                )
                rail_monitor.start()
                try:
                    rail_proc = subprocess.Popen([
                        "bash", boot_script,
                        "--rail", vm.macos_version,
                        "--persistent",
                        "--device", "reims-vgpu-pci",
                    ], env=env_vars)
                    rail_proc.wait()
                finally:
                    rail_monitor.stop()

                self._apply_host_power_policy(vm, policy, rail_monitor)
            else:
                self._launch_vm_native_qemu(
                    vm, custom_dir, installer_img, env_vars=env_vars, policy=policy
                )

            # Reavalia a fase após o encerramento e oferece concluir a instalação.
            self._post_vm_phase_update(vm, policy)

            console.print("\n[bold yellow]VM finalizada.[/bold yellow]")
            if sys.stdin.isatty():
                Prompt.ask("Pressione Enter para retornar ao gerenciador")
        finally:
            self.lock.release()

    def _apply_host_power_policy(self, vm: VMConfig, policy: LaunchPolicy, monitor) -> None:
        """
        Contabiliza reboots do guest e aplica (ou bloqueia) a sincronização de
        energia do host, de forma idêntica para o motor nativo e para o rail.

        GARANTIA: durante a instalação o computador físico NUNCA é
        reiniciado/desligado — era exatamente isso que interrompia a instalação.
        """
        if getattr(monitor, "reset_count", 0):
            vm.install_reboots = int(getattr(vm, "install_reboots", 0)) + monitor.reset_count
            self.store.update_vm(vm)

        if not policy.enable_host_power_sync:
            console.print(
                "\n[bold yellow]⏳ Instalação em andamento: o computador físico NÃO será "
                "reiniciado/desligado.[/bold yellow]"
            )
            if getattr(monitor, "reset_count", 0):
                console.print(f"[dim]Reboots absorvidos pela VM nesta sessão: {monitor.reset_count}[/dim]")
            return

        from core.power import PowerSync

        if monitor.last_reason == "guest-reset":
            PowerSync.sync_host_reboot(is_kiosk_mode=True)
        elif monitor.last_reason == "guest-shutdown":
            PowerSync.sync_host_shutdown(is_kiosk_mode=True)

    def _post_vm_phase_update(self, vm: VMConfig, policy: LaunchPolicy) -> None:
        """
        Reavalia o disco após a VM encerrar e oferece marcar a instalação como
        concluída. A promoção para INSTALLED exige confirmação explícita, pois um
        falso positivo reativaria o reboot do host no meio da instalação.
        """
        if policy.phase is InstallPhase.INSTALLED:
            return

        probe = InstallProbe.probe(vm.disk_path) if vm.disk_path else DiskProbe()
        console.print(f"\n[cyan]Sonda do disco da VM:[/cyan] [dim]{summarize_probe(probe)}[/dim]")
        vm.install_evidence = classify_evidence(probe).value

        if not probe.looks_like_complete_install:
            vm.install_phase = InstallPhase.INSTALLING.value
            self.store.update_vm(vm)
            console.print("[yellow]A instalação ainda NÃO está concluída.[/yellow]")
            console.print("[dim]Sincronização de energia do host permanece DESATIVADA (o PC não reinicia com a VM).[/dim]")
            return

        console.print("[bold green]✔ Foi detectado um sistema macOS instalado no disco desta VM.[/bold green]")
        if not sys.stdin.isatty():
            vm.install_phase = InstallPhase.INSTALLING.value
            self.store.update_vm(vm)
            return

        if Confirm.ask(
            "Marcar a instalação como CONCLUÍDA e ativar a sincronização de energia do host "
            "(reiniciar/desligar no macOS reinicia/desliga o computador)?",
            default=True,
        ):
            vm.install_phase = InstallPhase.INSTALLED.value
            vm.install_reboots = int(getattr(vm, "install_reboots", 0))
            self.store.update_vm(vm)
            console.print("[bold green]✔ Sincronização de energia do host ATIVADA para esta VM.[/bold green]")
        else:
            vm.install_phase = InstallPhase.INSTALLING.value
            self.store.update_vm(vm)
            console.print("[yellow]Mantida em modo INSTALAÇÃO: o host não será reiniciado/desligado.[/yellow]")

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

        # Remove barra de menus GTK (Machine, View) e ativa ajuste automático de janela
        env_vars["REIMS_VGPU_DISPLAY"] = "gtk,show-menubar=off,zoom-to-fit=on"

    def _launch_vm_native_qemu(
        self,
        vm: VMConfig,
        custom_dir: Optional[Path],
        installer_img: Optional[str],
        env_vars: Optional[dict] = None,
        policy: Optional[LaunchPolicy] = None,
    ) -> None:
        console.print("[cyan]Inicializando motor KVM nativo para macOS...[/cyan]")

        if policy is None:
            policy = InstallPhaseResolver.build_policy(vm)

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
            "-cpu", "Skylake-Client,-hle,-rtm,kvm=on,vendor=GenuineIntel,+invtsc,vmware-cpuid-freq=on,+ssse3,+sse4.2,+popcnt,+avx,+avx2,+aes,+xsave,+xsaveopt,check",
            "-machine", "q35,accel=kvm",
            "-global", "ICH9-LPC.disable_s3=1",
            "-global", "ICH9-LPC.disable_s4=1",
            "-device", "isa-applesmc,osk=ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc",
            "-smbios", "type=2",
            "-drive", f"if=pflash,format=raw,readonly=on,file={ovmf_code}",
        ]

        # Configura caminhos das ROMs e BIOS do QEMU (-L) para localizar kvmvapic.bin e vgabios
        qemu_pc_bios_dirs = [
            str(Path(qemu_bin).parent.parent / "pc-bios"),
            "/opt/reims-vgpu/vendor/qemu/pc-bios",
            "/home/felipeab10/reims-vgpu/vendor/qemu/pc-bios",
            "/usr/share/qemu",
            "/usr/share/seabios",
        ]
        for bdir in qemu_pc_bios_dirs:
            if os.path.isdir(bdir):
                cmd.extend(["-L", bdir])
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
            "-device", "qemu-xhci,id=xhci",
            "-device", "usb-kbd,bus=xhci.0",
            "-device", "usb-tablet,bus=xhci.0",
            "-device", "usb-ehci,id=ehci",
            "-device", "ich9-intel-hda",
            "-device", "hda-output",
        ])

        # Suporte a Passthrough VFIO direto para GPUs AMD compatíveis nativamente com macOS
        if vm.gpu_mode == "vfio-passthrough" and vm.selected_gpu:
            clean_pci = vm.selected_gpu.replace("0000:", "")
            cmd.extend(["-device", f"vfio-pci,host={clean_pci},multifunction=on"])
        else:
            cmd.extend(["-vga", "std"])

        # Adiciona socket QMP para sincronização de energia (PowerSync) e controle de reboot
        sock_path = "/tmp/vhackintosh-qmp.sock"
        if os.path.exists(sock_path):
            try:
                os.unlink(sock_path)
            except Exception:
                pass

        cmd.extend([
            "-qmp", f"unix:{sock_path},server=on,wait=off",
        ])

        # A flag -no-reboot força o QEMU a encerrar quando o guest pede reboot,
        # permitindo ao host acompanhar. Isso SÓ é seguro quando o macOS já está
        # instalado: durante a instalação os reboots da Apple precisam ser
        # absorvidos internamente pelo QEMU.
        if policy.enable_host_power_sync:
            cmd.append("-no-reboot")

        # Oculta menus GTK (Machine, View), ativa cursor visível, captura de mouse ao passar o cursor e tela cheia
        display_opts = "gtk,show-menubar=off,zoom-to-fit=on,show-cursor=on,grab-on-hover=on"
        is_tty = not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
        if getattr(vm, "fullscreen", True) or is_tty:
            cmd.extend(["-display", display_opts, "-full-screen"])
        else:
            cmd.extend(["-display", display_opts])

        # Em console TTY puro (appliance) não existe servidor gráfico e a VM
        # precisa de um. Usamos a sessão Wayland mínima (sway em modo kiosk), que
        # substituiu a antiga sessão X11 + Openbox iniciada por xinit.
        if is_tty:
            waysession_candidates = [
                "/usr/local/bin/vhackintosh-waysession",
                str(Path(__file__).resolve().parent.parent / "appliance" / "archiso" / "airootfs" / "usr" / "local" / "bin" / "vhackintosh-waysession"),
            ]
            waysession_bin = None
            for cand in waysession_candidates:
                if os.path.exists(cand) and os.access(cand, os.X_OK):
                    waysession_bin = cand
                    break

            if waysession_bin:
                cmd = [waysession_bin] + cmd
            else:
                console.print(
                    "[bold yellow]Aviso: vhackintosh-waysession não encontrado e não há "
                    "sessão gráfica ativa. A VM não conseguirá abrir a janela.[/bold yellow]"
                )

        if env_vars is None:
            env_vars = os.environ.copy()
            self._apply_gpu_environment(vm, env_vars)

        from core.power import QMPPowerMonitor
        monitor = QMPPowerMonitor(sock_path)
        monitor.start()

        try:
            subprocess.run(cmd, env=env_vars)
        finally:
            monitor.stop()

        # Contabiliza reboots e aplica a política de energia do host.
        self._apply_host_power_policy(vm, policy, monitor)

    def _menu_build_iso(self) -> None:
        console.clear()
        console.print(render_banner())
        console.print("[bold cyan]Gerador de Imagem ISO Bootável (vHackintosh Appliance Live USB)[/bold cyan]\n")
        console.print("O Appliance vHackintosh é um sistema operacional Linux dedicado e minimalista,")
        console.print("projetado especificamente para rodar máquinas virtuais macOS com aceleração gráfica.\n")
        console.print("[bold green]Recursos integrados na ISO:[/bold green]")
        console.print("  • Kernel Linux com otimizações KVM para macOS (MSRs ignorados, nested virtualization)")
        console.print("  • Pilha gráfica Vulkan + Wayland nativo (sway em modo kiosk, sem X11)")
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
        console.print(" [bold green]1[/bold green] - Instruções para compilar a ISO (com tag do GitHub ou binários locais)")
        console.print(" [bold green]2[/bold green] - Instalar / Atualizar Harnesses CLI neste sistema host agora")
        console.print(" [bold yellow]0[/bold yellow] - Voltar ao Menu Principal")

        choice = Prompt.ask("\nEscolha uma opção", choices=["1", "2", "0"], default="0")
        if choice == "1":
            console.print("\n[bold cyan]Opções de Compilação da Imagem ISO:[/bold cyan]\n")
            console.print("  [bold green]• Opção A (Recomendada): Usando Release Oficial do GitHub:[/bold green]")
            console.print(f"    [bold yellow]sudo bash {builder_script} --tag v1.0.0[/bold yellow]")
            console.print("    [dim]Baixa os binários pré-compilados do GitHub Releases sem precisar compilar nada.[/dim]\n")
            console.print("  [bold green]• Opção B: Usando a versão 'latest' mais recente do GitHub:[/bold green]")
            console.print(f"    [bold yellow]sudo bash {builder_script} --tag latest[/bold yellow]\n")
            console.print("  [bold green]• Opção C: Usando Binários Locais do Computador:[/bold green]")
            console.print(f"    [bold yellow]sudo bash {builder_script}[/bold yellow]\n")
            Prompt.ask("Pressione Enter para continuar")
        elif choice == "2":
            setup_script = Path(__file__).resolve().parent.parent / "appliance" / "archiso" / "airootfs" / "usr" / "local" / "bin" / "setup-harness-tools.sh"
            if setup_script.exists():
                subprocess.run(["bash", str(setup_script)])
            Prompt.ask("\nPressione Enter para continuar")
