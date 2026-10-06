"""
vHackintosh — Modern Terminal User Interface (TUI)
Interface rica em terminal construída com Python Rich para gestão de VMs macOS.
"""

from __future__ import annotations
import os
import sys
import time
import uuid
import datetime
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.layout import Layout
from rich.text import Text

from core.config import VMConfig, VMManagerStore, ExclusiveVMLock, CONFIG_DIR
from core.reims_build import (
    evaluate as evaluate_reims_build,
    build as build_reims_vgpu,
    ensure_executable_bits,
    QEMU_BIN_REL as REIMS_QEMU_BIN_REL,
)
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


def decide_rail(
    reims_dir: Optional[str],
    macos_version: str,
    custom_dir: Optional[Path],
    *,
    create_rail_dir: bool = True,
) -> tuple[bool, str]:
    """
    Decide se a VM pode subir pelo rail do reims-vgpu (o único motor), ou se a
    recusa de iniciar a VM — não há mais um motor de reserva para onde cair.

    Devolve `(usar_rail, motivo)` — o `motivo` existe porque um "não" silencioso
    é indistinguível de um defeito, e esta decisão já custou o salvamento de
    aceleração do appliance uma vez: a ISO não leva `vm/disks/`, e a ausência da
    quebra do rail passava sem ninguém perceber.

    Três condições, cada uma legítima:
      1. `reims_dir` com um `vm/boot-x86.sh` executável — sem ele não há harness;
      2. `vm/disks/rails/<macos_version>` no disco — o `boot-x86.sh` recusa um
         rail desconhecido de propósito (um nome errado criaria um rail novo e o
         boot viraria outra linha de guest), então quem pede é o dono da árvore;
      3. o layout que o `--persistent` valida (macos.qcow2 + OpenCore.qcow2 +
         OVMF_VARS.fd, vindos de `custom_dir`) — fora isso o `boot-x86.sh` aborta,
         só que bem mais tarde.

    `create_rail_dir=False` torna a função pura para quem só quer saber a
    decisão (ex.: testes), suprimindo a única escrita em disco.
    """
    if not reims_dir:
        return False, "sem diretório do reims-vgpu"
    if not os.path.exists(os.path.join(reims_dir, "vm", "boot-x86.sh")):
        return False, f"sem vm/boot-x86.sh em {reims_dir}"

    rail_dir = Path(reims_dir) / "vm" / "disks" / "rails" / macos_version
    if create_rail_dir:
        try:
            rail_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"não foi possível criar {rail_dir}: {exc}"
    if not rail_dir.exists():
        return False, f"rail {macos_version} inexistente em {rail_dir}"

    if not (
        custom_dir
        and (custom_dir / "macos.qcow2").exists()
        and (custom_dir / "OpenCore.qcow2").exists()
        and (custom_dir / "OVMF_VARS.fd").exists()
    ):
        return False, f"layout persistente incompleto em {custom_dir}"

    return True, f"rail {macos_version}"


def run_rail_session(
    launch: Callable[[], object],
    make_monitor: Callable[[], object],
    is_installing: bool,
    *,
    max_relaunches: int = 30,
    settle_sec: float = 0.6,
    on_relaunch: Optional[Callable[[int], None]] = None,
) -> tuple[int, Optional[str]]:
    """
    Roda o rail do reims-vgpu, relançando a VM a cada reboot do convidado.

    O QEMU roda com `-action reboot=shutdown` (QEMU_REBOOT_ACTION=exit), então um
    reboot do macOS **encerra** o QEMU em vez de reiniciá-lo. Isso é deliberado —
    o DeviceReset congelaria o host — mas sem relançar aqui a instalação morreria
    no primeiro reboot do instalador. Este é o "reiniciar" que falta.

    `launch()` deve iniciar a VM e devolver algo com `.wait()`; `make_monitor()`
    deve devolver um monitor QMP novo, ainda não iniciado. **Um monitor por
    boot** não é detalhe de estilo: o `_monitor_loop` fecha a própria thread
    quando o socket QMP cai junto com o QEMU, então um monitor reaproveitado
    contaria o primeiro reboot e ficaria mudo nos demais. Como cada monitor
    enxerga só a própria sessão, "este boot pediu reboot?" é simplesmente
    `monitor.reset_count > 0`.

    Devolve `(total_de_reboots, ultimo_motivo)` — somados entre todos os boots,
    porque quem chama decide a política de energia do host pela sessão inteira.

    Um boot que termina **sem** reboot conta como o usuário fechando a janela (ou
    o instalador desligando a máquina): o laço para em vez de relançar.
    """
    total_resets = 0
    last_reason: Optional[str] = None
    relaunches = 0

    while True:
        monitor = make_monitor()
        monitor.start()
        try:
            launch().wait()
        finally:
            # O QEMU entrega o evento QMP antes de sair; sem esta pausa a thread
            # do monitor pode não tê-lo lido ainda e o reboot passa despercebido.
            time.sleep(settle_sec)
            monitor.stop()

        total_resets += monitor.reset_count
        if monitor.last_reason:
            last_reason = monitor.last_reason

        if not is_installing or monitor.reset_count == 0:
            break

        relaunches += 1
        if relaunches > max_relaunches:
            if on_relaunch:
                on_relaunch(-1)
            break
        if on_relaunch:
            on_relaunch(relaunches)

    return total_resets, last_reason


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

            # Checagem de prontidão do sistema (Virtualização BIOS, Vulkan e Internet)
            readiness = SystemReadinessChecker.check_all()
            if not readiness.can_run_vms:
                for check in readiness.checks:
                    if not check.passed and check.is_critical:
                        if check.category == "Virtualização":
                            console.print(f"[bold red]⚠ ATENÇÃO: {check.name} ({check.status_text})[/bold red]")
                            console.print(f"[yellow]{check.details}[/yellow]")
                            if check.remediation:
                                console.print(f"[dim]{check.remediation}[/dim]\n")
                        elif check.category == "Gráficos":
                            console.print(f"[bold red]⚠ ATENÇÃO: Hardware Gráfico Incompatível com o Reims vGPU ({check.status_text})[/bold red]")
                            console.print(f"[yellow]{check.details}[/yellow]")
                            if check.remediation:
                                console.print(f"[dim]{check.remediation}[/dim]\n")
            if not readiness.has_internet:
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
            if not readiness.can_run_vms:
                console.print(" [dim]2[/dim] - [dim]Criar Nova VM macOS (Bloqueado: Hardware Incompatível)[/dim]")
            else:
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

        # ------------------------------------------------------------------
        # Validação antecipada de hardware (KVM e Vulkan 1.2+):
        # Impede o usuário de gastar tempo digitando nome, configurando CPU/RAM
        # e provisionando disco virtual caso o hardware não suporte.
        # ------------------------------------------------------------------
        readiness = SystemReadinessChecker.check_all()
        if not readiness.can_run_vms:
            crit_msgs = []
            for c in readiness.checks:
                if not c.passed and c.is_critical:
                    crit_msgs.append(f"• [bold white]{c.name}:[/bold white] {c.details}")
                    if c.remediation:
                        crit_msgs.append(f"  [dim]Solução: {c.remediation}[/dim]")

            erros_str = "\n".join(crit_msgs)

            console.print(Panel(
                f"[bold red]Criação Bloqueada: Requisitos de Hardware Não Atendidos[/bold red]\n\n"
                f"[bold]Problemas detectados:[/bold]\n{erros_str}\n\n"
                "O vHackintosh utiliza exclusivamente o Reims vGPU (Vulkan 1.2+ em GPU física)\n"
                "e aceleração KVM por hardware. Sem esses recursos, máquinas virtuais macOS não\n"
                "podem ser executadas.\n\n"
                "[bold yellow]Ambiente incompatível típico:[/bold yellow]\n"
                "• Você está testando o appliance dentro de uma máquina virtual emulada\n"
                "  (ex: QEMU nested com -vga std) sem GPU Passthrough;\n"
                "• A placa gráfica do computador é antiga ou não suporta Vulkan 1.2+;\n"
                "• A virtualização VT-x / AMD-V está desativada no setup da BIOS.\n\n"
                "[dim]Para utilizar o vHackintosh com aceleração gráfica, instale-o diretamente no disco (bare-metal)\n"
                "de um computador compatível com GPU Intel (Gen9+), NVIDIA (GTX 900+) ou AMD.[/dim]",
                title="Hardware Incompatível",
                border_style="red",
            ))
            if sys.stdin.isatty():
                Prompt.ask("Pressione Enter para retornar ao menu")
            return

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

            # ------------------------------------------------------------------
            # Verificação prévia de hardware (Vulkan 1.2+):
            # O Reims vGPU é estritamente obrigatório e exige aceleração física Vulkan.
            # Se o host estiver em ambiente emulado (ex: QEMU aninhado sem passthrough)
            # ou em GPU legada sem Vulkan, interrompemos IMEDIATAMENTE antes de pedir
            # compilação ou gastar tempo do usuário.
            # ------------------------------------------------------------------
            vulkan_ok, vulkan_msg = GPUChecker.check_vulkan_host_readiness()
            if not vulkan_ok:
                console.print(Panel(
                    f"[bold red]ERRO: Hardware Gráfico Incompatível com o Reims vGPU[/bold red]\n\n"
                    f"[bold]Diagnóstico:[/bold] {vulkan_msg}\n\n"
                    "O Reims vGPU é o motor gráfico obrigatório do vHackintosh e exige\n"
                    "suporte direto à API Vulkan 1.2+ com pelo menos uma GPU física.\n\n"
                    "[bold yellow]Possíveis causas:[/bold yellow]\n"
                    "• Você está testando o appliance dentro de uma máquina virtual emulada\n"
                    "  (ex: QEMU nested com -vga std) sem GPU PCI Passthrough.\n"
                    "• A GPU física da máquina é antiga e não suporta Vulkan 1.2+.\n"
                    "• Os drivers Vulkan do host não foram carregados corretamente.\n\n"
                    "[dim]Para executar o macOS com aceleração, instale o vHackintosh diretamente\n"
                    "no hardware físico (bare-metal) de um computador compatível.[/dim]",
                    border_style="red",
                ))
                if sys.stdin.isatty():
                    Prompt.ask("Pressione Enter para retornar")
                return

            # Compila o reims-vgpu quando a árvore trouxer fontes e o binário
            # estiver ausente ou defasado. É aqui que um patch novo deixa de
            # ficar invisível para a VM.
            if reims_dir and not self._ensure_reims_built(reims_dir):
                console.print("[bold red]Sem um binário do QEMU não é possível iniciar a VM.[/bold red]")
                Prompt.ask("Pressione Enter para continuar")
                return

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
            #   2. como um reboot do convidado é tratado (QEMU_REBOOT_ACTION=exit);
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

            # O rail É o caminho: existe exatamente um motor de vídeo, e é este.
            # O antigo motor nativo (`-vga std`, emulado) foi removido, não
            # guardado — a única coisa que ele fazia era ser para onde a VM ia
            # quando faltava um diretório na ISO, e foi por isso que o appliance
            # ficou rodando sem aceleração sem ninguém perceber.
            #
            # Logo, `use_reims_rail=False` não significa "degrada para outro
            # motor", significa recusa de iniciar: imprime-se o motivo e para tudo.
            use_reims_rail, rail_reason = decide_rail(
                reims_dir, vm.macos_version, custom_dir
            )
            if not use_reims_rail:
                console.print(Panel(
                    f"[bold red]A VM não foi iniciada: o reims-vgpu está indisponível[/bold red]\n\n"
                    f"[bold]Motivo:[/bold] {rail_reason}\n\n"
                    "O reims-vgpu é o único motor de vídeo do vHackintosh. Sem ele o "
                    "convidado\nnão seria acelerado, e este projeto não aceita mais "
                    "fundo de tela lento\ncomo saída silenciosa.\n\n"
                    "[dim]Corrija o motivo acima e tente iniciar de novo.[/dim]",
                    border_style="red",
                ))
                if sys.stdin.isatty():
                    Prompt.ask("Pressione Enter para continuar")
                return
            boot_script = os.path.join(reims_dir, "vm", "boot-x86.sh")

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
            #
            # NUNCA usamos "reset". A opção `reset` do boot-x86.sh não emite flag
            # alguma, então o QEMU fica com o próprio default — e o default é
            # `RebootAction reboot_action = REBOOT_ACTION_RESET`
            # (vendor/qemu/system/runstate-action.c). Esse caminho faz um
            # DeviceReset em todos os dispositivos, que no reims-vgpu passa por
            # `reims_vgpu_pci_reset` → `engine::reset_guest_state()` →
            # `queue_wait_idle()`, e este último roda no main loop do QEMU com o
            # BQL preso: um `vkQueueWaitIdle` que não retorna congela o host
            # inteiro (o compositor desenha pela mesma GPU). O timeout de 10 s que
            # existe hoje limita o estrago, mas deixa o motor degradado.
            #
            # Com "exit" o QEMU encerra e nós relançamos a VM (ver o laço abaixo),
            # de modo que o reboot da instalação nunca chega a um DeviceReset — o
            # processo novo tem um motor Vulkan limpo.
            env_vars["QEMU_REBOOT_ACTION"] = "exit"

            # Aplica diretivas de aceleração gráfica para a GPU vinculada
            self._apply_gpu_environment(vm, env_vars)

            if installer_img:
                env_vars["INSTALL_MEDIA"] = installer_img

            # A janela que apresenta o frame é a do reims-vgpu (REIMS_VGPU_WINDOW
            # =1, acima). O QEMU não pode abrir outra por cima: com a variável
            # ausente o boot-x86.sh já resolve para `none`, e fixá-la aqui
            # torna isso independente de qualquer REIMS_VGPU_DISPLAY herdada
            # do shell de quem chamou.
            env_vars["REIMS_VGPU_DISPLAY"] = "none"

            if custom_dir:
                env_vars["PERSISTENT_DIR"] = str(custom_dir)
                env_vars["DISK_MASTER"] = str(vm.disk_path)
                env_vars["OPENCORE_MASTER"] = str(custom_dir / "OpenCore.qcow2")

            # Fixa o RUN_DIR do rail para sabermos exatamente onde o socket QMP
            # é publicado (`$RUN_DIR/qmp.path`), permitindo monitorar os eventos
            # de energia do guest também pelo caminho rail.
            rail_run_dir = os.path.join(reims_dir, "vm", "disks", "run")
            env_vars["RUN_DIR"] = rail_run_dir

            rail_cmd = [
                "bash", boot_script,
                "--rail", vm.macos_version,
                "--persistent",
                "--device", "reims-vgpu-pci",
            ]

            # A janela do reims-vgpu é winit falando Wayland direto: sem uma
            # conexão de display ela sobe e não há nada consumindo — o próprio
            # boot-x86.sh avisa disso e trata qualquer número de presente como
            # inválido. No appliance a TUI está num TTY, então é preciso envolver
            # o rail numa sessão kiosk. Quando já existe sessão (ex.: rodando a
            # partir do compositor do host), não mexemos em nada.
            if not (env_vars.get("DISPLAY") or env_vars.get("WAYLAND_DISPLAY")):
                waysession_bin = self._find_waysession()
                if waysession_bin:
                    rail_cmd = [waysession_bin] + rail_cmd
                else:
                    console.print(self._WAYSESSION_MISSING)

            from core.power import QMPPowerMonitor
            from types import SimpleNamespace

            rail_sock = os.path.join(rail_run_dir, "qmp.sock")
            rail_sock_pathfile = os.path.join(rail_run_dir, "qmp.path")

            def _new_rail_monitor():
                # Um por boot — ver o docstring de `run_rail_session`.
                return QMPPowerMonitor(
                    sock_path=rail_sock, path_file=rail_sock_pathfile
                )

            def _relaunch_notice(step: int) -> None:
                if step < 0:
                    console.print(
                        "[yellow]Muitos reboots seguidos; parando de relançar "
                        "a VM automaticamente.[/yellow]"
                    )
                else:
                    console.print(
                        f"[cyan]Instalador reiniciou a VM — relançando "
                        f"({step})…[/cyan]"
                    )

            # QEMU_REBOOT_ACTION=exit (definido acima) faz um reboot do
            # convidado encerrar o QEMU. O laço é quem o recoloca: sem ele a
            # instalação morreria no primeiro reboot do instalador.
            total_resets, last_reason = run_rail_session(
                launch=lambda: subprocess.Popen(rail_cmd, env=env_vars),
                make_monitor=_new_rail_monitor,
                is_installing=policy.is_installing,
                on_relaunch=_relaunch_notice,
            )

            # `_apply_host_power_policy` lê `reset_count`/`last_reason` de um
            # único objeto; entregue a sessão inteira, não o último boot.
            self._apply_host_power_policy(
                vm,
                policy,
                SimpleNamespace(
                    reset_count=total_resets,
                    last_reason=last_reason,
                    shutdown_count=0,
                ),
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
        energia do host.

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

        # `REIMS_VGPU_DISPLAY` não é gravada aqui, de propósito. Quem lê essa
        # variável é só o boot-x86.sh, e ele já tem por default exatamente o
        # valor que este lugar costumava escrever (gtk,show-menubar=off,
        # zoom-to-fit=on) — gravá-lo aqui tinha um único efeito real: impedir o
        # `${REIMS_VGPU_DISPLAY:-none}` do caso em que a janela do reims-vgpu
        # apresenta o frame. O resultado era `-display gtk` E a janela winit na
        # mesma boot.

    # A janela do reims-vgpu (winit) precisa de uma conexão de display para
    # abrir. No appliance a TUI sobe pelo profile.d numa sessão TTY (autologin
    # em tty1, sem DISPLAY nem WAYLAND_DISPLAY), então a VM é envolvida por uma
    # sessão Wayland mínima (sway em kiosk) criada pelo `vhackintosh-waysession`.
    _WAYSESSION_CANDIDATES = (
        "/usr/local/bin/vhackintosh-waysession",
        str(
            Path(__file__).resolve().parent.parent
            / "appliance" / "archiso" / "airootfs" / "usr" / "local" / "bin"
            / "vhackintosh-waysession"
        ),
    )
    _WAYSESSION_MISSING = (
        "[bold yellow]Aviso: vhackintosh-waysession não encontrado e não há "
        "sessão gráfica ativa. A VM não conseguirá abrir a janela.[/bold yellow]"
    )

    @classmethod
    def _find_waysession(cls) -> Optional[str]:
        for cand in cls._WAYSESSION_CANDIDATES:
            if os.path.exists(cand) and os.access(cand, os.X_OK):
                return cand
        return None

    def _ensure_reims_built(self, reims_dir: Optional[str]) -> bool:
        """Garante que o binário do reims-vgpu corresponda ao fonte.

        A ISO leva as **fontes** do reims-vgpu, então aqui é onde o appliance as
        compila. Sem isso o QEMU em uso poderia ser um binário antigo sem que
        nada avisasse — foi exatamente o que aconteceu com o patch do
        `wait_idle`, que ficou em `vendor/qemu/build/` enquanto o teste usava
        `/usr/bin/qemu-system-x86_64`.

        Devolve True se pode seguir (nada a fazer ou build concluído). Devolve
        False **apenas** quando não há binário algum para lançar a VM — recusar
        compilar com um binário existente continua sendo um caminho válido.
        """
        state = evaluate_reims_build(explicit=reims_dir)
        if state.reims_dir is None:
            return False

        def binary_present() -> bool:
            return (state.reims_dir / REIMS_QEMU_BIN_REL).exists()

        # Os bits de execução são restaurados aqui, e não só dentro de `build()`:
        # o `vm/boot-x86.sh` exige `[ -x ]` em
        # `crates/reims-vgpu-efi/scripts/.../reims-vgpu-efi-rom.sh` e morre com
        # "EFI ROM builder not executable" — inclusive no caminho em que o build
        # é pulado (stamp atualizado) ou recusado. Custo medido: 0,15 s, e 0 na
        # segunda vez, porque só toca no que está errado.
        try:
            ensure_executable_bits(state.reims_dir)
        except Exception:  # noqa: BLE001 — melhor tentar lançar do que abortar
            pass

        if not state.sources_present:
            # Árvore só com binário (ISO de release): nada a compilar.
            return binary_present()

        if not state.needs_build:
            return True

        console.print()
        console.print(Panel(
            f"[bold yellow]O reims-vgpu precisa ser compilado[/bold yellow]\n"
            f"[dim]Motivo: {state.reason}[/dim]\n\n"
            f"A primeira compilação leva cerca de 5 minutos e roda uma única vez.\n"
            f"Depois disso ela só repete quando a fonte do repositório do reims-vgpu atualizar.",
            border_style="yellow",
        ))
        if not Confirm.ask("Compilar agora?", default=True):
            if binary_present():
                console.print(
                    "[yellow]Compilação recusada — a VM usará o binário que já existe "
                    "(pode estar desatualizado em relação ao fonte).[/yellow]\n"
                )
                return True
            console.print(
                "[bold red]Compilação recusada e não há binário algum: a VM não pode "
                "ser lançada.[/bold red]"
            )
            Prompt.ask("Pressione Enter para continuar")
            return False

        from rich.progress import (
            Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn,
        )

        result = {"ok": False}
        with Progress(
            SpinnerColumn(spinner_name="dots"),
            TextColumn("[bold cyan]{task.description}[/bold cyan]"),
            BarColumn(bar_width=36),
            TextColumn("[bold green]{task.percentage:>3.0f}%[/bold green]"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Preparando...", total=100)
            current = {"name": ""}

            def on_start(name: str, _expected: float) -> None:
                current["name"] = name
                progress.update(task, description=f"Compilando {name}...")

            def on_progress(pct: float) -> None:
                progress.update(task, completed=pct)

            def on_done(name: str, ok: bool) -> None:
                if not ok:
                    progress.update(
                        task,
                        description=f"[bold red]Falha ao compilar {name}[/bold red]",
                    )

            try:
                result["ok"] = build_reims_vgpu(
                    state.reims_dir,
                    on_start=on_start,
                    on_done=on_done,
                    progress=on_progress,
                )
            except Exception as exc:  # noqa: BLE001 — a TUI não pode morrer aqui
                progress.update(task, description=f"[bold red]Erro: {exc}[/bold red]")

            if result["ok"]:
                progress.update(task, completed=100.0, description="[bold green]Concluído[/bold green]")

        if not result["ok"]:
            from core.reims_build import tail_log
            console.print("[bold red]A compilação do reims-vgpu falhou.[/bold red]")
            for log_name in ("build-qemu-reims.log", "build-rom.log"):
                snippet = tail_log(log_name, lines=12)
                if snippet:
                    console.print(f"[dim]--- {log_name} ---[/dim]")
                    console.print(snippet)
            console.print("[dim]Logs completos em ~/.config/vhackintosh/logs/[/dim]")
            Prompt.ask("Pressione Enter para continuar")
            # O configure do QEMU apaga o build/ antes de reconfigurar, então uma
            # falha pode ter levado o binário anterior junto. Se não sobrou
            # nenhum, não há o que lançar — e dizer o contrário seria mentira.
            if not binary_present():
                console.print(
                    "[bold red]O binário anterior também foi perdido na tentativa de "
                    "build — a VM não pode ser lançada até a compilação funcionar.[/bold red]"
                )
                return False
            console.print("[yellow]Seguindo com o binário anterior.[/yellow]\n")
            return True

        console.print("[bold green]reims-vgpu compilado com sucesso.[/bold green]\n")
        return True

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
            console.print(f"    [bold yellow]sudo bash {builder_script} --tag v1.0.1[/bold yellow]")
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
