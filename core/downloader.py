"""
vHackintosh — Apple macOS Recovery Downloader & Image Preparer
Baixa imagens oficiais do macOS (BaseSystem.dmg) dos servidores da Apple (SUS)
e converte para imagens de disco (.img) utilizando o dmg2img com barra de progresso Rich.
"""

from __future__ import annotations
import os
import sys
import time
import random
import string
import subprocess
from pathlib import Path
from typing import Optional, Dict
from urllib.request import Request, urlopen, HTTPError
from urllib.parse import urlparse

from rich.console import Console
from rich.markup import escape
from rich.progress import (
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TextColumn,
)
from rich.text import Text

console = Console()


class _ProgressPercentColumn(ProgressColumn):
    """Percentual compacto, com cor estável independente do tema do terminal."""

    def render(self, task) -> Text:
        percentage = task.percentage
        if percentage is None:
            return Text(" --%", style="bold white")
        return Text(f"{percentage:3.0f}%", style="bold white")


class _DownloadSizeColumn(ProgressColumn):
    """Tamanho transferido no formato compacto usado pelo instalador."""

    def render(self, task) -> Text:
        downloaded_mb = round(task.completed / 1_000_000)
        if task.total is None:
            return Text(f"{downloaded_mb} MB", style="bold white")
        total_mb = round(task.total / 1_000_000)
        return Text(f"{downloaded_mb}/{total_mb} MB", style="bold white")


class _BarColumn(ProgressColumn):
    """Barra compacta de uma linha para o progresso do download."""

    def __init__(self, width: int = 28) -> None:
        super().__init__()
        self.width = width

    def render(self, task) -> Text:
        percentage = task.percentage
        if percentage is None:
            pulse_width = min(5, self.width)
            phase = int((task.elapsed or 0) * 2) % (2 * self.width - pulse_width)
            start = phase if phase <= self.width - pulse_width else 2 * self.width - pulse_width - phase
            line = Text("░" * start, style="#153247")
            line.append("█" * pulse_width, style="#12cbd5")
            line.append("░" * (self.width - start - pulse_width), style="#153247")
        else:
            filled = round(self.width * percentage / 100)
            line = Text("█" * filled, style="#12cbd5")
            line.append("░" * (self.width - filled), style="#153247")
        return line

IMAGES_CACHE_DIR = Path(os.path.expanduser("~/.config/vhackintosh/images"))

# Catálogo oficial de Board IDs e parâmetros por versão do macOS suportada
MACOS_PRODUCTS = {
    "tahoe": {
        "name": "macOS Tahoe (26)",
        "board_id": "Mac-CFF7D910A743CAAF",
        "mlb": "00000000000000000",
        "os_type": "latest",
    },
    "sequoia": {
        "name": "macOS Sequoia (15)",
        "board_id": "Mac-7BA5B2D9E42DDD94",
        "mlb": "00000000000000000",
        "os_type": "default",
    },
    "ventura": {
        "name": "macOS Ventura (13)",
        "board_id": "Mac-4B682C642B45593E",
        "mlb": "00000000000000000",
        "os_type": "latest",
    },
}


class MacOSDownloader:
    """Gerencia o download e conversão de instaladores oficiais da Apple."""

    @classmethod
    def get_supported_versions(cls) -> list[str]:
        return list(MACOS_PRODUCTS.keys())

    @classmethod
    def get_cached_image_path(cls, version: str) -> Optional[Path]:
        """Retorna o caminho da imagem se já foi baixada e convertida."""
        img_path = IMAGES_CACHE_DIR / version / "BaseSystem.img"
        if img_path.exists() and img_path.stat().st_size > 100 * 1024 * 1024:
            return img_path
        return None

    @classmethod
    def prepare_installer(cls, version: str) -> Optional[Path]:
        """
        Baixa o BaseSystem.dmg da Apple e converte para BaseSystem.img.
        Retorna o caminho do arquivo .img gerado.
        """
        ver_key = version.lower()
        if ver_key not in MACOS_PRODUCTS:
            console.print(f"[red]Versão do macOS desconhecida: {version}[/red]")
            return None

        cached = cls.get_cached_image_path(ver_key)
        if cached:
            console.print(f"[bold green]✔ Imagem do instalador {MACOS_PRODUCTS[ver_key]['name']} já disponível no cache local.[/bold green]")
            return cached

        target_dir = IMAGES_CACHE_DIR / ver_key
        target_dir.mkdir(parents=True, exist_ok=True)
        dmg_path = target_dir / "BaseSystem.dmg"
        chunk_path = target_dir / "BaseSystem.chunklist"
        img_path = target_dir / "BaseSystem.img"

        meta = MACOS_PRODUCTS[ver_key]
        console.print(f"\n[bold cyan]▶ Obtendo metadados de instalação para {meta['name']} dos servidores da Apple...[/bold cyan]")

        try:
            session = cls._get_apple_session()
            info = cls._get_recovery_payload_info(
                session=session,
                board_id=meta["board_id"],
                mlb=meta["mlb"],
                os_type=meta.get("os_type", "default"),
            )
        except Exception as e:
            console.print(f"[bold red]Falha ao obter metadados da Apple:[/bold red] {e}")
            return None

        # A operação tem até três etapas: imagem, checksum e conversão local.
        # A conversão não tem percentual confiável, então ela será exibida com spinner.
        dmg_url = info.get("AU")
        dmg_token = info.get("AT")
        if not dmg_url or not dmg_token:
            console.print("[red]Link de download do BaseSystem.dmg não encontrado na resposta da Apple.[/red]")
            return None

        chunk_url = info.get("CU")
        chunk_token = info.get("CT")
        has_chunklist = bool(chunk_url and chunk_token)
        stage_count = 3 if has_chunklist else 2

        console.print(f"[bold green]▶ Baixando BaseSystem.dmg oficial da Apple...[/bold green]")
        success = cls._stream_download(
            url=dmg_url,
            asset_token=dmg_token,
            destination=dmg_path,
            description=f"[1/{stage_count}] macOS {ver_key.capitalize()} installer",
        )
        if not success:
            console.print("[red]Erro durante o download do BaseSystem.dmg.[/red]")
            return None

        # 2. Download do BaseSystem.chunklist
        if has_chunklist:
            cls._stream_download(
                url=chunk_url,
                asset_token=chunk_token,
                destination=chunk_path,
                description=f"[2/{stage_count}] Verificando BaseSystem",
            )

        # 3. Conversão de BaseSystem.dmg para BaseSystem.img usando dmg2img
        console.print(f"\n[bold cyan]▶ Convertendo BaseSystem.dmg para formato de disco KVM (.img)...[/bold cyan]")
        conversion_stage = 3 if has_chunklist else 2
        with Progress(
            SpinnerColumn(spinner_name="dots"),
            TextColumn(f"[bold cyan]\\[{conversion_stage}/{stage_count}] Convertendo instalador para imagem KVM[/bold cyan]"),
            console=console,
        ):
            conv_success = cls._convert_dmg_to_img(dmg_path, img_path)
        if not conv_success:
            console.print("[bold red]Falha na conversão do BaseSystem.dmg via dmg2img![/bold red]")
            return None

        console.print(f"[bold green]✔ Instalador do {meta['name']} pronto para uso![/bold green] ({img_path})\n")
        return img_path

    @classmethod
    def _get_apple_session(cls) -> str:
        headers = {
            "Host": "osrecovery.apple.com",
            "Connection": "close",
            "User-Agent": "InternetRecovery/1.0",
        }
        req = Request(url="http://osrecovery.apple.com/", headers=headers)
        resp = urlopen(req, timeout=10)
        for h, v in resp.info().items():
            if h.lower() == "set-cookie":
                for cookie in v.split("; "):
                    if cookie.startswith("session="):
                        return cookie
        raise RuntimeError("Não foi possível obter sessão do osrecovery.apple.com")

    @classmethod
    def _get_recovery_payload_info(cls, session: str, board_id: str, mlb: str, os_type: str) -> Dict[str, str]:
        headers = {
            "Host": "osrecovery.apple.com",
            "Connection": "close",
            "User-Agent": "InternetRecovery/1.0",
            "Cookie": session,
            "Content-Type": "text/plain",
        }
        cid = "".join(random.choices(string.hexdigits[:16].upper(), k=16))
        k_val = "".join(random.choices(string.hexdigits[:16].upper(), k=64))
        fg_val = "".join(random.choices(string.hexdigits[:16].upper(), k=64))

        post_data = "\n".join([
            f"cid={cid}",
            f"sn={mlb}",
            f"bid={board_id}",
            f"k={k_val}",
            f"fg={fg_val}",
            f"os={os_type}",
        ]).encode("utf-8")

        req = Request(
            url="http://osrecovery.apple.com/InstallationPayload/RecoveryImage",
            headers=headers,
            data=post_data,
        )
        resp = urlopen(req, timeout=15)
        raw_text = resp.read().decode("utf-8")

        info: Dict[str, str] = {}
        for line in raw_text.splitlines():
            if ": " in line:
                k, v = line.split(": ", 1)
                info[k.strip()] = v.strip()
        return info

    @classmethod
    def _stream_download(cls, url: str, asset_token: str, destination: Path, description: str) -> bool:
        purl = urlparse(url)
        headers = {
            "Host": purl.hostname,
            "Connection": "close",
            "User-Agent": "InternetRecovery/1.0",
            "Cookie": f"AssetToken={asset_token}",
        }
        req = Request(url=url, headers=headers)
        try:
            resp = urlopen(req, timeout=30)
            total_size = int(resp.headers.get("Content-Length", 0))

            with Progress(
                SpinnerColumn(spinner_name="dots", style="cyan"),
                TextColumn("[bold cyan]{task.description}[/bold cyan]"),
                _BarColumn(width=28),
                _ProgressPercentColumn(),
                _DownloadSizeColumn(),
                console=console,
            ) as progress:
                task = progress.add_task(escape(description), total=total_size if total_size > 0 else None)

                with open(destination, "wb") as f:
                    while True:
                        chunk = resp.read(1024 * 1024)  # 1 MB chunk
                        if not chunk:
                            break
                        f.write(chunk)
                        progress.update(task, advance=len(chunk))
            return True
        except Exception as e:
            console.print(f"[red]Erro ao baixar {url}: {e}[/red]")
            return False

    @classmethod
    def _convert_dmg_to_img(cls, dmg_path: Path, img_path: Path) -> bool:
        """Converte a imagem BaseSystem.dmg completa para BaseSystem.img com tabela GPT preservada."""
        cmd = ["dmg2img", "-i", str(dmg_path), str(img_path)]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return proc.returncode == 0 and img_path.exists() and img_path.stat().st_size > 0
        except Exception as e:
            console.print(f"[red]Erro executando dmg2img: {e}[/red]")
            return False
