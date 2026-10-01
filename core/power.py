"""
vHackintosh — Host Power Synchronization & Kiosk Enforcer
Sincroniza o desligamento e reinicialização do macOS com o computador físico (Host Linux).
"""

from __future__ import annotations
import os
import sys
import time
import json
import socket
import threading
import subprocess
from typing import Optional
from rich.console import Console

console = Console()


class QMPPowerMonitor:
    """Monitora eventos QMP para detectar desligamento vs reinicialização do guest."""

    def __init__(self, sock_path: str = "/tmp/vhackintosh-qmp.sock"):
        self.sock_path = sock_path
        self.last_reason: Optional[str] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def _monitor_loop(self) -> None:
        s = None
        for _ in range(40):
            if self._stop_event.is_set():
                return
            if os.path.exists(self.sock_path):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(self.sock_path)
                    break
                except Exception:
                    time.sleep(0.25)
            else:
                time.sleep(0.25)

        if not s:
            return

        try:
            s.settimeout(1.0)
            data = b""
            while not self._stop_event.is_set():
                try:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if b"\n" in data:
                        break
                except socket.timeout:
                    continue

            try:
                s.sendall(json.dumps({"execute": "qmp_capabilities"}).encode() + b"\n")
            except Exception:
                pass

            buffer = b""
            while not self._stop_event.is_set():
                try:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if not line.strip():
                            continue
                        try:
                            msg = json.loads(line.decode("utf-8", errors="ignore"))
                            ev = msg.get("event")
                            if ev == "RESET":
                                self.last_reason = "guest-reset"
                            elif ev == "SHUTDOWN":
                                data_obj = msg.get("data", {})
                                reason = data_obj.get("reason")
                                if reason == "guest-reset":
                                    self.last_reason = "guest-reset"
                                elif reason == "guest-shutdown":
                                    if self.last_reason != "guest-reset":
                                        self.last_reason = "guest-shutdown"
                                elif not self.last_reason:
                                    self.last_reason = "guest-shutdown"
                        except Exception:
                            pass
                except socket.timeout:
                    continue
                except Exception:
                    break
        finally:
            try:
                s.close()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)


class PowerSync:
    """Gerencia a sincronização de energia entre a VM macOS e o Host Linux."""

    @classmethod
    def handle_vm_exit(cls, reason: Optional[str] = None, is_kiosk_mode: bool = True) -> None:
        """Determina e executa o desligamento ou reinício do host com base na saída do macOS."""
        if reason == "guest-reset":
            cls.sync_host_reboot(is_kiosk_mode=is_kiosk_mode)
        else:
            cls.sync_host_shutdown(is_kiosk_mode=is_kiosk_mode)

    @classmethod
    def sync_host_shutdown(cls, is_kiosk_mode: bool = True) -> None:
        if not is_kiosk_mode:
            console.print("[yellow]VM desligada. Modo Kiosk desativado, retornando ao gerenciador.[/yellow]")
            return

        console.print("[bold red]⏻ macOS solicitou desligamento. Desligando o computador físico em 2 segundos...[/bold red]")
        time.sleep(2)
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

        console.print("[bold cyan]🔄 macOS solicitou reinicialização. Reiniciando o computador físico em 2 segundos...[/bold cyan]")
        time.sleep(2)
        try:
            subprocess.run(["systemctl", "reboot"], check=False)
        except Exception:
            try:
                subprocess.run(["reboot"], check=False)
            except Exception:
                pass
