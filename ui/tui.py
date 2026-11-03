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
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from rich import box
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.rule import Rule
from rich.text import Text

from core.config import VMConfig, VMManagerStore, ExclusiveVMLock, CONFIG_DIR, SMBIOSConfig
from core import i18n
from core.reims_build import evaluate as evaluate_reims_release, find_reims_dir, install_prebuilt
from core import reims_ctl
from core.web_access import access_details
from core.web_client import agent_is_required, call_agent, read_agent_catalog, WebAgentUnavailable
from core.hardware import HardwareAdvisor
from core.gpu import GPUChecker
from core.readiness import SystemReadinessChecker
from core.mode import detect_system_mode, has_installer_binary, is_live_media
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
from core.vm_lifecycle import (
    VMLifecycleService,
    decide_reimsctl,
    recover_reimsctl_vm_storage as _recover_reimsctl_vm_storage,
    run_rail_session,
    default_launch_hooks,
)
from ui.banner import animate_banner, centered_banner

console = Console()


def recover_reimsctl_vm_storage(vm: VMConfig) -> tuple[Optional[Path], str]:
    """Compatibility adapter retaining the TUI's configurable state root."""
    return _recover_reimsctl_vm_storage(vm, config_dir=CONFIG_DIR)








class VMTUI:
    """Interface principal do gerenciador vHackintosh."""

    def __init__(self, store: VMManagerStore):
        self.store = store
        self._catalog_revision: str | None = None
        self._agent_vm_snapshots: dict[str, VMConfig] = {}
        self.lock = ExclusiveVMLock()
        launch_hooks = default_launch_hooks(store)
        launch_hooks.ensure_release = self._ensure_reims_release
        launch_hooks.on_usb_ready = self._usb_auto_attach
        launch_hooks.apply_gpu_environment = self._apply_gpu_environment
        launch_hooks.apply_power_policy = self._apply_host_power_policy
        launch_hooks.post_phase_update = self._post_vm_phase_update
        self.lifecycle_service = VMLifecycleService(
            store, lock=self.lock, lock_factory=ExclusiveVMLock, hooks=launch_hooks
        )
        # Detectado uma única vez: o modo de execução não muda durante a sessão.
        self.system_mode = detect_system_mode()
        self.installer_available = has_installer_binary()
        self._banner_animated = False

    def run_main_loop(self) -> None:
        # Primeira tela do appliance (specs/language-selection): enquanto o
        # idioma não estiver escolhido, nada mais aparece — nem o banner com
        # opções, nem a prontidão.
        self._language_screen()
        self._backfill_install_phases()
        while True:
            console.clear()
            if not getattr(self, "_banner_animated", False):
                animate_banner(console)
                self._banner_animated = True
            else:
                console.print(centered_banner(console))

            # Só mostra o card no appliance que realmente empacota o serviço;
            # o projeto também roda em ambientes de desenvolvimento sem systemd.
            if Path("/etc/systemd/system/vhackintosh-web.service").is_file():
                console.print(
                    Panel(
                        "\n".join(access_details()),
                        title="Painel de gerenciamento",
                        border_style="#168bd2",
                        box=box.ROUNDED,
                        padding=(0, 1),
                    )
                )

            running_vms = self.lock.get_running_vm_info()
            if running_vms:
                console.print(i18n.t("tui.main.running_vm", vm=running_vms))

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

            try:
                vms = self._list_managed_vms()
            except WebAgentUnavailable as exc:
                vms = []
                console.print(Panel(
                    f"O catálogo não foi carregado e nenhuma VM será iniciada pela TUI. {exc}",
                    title="Agente de gerenciamento indisponível",
                    border_style="red",
                ))
            self._render_vm_table(vms)

            console.print(Rule(style="#164b66"))
            console.print(i18n.t("tui.menu.title"))
            console.print(i18n.t("tui.menu.vm_shortcuts"))

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
            console.print(i18n.t("tui.menu.reims_release"))
            console.print(i18n.t("tui.menu.control_release"))
            console.print(i18n.t("tui.menu.download_images"))
            console.print(i18n.t("tui.menu.restart"))
            console.print(i18n.t("tui.menu.build_iso"))
            console.print(i18n.t("tui.menu.shutdown"))
            console.print(i18n.t("tui.menu.smbios_diagnostics"))
            console.print(i18n.t("tui.menu.exit"))

            choices = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "A", "a", "C", "c"]
            if installer_available:
                choices.extend(["I", "i"])

            choice = Prompt.ask(i18n.t("tui.menu.prompt_choice"), choices=choices, default="1")

            if choice in ("I", "i"):
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
            elif choice in ("A", "a"):
                self._menu_reims_release()
            elif choice in ("C", "c"):
                self._menu_control_release()
            elif choice == "9":
                self._view_smbios_diagnostics(vms)
            elif choice == "5":
                self._menu_download_images()
            elif choice == "6":
                from core.power import PowerSync

                console.print(i18n.t("tui.power.restarting_host"))
                console.file.flush()
                if PowerSync.request_host_action("reboot"):
                    break
                console.print(i18n.t("tui.power.host_action_failed"))
            elif choice == "7":
                self._menu_build_iso()
            elif choice == "8":
                from core.power import PowerSync

                console.print(i18n.t("tui.power.shutting_down_host"))
                console.file.flush()
                if PowerSync.request_host_action("poweroff"):
                    break
                console.print(i18n.t("tui.power.host_action_failed"))
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
        # The appliance's agent owns catalog mutations. Do not write through a
        # second store instance from the TUI during boot.
        if agent_is_required():
            return
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
            self._patch_managed_vm(vm, {
                "install_evidence": classify_evidence(probe).value,
                "install_phase": InstallPhaseResolver.resolve_phase(vm, probe=probe).value,
            })
            console.print(f"  [dim]{vm.name}: {summarize_probe(probe)} → {vm.install_phase}[/dim]")
        console.print()

    def _list_managed_vms(self) -> list[VMConfig]:
        """Use the appliance agent as catalog source; local mode stays useful for development."""
        if not agent_is_required():
            return self.store.list_vms()
        vms, revision = read_agent_catalog()
        self._catalog_revision = revision
        self._agent_vm_snapshots = {vm.id: VMConfig.from_dict(vm.to_dict()) for vm in vms}
        return vms

    def _update_managed_vm(self, vm: VMConfig) -> dict | None:
        """Persist a supported config snapshot through the appliance agent."""
        if not agent_is_required():
            self.store.update_vm(vm)
            return None
        if not self._catalog_revision:
            self._list_managed_vms()
        try:
            result = call_agent("vms.config.update", {
                "vm_id": vm.id,
                "expected_revision": self._catalog_revision,
                "config": {
                    "name": vm.name,
                    "macos_version": vm.macos_version,
                    "vcpus": vm.vcpus,
                    "ram_gb": vm.ram_gb,
                    "disk_size_gb": vm.disk_size_gb,
                    "auto_start": vm.auto_start,
                    "audio_device": vm.audio_device,
                    "fullscreen": vm.fullscreen,
                    "opencore_show_picker": vm.opencore_show_picker,
                    "gpu_mode": vm.gpu_mode,
                    "selected_gpu": vm.selected_gpu,
                    "selected_gpu_uuid": vm.selected_gpu_uuid,
                    "network_enabled": True,
                    "power_sync_mode": vm.power_sync_mode,
                    "qemu_extra_args": list(vm.qemu_extra_args),
                },
            })
        except WebAgentUnavailable:
            self._restore_agent_snapshot(vm)
            raise
        if not isinstance(result, dict) or not isinstance(result.get("revision"), str):
            self._restore_agent_snapshot(vm)
            raise WebAgentUnavailable("O agente não confirmou a revisão da configuração; nenhuma gravação local foi tentada.")
        self._catalog_revision = result["revision"]
        self._agent_vm_snapshots[vm.id] = VMConfig.from_dict(vm.to_dict())
        return result

    def _patch_managed_vm(self, vm: VMConfig, fields: dict) -> dict | None:
        """Persist narrowly-scoped TUI metadata through the local agent."""
        normalized = dict(fields)
        if "smbios" in normalized and isinstance(normalized["smbios"], dict):
            normalized["smbios"] = SMBIOSConfig(**normalized["smbios"])
        if not agent_is_required():
            for name, value in normalized.items():
                setattr(vm, name, value)
            self.store.update_vm(vm)
            return None
        if not self._catalog_revision:
            self._list_managed_vms()
        try:
            result = call_agent("vms.config.patch", {
                "vm_id": vm.id, "expected_revision": self._catalog_revision,
                "fields": {name: value.__dict__ if isinstance(value, SMBIOSConfig) else value
                           for name, value in normalized.items()},
            })
        except WebAgentUnavailable:
            self._restore_agent_snapshot(vm)
            raise
        if not isinstance(result, dict) or not isinstance(result.get("revision"), str):
            self._restore_agent_snapshot(vm)
            raise WebAgentUnavailable("O agente não confirmou a revisão da configuração.")
        for name, value in normalized.items():
            setattr(vm, name, value)
        self._catalog_revision = result["revision"]
        self._agent_vm_snapshots[vm.id] = VMConfig.from_dict(vm.to_dict())
        return result

    def _delete_managed_vm(self, vm: VMConfig, *, delete_disk: bool) -> None:
        if not agent_is_required():
            self.store.delete_vm(vm.id, delete_disk=delete_disk)
            return
        if not self._catalog_revision:
            self._list_managed_vms()
        result = call_agent("vms.config.delete", {
            "vm_id": vm.id, "expected_revision": self._catalog_revision,
            "delete_disk": delete_disk,
        })
        if not isinstance(result, dict) or result.get("deleted") is not True:
            raise WebAgentUnavailable("O agente não confirmou a exclusão da VM; nenhum fallback local foi tentado.")
        self._catalog_revision = result.get("revision")
        self._agent_vm_snapshots.pop(vm.id, None)

    def _add_managed_vm(self, vm: VMConfig) -> None:
        """Development-only synchronous registration; appliance uses vms.provision jobs."""
        if agent_is_required():
            raise WebAgentUnavailable(
                "No modo appliance, use o trabalho de provisionamento do agente; gravação local foi recusada."
            )
        self.store.add_vm(vm)

    @staticmethod
    def _submit_vm_provision_job(payload: dict) -> str:
        """Submit TUI-created VMs to the same durable provisioner as the panel."""
        result = call_agent("vms.provision", payload)
        if not isinstance(result, dict) or not isinstance(result.get("job_id"), str):
            raise WebAgentUnavailable(
                "O agente não confirmou a tarefa de provisionamento; nenhuma criação local foi tentada."
            )
        return result["job_id"]

    def _restore_agent_snapshot(self, vm: VMConfig) -> None:
        snapshot = getattr(self, "_agent_vm_snapshots", {}).get(vm.id)
        if snapshot:
            for field in VMConfig.__dataclass_fields__:
                setattr(vm, field, getattr(snapshot, field))

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
        console.print(centered_banner(console))
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
        try:
            with passthrough.QmpClient(sock):
                pass
        except passthrough.QmpError as exc:
            pid = reims_ctl.vms_rodando().get(vm.id)
            if pid:
                console.print(i18n.t(
                    "tui.usb.qmp_unavailable", pid=pid, error=exc.message
                ))
            else:
                console.print(i18n.t("tui.usb.not_running"))
                console.print(f"[dim]{exc.message}[/dim]")
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
                    try:
                        self._patch_managed_vm(vm, {"usb_auto_attach": sorted(auto_ids)})
                    except WebAgentUnavailable as agent_error:
                        console.print(Panel(str(agent_error), title="USB não salvo", border_style="red"))
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
        table = Table(
            title=i18n.t("tui.vm.table.title"),
            title_style="bold #00e5ff",
            border_style="#168ca5",
            header_style="bold #49d9e8",
            box=box.ROUNDED,
            row_styles=["", "#d6e7f5"],
        )
        table.add_column("#", justify="center", style="bold #79a8ff", width=4)
        table.add_column(i18n.t("tui.vm.col.name"), style="bold white")
        table.add_column(i18n.t("tui.vm.col.macos_version"), style="#49d9e8")
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
        console.print(centered_banner(console))
        console.print(i18n.t("tui.phase.title"))

        current = normalize_phase(getattr(vm, "install_phase", None))
        probe = InstallProbe.probe(vm.disk_path) if vm.disk_path else DiskProbe()
        try:
            self._patch_managed_vm(vm, {"install_evidence": classify_evidence(probe).value})
        except WebAgentUnavailable as exc:
            console.print(Panel(str(exc), title="Fase não salva", border_style="red"))
            if sys.stdin.isatty():
                Prompt.ask(i18n.t("tui.prompt.enter_continue"))
            return

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
                try:
                    self._patch_managed_vm(vm, {"install_phase": InstallPhase.INSTALLING.value})
                except WebAgentUnavailable as exc:
                    console.print(Panel(str(exc), title="Fase não salva", border_style="red"))
                    Prompt.ask(i18n.t("tui.prompt.enter_continue"))
                    return
                console.print(i18n.t("tui.phase.reverted"))
        else:
            if Confirm.ask(i18n.t("tui.phase.confirm.mark_installed"), default=False):
                try:
                    self._patch_managed_vm(vm, {"install_phase": InstallPhase.INSTALLED.value})
                except WebAgentUnavailable as exc:
                    console.print(Panel(str(exc), title="Fase não salva", border_style="red"))
                    Prompt.ask(i18n.t("tui.prompt.enter_continue"))
                    return
                console.print(i18n.t("tui.phase.marked_installed"))

        Prompt.ask(i18n.t("tui.prompt.enter_continue_nl"))

    def _parar_vm_interativo(
        self, vm: VMConfig, pid: int, *, force: bool = False
    ) -> None:
        """Encerra a VM pela TUI. Até 19/10 não havia como: só o `reims-ctl`
        cru parava uma VM, e uma VM morta ficava presa como "already runs".

        A ação normal pede ao macOS para desligar. A ação forçada é separada,
        pede confirmação explícita e corta o processo sem esperar pelo convidado.
        """
        if not pid:
            # Chegou aqui por chamada direta, não pela tela (que só oferece a
            # opção quando há VM rodando). Não há o que parar.
            console.print(i18n.t("tui.vm.actions.stop_none"))
            Prompt.ask(i18n.t("tui.prompt.enter_continue"))
            return

        confirm_key = (
            "tui.vm.actions.stop_force_confirm" if force
            else "tui.vm.actions.stop_confirm"
        )
        if not Confirm.ask(
            i18n.t(confirm_key, name=vm.name), default=not force
        ):
            return

        if agent_is_required():
            confirmation = None
            if force:
                confirmation = Prompt.ask(
                    f"Digite exatamente o identificador {vm.id} para confirmar",
                    default="",
                )
                if confirmation != vm.id:
                    console.print("Confirmação não corresponde ao identificador da VM; nenhuma ação foi enviada.")
                    return
            try:
                accepted = self._send_agent_vm_action(
                    vm, "force_stop" if force else "shutdown", confirmation=confirmation,
                )
                console.print(f"Solicitação enviada ao agente (operação {accepted['job_id']}).")
            except WebAgentUnavailable as exc:
                console.print(Panel(str(exc), title="Parada não executada", border_style="red"))
            if sys.stdin.isatty():
                Prompt.ask(i18n.t("tui.prompt.enter_continue"))
            return

        from core import reims_ctl

        if force:
            console.print(i18n.t("tui.vm.actions.stop_force"))
        ok, msg = reims_ctl.parar_vm(vm, force=force)
        if ok:
            console.print(i18n.t("tui.vm.actions.stop_done", msg=msg))
        else:
            console.print(i18n.t("tui.vm.actions.stop_fail", msg=msg))
            if not force:
                console.print(i18n.t("tui.vm.actions.stop_graceful_failed_hint"))

        Prompt.ask(i18n.t("tui.prompt.enter_continue"))

    def _visualizar_vm_interativo(self, vm: VMConfig) -> None:
        """Reconecta à tela de uma VM ativa sem tentar iniciá-la outra vez."""
        if not sys.stdin.isatty():
            console.print(i18n.t(
                "tui.vm.actions.view_error",
                error="o viewer precisa de um terminal interativo",
            ))
            return
        ok, error = reims_ctl.visualizar_vm(vm)
        if not ok:
            console.print(i18n.t("tui.vm.actions.view_error", error=error))
            Prompt.ask(i18n.t("tui.prompt.enter_continue"))

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
            console.print(centered_banner(console))

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

            # Estado de execução. Antes a TUI não dizia se a VM estava rodando;
            # a única pista era o "already runs (pid N)" que o `start` devolvia
            # depois de o operador já ter pedido para iniciar. Quem sabe o
            # estado é o reims-ctl (o mesmo `runtime::pid` que barra o start).
            rodando_agora = {}
            try:
                from core.reims_ctl import vms_rodando

                rodando_agora = vms_rodando()
            except Exception:  # noqa: BLE001 — estado é informativo, não bloqueia
                rodando_agora = {}
            pid_desta = rodando_agora.get(vm.id)
            details_text.append(i18n.t("tui.vm.details.run_state"), style="bold")
            if pid_desta:
                details_text.append(i18n.t("tui.vm.actions.state_running", pid=pid_desta) + "\n")
            else:
                details_text.append(i18n.t("tui.vm.actions.state_stopped") + "\n")

            details_text.append("── OpenCore SMBIOS ──\n", style="dim cyan")
            details_text.append(i18n.t("tui.vm.details.smbios_model", value=vm.smbios.model), style="dim")
            # Hardware-like identity values are intentionally redacted in the UI/logs.
            details_text.append(i18n.t("tui.vm.details.smbios_serial", value="[redigido]"), style="dim")
            details_text.append(i18n.t("tui.vm.details.smbios_mlb", value="[redigido]"), style="dim")
            details_text.append(i18n.t("tui.vm.details.smbios_smuuid", value="[redigido]"), style="dim")

            panel = Panel(details_text, title=i18n.t("tui.vm.details.title", name=vm.name), border_style="green")
            console.print(panel)

            gpus = GPUChecker.list_gpus()
            has_multiple_gpus = len(gpus) > 1

            console.print(i18n.t("tui.vm.actions.title"))
            console.print(
                i18n.t("tui.vm.actions.start_running" if pid_desta else "tui.vm.actions.start")
            )
            console.print(i18n.t("tui.vm.actions.toggle_autostart"))
            console.print(i18n.t("tui.vm.actions.toggle_picker"))
            console.print(i18n.t("tui.vm.actions.adjust_resources"))
            console.print(i18n.t("tui.vm.actions.regenerate_serials"))
            console.print(i18n.t("tui.vm.actions.usb"))
            console.print(i18n.t("tui.vm.actions.toggle_phase"))
            if has_multiple_gpus:
                console.print(i18n.t("tui.vm.actions.change_gpu"))
                allowed_actions = ["1", "2", "3", "4", "5", "6", "7", "d", "D", "i", "I", "p", "P", "0"]
            else:
                allowed_actions = ["1", "2", "3", "4", "5", "7", "d", "D", "i", "I", "p", "P", "0"]
            # A opção de exclusão é sempre 7 na tela. Sem múltiplas GPUs,
            # a ação 6 (alterar GPU) some, mas 7 continua disponível.
            console.print(i18n.t("tui.vm.actions.delete"))
            # Só oferece parar se há o que parar; sem isso a opção falharia
            # sempre, e o operador não saberia por quê.
            if pid_desta:
                console.print(i18n.t("tui.vm.actions.stop"))
                console.print(i18n.t("tui.vm.actions.stop_force_option"))
                console.print(i18n.t("tui.vm.actions.view"))
                allowed_actions.extend(["f", "F", "v", "V"])
            console.print(i18n.t("tui.vm.actions.back"))

            action = Prompt.ask(i18n.t("tui.vm.actions.prompt"), choices=allowed_actions, default="1")

            if action == "1":
                if pid_desta:
                    self._visualizar_vm_interativo(vm)
                    continue
                self._launch_vm(vm)
                break
            elif action in ("v", "V"):
                self._visualizar_vm_interativo(vm)
            elif action in ("p", "P"):
                self._parar_vm_interativo(vm, pid_desta, force=False)
                break
            elif action in ("f", "F"):
                self._parar_vm_interativo(vm, pid_desta, force=True)
                break
            elif action in ("d", "D"):
                self._menu_usb_devices(vm)
            elif action in ("i", "I"):
                self._toggle_install_phase(vm)
            elif action == "2":
                new_state = not vm.auto_start
                try:
                    self._set_auto_start(vm, new_state)
                    vm.auto_start = new_state
                except (WebAgentUnavailable, ValueError) as exc:
                    console.print(Panel(str(exc), title="Não foi possível alterar o auto-start", border_style="red"))
                    if sys.stdin.isatty():
                        Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
            elif action == "3":
                new_picker = not getattr(vm, "opencore_show_picker", False)
                vm.opencore_show_picker = new_picker
                try:
                    self._update_managed_vm(vm)
                except (WebAgentUnavailable, ValueError) as exc:
                    self._restore_agent_snapshot(vm)
                    console.print(Panel(str(exc), title="Opção não salva", border_style="red"))
                    if sys.stdin.isatty():
                        Prompt.ask(i18n.t("tui.prompt.enter_continue"))
                    continue
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
                existing_vm_dir = Path(vm.disk_path).parent if vm.disk_path else Path(os.path.expanduser(f"~/.config/vhackintosh/vms/{vm.id}"))
                if not Confirm.ask(i18n.t("smbios.regenerate.confirm"), default=False):
                    console.print(i18n.t("smbios.regenerate.cancelled"))
                    Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
                    continue
                backup_dir = existing_vm_dir / "smbios-backup-before-regeneration"
                try:
                    backup_dir.mkdir(mode=0o700, exist_ok=False)
                    existing_file = existing_vm_dir / "OpenCore.qcow2"
                    if not existing_file.is_file():
                        backup_dir.rmdir()
                        raise RuntimeError("EFI da VM não encontrada; identidade não foi alterada")
                    shutil.copy2(existing_file, backup_dir / "OpenCore.qcow2")
                    new_smbios = GenSMBIOS.generate(vm.macos_version)
                    existing_efi = existing_vm_dir / "OpenCore.qcow2"
                    if existing_efi.is_file():
                        old_identity = vm.smbios
                        try:
                            vm.smbios = new_smbios
                            DiskProvisioner.inject_opencore_config(existing_efi, vm)
                            self._patch_managed_vm(vm, {"smbios": vm.smbios.__dict__.copy()})
                        except Exception:
                            vm.smbios = old_identity
                            shutil.copy2(backup_dir / "OpenCore.qcow2", existing_efi)
                            raise
                    else:
                        raise RuntimeError("EFI da VM não encontrada; identidade não foi alterada")
                    console.print(i18n.t("smbios.regenerate.backup", path=backup_dir))
                    console.print(i18n.t("tui.smbios.generated"))
                except Exception as exc:
                    console.print(i18n.t("smbios.regenerate.failed", error=exc))
                Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
            elif has_multiple_gpus and action == "6":
                previous_gpu = (vm.selected_gpu, vm.selected_gpu_name, vm.selected_gpu_uuid)
                self._choose_gpu_for_vm(vm, gpus)
                try:
                    self._update_managed_vm(vm)
                    console.print(i18n.t("tui.gpu.updated", name=vm.selected_gpu_name))
                    Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
                except (WebAgentUnavailable, ValueError) as exc:
                    vm.selected_gpu, vm.selected_gpu_name, vm.selected_gpu_uuid = previous_gpu
                    console.print(Panel(str(exc), title="GPU não salva", border_style="red"))
                    if sys.stdin.isatty():
                        Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
            elif action == "7":
                if Confirm.ask(i18n.t("tui.vm.delete.confirm", name=vm.name)):
                    del_disk = Confirm.ask(i18n.t("tui.vm.delete.disk"))
                    try:
                        self._delete_managed_vm(vm, delete_disk=del_disk)
                    except (WebAgentUnavailable, ValueError) as exc:
                        console.print(Panel(str(exc), title="VM não removida", border_style="red"))
                        if sys.stdin.isatty():
                            Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
                        continue
                    console.print(i18n.t("tui.vm.delete.done"))
                    Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
                    break
            elif action == "0":
                break

    def _wizard_create_vm(self) -> None:
        console.clear()
        console.print(centered_banner(console))
        console.print(i18n.t("tui.wizard.title"))
        appliance_managed = agent_is_required()

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
        vm_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "macos"
        vm_id = f"{vm_id}-{uuid.uuid4().hex[:8]}"

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
            # Pré-seleção: NÃO por fabricante. Até 19/10 o critério era
            # `vendor_name in ("NVIDIA", "AMD")`, o que pré-selecionava a RTX
            # 3050 por ela ser NVIDIA — e a VM abriu em tela preta, enquanto a
            # Iris Xe funcionava. Ser discreta não é prova de funcionar.
            #
            # Critério medido: a GPU que o Vulkan enumera (via `reims-ctl gpus`).
            # É o mesmo dado que o reims-ctl usa. Se mais de uma serve — o caso
            # deste host, onde as duas são enumeradas — não há base para
            # preferir, então a escolha fica sem preferência declarada e o
            # operador decide. Sem reims-ctl não afirmamos nada.
            servem = []
            try:
                from core.gpu import vulkan_dirige

                servem = [i for i, g in enumerate(gpus, start=1) if vulkan_dirige(g)]
            except Exception:  # noqa: BLE001 — pré-seleção nunca impede a criação
                servem = []

            default_choice = str(servem[0]) if len(servem) == 1 else "1"
            for idx, g in enumerate(gpus, start=1):
                if g.vendor_name == "AMD":
                    tipo = i18n.t("tui.gpu.type.dedicated_apple")
                elif g.vendor_name == "NVIDIA":
                    tipo = i18n.t("tui.gpu.type.dedicated_reims")
                else:
                    tipo = i18n.t("tui.gpu.type.integrated")

                console.print(i18n.t("tui.gpu.row", idx=idx, vendor=g.vendor_name, device=g.device_name, slot=g.pci_slot, tipo=tipo))

            if len(servem) == 1:
                console.print(i18n.t("tui.gpu.only_one_vulkan", idx=servem[0]))
            elif len(servem) > 1:
                console.print(i18n.t("tui.gpu.many_vulkan"))
            else:
                console.print(i18n.t("tui.gpu.no_vulkan_known"))

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
        smbios = None if appliance_managed else GenSMBIOS.generate(macos_version=macos_version)

        # 9. Download / Preparação do Instalador Apple
        cached_img = MacOSDownloader.get_cached_image_path(macos_version)
        installer_path = str(cached_img) if cached_img else ""
        installer_policy = "cached" if cached_img else "none"
        if not cached_img:
            if Confirm.ask(i18n.t("tui.wizard.download.confirm", version=macos_version.capitalize()), default=True):
                installer_policy = "download"
                if not appliance_managed:
                    img_result = MacOSDownloader.prepare_installer(macos_version)
                    if img_result:
                        installer_path = str(img_result)

        if appliance_managed:
            # With a single board, let Reims use its ordinary default. Explicit
            # BDF selection is only sent when the operator had a real choice.
            if len(gpus) == 1:
                selected_gpu_slot = None
            selected_gpu_uuid = None
            if selected_gpu_slot:
                try:
                    inventory = call_agent("host.gpus")
                except WebAgentUnavailable as exc:
                    console.print(Panel(
                        f"O inventário de GPU do agente não foi consultado; nenhum provisionamento local foi iniciado. {exc}",
                        title="Provisionamento não iniciado", border_style="red",
                    ))
                    return
                items = inventory.get("items") if isinstance(inventory, dict) else None
                selected = next((
                    item for item in items or []
                    if isinstance(item, dict) and item.get("pci_bdf") == selected_gpu_slot
                ), None)
                if not selected or not selected.get("selection_supported") or not selected.get("vulkan_uuid"):
                    console.print(Panel(
                        "A GPU escolhida ainda não possui seleção por BDF/UUID confirmada pelo agente. "
                        "Nenhuma VM foi criada; escolha outra placa no painel quando a capacidade estiver disponível.",
                        title="Seleção de GPU indisponível", border_style="red",
                    ))
                    if sys.stdin.isatty():
                        Prompt.ask(i18n.t("tui.prompt.enter_back"))
                    return
                selected_gpu_uuid = selected["vulkan_uuid"]

            try:
                job_id = self._submit_vm_provision_job({
                    "idempotency_key": uuid.uuid4().hex,
                    "id": vm_id,
                    "name": name,
                    "macos_version": macos_version,
                    "vcpus": vcpus,
                    "ram_gb": ram_gb,
                    "disk_size_gb": disk_size_gb,
                    "auto_start": auto_start,
                    "selected_gpu_bdf": selected_gpu_slot,
                    "selected_gpu_uuid": selected_gpu_uuid,
                    "installer_policy": installer_policy,
                    "fullscreen": True,
                    "opencore_show_picker": installer_policy != "none",
                })
            except WebAgentUnavailable as exc:
                console.print(Panel(
                    f"A tarefa não foi enviada ao agente; nenhum provisionamento local foi iniciado. {exc}",
                    title="Provisionamento não iniciado", border_style="red",
                ))
                if sys.stdin.isatty():
                    Prompt.ask(i18n.t("tui.prompt.enter_back"))
                return
            console.print(Panel(
                f"Provisionamento enviado ao agente. Tarefa: {job_id}. "
                "Acompanhe o progresso na página Tarefas do painel.",
                title="VM em preparação", border_style="green",
            ))
            if sys.stdin.isatty():
                Prompt.ask(i18n.t("tui.prompt.enter_back_menu"))
            return

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
            # O instalador pode reiniciar várias vezes; deixe o picker visível
            # para o operador escolher Recovery/Installer sem depender de
            # segurar uma tecla na janela da VM.
            opencore_show_picker=bool(installer_path),
            created_at=datetime.datetime.now().isoformat(),
        )

        console.print(i18n.t("tui.wizard.provisioning"))
        try:
            bundle = DiskProvisioner.provision(vm, installer_img=installer_path)
            vm.disk_path = str(bundle.hdd_path)
        except Exception as e:
            console.print(i18n.t("tui.wizard.provision_error", error=e))
            # Não registre nem inicie uma VM sem os artefatos provisionados.
            # Antes o wizard continuava após falha ao copiar/injetar o template
            # e o reims-ctl podia acabar subindo uma EFI antiga ou própria.
            if sys.stdin.isatty():
                Prompt.ask(i18n.t("tui.prompt.enter_back"))
            return

        self._add_managed_vm(vm)
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

        try:
            self._update_managed_vm(vm)
        except (WebAgentUnavailable, ValueError) as exc:
            console.print(Panel(str(exc), title="Configuração não salva", border_style="red"))
            if sys.stdin.isatty():
                Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
            return
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

    def _view_smbios_diagnostics(self, vms: list) -> None:
        from core.smbios import GenSMBIOS
        console.print(i18n.t("smbios.diag.menu"))
        if not vms:
            console.print(i18n.t("smbios.diag.unavailable", reason="nenhuma VM cadastrada"))
        for vm in vms:
            vm_dir = Path(vm.disk_path).parent if vm.disk_path else Path.home() / ".config/vhackintosh/vms" / vm.id
            result = GenSMBIOS.inspect_vm(vm, vm_dir / "OpenCore.qcow2")
            if result["ok"]:
                console.print(f"[green]{vm.name}: {i18n.t('smbios.diag.match')}[/green]")
            elif result.get("reason") == "identity fields missing or differ; Apple activation remains unverified":
                console.print(f"[yellow]{vm.name}: {i18n.t('smbios.diag.mismatch')} {result['reason']}[/yellow]")
            else:
                console.print(f"[yellow]{vm.name}: {i18n.t('smbios.diag.unavailable', reason=result['reason'])}[/yellow]")
        console.print(i18n.t("smbios.diag.icloud"))
        Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))

    def _view_hardware_diagnostics(self, interactive: bool = True) -> None:
        if interactive and sys.stdin.isatty():
            console.clear()
        console.print(centered_banner(console))

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
        console.clear()
        console.print(centered_banner(console))
        console.print(Panel(
            i18n.t("tui.update.title"),
            border_style="cyan",
        ))
        from core.updater import SystemUpdater
        SystemUpdater.run_vhackintosh_update()
        Prompt.ask(i18n.t("tui.prompt.enter_continue_nl"))

    def _menu_download_images(self) -> None:
        console.clear()
        console.print(centered_banner(console))
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

    def _set_auto_start(self, vm: VMConfig, enabled: bool) -> None:
        """Route appliance catalog writes through its sole catalog agent."""
        if agent_is_required():
            if not self._catalog_revision:
                self._list_managed_vms()
            result = call_agent("vms.autostart.set", {
                "vm_id": vm.id,
                "enabled": enabled,
                "expected_revision": self._catalog_revision,
            })
            if (not isinstance(result, dict) or result.get("enabled") is not enabled
                    or not isinstance(result.get("revision"), str)):
                raise WebAgentUnavailable("O agente não confirmou a alteração do auto-start.")
            self._catalog_revision = result["revision"]
            vm.auto_start = enabled
            self._agent_vm_snapshots[vm.id] = VMConfig.from_dict(vm.to_dict())
            return
        # A source checkout may run without systemd or the appliance agent.
        # Keep this developer-only path useful while retaining catalog CAS.
        self.store.set_auto_start(vm.id, enabled, expected_revision=self.store.revision)
        self.store.refresh()

    def _launch_vm(self, vm: VMConfig, is_installation: bool = False) -> None:
        """Render the shared lifecycle service in the terminal interface."""
        console.clear()
        console.print(centered_banner(console))
        if agent_is_required():
            try:
                accepted = self._send_agent_vm_action(vm, "start")
                console.print(f"Solicitação enviada ao agente (operação {accepted['job_id']}).")
            except WebAgentUnavailable as exc:
                console.print(Panel(str(exc), title="Inicialização não executada", border_style="red"))
            if sys.stdin.isatty():
                Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
            return
        result = self.lifecycle_service.start(
            vm,
            force_installation=is_installation,
            on_event=self._render_launch_event,
        )
        if result.state == "completed":
            if sys.stdin.isatty():
                Prompt.ask(i18n.t("tui.prompt.enter_return_manager"))
            return

        code = result.error_code or "launch_failed"
        if code == "admission_failed":
            error = result.details or {}
            if error.get("reason") == "already_running":
                console.print(i18n.t("tui.launch.vm_already_running", name=vm.name))
            elif "requested_gb" not in error:
                console.print(i18n.t(
                    "tui.launch.memory_check_failed",
                    error=error.get("reason", "erro inesperado"),
                ))
            else:
                console.print(Panel(
                    i18n.t(
                        "tui.launch.insufficient_memory",
                        requested=error.get("requested_gb", vm.ram_gb),
                        available=error.get("available_gb", 0),
                        reserve=error.get("host_reserve_gb", 0),
                        active=", ".join(error.get("active_vms", [])) or "nenhuma",
                    ),
                    border_style="red",
                ))
            Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
            return

        if code == "vulkan_blocked":
            console.print(Panel(
                i18n.t("tui.launch.vulkan_blocked", diag=result.message or ""),
                border_style="red",
            ))
            if sys.stdin.isatty():
                Prompt.ask(i18n.t("tui.prompt.enter_return"))
            return
        if code == "release_unavailable":
            console.print(i18n.t("tui.launch.no_qemu"))
            Prompt.ask(i18n.t("tui.prompt.enter_continue"))
            return
        if code == "runtime_unavailable":
            console.print(Panel(
                i18n.t("tui.launch.rail_unavailable", reason=result.message or ""),
                border_style="red",
            ))
        elif code == "gpu_selection_failed":
            console.print(Panel(
                i18n.t("tui.launch.gpu_selection_failed", error=result.message or ""),
                border_style="red",
            ))
        elif code == "ctl_missing":
            console.print(Panel(i18n.t("tui.launch.ctl_missing"), border_style="red"))
        else:
            console.print(Panel(result.message or "Falha ao iniciar a VM.", border_style="red"))

        interaction = (result.details or {}).get("interaction")
        if interaction == "continue":
            Prompt.ask(i18n.t("tui.prompt.enter_continue"))
        elif interaction == "continue_short":
            Prompt.ask(i18n.t("tui.prompt.enter_continue_short"))
        elif interaction == "return" and sys.stdin.isatty():
            Prompt.ask(i18n.t("tui.prompt.enter_return"))

    @staticmethod
    def _render_launch_event(name: str, payload: dict) -> None:
        if name == "starting":
            console.print(i18n.t(
                "tui.launch.starting",
                name=payload["name"],
                version=payload["macos_version"].capitalize(),
            ))
        elif name == "storage_recovered":
            console.print(f"[green]✔ {payload['message']}[/green]")
        elif name == "install_policy":
            phase = normalize_phase(payload["phase"])
            console.print(i18n.t("tui.launch.install_state", phase=phase_label(phase)))
            console.print(f"[dim]{payload['reason']}[/dim]")
            if payload["is_installing"]:
                console.print(i18n.t("tui.launch.reboots_absorbed"))
        elif name == "no_graphical_session":
            console.print(i18n.t("tui.launch.no_waysession"))
        elif name == "relaunching":
            step = payload["step"]
            if step < 0:
                console.print(i18n.t("tui.launch.too_many_reboots"))
            else:
                console.print(i18n.t("tui.launch.relaunching", step=step))
        elif name == "finished":
            console.print(i18n.t("tui.launch.finished"))

    @staticmethod
    def _send_agent_vm_action(
        vm: VMConfig, operation: str, *, confirmation: str | None = None,
    ) -> dict:
        """Submit a fixed VM operation; never tunnel to QEMU or fall back."""
        result = call_agent("vms.action", {
            "vm_id": vm.id,
            "action": {
                "operation": operation,
                "expected_revision": None,
                "confirmation": confirmation,
                "idempotency_key": uuid.uuid4().hex,
            },
        })
        if not isinstance(result, dict) or not isinstance(result.get("job_id"), str):
            raise WebAgentUnavailable(
                "O agente retornou uma resposta inválida para a operação de VM; nenhuma ação local foi tentada."
            )
        return result


    def _apply_host_power_policy(self, vm: VMConfig, policy: LaunchPolicy, monitor) -> None:
        """
        Contabiliza reboots do guest e aplica (ou bloqueia) a sincronização de
        energia do host.

        GARANTIA: durante a instalação o computador físico NUNCA é
        reiniciado/desligado — era exatamente isso que interrompia a instalação.
        """
        if getattr(monitor, "reset_count", 0):
            try:
                self._patch_managed_vm(vm, {
                    "install_reboots": int(getattr(vm, "install_reboots", 0)) + monitor.reset_count,
                })
            except WebAgentUnavailable as exc:
                console.print(Panel(str(exc), title="Estado da VM não salvo", border_style="red"))
                return

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
        evidence = classify_evidence(probe).value

        if not probe.looks_like_complete_install:
            try:
                self._patch_managed_vm(vm, {
                    "install_evidence": evidence,
                    "install_phase": InstallPhase.INSTALLING.value,
                })
            except WebAgentUnavailable as exc:
                console.print(Panel(str(exc), title="Fase não salva", border_style="red"))
                return
            console.print(i18n.t("tui.post.not_complete"))
            console.print(i18n.t("tui.post.sync_stays_off"))
            return

        console.print(i18n.t("tui.post.detected_installed"))
        if not sys.stdin.isatty():
            try:
                self._patch_managed_vm(vm, {
                    "install_evidence": evidence,
                    "install_phase": InstallPhase.INSTALLING.value,
                })
            except WebAgentUnavailable as exc:
                console.print(Panel(str(exc), title="Fase não salva", border_style="red"))
            return

        if Confirm.ask(i18n.t("tui.post.confirm_complete"), default=True):
            try:
                self._patch_managed_vm(vm, {
                    "install_evidence": evidence,
                    "install_phase": InstallPhase.INSTALLED.value,
                    "install_reboots": int(getattr(vm, "install_reboots", 0)),
                })
            except WebAgentUnavailable as exc:
                console.print(Panel(str(exc), title="Fase não salva", border_style="red"))
                return
            console.print(i18n.t("tui.post.sync_enabled"))
        else:
            try:
                self._patch_managed_vm(vm, {
                    "install_evidence": evidence,
                    "install_phase": InstallPhase.INSTALLING.value,
                })
            except WebAgentUnavailable as exc:
                console.print(Panel(str(exc), title="Fase não salva", border_style="red"))
                return
            console.print(i18n.t("tui.post.kept_installing"))

    def _apply_gpu_environment(self, vm: VMConfig, env_vars: dict) -> None:
        """Nada a fazer: o ICD da GPU é do `reims-ctl`, não deste lugar.

        Esta função já escreveu `VK_DRIVER_FILES` escolhendo por *nome de
        fabricante* (`"NVIDIA" in gpu_name_upper`) e procurando os manifestos
        em `intel_icd.x86_64.json` / `radeon_icd.x86_64.json` — sufixo de
        outra distribuição, que não existe nem aqui nem no appliance. Dois
        defeitos de uma vez: a Intel e a AMD nunca recebiam ICD nenhum, e o
        critério era o fabricante em vez da placa.

        E não faltava só isso: a variável era escrita *aqui* e *também* no
        script do QEMU que o `reims-ctl` monta (a seção `[gpu]` do vm.toml
        vira `VK_DRIVER_FILES`/`VK_ICD_FILENAMES` no `exec`, ver
        `crates/reims-ctl/src/qemu.rs`). Duas fontes para o mesmo fato: quando
        concordavam ninguém notava, e quando discordavam quem decidia era o
        `reims-ctl` — que lê a escolha medida no Vulkan, não o nome da placa.

        As demais variáveis que saíram daqui eram inócuas ou pior:
        `__NV_PRIME_RENDER_OFFLOAD`, `__GLX_VENDOR_LIBRARY_NAME` e
        `__VK_LAYER_NV_optimus` são do mundo GLX/X11, e o reims-vgpu fala
        Vulkan direto; `DRI_PRIME` é do Mesa; e `REIMS_VGPU_PCI_SLOT` não é
        lida por ninguém, nem no reims-vgpu nem aqui.

        O que sobra é o que sempre bastou: `write_vm_toml` grava a escolha na
        seção `[gpu]`, e o `reims-ctl start` a aplica ao QEMU. Este método
        fica existindo para o chamador não precisar saber disso.

        `REIMS_VGPU_DISPLAY` também não é gravada aqui, de propósito: quem lê
        essa variável é só o boot-x86.sh, e ele já tem por default exatamente
        o valor que este lugar costumava escrever (gtk,show-menubar=off,
        zoom-to-fit=on) — gravá-lo aqui tinha um único efeito real: impedir o
        `${REIMS_VGPU_DISPLAY:-none}` do caso em que a janela do reims-vgpu
        apresenta o frame. O resultado era `-display gtk` E a janela winit na
        mesma boot.
        """

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

    def _ensure_reims_release(self, reims_dir: Optional[str]) -> bool:
        """Instala e valida o binário da release; o appliance não compila fontes."""
        state = evaluate_reims_release(explicit=reims_dir)
        if state.reims_dir is None:
            return False
        # O QEMU pode já existir dentro de /opt/reims-vgpu na ISO. Isso não
        # significa que o bundle já foi instalado no prefixo de runtime:
        # install_prebuilt também copia o reims-ctl para ~/.cargo/bin, que é o
        # executável efetivamente usado para iniciar a VM. A função é
        # idempotente, então chamá-la sempre cobre ambos sem recópias inúteis.
        with console.status(i18n.t("tui.release.installing")):
            if not install_prebuilt(state.reims_dir):
                return False
        state = evaluate_reims_release(explicit=str(state.reims_dir))
        return bool(state.binary_path and state.binary_path.exists())

    def _menu_reims_release(self) -> None:
        """Item "A": seleciona qualquer release estável disponível do fork.

        É o caminho pré-compilado: a ISO nasce com o binário de uma release e
        aqui o usuário pode escolher uma versão nova ou voltar para uma anterior.
        Os diretórios de QEMU por tag são mantidos, e só o link atual muda.

        O download cai no diretório do reims-vgpu e, em seguida, `install_prebuilt`
        copia a árvore para o prefixo que a VM executa
        (`$XDG_DATA_HOME/reims-ctl/qemu/<tag>` + o symlink `current`), junto com o
        motor `reims-ctl` do bundle; a VM passa a usar a release instalada.

        Nada aqui é fatal: falha de rede, API do GitHub fora do ar ou release sem
        o asset apenas avisam.
        """
        # A release é baixada para o checkout em /opt/reims-vgpu e depois
        # instalada no prefixo que a VM executa. Em instalações appliance,
        # ambos pertencem ao root; verifique antes de consultar/baixar para
        # evitar uma falha de permissão no meio da troca.
        if os.geteuid() != 0:
            console.print(i18n.t("tui.release.not_root"))
            return

        try:
            from core.reims_release import (
                DEFAULT_REIMS_DIR,
                REIMS_REPO,
                ReleaseError,
                current_release,
                list_releases,
                update_from_release,
                vulkan_runtime_ok,
            )
        except ImportError:  # appliance sem o módulo (ISO antiga)
            console.print(
                i18n.t("tui.release.check_failed", error="core.reims_release ausente")
            )
            return

        # `find_reims_dir` reconhece o payload pré-compilado que a ISO instala
        # em /opt/reims-vgpu. O fallback só cobre o caso legítimo de "ainda
        # não existe nada" —
        # e é para lá que a instalação grava. Ele NÃO pode ser a única coisa
        # que faz o menu enxergar uma instalação: enquanto era, o menu dizia
        # "instalado: v1.2.0" e o lançamento da VM dizia "sem diretório do
        # reims-vgpu" na mesma sessão, porque só o menu tinha o fallback.
        reims_dir = find_reims_dir() or DEFAULT_REIMS_DIR

        console.clear()
        console.print(centered_banner(console))
        console.print(i18n.t("tui.release.menu_title"))
        console.print(i18n.t("tui.release.menu_desc", repo=REIMS_REPO))

        try:
            installed = current_release(reims_dir) or i18n.t("tui.release.unknown")

            ok, message = vulkan_runtime_ok()
            if not ok:
                # Atualizar o binário não adianta se o host não tem Vulkan
                # utilizável — mas o aviso não impede a troca: o motivo de estar
                # aqui pode ser justamente testar uma release mais nova.
                console.print(f"[yellow]{message}[/yellow]")

            console.print()
            if not Confirm.ask(
                i18n.t("tui.release.confirm_check", installed=installed), default=False
            ):
                console.print(i18n.t("tui.release.skipped"))
                return

            try:
                with console.status(i18n.t("tui.release.checking")):
                    releases = list_releases()
            except ReleaseError as exc:
                console.print(i18n.t("tui.release.check_failed", error=str(exc)))
                return

            if not releases:
                console.print(i18n.t("tui.release.none"))
                return

            console.print()
            for number, release in enumerate(releases, 1):
                current_marker = (
                    f" [green]{i18n.t('tui.release.current_marker')}[/green]"
                    if release.tag == installed else ""
                )
                published = release.published_at[:10] if release.published_at else ""
                size = f"{release.size_mb:.1f} MB" if release.asset_size else ""
                metadata = " · ".join(part for part in (published, size) if part)
                suffix = f" [dim]({metadata})[/dim]" if metadata else ""
                console.print(
                    f" [bold cyan]{number}[/bold cyan] - [bold]{release.tag}[/bold]"
                    f"{suffix}{current_marker}"
                )
            choices = [str(i) for i in range(0, len(releases) + 1)]
            selected_number = Prompt.ask(
                i18n.t("tui.release.select_prompt"),
                choices=choices,
                default="0",
            )
            if selected_number == "0":
                console.print(i18n.t("tui.release.skipped"))
                return
            selected = releases[int(selected_number) - 1]
            if selected.tag == installed:
                console.print(i18n.t("tui.release.up_to_date", tag=installed))
                return

            console.print(Panel(
                i18n.t("tui.release.available", installed=installed, tag=selected.tag),
                border_style="cyan",
            ))
            if not Confirm.ask(
                i18n.t("tui.release.confirm_update", tag=selected.tag), default=True
            ):
                console.print(i18n.t("tui.release.declined"))
                return

            try:
                with console.status(i18n.t("tui.release.downloading", tag=selected.tag)):
                    result = update_from_release(reims_dir=reims_dir, tag=selected.tag)
            except ReleaseError as exc:
                console.print(i18n.t("tui.release.update_failed", error=str(exc)))
                return

            if not result.ok:
                console.print(i18n.t("tui.release.update_failed", error=result.message))
                return
            if not result.changed:
                console.print(i18n.t("tui.release.up_to_date", tag=installed))
                return

            console.print(i18n.t("tui.release.updated", tag=selected.tag))

            # Levar a árvore baixada para o prefixo que a VM executa, com o motor.
            with console.status(i18n.t("tui.release.installing")):
                pronto = install_prebuilt(reims_dir)
            if not pronto:
                console.print(i18n.t("tui.release.install_failed"))
                return

            console.print(i18n.t("tui.release.installed_ok", tag=selected.tag))
            console.print(i18n.t("tui.release.restart_hint"))
            if is_live_media():
                console.print(i18n.t("tui.release.session_only"))
        finally:
            # Pausa no estilo da casa: o laço principal limpa a tela na volta.
            Prompt.ask(i18n.t("tui.prompt.enter_continue_nl"))

    def _menu_control_release(self) -> None:
        """Install a published native Control package from the terminal."""
        console.clear()
        console.print(centered_banner(console))
        console.print(i18n.t("tui.control_release.menu_title"))
        wait_for_continue = True
        try:
            from core.control_release import (
                CONTROL_REPO,
                ControlReleaseError,
                current_version,
                is_current_release,
                install_release,
                list_releases,
                package_architecture,
                package_kind,
            )
            if os.geteuid() != 0:
                sudo = shutil.which("sudo")
                if not sudo:
                    console.print(i18n.t("tui.control_release.not_root"))
                    return
                root = Path(__file__).resolve().parent.parent
                wait_for_continue = False
                subprocess.run(
                    [sudo, "--", sys.executable, str(root / "cli" / "main.py"), "update-control"],
                    check=False,
                )
                return
            installed = current_version()
            console.print(i18n.t(
                "tui.control_release.menu_desc",
                repo=CONTROL_REPO,
                installed=installed,
                package=package_kind(),
                architecture=package_architecture(),
            ))
            with console.status(i18n.t("tui.control_release.checking")):
                releases = list_releases()
            if not releases:
                console.print(i18n.t("tui.control_release.none"))
                return
            for number, release in enumerate(releases, 1):
                marker = f" [green]{i18n.t('tui.release.current_marker')}[/green]" if is_current_release(release.tag, installed) else ""
                suffix = " · pré-lançamento" if release.prerelease else ""
                console.print(f" [bold cyan]{number}[/bold cyan] - [bold]{release.tag}[/bold]{suffix}{marker}")
            selected_number = Prompt.ask(
                i18n.t("tui.control_release.select_prompt"),
                choices=[str(index) for index in range(0, len(releases) + 1)],
                default="0",
            )
            if selected_number == "0":
                return
            selected = releases[int(selected_number) - 1]
            if not Confirm.ask(i18n.t("tui.control_release.confirm_update", tag=selected.tag), default=True):
                return
            try:
                with console.status(i18n.t("tui.control_release.installing", tag=selected.tag)):
                    result = install_release(selected, log=lambda message: console.print(f"[dim]{message}[/dim]"))
            except ControlReleaseError as exc:
                console.print(i18n.t("tui.control_release.failed", error=str(exc)))
                return
            restart = subprocess.run(
                ["systemctl", "restart", "vhackintosh-agent.service", "vhackintosh-web.service"],
                capture_output=True, text=True, check=False, timeout=45,
            )
            if restart.returncode:
                detail = (restart.stderr or restart.stdout).strip()[:512]
                console.print(i18n.t("tui.control_release.restart_failed", error=detail or "systemctl falhou"))
                return
            console.print(i18n.t("tui.control_release.updated", tag=result.tag))
        except ControlReleaseError as exc:
            console.print(i18n.t("tui.control_release.failed", error=str(exc)))
        finally:
            if wait_for_continue:
                Prompt.ask(i18n.t("tui.prompt.enter_continue_nl"))

    def _menu_build_iso(self) -> None:
        console.clear()
        console.print(centered_banner(console))
        console.print(i18n.t("tui.iso.title"))
        console.print(i18n.t("tui.iso.desc1"))
        console.print(i18n.t("tui.iso.desc2"))
        console.print(i18n.t("tui.iso.features"))
        console.print(i18n.t("tui.iso.feature.kernel"))
        console.print(i18n.t("tui.iso.feature.graphics"))
        console.print(i18n.t("tui.iso.feature.audio"))
        console.print(i18n.t("tui.iso.feature.autologin"))
        console.print(i18n.t("tui.iso.feature.grub"))

        builder_script = Path(__file__).resolve().parent.parent / "appliance" / "build-iso.sh"
        if not shutil.which("mkarchiso"):
            console.print(i18n.t("tui.iso.mkarchiso_missing"))
            console.print(i18n.t("tui.iso.mkarchiso_hint"))
            console.print("  [bold green]sudo pacman -S --needed archiso[/bold green]\n")

        console.print(i18n.t("tui.iso.build_script", path=builder_script))
        console.print(i18n.t("tui.iso.opt.instructions"))
        console.print(i18n.t("tui.vm.actions.back"))

        choice = Prompt.ask(i18n.t("tui.menu.prompt_choice"), choices=["1", "0"], default="0")
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
