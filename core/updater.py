"""
vHackintosh — System & Reims vGPU Auto-Updater
Verifica e atualiza os binários pré-compilados do Reims vGPU via GitHub Releases
e sincroniza o repositório do vHackintosh com barra de progresso visual.
"""

from __future__ import annotations
import os
import sys
import json
import shutil
import tarfile
import urllib.request
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn

console = Console()

REIMS_REPO = "felipeab10/reims-vgpu"
VHACK_REPO = "felipeab10/vhackintosh"
DEFAULT_REIMS_DIR = Path("/opt/reims-vgpu")
FALLBACK_REIMS_DIR = Path("/home/felipeab10/reims-vgpu")


class SystemUpdater:
    """Gerencia atualizações de binários do Reims vGPU e código do vHackintosh."""

    @classmethod
    def get_target_reims_dir(cls) -> Path:
        if DEFAULT_REIMS_DIR.exists():
            return DEFAULT_REIMS_DIR
        return FALLBACK_REIMS_DIR

    @classmethod
    def get_installed_reims_version(cls) -> str:
        target_dir = cls.get_target_reims_dir()
        version_file = target_dir / ".version"
        if version_file.exists():
            try:
                return version_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        return "desconhecida (build local)"

    @classmethod
    def check_latest_reims_release(cls) -> Optional[Dict[str, Any]]:
        """Consulta a API do GitHub para obter a release mais recente de felipeab10/reims-vgpu."""
        api_url = f"https://api.github.com/repos/{REIMS_REPO}/releases/latest"
        req = urllib.request.Request(
            api_url,
            headers={
                "User-Agent": "vHackintosh-Updater/1.0",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    return data
        except Exception as e:
            console.print(f"[dim yellow]Aviso ao verificar releases no GitHub: {e}[/dim yellow]")
        return None

    @classmethod
    def download_with_progress(cls, url: str, dest_path: Path, desc: str = "Baixando atualização...") -> bool:
        """Faz download com barra de progresso do Rich."""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "vHackintosh-Updater/1.0"})
            with urllib.request.urlopen(req, timeout=30) as response:
                total_size = int(response.headers.get("content-length", 0))

                with Progress(
                    SpinnerColumn(),
                    TextColumn("[bold cyan]{task.description}[/bold cyan]"),
                    BarColumn(bar_width=40),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                    TimeRemainingColumn(),
                    console=console,
                ) as progress:
                    task = progress.add_task(desc, total=total_size or None)

                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(dest_path, "wb") as out_file:
                        while True:
                            chunk = response.read(64 * 1024)
                            if not chunk:
                                break
                            out_file.write(chunk)
                            progress.update(task, advance=len(chunk))
            return True
        except Exception as e:
            console.print(f"[bold red]Falha no download:[/bold red] {e}")
            if dest_path.exists():
                dest_path.unlink()
            return False

    @classmethod
    def update_reims_vgpu(cls, force: bool = False) -> bool:
        """Baixa e instala a última versão de binários do Reims vGPU."""
        console.print(f"\n[bold cyan]▶ Verificando atualizações de binários do Reims vGPU ({REIMS_REPO})...[/bold cyan]")
        current_version = cls.get_installed_reims_version()
        console.print(f"Versão atualmente instalada: [bold white]{current_version}[/bold white]")

        release = cls.check_latest_reims_release()
        if not release:
            console.print("[yellow]Nenhuma release encontrada ou sem conexão com a internet.[/yellow]")
            return False

        latest_tag = release.get("tag_name", "")
        console.print(f"Última versão disponível no GitHub: [bold green]{latest_tag}[/bold green]")

        if latest_tag == current_version and not force:
            console.print("[bold green]✔ O Reims vGPU já está na versão mais recente![/bold green]")
            return True

        # Localiza o asset .tar.gz
        download_url = None
        asset_name = ""
        for asset in release.get("assets", []):
            name = asset.get("name", "")
            if name.endswith(".tar.gz") and "linux" in name.lower():
                download_url = asset.get("browser_download_url")
                asset_name = name
                break

        if not download_url:
            # Fallback para qualquer .tar.gz
            for asset in release.get("assets", []):
                name = asset.get("name", "")
                if name.endswith(".tar.gz"):
                    download_url = asset.get("browser_download_url")
                    asset_name = name
                    break

        if not download_url:
            console.print("[bold red]Nenhum pacote binário (.tar.gz) encontrado na release mais recente.[/bold red]")
            return False

        target_dir = cls.get_target_reims_dir()
        temp_tar = Path(f"/tmp/{asset_name}")

        console.print(f"\n[cyan]Baixando pacote binário pré-compilado ({asset_name})...[/cyan]")
        ok = cls.download_with_progress(download_url, temp_tar, desc=f"Reims vGPU {latest_tag}")
        if not ok:
            return False

        console.print(f"[cyan]Extraindo componentes em {target_dir}...[/cyan]")
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(temp_tar, "r:gz") as tar:
                tar.extractall(path=target_dir)

            # Grava tag da versão instalada
            (target_dir / ".version").write_text(latest_tag, encoding="utf-8")

            # Permissões de execução
            qemu_bin = target_dir / "vendor" / "qemu" / "build" / "qemu-system-x86_64"
            if qemu_bin.exists():
                os.chmod(qemu_bin, 0o755)

            boot_script = target_dir / "vm" / "boot-x86.sh"
            if boot_script.exists():
                os.chmod(boot_script, 0o755)

            console.print(f"[bold green]✔ Reims vGPU atualizado com sucesso para {latest_tag}![/bold green]")
            return True
        except Exception as e:
            console.print(f"[bold red]Erro ao descompactar atualização:[/bold red] {e}")
            return False
        finally:
            if temp_tar.exists():
                try:
                    temp_tar.unlink()
                except Exception:
                    pass

    @classmethod
    def update_vhackintosh_repo(cls) -> bool:
        """Atualiza o código do próprio vHackintosh via git pull."""
        console.print(f"\n[bold cyan]▶ Sincronizando repositório do vHackintosh...[/bold cyan]")
        app_dir = Path("/opt/vhackintosh")
        if not app_dir.exists():
            app_dir = Path(__file__).resolve().parent.parent

        if not (app_dir / ".git").exists():
            console.print("[dim]Diretório não é um repositório Git ativo, pulando git pull.[/dim]")
            return False

        try:
            res = subprocess.run(["git", "pull", "--ff-only"], cwd=app_dir, capture_output=True, text=True)
            if res.returncode == 0:
                console.print(f"[bold green]✔ vHackintosh atualizado:[/bold green]\n{res.stdout.strip()}")
                return True
            else:
                console.print(f"[yellow]Aviso no git pull:[/yellow] {res.stderr.strip()}")
                return False
        except Exception as e:
            console.print(f"[yellow]Não foi possível executar git pull:[/yellow] {e}")
            return False

    @classmethod
    def run_full_update(cls, force: bool = False) -> None:
        """Executa atualização completa de todo o sistema."""
        console.print("[bold green]═══════════════════════════════════════════════════════[/bold green]")
        console.print("[bold white]   vHackintosh — Central de Atualizações do Sistema    [/bold white]")
        console.print("[bold green]═══════════════════════════════════════════════════════[/bold green]")

        # 1. Atualiza vHackintosh
        cls.update_vhackintosh_repo()

        # 2. Atualiza binários do Reims vGPU
        cls.update_reims_vgpu(force=force)

        console.print("\n[bold green]Atualização concluída com sucesso![/bold green]")
