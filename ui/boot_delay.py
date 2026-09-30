"""
vHackintosh — Boot Interceptor (5-Second Countdown)
Aguarda 5 segundos antes de iniciar a VM padrão.
Se a tecla [ESPAÇO] for pressionada, redireciona para o Gerenciador de VMs.
"""

from __future__ import annotations
import sys
import select
import termios
import tty
import time
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.align import Align
from rich.text import Text

console = Console()


class BootInterceptor:
    """Interrompe o auto-start se a tecla ESPAÇO for pressionada."""

    @classmethod
    def wait_for_key_or_timeout(cls, vm_name: str, seconds: int = 5) -> bool:
        """
        Retorna:
          True: Se o usuário pressionou ESPAÇO (abrir menu).
          False: Se o tempo esgotou (prosseguir com o boot da VM).
        """
        # Salva configurações do terminal para leitura sem bloqueio
        old_settings = None
        try:
            old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except Exception:
            pass

        interrupted = False
        start_time = time.time()

        try:
            with Live(console=console, refresh_per_second=10) as live:
                while True:
                    elapsed = time.time() - start_time
                    remaining = max(0, seconds - int(elapsed))

                    msg = Text()
                    msg.append("⚡ Inicialização Automática vHackintosh\n\n", style="bold yellow")
                    msg.append(f"Iniciando ", style="white")
                    msg.append(f"[{vm_name}]", style="bold green")
                    msg.append(f" em ", style="white")
                    msg.append(f"{remaining}s", style="bold cyan")
                    msg.append("...\n\n", style="white")
                    msg.append("Pressione ", style="dim")
                    msg.append("[ BARRA DE ESPAÇO ]", style="bold black on white")
                    msg.append(" para abrir o Gerenciador de VMs", style="dim")

                    panel = Panel(
                        Align.center(msg),
                        title="[bold cyan]vHackintosh Kiosk Boot[/bold cyan]",
                        border_style="cyan",
                        padding=(1, 2),
                    )
                    live.update(panel)

                    if remaining <= 0:
                        break

                    # Verifica entrada no stdin com timeout curto
                    r, _, _ = select.select([sys.stdin], [], [], 0.1)
                    if r:
                        key = sys.stdin.read(1)
                        if key == " ":
                            interrupted = True
                            break

        finally:
            if old_settings:
                try:
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                except Exception:
                    pass

        return interrupted
