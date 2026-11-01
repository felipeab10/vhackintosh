"""
vHackintosh — CLI & Main Entrypoint
"""

from __future__ import annotations
import sys
import os
from pathlib import Path

# Adiciona a raiz do repositório ao sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import VMManagerStore
from ui.boot_delay import BootInterceptor
from ui.tui import VMTUI
from core.web_client import agent_is_required, call_agent, list_agent_vms, WebAgentUnavailable


def main():
    store = VMManagerStore()
    tui = VMTUI(store)

    # Se chamado com argumentos de linha de comando
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd in ["--help", "-h"]:
            print("Uso: vhackintosh [start <vm> | list | doctor | update | test-install]")
            return
        elif cmd == "list":
            if agent_is_required():
                try:
                    vms = list_agent_vms()
                except WebAgentUnavailable as exc:
                    print(f"Não foi possível listar as VMs pelo serviço de gerenciamento: {exc}")
                    sys.exit(1)
            else:
                vms = store.list_vms()
            tui._render_vm_table(vms)
            return
        elif cmd == "doctor":
            tui._view_hardware_diagnostics(interactive=False)
            return
        elif cmd == "update":
            tui._update_upstream()
            return
        elif cmd in ["check", "check-hardware", "preflight"]:
            quiet = "-q" in sys.argv or "--quiet" in sys.argv
            from core.readiness import SystemReadinessChecker
            report = SystemReadinessChecker.check_all()
            if not report.can_run_vms:
                if not quiet:
                    print("ERRO: Hardware incompatível com o vHackintosh OS!")
                    for c in report.checks:
                        if not c.passed and c.is_critical:
                            print(f" • {c.name}: {c.details}")
                            if c.remediation:
                                print(f"   Solução: {c.remediation}")
                sys.exit(1)
            else:
                if not quiet:
                    print("OK: Hardware compatível com vHackintosh OS (KVM e Reims vGPU prontos).")
                sys.exit(0)
        elif cmd in ["check-vulkan"]:
            quiet = "-q" in sys.argv or "--quiet" in sys.argv
            from core.gpu import GPUChecker
            ok, msg = GPUChecker.check_vulkan_host_readiness()
            if not ok:
                if not quiet:
                    print(f"ERRO: {msg}")
                sys.exit(1)
            else:
                if not quiet:
                    print(f"OK: {msg}")
                sys.exit(0)
        elif cmd in ["test-install", "test"]:
            from core.installer_runner import ABInstallerTest
            ABInstallerTest.run_interactive()
            return
        elif cmd == "start" and len(sys.argv) > 2:
            vm_name = sys.argv[2]
            target_vm = None
            if agent_is_required():
                try:
                    vms = list_agent_vms()
                except WebAgentUnavailable as exc:
                    print(f"Não foi possível iniciar a VM pelo serviço de gerenciamento: {exc}")
                    return
            else:
                vms = store.list_vms()
            for vm in vms:
                if vm.name.lower() == vm_name.lower() or vm.id == vm_name:
                    target_vm = vm
                    break
            if target_vm:
                tui._launch_vm(target_vm)
            else:
                print(f"VM '{vm_name}' não encontrada.")
            return

    # Fluxo Padrão de Inicialização (Appliance Kiosk Boot):
    appliance_managed = agent_is_required()
    auto_vm = None if appliance_managed else store.get_auto_start_vm()
    if appliance_managed:
        try:
            target = call_agent("vms.autostart.get")
            if target is None:
                auto_vm = None
            elif isinstance(target, dict) and isinstance(target.get("id"), str) and target["id"]:
                auto_vm = next((vm for vm in list_agent_vms() if vm.id == target["id"]), None)
                if auto_vm is None:
                    print("O agente informou uma VM de auto-start que não consta no catálogo; autostart ignorado.")
            else:
                raise WebAgentUnavailable("O agente retornou um destino de auto-start inválido.")
        except (WebAgentUnavailable, KeyError, TypeError) as exc:
            # Never launch from a stale local snapshot when the appliance agent
            # is the catalog owner. Continue to the TUI with an actionable error.
            auto_vm = None
            print(f"Auto-start ignorado: {exc}")
    if auto_vm:
        interrupted = BootInterceptor.wait_for_key_or_timeout(auto_vm.name, seconds=5)
        if not interrupted:
            tui._launch_vm(auto_vm)
            return

    # Se não houver auto-start ou se o usuário pressionou ESPAÇO, abre a TUI
    tui.run_main_loop()


if __name__ == "__main__":
    main()
