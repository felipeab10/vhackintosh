"""
vHackintosh — Silent Builder with Rich Progress Bar
Gerencia a compilação de reims-vgpu, ROMs EFI e QEMU em segundo plano
com visual moderno de barra de progresso animada.
"""

from __future__ import annotations
import os
import sys
import subprocess
import time
from pathlib import Path
from typing import Optional, Callable
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.console import Console

from core.config import LOGS_DIR

console = Console()


class SilentBuilder:
    """Compila componentes em background mantendo a tela limpa com barras de progresso."""

    @classmethod
    def build_component(
        cls,
        name: str,
        cmd: list[str],
        cwd: str | Path,
        log_filename: str,
        expected_time_sec: float = 30.0,
    ) -> bool:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOGS_DIR / log_filename

        with open(log_file, "w", encoding="utf-8") as lf:
            with Progress(
                SpinnerColumn(spinner_name="dots"),
                TextColumn("[bold cyan]{task.description}[/bold cyan]"),
                BarColumn(bar_width=40),
                TextColumn("[bold green]{task.percentage:>3.0f}%[/bold green]"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task(f"Compilando {name}...", total=100)

                proc = subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    text=True,
                )

                start_time = time.time()
                while proc.poll() is None:
                    elapsed = time.time() - start_time
                    # Simula avanço suave da barra baseado no tempo esperado até 95%
                    pct = min(95.0, (elapsed / expected_time_sec) * 100.0)
                    progress.update(task, completed=pct)
                    time.sleep(0.1)

                if proc.returncode == 0:
                    progress.update(task, completed=100.0)
                    time.sleep(0.3)
                    return True
                else:
                    progress.update(task, description=f"[bold red]Falha ao compilar {name}![/bold red]")
                    return False

    @classmethod
    def build_reims_vgpu(cls, reims_repo_path: str | Path) -> bool:
        """Compila o reims-vgpu e a ROM UEFI GOP silenciosamente."""
        repo = Path(reims_repo_path)
        if not repo.exists():
            return False

        build_script = repo / "scripts" / "qemu-build" / "qemu-build.sh"
        if not build_script.exists():
            return False

        console.print(f"[bold green]▶ Preparando ambiente de aceleração gráfica reims-vgpu...[/bold green]")
        
        # 1. Compila ROM UEFI GOP se necessário
        rom_script = repo / "crates" / "reims-vgpu-efi" / "build.sh"
        if rom_script.exists():
            cls.build_component(
                name="UEFI GOP ROM (reims-vgpu)",
                cmd=["bash", str(rom_script)],
                cwd=repo / "crates" / "reims-vgpu-efi",
                log_filename="build-rom.log",
                expected_time_sec=10.0,
            )

        # 2. Compila QEMU + reims-vgpu staticlib
        return cls.build_component(
            name="reims-vgpu & QEMU Core",
            cmd=[str(build_script), "--target", "x86_64", "--backend", "vulkan"],
            cwd=repo,
            log_filename="build-qemu-reims.log",
            expected_time_sec=45.0,
        )
