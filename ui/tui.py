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
from core import i18n
from core.reims_build import (
    evaluate as evaluate_reims_build,
    build as build_reims_vgpu,
    ensure_executable_bits,
)
from core import reims_ctl
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


def decide_reimsctl(
    reims_dir: Optional[str],
    macos_version: str,
    custom_dir: Optional[Path],
    *,
    create_rail_dir: bool = True,
) -> tuple[bool, str]:
    """
    Decide se a VM pode subir pelo reims-ctl (o único motor), ou a recusa de
    iniciar a VM — não há mais um motor de reserva para onde cair.

    Devolve `(pode_subir, motivo)` — o `motivo` existe porque um "não"
    silencioso é indistinguível de um defeito, e esta decisão já custou o
    salvamento de aceleração do appliance uma vez: a ausência de algo
    essencial passava sem ninguém perceber.

    Três condições, cada uma legítima:
      1. o `reims-ctl` instalado (install-ctl.sh) — sem ele não há quem suba
         a VM nem registre o QMP;
      2. o QEMU do reims-vgpu compilado (`qemu/current`) — sem o motor
         acelerado o convidado não sobe (o appliance não tem motor de reserva);
      3. o layout da VM (macos.qcow2 + OpenCore.qcow2 + OVMF_VARS.fd, vindos
         de `custom_dir`) mais o OVMF_CODE.fd, que é copiado do template aqui.

    `create_rail_dir` permanece só para compatibilidade de assinatura (o
    antigo rail criava um diretório; agora nada é escrito aqui).
    """
    del create_rail_dir  # nenhum efeito colateral nesta decisão
    if not reims_dir:
        return False, "sem diretório do reims-vgpu"
    if not reims_ctl.reims_ctl_bin():
        return False, "reims-ctl não instalado (compile o reims-vgpu: scripts/install-ctl.sh)"
    from core.reims_build import qemu_binary

    if not qemu_binary().exists():
        return False, f"QEMU do reims-vgpu não compilado (esperado em {qemu_binary()})"

    if not (
        custom_dir
        and (custom_dir / "macos.qcow2").exists()
        and (custom_dir / "OpenCore.qcow2").exists()
        and (custom_dir / "OVMF_VARS.fd").exists()
    ):
        return False, f"layout persistente incompleto em {custom_dir}"

    # O reims-ctl exige o código do OVMF ao lado das variáveis, no diretório da
    # VM. VMs provisionadas antes desta integração só têm o par de variáveis;
    # completamos do template (idempotente).
    if not (custom_dir / "OVMF_CODE.fd").exists():
        if not DiskProvisioner.ensure_ovmf_code(custom_dir):
            return False, f"OVMF_CODE.fd ausente e sem template para copiar (em {custom_dir})"

    return True, f"reims-ctl ({macos_version})"


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

    O QEMU roda com `-action reboot=shutdown` (extra_args do vm.toml), então um
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
        # Primeira tela do appliance (specs/language-selection): enquanto o
        # idioma não estiver escolhido, nada mais aparece — nem o banner com
        # opções, nem a prontidão.
        self._language_screen()
        self._backfill_install_phases()
        while True:
            console.clear()
            console.print(render_banner())

            running_vm = self.lock.get_running_vm_info()
            if running_vm:
                console.print(i18n.t("tui.main.running_vm", vm=running_vm))

            if self.system_mode == "live":
                console.print(i18n.t("tui.main.live_media"))

            # Checagem de prontidão do sistema (Virtualização BIOS, Vulkan e Internet)
            readiness = SystemReadinessChecker.check_all()
            if not readiness.can_run_vms:
                for check in readiness.checks:
                    if not check.passed and check.is_critical:
                        if check.category == "Virtualização":
                            console.print(i18n.t("tui.main.attention", name=check.name, status=check.status_text))
                            console.print(f"[yellow]{check.details}[/yellow]")
                            if check.remediation:
                                console.print(f"[dim]{check.remediation}[/dim]\n")
                        elif check.category == "Gráficos":
                            console.print(i18n.t("tui.main.attention_gpu", status=check.status_text))
                            console.print(f"[yellow]{check.details}[/yellow]")
                            if check.remediation:
                                console.print(f"[dim]{check.remediation}[/dim]\n")
            if not readiness.has_internet:
                console.print(i18n.t("tui.main.no_internet"))

            vms = self.store.list_vms()
            self._render_vm_table(vms)

            console.print(i18n.t("tui.menu.title"))

            # O instalador bare-metal permanece sempre acessível quando presente.
            # Ele é copiado para o sistema definitivo por design, portanto a sua
            # existência NÃO indica Live USB (ver core.mode.detect_system_mode).
            installer_available = self.installer_available
            if installer_available:
                if not readiness.can_run_vms:
                    console.print(i18n.t("tui.menu.install_locked"))
                else:
                    console.print(i18n.t("tui.menu.install"))

            console.print(i18n.t("tui.menu.start_vm"))
            if not readiness.can_run_vms:
                console.print(i18n.t("tui.menu.create_vm_locked"))
            else:
                console.print(i18n.t("tui.menu.create_vm"))
            console.print(i18n.t("tui.menu.diagnostics"))
            console.print(i18n.t("tui.menu.update_upstream"))
            console.print(i18n.t("tui.menu.download_images"))
            console.print(i18n.t("tui.menu.ab_test"))
            console.print(i18n.t("tui.menu.build_iso"))
            console.print(i18n.t("tui.menu.exit"))

            choices = ["1", "2", "3", "4", "5", "6", "7", "0"]
            if installer_available:
                choices.extend(["I", "i", "8"])

            choice = Prompt.ask(i18n.t("tui.menu.prompt_choice"), choices=choices, default="1")

            if choice in ("I", "i", "8"):
                if not readiness.can_run_vms:
                    console.print(Panel(
                        i18n.t("tui.install_blocked.panel"),
                        title=i18n.t("tui.install_blocked.title"),
                        border_style="red"
                    ))
                    if sys.stdin.isatty():
                        Prompt.ask(i18n.t("tui.prompt.enter_continue"))
                elif shutil.which("vhackintosh-install"):
                    cmd = ["sudo", "vhackintosh-install"] if os.geteuid() != 0 else ["vhackintosh-install"]
                    subprocess.run(cmd)
                else:
                    console.print(i18n.t("tui.install_cli.missing"))
                    Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
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
                console.print(i18n.t("tui.menu.goodbye"))
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

        console.print(i18n.t("tui.backfill.checking"))
        for vm in unverified:
            probe = InstallProbe.probe(vm.disk_path) if vm.disk_path else DiskProbe()
            vm.install_evidence = classify_evidence(probe).value
            vm.install_phase = InstallPhaseResolver.resolve_phase(vm, probe=probe).value
            self.store.update_vm(vm)
            console.print(f"  [dim]{vm.name}: {summarize_probe(probe)} → {vm.install_phase}[/dim]")
        console.print()

    def _language_screen(self) -> None:
        """Tela de idioma: a primeira do appliance, antes de qualquer outra.

        Spec language-selection: só aparece enquanto o settings não tem
        `language` (instalação nova); depois disso vale a persistência e a
        troca fica por `vhackintosh-lang pt|en`. Idioma inválido não é
        possível aqui (as opções são exatamente os suportados).
        """
        if i18n.has_language_choice():
            return
        console.clear()
        console.print(render_banner())
        console.print(Panel(
            i18n.t("lang.subtitle"),
            title=f"[bold cyan]{i18n.t('lang.title')}[/bold cyan]",
            border_style="cyan",
        ))
        options = list(i18n.LANGUAGES)
        for idx, code in enumerate(options, 1):
            console.print(f" [bold green]{idx}[/bold green] - {i18n.LANGUAGES[code]}")
        choice = Prompt.ask(
            "\n" + i18n.t("lang.choice"),
            choices=[str(i) for i in range(1, len(options) + 1)],
            default="1",
        )
        i18n.set_language(options[int(choice) - 1])
        console.print(f"[green]{i18n.t('lang.set', language=i18n.LANGUAGES[i18n.get_language()])}[/green]")
        time.sleep(1.0)

    def _menu_usb_devices(self, vm: VMConfig) -> None:
        """Hotplug de USB do host na VM viva (specs/host-device-passthrough).

        QMP `device_add usb-host`/`device_del` (D3): sem relançar a VM e sem
        mexer no argv — que é do reims-ctl. "Anexar sempre" (★) persiste no
        `usb_auto_attach` da VM e o boot seguinte liga o dispositivo sozinho
        (cenário do spec: pen drive plugado antes do boot).
        """
        from core import passthrough

        sock = reims_ctl.qmp_socket(vm)
        if not passthrough.probe_qmp(sock):
            console.print(
                i18n.t("tui.usb.not_running")
            )
            console.print(
                i18n.t("tui.usb.auto_hint")
            )
            Prompt.ask(i18n.t("tui.prompt.enter_continue"))
            return

        auto_ids = set(getattr(vm, "usb_auto_attach", []) or [])
        while True:
            console.clear()
            console.print(Panel(
                i18n.t("tui.usb.title", name=vm.name),
                border_style="green",
            ))
            try:
                devices = passthrough.list_usb_devices()
                attached = passthrough.attached_ids(sock)
            except passthrough.QmpError as exc:
                console.print(i18n.t("tui.usb.qmp_failed", error=exc.message))
                Prompt.ask(i18n.t("tui.prompt.enter_continue"))
                return

            if not devices:
                console.print(i18n.t("tui.usb.none"))
            for idx, dev in enumerate(devices, 1):
                mark = "●" if dev.qmp_id in attached else "○"
                star = i18n.t("tui.usb.always") if f"{dev.vendor_id}:{dev.product_id}" in auto_ids else ""
                console.print(
                    f" [bold]{idx}[/bold]{mark} {dev.short_label} "
                    f"[{dev.vendor_id}:{dev.product_id}]{star}"
                )
            # Degradação explícita (spec host-device-passthrough 3.4): o que o
            # host não tem, dizemos — sem impedir o uso do resto do menu.
            try:
                extras = passthrough.list_attachable_extra()
            except Exception:  # noqa: BLE001 — degradação nunca quebra o menu
                extras = {}
            if not extras.get("camera"):
                console.print(i18n.t("tui.usb.no_camera"))
            if not extras.get("wireless"):
                console.print(i18n.t("tui.usb.no_wireless"))

            nums = [str(i) for i in range(1, len(devices) + 1)]
            autos = [f"a{i}" for i in nums]
            sel = Prompt.ask(
                i18n.t("tui.usb.prompt_help"),
                choices=nums + autos + ["n", "0"],
                default="0",
            )
            if sel == "0":
                return
            if sel == "n":
                continue

            auto = sel.startswith("a")
            dev = devices[int(sel.lstrip("a")) - 1]
            key = f"{dev.vendor_id}:{dev.product_id}"
            try:
                if dev.qmp_id in attached:
                    passthrough.detach(sock, dev)
                    console.print(i18n.t("tui.usb.detached", device=dev.short_label))
                else:
                    passthrough.attach(sock, dev)
                    console.print(i18n.t("tui.usb.attached", device=dev.short_label))
                if auto:
                    if key in auto_ids:
                        auto_ids.discard(key)
                    else:
                        auto_ids.add(key)
                    vm.usb_auto_attach = sorted(auto_ids)
                    self.store.update_vm(vm)
            except passthrough.QmpError as exc:
                console.print(i18n.t("tui.usb.hotplug_failed", error=exc.message))
            Prompt.ask(i18n.t("tui.prompt.enter_continue"))

    def _usb_auto_attach(self, vm: VMConfig) -> None:
        """Anexa os USBs marcados 'anexar sempre' logo que a VM sobe.

        Chamado pelo `ReimsCtlSession.on_ready` (o `reims-ctl start` só
        retorna com o QMP respondendo). Melhor-esforço: qualquer falha avisa
        e a VM segue normalmente.
        """
        wanted = getattr(vm, "usb_auto_attach", []) or []
        if not wanted:
            return
        from core import passthrough

        sock = reims_ctl.qmp_socket(vm)
        try:
            devices = {
                f"{d.vendor_id}:{d.product_id}": d
                for d in passthrough.list_usb_devices()
            }
        except Exception:  # noqa: BLE001 — auto-anexo nunca derruba a VM
            return
        for key in wanted:
            dev = devices.get(key)
            if not dev:
                continue
            try:
                passthrough.attach(sock, dev)
                print(i18n.t("tui.usb.auto_attached", device=dev.short_label))
            except Exception as exc:  # noqa: BLE001
                print(i18n.t("tui.usb.auto_failed", device=dev.short_label, error=exc))

    def _render_vm_table(self, vms: list[VMConfig]) -> None:
        table = Table(title=i18n.t("tui.vm.table.title"), border_style="cyan", header_style="bold magenta")
        table.add_column("#", justify="center", style="bold yellow", width=4)
        table.add_column(i18n.t("tui.vm.col.name"), style="bold white")
        table.add_column(i18n.t("tui.vm.col.macos_version"), style="cyan")
        table.add_column("Status", justify="center")
        table.add_column("vCPUs", justify="center")
        table.add_column("RAM", justify="center")
        table.add_column(i18n.t("tui.vm.col.disk"), justify="center")
        table.add_column(i18n.t("tui.vm.col.graphics"), justify="center")
        table.add_column("Auto-Start", justify="center")

        if not vms:
            table.add_row("-", i18n.t("tui.vm.table.empty"), "-", "-", "-", "-", "-", "-", "-")
        else:
            for idx, vm in enumerate(vms, start=1):
                auto_str = i18n.t("tui.vm.table.auto_yes") if vm.auto_start else i18n.t("tui.vm.table.auto_no")
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
        console.print(i18n.t("tui.phase.title"))

        current = normalize_phase(getattr(vm, "install_phase", None))
        probe = InstallProbe.probe(vm.disk_path) if vm.disk_path else DiskProbe()
        vm.install_evidence = classify_evidence(probe).value
        self.store.update_vm(vm)

        console.print(i18n.t("tui.phase.current", phase=phase_label(current)))
        console.print(i18n.t("tui.phase.reboots", count=getattr(vm, "install_reboots", 0)))
        console.print(i18n.t("tui.phase.disk_probe", probe=summarize_probe(probe)))

        if probe.looks_like_complete_install:
            console.print(i18n.t("tui.phase.evidence_installed"))
        elif probe.has_any_system_data:
            console.print(i18n.t("tui.phase.evidence_partial"))
        else:
            console.print(i18n.t("tui.phase.evidence_none"))

        console.print(i18n.t("tui.phase.effect.title"))
        console.print(i18n.t("tui.phase.effect.installed"))
        console.print(i18n.t("tui.phase.effect.installed_note"))
        console.print(i18n.t("tui.phase.effect.installing"))
        console.print(i18n.t("tui.phase.effect.installing_note"))

        if current is InstallPhase.INSTALLED:
            if Confirm.ask(i18n.t("tui.phase.confirm.revert"), default=False):
                vm.install_phase = InstallPhase.INSTALLING.value
                self.store.update_vm(vm)
                console.print(i18n.t("tui.phase.reverted"))
        else:
            if Confirm.ask(i18n.t("tui.phase.confirm.mark_installed"), default=False):
                vm.install_phase = InstallPhase.INSTALLED.value
                self.store.update_vm(vm)
                console.print(i18n.t("tui.phase.marked_installed"))

        Prompt.ask(i18n.t("tui.prompt.enter_continue_nl"))

    def _menu_manage_vms(self, vms: list[VMConfig]) -> None:
        if not vms:
            console.print(i18n.t("tui.vm.manage.none"))
            Prompt.ask(i18n.t("tui.prompt.enter_continue"))
            return

        choices = [str(i) for i in range(1, len(vms) + 1)] + ["b"]
        idx_str = Prompt.ask(i18n.t("tui.vm.manage.prompt"), choices=choices)
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
            details_text.append(i18n.t("tui.vm.details.name"), style="bold")
            details_text.append(f"{vm.name}\n", style="bold green")
            details_text.append(i18n.t("tui.vm.details.macos_version"), style="bold")
            details_text.append(f"{vm.macos_version.capitalize()}\n", style="cyan")
            details_text.append(i18n.t("tui.vm.details.processing"), style="bold")
            details_text.append(f"{vm.vcpus} vCPUs\n", style="white")
            details_text.append(i18n.t("tui.vm.details.ram"), style="bold")
            details_text.append(f"{vm.ram_gb} GB\n", style="white")
            # Informações de disco
            disk_info = DiskProvisioner.get_disk_info(vm.disk_path) if vm.disk_path else {"virtual_gb": vm.disk_size_gb, "disk_gb": 0}
            details_text.append(i18n.t("tui.vm.details.virtual_disk"), style="bold")
            details_text.append(i18n.t("tui.vm.details.disk_usage", size=vm.disk_size_gb, used=disk_info["disk_gb"]), style="white")
            details_text.append(i18n.t("tui.vm.details.disk_file"), style="bold")
            details_text.append(i18n.t("tui.vm.details.disk_path", path=vm.disk_path or i18n.t("tui.vm.details.default")), style="dim")
            details_text.append(i18n.t("tui.vm.details.gpu_adapter"), style="bold")
            gpu_display = vm.selected_gpu_name or vm.gpu_mode
            pci_suffix = f" [PCI: {vm.selected_gpu}]" if vm.selected_gpu else ""
            details_text.append(f"{gpu_display}{pci_suffix}\n", style="magenta")
            details_text.append(i18n.t("tui.vm.details.autostart"), style="bold")
            details_text.append((i18n.t("tui.vm.details.enabled") if vm.auto_start else i18n.t("tui.vm.details.disabled")) + "\n", style="bold green" if vm.auto_start else "dim")
            show_picker = getattr(vm, "opencore_show_picker", False)
            details_text.append(i18n.t("tui.vm.details.opencore_boot"), style="bold")
            details_text.append((i18n.t("tui.vm.details.picker_menu") if show_picker else i18n.t("tui.vm.details.picker_direct")) + "\n", style="yellow" if show_picker else "bold cyan")
            current_phase = normalize_phase(getattr(vm, "install_phase", None))
            details_text.append(i18n.t("tui.vm.details.install_state"), style="bold")
            details_text.append(f"{phase_label(current_phase)}\n")
            details_text.append(i18n.t("tui.vm.details.install_reboots"), style="bold")
            details_text.append(f"{getattr(vm, 'install_reboots', 0)}\n")
            details_text.append(i18n.t("tui.vm.details.host_power_sync"), style="bold")
            if current_phase is InstallPhase.INSTALLED:
                details_text.append(i18n.t("tui.vm.details.sync_on"), style="bold green")
            else:
                details_text.append(i18n.t("tui.vm.details.sync_off"), style="bold yellow")

            details_text.append("── OpenCore SMBIOS ──\n", style="dim cyan")
            details_text.append(i18n.t("tui.vm.details.smbios_model", value=vm.smbios.model), style="dim")
            details_text.append(i18n.t("tui.vm.details.smbios_serial", value=vm.smbios.serial_number), style="dim")
            details_text.append(i18n.t("tui.vm.details.smbios_mlb", value=vm.smbios.board_serial), style="dim")
            details_text.append(i18n.t("tui.vm.details.smbios_smuuid", value=vm.smbios.smuuid), style="dim")

            panel = Panel(details_text, title=i18n.t("tui.vm.details.title", name=vm.name), border_style="green")
            console.print(panel)

            gpus = GPUChecker.list_gpus()
            has_multiple_gpus = len(gpus) > 1

            console.print(i18n.t("tui.vm.actions.title"))
            console.print(i18n.t("tui.vm.actions.start"))
            console.print(i18n.t("tui.vm.actions.toggle_autostart"))
            console.print(i18n.t("tui.vm.actions.toggle_picker"))
            console.print(i18n.t("tui.vm.actions.adjust_resources"))
            console.print(i18n.t("tui.vm.actions.regenerate_serials"))
            console.print(i18n.t("tui.vm.actions.usb"))
            console.print(i18n.t("tui.vm.actions.toggle_phase"))
            if has_multiple_gpus:
                console.print(i18n.t("tui.vm.actions.change_gpu"))
                console.print(i18n.t("tui.vm.actions.delete"))
                allowed_actions = ["1", "2", "3", "4", "5", "6", "7", "d", "D", "i", "I", "0"]
            else:
                console.print(i18n.t("tui.vm.actions.delete"))
                allowed_actions = ["1", "2", "3", "4", "5", "6", "d", "D", "i", "I", "0"]
            console.print(i18n.t("tui.vm.actions.back"))

            action = Prompt.ask(i18n.t("tui.vm.actions.prompt"), choices=allowed_actions, default="1")

            if action == "1":
                self._launch_vm(vm)
                break
            elif action in ("d", "D"):
                self._menu_usb_devices(vm)
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
                console.print(i18n.t("tui.opencore.syncing"))
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
                        status_str = i18n.t("tui.vm.details.picker_menu") if new_picker else i18n.t("tui.opencore.autoboot_direct")
                        console.print(i18n.t("tui.opencore.updated", status=status_str))
                    except Exception as e:
                        console.print(i18n.t("tui.opencore.write_error", error=e))
                else:
                    console.print(i18n.t("tui.opencore.not_found"))
                Prompt.ask(i18n.t("tui.prompt.enter_continue"))
            elif action == "4":
                self._edit_vm_resources(vm)
            elif action == "5":
                new_smbios = GenSMBIOS.generate(vm.macos_version)
                vm.smbios = new_smbios
                self.store.update_vm(vm)
                console.print(i18n.t("tui.smbios.generated"))
                Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
            elif has_multiple_gpus and action == "6":
                self._choose_gpu_for_vm(vm, gpus)
                self.store.update_vm(vm)
                console.print(i18n.t("tui.gpu.updated", name=vm.selected_gpu_name))
                Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
            elif (has_multiple_gpus and action == "7") or (not has_multiple_gpus and action == "6"):
                if Confirm.ask(i18n.t("tui.vm.delete.confirm", name=vm.name)):
                    del_disk = Confirm.ask(i18n.t("tui.vm.delete.disk"))
                    self.store.delete_vm(vm.id, delete_disk=del_disk)
                    console.print(i18n.t("tui.vm.delete.done"))
                    Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
                    break
            elif action == "0":
                break

    def _wizard_create_vm(self) -> None:
        console.clear()
        console.print(render_banner())
        console.print(i18n.t("tui.wizard.title"))

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
                    crit_msgs.append(i18n.t("tui.wizard.crit_check", name=c.name, details=c.details))
                    if c.remediation:
                        crit_msgs.append(i18n.t("tui.wizard.remediation", remediation=c.remediation))

            erros_str = "\n".join(crit_msgs)

            console.print(Panel(
                i18n.t("tui.wizard.blocked", errors=erros_str),
                title=i18n.t("tui.install_blocked.title"),
                border_style="red",
            ))
            if sys.stdin.isatty():
                Prompt.ask(i18n.t("tui.prompt.enter_return_menu"))
            return

        # Análise automática de hardware
        profile = HardwareAdvisor.analyze()

        # 1. Nome da VM
        name = Prompt.ask(i18n.t("tui.wizard.name.prompt"), default="macOS Workstation")
        vm_id = name.lower().replace(" ", "-") + "-" + str(uuid.uuid4())[:4]

        # 2. Versão do macOS
        console.print(i18n.t("tui.wizard.version.title"))
        console.print(i18n.t("tui.wizard.option.tahoe"))
        console.print(i18n.t("tui.wizard.option.sequoia"))
        console.print(i18n.t("tui.wizard.option.ventura"))
        ver_opt = Prompt.ask(i18n.t("tui.wizard.version.prompt"), choices=["1", "2", "3"], default="2")
        version_map = {"1": "tahoe", "2": "sequoia", "3": "ventura"}
        macos_version = version_map[ver_opt]

        # 3. vCPUs recomendadas
        console.print(i18n.t("tui.wizard.cpu.topology", model=profile.cpu.model_name, threads=profile.cpu.total_threads))
        vcpus = IntPrompt.ask(i18n.t("tui.wizard.vcpus.prompt"), default=profile.cpu.suggested_vcpus)

        # 4. RAM recomendada
        console.print(i18n.t("tui.wizard.ram.host_total", gb=profile.ram.total_gb))
        ram_gb = IntPrompt.ask(i18n.t("tui.wizard.ram.prompt"), default=profile.ram.suggested_vm_ram_gb)

        # 5. Tamanho do Disco
        disk_size_gb = IntPrompt.ask(i18n.t("tui.wizard.disk.prompt"), default=64)

        # 6. Seleção de Placa de Vídeo (somente se houver mais de 1 GPU no host)
        gpus = GPUChecker.list_gpus()
        selected_gpu_slot = None
        selected_gpu_name = None

        if len(gpus) > 1:
            console.print(i18n.t("tui.gpu.detected"))
            default_choice = "1"
            for idx, g in enumerate(gpus, start=1):
                if g.vendor_name == "AMD":
                    tipo = i18n.t("tui.gpu.type.dedicated_apple")
                elif g.vendor_name == "NVIDIA":
                    tipo = i18n.t("tui.gpu.type.dedicated_reims")
                else:
                    tipo = i18n.t("tui.gpu.type.integrated")

                if g.vendor_name in ["NVIDIA", "AMD"]:
                    default_choice = str(idx)
                console.print(i18n.t("tui.gpu.row", idx=idx, vendor=g.vendor_name, device=g.device_name, slot=g.pci_slot, tipo=tipo))

            gpu_choices = [str(i) for i in range(1, len(gpus) + 1)]
            chosen_idx = Prompt.ask(
                i18n.t("tui.gpu.select"),
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
        auto_start = Confirm.ask(i18n.t("tui.wizard.autostart.confirm"), default=False)

        # 8. Geração automática de SMBIOS
        smbios = GenSMBIOS.generate(macos_version=macos_version)

        # 9. Download / Preparação do Instalador Apple
        cached_img = MacOSDownloader.get_cached_image_path(macos_version)
        installer_path = str(cached_img) if cached_img else ""
        if not cached_img:
            if Confirm.ask(i18n.t("tui.wizard.download.confirm", version=macos_version.capitalize()), default=True):
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

        console.print(i18n.t("tui.wizard.provisioning"))
        try:
            bundle = DiskProvisioner.provision(vm, installer_img=installer_path)
            vm.disk_path = str(bundle.hdd_path)
        except Exception as e:
            console.print(i18n.t("tui.wizard.provision_error", error=e))

        self.store.add_vm(vm)
        console.print(i18n.t("tui.wizard.created", name=name))

        if Confirm.ask(i18n.t("tui.wizard.start_install.confirm"), default=True):
            self._launch_vm(vm, is_installation=True)
        else:
            Prompt.ask(i18n.t("tui.prompt.enter_back_menu"))

    def _edit_vm_resources(self, vm: VMConfig) -> None:
        console.print(i18n.t("tui.resources.title", name=vm.name))
        vm.vcpus = IntPrompt.ask("vCPUs", default=vm.vcpus)
        vm.ram_gb = IntPrompt.ask("RAM (GB)", default=vm.ram_gb)

        gpus = GPUChecker.list_gpus()
        if len(gpus) > 1:
            if Confirm.ask(i18n.t("tui.resources.change_gpu.confirm"), default=False):
                self._choose_gpu_for_vm(vm, gpus)

        self.store.update_vm(vm)
        console.print(i18n.t("tui.resources.updated"))
        Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))

    def _choose_gpu_for_vm(self, vm: VMConfig, gpus: list) -> None:
        console.print(i18n.t("tui.gpu.detected"))
        for idx, g in enumerate(gpus, start=1):
            if g.vendor_name == "AMD":
                tipo = i18n.t("tui.gpu.type.dedicated_apple")
            elif g.vendor_name == "NVIDIA":
                tipo = i18n.t("tui.gpu.type.dedicated_reims")
            else:
                tipo = i18n.t("tui.gpu.type.integrated")

            selected_mark = i18n.t("tui.gpu.current_mark") if vm.selected_gpu == g.pci_slot else ""
            console.print(i18n.t("tui.gpu.row_marked", idx=idx, vendor=g.vendor_name, device=g.device_name, slot=g.pci_slot, tipo=tipo, mark=selected_mark))

        gpu_choices = [str(i) for i in range(1, len(gpus) + 1)]
        g_choice = Prompt.ask(i18n.t("tui.gpu.select_new"), choices=gpu_choices, default="1")
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

        console.print(i18n.t("tui.diag.cpu.title"))
        console.print(i18n.t("tui.diag.cpu.model", model=profile.cpu.model_name))
        console.print(i18n.t("tui.diag.cpu.threads", total=profile.cpu.total_threads, physical=profile.cpu.physical_cores))
        console.print(i18n.t("tui.diag.ram", total=profile.ram.total_gb, available=profile.ram.available_gb))
        console.print(i18n.t("tui.diag.recommendation", vcpus=profile.cpu.suggested_vcpus, ram=profile.ram.suggested_vm_ram_gb))

        console.print(i18n.t("tui.diag.gpu.title"))
        for gpu in gpus:
            console.print(i18n.t("tui.diag.gpu.row", vendor=gpu.vendor_name, device=gpu.device_name, slot=gpu.pci_slot))
            console.print(i18n.t("tui.diag.gpu.reims_compat", compat=i18n.t("tui.diag.gpu.reims_yes") if gpu.reims_vgpu_compatible else i18n.t("tui.diag.gpu.reims_no")))
            console.print(f"  IOMMU Group: {gpu.iommu_group or 'N/A'} | VFIO Stubbed: {gpu.is_vfio_stubbed}")
            console.print(i18n.t("tui.diag.gpu.recommendation", recommendation=gpu.recommendation))

        if interactive and sys.stdin.isatty():
            Prompt.ask(i18n.t("tui.prompt.enter_back"))

    def _update_upstream(self) -> None:
        from core.updater import SystemUpdater
        SystemUpdater.run_full_update(force=False)
        Prompt.ask(i18n.t("tui.prompt.enter_continue_nl"))

    def _menu_download_images(self) -> None:
        console.clear()
        console.print(render_banner())
        console.print(i18n.t("tui.download.title"))

        readiness = SystemReadinessChecker.check_all()
        if not readiness.has_internet:
            console.print(i18n.t("tui.download.no_net_error"))
            console.print(i18n.t("tui.download.net_required"))
            if shutil.which("nmtui"):
                if Confirm.ask(i18n.t("tui.download.nmtui.confirm"), default=True):
                    subprocess.run(["nmtui"])
            Prompt.ask(i18n.t("tui.prompt.enter_continue"))
            return

        versions = list(MACOS_PRODUCTS.keys())
        for idx, ver in enumerate(versions, start=1):
            info = MACOS_PRODUCTS[ver]
            cached = MacOSDownloader.get_cached_image_path(ver)
            status_str = i18n.t("tui.download.status_downloaded", cached=cached) if cached else i18n.t("tui.download.status_not_downloaded")
            console.print(i18n.t("tui.download.row", idx=idx, name=info['name'], status=status_str))

        console.print(i18n.t("tui.download.back"))

        choice = Prompt.ask(i18n.t("tui.download.prompt"), choices=[str(i) for i in range(len(versions) + 1)], default="0")
        if choice == "0":
            return

        chosen_ver = versions[int(choice) - 1]
        console.print(i18n.t("tui.download.starting", name=MACOS_PRODUCTS[chosen_ver]['name']))
        MacOSDownloader.prepare_installer(chosen_ver)
        Prompt.ask(i18n.t("tui.prompt.enter_continue_nl"))

    def _launch_vm(self, vm: VMConfig, is_installation: bool = False) -> None:
        console.clear()
        console.print(render_banner())

        if not self.lock.acquire(vm.name):
            console.print(i18n.t("tui.launch.other_vm_running"))
            console.print(i18n.t("tui.launch.only_one_vm"))
            Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
            return

        try:
            console.print(i18n.t("tui.launch.starting", name=vm.name, version=vm.macos_version.capitalize()))

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
                    i18n.t("tui.launch.vulkan_blocked", diag=vulkan_msg),
                    border_style="red",
                ))
                if sys.stdin.isatty():
                    Prompt.ask(i18n.t("tui.prompt.enter_return"))
                return

            # Compila o reims-vgpu quando a árvore trouxer fontes e o binário
            # estiver ausente ou defasado. É aqui que um patch novo deixa de
            # ficar invisível para a VM.
            if reims_dir and not self._ensure_reims_built(reims_dir):
                console.print(i18n.t("tui.launch.no_qemu"))
                Prompt.ask(i18n.t("tui.prompt.enter_continue"))
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
            #   2. como um reboot do convidado é tratado (extra_args do vm.toml);
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

            console.print(i18n.t("tui.launch.install_state", phase=phase_label(policy.phase)))
            console.print(f"[dim]{policy.reason}[/dim]")
            if policy.is_installing:
                console.print(i18n.t("tui.launch.reboots_absorbed"))

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
            use_reims_rail, rail_reason = decide_reimsctl(
                reims_dir, vm.macos_version, custom_dir
            )
            if not use_reims_rail:
                console.print(Panel(
                    i18n.t("tui.launch.rail_unavailable", reason=rail_reason),
                    border_style="red",
                ))
                if sys.stdin.isatty():
                    Prompt.ask(i18n.t("tui.prompt.enter_continue"))
                return
            # --- Delegação ao reims-ctl (árvore nova do reims-vgpu) -----------
            # O appliance não monta argv de QEMU: grava o vm.toml no layout do
            # reims-ctl (cpus/RAM sugeridos pelo host, Haswell-noTSX fixo,
            # installer da fase atual), registra a VM e delega o start. O QMP
            # nasce em <vmdir>/qmp.sock — socket fixo que o monitor observa,
            # mesmo contrato do antigo rail, sem argv de QEMU pra manter aqui.
            env_vars = os.environ.copy()
            self._apply_gpu_environment(vm, env_vars)

            installer_path = (
                installer_img if (installer_img and policy.attach_installer) else None
            )
            reims_ctl.write_vm_toml(vm, installer=installer_path)
            reims_ctl.ensure_registered(vm)

            ctl_bin = reims_ctl.reims_ctl_bin()
            if not ctl_bin:
                console.print(Panel(
                    i18n.t("tui.launch.ctl_missing"),
                    border_style="red",
                ))
                if sys.stdin.isatty():
                    Prompt.ask(i18n.t("tui.prompt.enter_continue"))
                return
            start_cmd = [ctl_bin, "start", vm.id]

            # A janela do reims-vgpu (winit) fala Wayland direto: sem conexão de
            # display ela não abre. No appliance a TUI está num TTY, então o
            # start é envolvido numa sessão kiosk — quando já existe sessão
            # (ex.: compositor do host), não mexemos em nada.
            start_cmd, sem_sessao = reims_ctl.session_argv(
                [ctl_bin, "start", vm.id], env_vars
            )
            if sem_sessao:
                console.print(i18n.t("tui.launch.no_waysession"))

            # Um reboot do convidado encerra o QEMU (vm.toml:
            # extra_args = ["-action", "reboot=shutdown"]) e o laço de sessão
            # relança — a alternativa é o DeviceReset no lugar, que congela o
            # host no vkQueueWaitIdle com o BQL preso (a troca já era assim no
            # antigo rail via QEMU_REBOOT_ACTION=exit).
            from core.power import QMPPowerMonitor
            from types import SimpleNamespace

            def _new_rail_monitor():
                # Um por boot — ver o docstring de `run_rail_session`.
                return QMPPowerMonitor(sock_path=str(reims_ctl.qmp_socket(vm)))

            def _relaunch_notice(step: int) -> None:
                if step < 0:
                    console.print(i18n.t("tui.launch.too_many_reboots"))
                else:
                    console.print(i18n.t("tui.launch.relaunching", step=step))

            # O vm.toml pede `-action reboot=shutdown`: um reboot do convidado
            # encerra o QEMU e o laço é quem o recoloca — sem ele a instalação
            # morreria no primeiro reboot do instalador.
            total_resets, last_reason = run_rail_session(
                launch=lambda: reims_ctl.ReimsCtlSession(
                    start_cmd, env=env_vars, on_ready=lambda: self._usb_auto_attach(vm)
                ),
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

            console.print(i18n.t("tui.launch.finished"))
            if sys.stdin.isatty():
                Prompt.ask(i18n.t("tui.prompt.enter_return_manager"))
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
            console.print(i18n.t("tui.power.install_in_progress"))
            if getattr(monitor, "reset_count", 0):
                console.print(i18n.t("tui.power.reboots_absorbed", count=monitor.reset_count))
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
        console.print(i18n.t("tui.post.vm_disk_probe", probe=summarize_probe(probe)))
        vm.install_evidence = classify_evidence(probe).value

        if not probe.looks_like_complete_install:
            vm.install_phase = InstallPhase.INSTALLING.value
            self.store.update_vm(vm)
            console.print(i18n.t("tui.post.not_complete"))
            console.print(i18n.t("tui.post.sync_stays_off"))
            return

        console.print(i18n.t("tui.post.detected_installed"))
        if not sys.stdin.isatty():
            vm.install_phase = InstallPhase.INSTALLING.value
            self.store.update_vm(vm)
            return

        if Confirm.ask(i18n.t("tui.post.confirm_complete"), default=True):
            vm.install_phase = InstallPhase.INSTALLED.value
            vm.install_reboots = int(getattr(vm, "install_reboots", 0))
            self.store.update_vm(vm)
            console.print(i18n.t("tui.post.sync_enabled"))
        else:
            vm.install_phase = InstallPhase.INSTALLING.value
            self.store.update_vm(vm)
            console.print(i18n.t("tui.post.kept_installing"))

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
            # `evaluate` já resolve o binário em uso: o instalado pelo
            # install-ctl.sh (qemu/current) ou, na árvore antiga, o in-tree.
            return bool(state.binary_path and state.binary_path.exists())

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
            i18n.t("tui.build.needs_build", reason=state.reason),
            border_style="yellow",
        ))
        if not Confirm.ask(i18n.t("tui.build.confirm_now"), default=True):
            if binary_present():
                console.print(i18n.t("tui.build.refused_existing"))
                return True
            console.print(i18n.t("tui.build.refused_no_binary"))
            Prompt.ask(i18n.t("tui.prompt.enter_continue"))
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
            task = progress.add_task(i18n.t("tui.build.preparing"), total=100)
            current = {"name": ""}

            def on_start(name: str, _expected: float) -> None:
                current["name"] = name
                progress.update(task, description=i18n.t("tui.build.compiling", name=name))

            def on_progress(pct: float) -> None:
                progress.update(task, completed=pct)

            def on_done(name: str, ok: bool) -> None:
                if not ok:
                    progress.update(
                        task,
                        description=i18n.t("tui.build.failed_task", name=name),
                    )

            try:
                result["ok"] = build_reims_vgpu(
                    state.reims_dir,
                    on_start=on_start,
                    on_done=on_done,
                    progress=on_progress,
                )
            except Exception as exc:  # noqa: BLE001 — a TUI não pode morrer aqui
                progress.update(task, description=i18n.t("tui.build.error", error=exc))

            if result["ok"]:
                progress.update(task, completed=100.0, description=i18n.t("tui.build.done"))

        if not result["ok"]:
            from core.reims_build import tail_log
            console.print(i18n.t("tui.build.failed"))
            for log_name in ("build-qemu-reims.log", "build-rom.log"):
                snippet = tail_log(log_name, lines=12)
                if snippet:
                    console.print(i18n.t("tui.build.log_header", name=log_name))
                    console.print(snippet)
            console.print(i18n.t("tui.build.logs_path"))
            Prompt.ask(i18n.t("tui.prompt.enter_continue"))
            # O configure do QEMU apaga o build/ antes de reconfigurar, então uma
            # falha pode ter levado o binário anterior junto. Se não sobrou
            # nenhum, não há o que lançar — e dizer o contrário seria mentira.
            if not binary_present():
                console.print(i18n.t("tui.build.previous_lost"))
                return False
            console.print(i18n.t("tui.build.using_previous"))
            return True

        console.print(i18n.t("tui.build.success"))
        return True

    def _menu_build_iso(self) -> None:
        console.clear()
        console.print(render_banner())
        console.print(i18n.t("tui.iso.title"))
        console.print(i18n.t("tui.iso.desc1"))
        console.print(i18n.t("tui.iso.desc2"))
        console.print(i18n.t("tui.iso.features"))
        console.print(i18n.t("tui.iso.feature.kernel"))
        console.print(i18n.t("tui.iso.feature.graphics"))
        console.print(i18n.t("tui.iso.feature.audio"))
        console.print(i18n.t("tui.iso.feature.autologin"))
        console.print(i18n.t("tui.iso.feature.ai"))
        console.print("      - [cyan]claude[/cyan]   (@anthropic-ai/claude-code)")
        console.print("      - [cyan]codex[/cyan]    (@openai/codex)")
        console.print("      - [cyan]opencode[/cyan] (opencode-cli)")
        console.print(i18n.t("tui.iso.feature.grub"))

        builder_script = Path(__file__).resolve().parent.parent / "appliance" / "build-iso.sh"
        if not shutil.which("mkarchiso"):
            console.print(i18n.t("tui.iso.mkarchiso_missing"))
            console.print(i18n.t("tui.iso.mkarchiso_hint"))
            console.print("  [bold green]sudo pacman -S --needed archiso[/bold green]\n")

        console.print(i18n.t("tui.iso.build_script", path=builder_script))
        console.print(i18n.t("tui.iso.opt.instructions"))
        console.print(i18n.t("tui.iso.opt.harnesses"))
        console.print(i18n.t("tui.vm.actions.back"))

        choice = Prompt.ask(i18n.t("tui.menu.prompt_choice"), choices=["1", "2", "0"], default="0")
        if choice == "1":
            console.print(i18n.t("tui.iso.build_options"))
            console.print(i18n.t("tui.iso.option.a"))
            console.print(i18n.t("tui.iso.cmd.a", script=builder_script))
            console.print(i18n.t("tui.iso.option.a_note"))
            console.print(i18n.t("tui.iso.option.b"))
            console.print(i18n.t("tui.iso.cmd.b", script=builder_script))
            console.print(i18n.t("tui.iso.option.c"))
            console.print(i18n.t("tui.iso.cmd.c", script=builder_script))
            Prompt.ask(i18n.t("tui.prompt.enter_continue"))
        elif choice == "2":
            setup_script = Path(__file__).resolve().parent.parent / "appliance" / "archiso" / "airootfs" / "usr" / "local" / "bin" / "setup-harness-tools.sh"
            if setup_script.exists():
                subprocess.run(["bash", str(setup_script)])
            Prompt.ask(i18n.t("tui.prompt.enter_continue_nl"))
