"""
vHackintosh — Teste A/B de Instalação do Zero
Permite provisionar uma VM temporária ou de teste limpa e iniciar o processo
de boot do instalador oficial do macOS com opções comparativas A/B de áudio e vídeo.
"""

from __future__ import annotations
import os
import sys
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt

from core.config import VMConfig, SMBIOSConfig, ExclusiveVMLock, VMManagerStore
from core.smbios import GenSMBIOS
from core.disk import DiskProvisioner
from core.downloader import MacOSDownloader, MACOS_PRODUCTS
from ui.banner import render_banner

from core import i18n

console = Console()


class ABInstallerTest:
    """Orquestrador do teste A/B de instalação do zero."""

    @classmethod
    def run_interactive(cls) -> None:
        console.clear()
        console.print(render_banner())
        console.print(i18n.t("installer.title"))

        # Validação antecipada de hardware
        from core.readiness import SystemReadinessChecker
        readiness = SystemReadinessChecker.check_all()
        if not readiness.can_run_vms:
            crit_msgs = []
            for c in readiness.checks:
                if not c.passed and c.is_critical:
                    crit_msgs.append(i18n.t("installer.crit_check", name=c.name, details=c.details))

            console.print(Panel(
                i18n.t("installer.aborted.title")
                + "\n".join(crit_msgs) + "\n\n"
                + i18n.t("installer.aborted.body"),
                title=i18n.t("installer.hardware_incompatible"),
                border_style="red"
            ))
            Prompt.ask(i18n.t("installer.prompt.press_enter_back"))
            return

        console.print(i18n.t("installer.intro.wizard"))
        console.print(i18n.t("installer.intro.installer"))
        console.print(i18n.t("installer.intro.audio"))

        # 1. Escolha da versão
        console.print(i18n.t("installer.step.version"))
        console.print(i18n.t("installer.option.sequoia"))
        console.print(i18n.t("installer.option.tahoe"))
        console.print(i18n.t("installer.option.ventura"))
        v_opt = Prompt.ask(i18n.t("installer.prompt.version"), choices=["1", "2", "3"], default="1")
        v_map = {"1": "sequoia", "2": "tahoe", "3": "ventura"}
        macos_version = v_map[v_opt]

        # 2. Imagem de Instalação/Recuperação da Apple (Automático)
        console.print(i18n.t("installer.step.image", version=macos_version.capitalize()))
        installer_img = MacOSDownloader.get_cached_image_path(macos_version)
        if installer_img:
            console.print(i18n.t("installer.image.cached", name=installer_img.name))
        else:
            console.print(i18n.t("installer.image.downloading"))
            installer_img = MacOSDownloader.prepare_installer(macos_version)

        if not installer_img:
            console.print(i18n.t("installer.image.error"))
            Prompt.ask(i18n.t("installer.prompt.exit"))
            return

        # 3. Tamanho do Disco Virtual
        console.print(i18n.t("installer.step.disk"))
        disk_size_gb = IntPrompt.ask(i18n.t("installer.disk.size_gb"), default=64)

        # 4. Modo de visualização
        console.print(i18n.t("installer.step.display"))
        console.print(i18n.t("installer.option.fullscreen"))
        console.print(i18n.t("installer.option.windowed"))
        disp_choice = Prompt.ask(i18n.t("installer.prompt.mode"), choices=["1", "2"], default="1")
        fullscreen = "1" if disp_choice == "1" else "0"

        # Áudio corrigido definitivo (ich9-intel-hda)
        audio_device = "ich9-intel-hda"

        # 5. Provisionamento da VM de teste (limpeza automática)
        test_vm_id = f"test-ab-{macos_version}-{os.getpid()}"
        test_vm_dir = Path(os.path.expanduser("~/.config/vhackintosh/vms")) / test_vm_id
        if test_vm_dir.exists():
            import shutil
            try:
                shutil.rmtree(test_vm_dir)
            except Exception as e:
                console.print(i18n.t("installer.warn.clean_old_dir", error=e))

        console.print(i18n.t("installer.provision", size=disk_size_gb, vm_id=test_vm_id))
        try:
            smbios = GenSMBIOS.generate(macos_version)
        except Exception as e:
            console.print(f"[bold red]Não foi possível gerar SMBIOS com macserial: {e}[/bold red]")
            Prompt.ask(i18n.t("installer.prompt.exit"))
            return
        vm = VMConfig(
            id=test_vm_id,
            name=i18n.t("installer.vm.name", version=macos_version.capitalize()),
            macos_version=macos_version,
            vcpus=12,
            ram_gb=8,
            disk_size_gb=disk_size_gb,
            audio_device=audio_device,
            smbios=smbios,
        )

        bundle = DiskProvisioner.provision(vm, installer_img=installer_img)

        # 6. Execução do Boot — delegada ao reims-ctl (árvore nova do
        # reims-vgpu): o appliance grava o vm.toml e registra a VM, o QEMU é
        # do upstream.
        from core import reims_ctl

        if not reims_ctl.reims_ctl_bin():
            console.print(i18n.t("installer.error.reims_missing"))
            Prompt.ask(i18n.t("installer.prompt.exit"))
            return

        lock = ExclusiveVMLock()
        if not lock.acquire(vm.name):
            console.print(i18n.t("installer.error.other_vm_running"))
            Prompt.ask(i18n.t("installer.prompt.back"))
            return

        try:
            console.print(i18n.t("installer.boot.test_vm"))
            console.print(i18n.t("installer.info.audio", device=audio_device))
            console.print(i18n.t("installer.info.screen", mode=i18n.t("installer.mode.fullscreen") if fullscreen == "1" else i18n.t("installer.mode.window")))
            console.print(i18n.t("installer.info.installer", image=installer_img))

            env = os.environ.copy()
            # O vm.toml carrega tudo o que o antigo bloco de env fazia: cpus/
            # RAM (do próprio VMConfig), áudio ligado, e o instalador da fase
            # (imagem absoluta, entrai em [q35].installer). O reboot do
            # instalador encerra o QEMU (extra_args -action reboot=shutdown).
            try:
                reims_ctl.write_vm_toml(
                    vm, installer=str(installer_img), borderless=(fullscreen == "1")
                )
            except reims_ctl.GPUSelectionError as exc:
                console.print(i18n.t("tui.launch.gpu_selection_failed", error=exc))
                Prompt.ask(i18n.t("installer.prompt.exit"))
                return
            reims_ctl.ensure_registered(vm)

            ctl = reims_ctl.reims_ctl_bin()
            cmd, sem_sessao = reims_ctl.session_argv([ctl, "start", vm.id], env)
            if sem_sessao:
                console.print(i18n.t("installer.warn.no_session"))
            reims_ctl.ReimsCtlSession(cmd, env=env).wait()
            console.print(i18n.t("installer.session.finished"))

            # Registra automaticamente a VM recém-instalada no vHackintosh
            try:
                from core.config import VMManagerStore
                store = VMManagerStore()
                vm.disk_path = str(bundle.hdd_path)
                store.add_vm(vm)
                console.print(i18n.t("installer.vm.registered", name=vm.name))
            except Exception as e:
                console.print(i18n.t("installer.warn.register_failed", error=e))
        finally:
            lock.release()

        Prompt.ask(i18n.t("installer.prompt.return"))
