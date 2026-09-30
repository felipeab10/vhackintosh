"""
vHackintosh — Host Power Synchronization & Kiosk Enforcer
Sincroniza o desligamento e reinicialização do macOS com o computador físico (Host Linux).
"""

from __future__ import annotations
import os
import sys
import subprocess
from rich.console import Console

console = Console()


class PowerSync:
    """Gerencia a sincronização de energia entre a VM macOS e o Host Linux."""

    @classmethod
    def sync_host_shutdown(cls, is_kiosk_mode: bool = True) -> None:
        if not is_kiosk_mode:
            console.print("[yellow]VM desligada. Modo Kiosk desativado, retornando ao gerenciador.[/yellow]")
            return

        console.print("[bold red]⏻ macOS solicitou desligamento. Desligando o host em 2 segundos...[/bold red]")
        try:
            subprocess.run(["systemctl", "poweroff"], check=False)
        except Exception:
            try:
                subprocess.run(["poweroff"], check=False)
            except Exception:
                pass

    @classmethod
    def sync_host_reboot(cls, is_kiosk_mode: bool = True) -> None:
        if not is_kiosk_mode:
            console.print("[yellow]VM reiniciada. Modo Kiosk desativado, retornando ao gerenciador.[/yellow]")
            return

        console.print("[bold cyan]🔄 macOS solicitou reinicialização. Reiniciando o host em 2 segundos...[/bold cyan]")
        try:
            subprocess.run(["systemctl", "reboot"], check=False)
        except Exception:
            try:
                subprocess.run(["reboot"], check=False)
            except Exception:
                pass
