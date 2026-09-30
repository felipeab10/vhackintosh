"""
vHackintosh — CLI & Main Entrypoint
"""

from __future__ import annotations
import sys
import os
from pathlib import Path

# Adiciona a raiz do repositório ao sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import VMManagerStore, VMConfig, ExclusiveVMLock
from ui.boot_delay import BootInterceptor
from ui.tui import VMTUI


def main():
    store = VMManagerStore()
    tui = VMTUI(store)

    # Se chamado com argumentos de linha de comando
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd in ["--help", "-h"]:
            print("Uso: vhackintosh [start <vm> | list | doctor | update]")
            return
        elif cmd == "list":
            tui._render_vm_table(store.list_vms())
            return
        elif cmd == "doctor":
            tui._view_hardware_diagnostics(interactive=False)
            return
        elif cmd == "update":
            tui._update_upstream()
            return
        elif cmd == "start" and len(sys.argv) > 2:
            vm_name = sys.argv[2]
            target_vm = None
            for vm in store.list_vms():
                if vm.name.lower() == vm_name.lower() or vm.id == vm_name:
                    target_vm = vm
                    break
            if target_vm:
                tui._launch_vm(target_vm)
            else:
                print(f"VM '{vm_name}' não encontrada.")
            return

    # Fluxo Padrão de Inicialização (Appliance Kiosk Boot):
    auto_vm = store.get_auto_start_vm()
    if auto_vm:
        interrupted = BootInterceptor.wait_for_key_or_timeout(auto_vm.name, seconds=5)
        if not interrupted:
            tui._launch_vm(auto_vm)
            return

    # Se não houver auto-start ou se o usuário pressionou ESPAÇO, abre a TUI
    tui.run_main_loop()


if __name__ == "__main__":
    main()
