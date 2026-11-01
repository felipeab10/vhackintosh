"""
vHackintosh — Host Power Synchronization & Kiosk Enforcer
Sincroniza o desligamento e reinicialização do macOS com o computador físico (Host Linux).
"""

from __future__ import annotations
import os
import time
import queue
import threading
import subprocess
from pathlib import Path
from typing import Optional
from rich.console import Console

from core.passthrough import QmpClient, QmpError, close_qmp_broker

console = Console()


class QMPPowerMonitor:
    """Monitora eventos QMP para detectar desligamento vs reinicialização do guest."""

    def __init__(
        self,
        sock_path: str = "/tmp/vhackintosh-qmp.sock",
        path_file: Optional[str] = None,
        discovery_timeout_sec: float = 60.0,
        control_socket_path: Optional[str] = None,
    ):
        self.sock_path = sock_path
        # Arquivo opcional que contém o caminho real do socket QMP. Usado pelo
        # harness rail do reims-vgpu, que cria o socket em um diretório temporário
        # e publica o caminho em `$RUN_DIR/qmp.path`.
        self.path_file = path_file
        self.control_socket_path = control_socket_path
        self._control_relay = None
        self.discovery_timeout_sec = discovery_timeout_sec
        self.last_reason: Optional[str] = None
        self.reset_count: int = 0
        self.shutdown_count: int = 0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client: Optional[QmpClient] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def _resolve_socket_path(self) -> Optional[str]:
        """Descobre o socket QMP: caminho fixo ou publicado em `qmp.path`."""
        if os.path.exists(self.sock_path):
            return self.sock_path
        if self.path_file and os.path.exists(self.path_file):
            try:
                candidate = Path(self.path_file).read_text(encoding="utf-8").strip()
            except Exception:
                candidate = ""
            if candidate and os.path.exists(candidate):
                return candidate
        return None

    def _monitor_loop(self) -> None:
        deadline = time.monotonic() + max(self.discovery_timeout_sec, 10.0)
        candidate: Optional[str] = None
        client: Optional[QmpClient] = None
        while time.monotonic() < deadline and not self._stop_event.is_set():
            candidate = self._resolve_socket_path()
            if candidate:
                try:
                    client = QmpClient(candidate, timeout=2.0)
                    # Publish the handle before connect returns. The server can
                    # deliver an event and EOF in the same packet as the
                    # capability response, while wait_for_disconnect is already
                    # waiting to drain the broker's last queued event.
                    self._client = client
                    client.connect()
                    break
                except (OSError, QmpError):
                    self._client = None
                    client = None
            time.sleep(0.25)

        if client is None or candidate is None:
            return

        self._client = client
        try:
            if self.control_socket_path:
                try:
                    from core.qmp.relay import QmpControlRelay

                    self._control_relay = QmpControlRelay(client, self.control_socket_path)
                    self._control_relay.start()
                except Exception:
                    # QMP event observation remains useful if the local relay
                    # cannot start; the web agent reports its controls absent.
                    self._control_relay = None
            while not self._stop_event.is_set():
                try:
                    event = client.get_event(timeout=1.0)
                except queue.Empty:
                    continue
                except QmpError:
                    break
                self._handle_event(event)
        finally:
            if self._control_relay is not None:
                self._control_relay.close()
                self._control_relay = None
            # Keep the handle until wait_for_disconnect() has had a chance to
            # drain events queued just before the reader observed EOF.
            close_qmp_broker(candidate)

    def _handle_event(self, event: dict) -> None:
        """Preserve the legacy shutdown/reset policy on broker-delivered events."""
        event_name = event.get("event")
        if event_name == "RESET":
            self.last_reason = "guest-reset"
            self.reset_count += 1
        elif event_name == "SHUTDOWN":
            data_obj = event.get("data", {})
            reason = data_obj.get("reason") if isinstance(data_obj, dict) else None
            if reason == "guest-reset":
                self.last_reason = "guest-reset"
                self.reset_count += 1
            elif reason == "guest-shutdown":
                if self.last_reason != "guest-reset":
                    self.last_reason = "guest-shutdown"
                self.shutdown_count += 1
            else:
                if not self.last_reason:
                    self.last_reason = "guest-shutdown"
                self.shutdown_count += 1

    def stop(self) -> None:
        self._stop_event.set()
        if self._client:
            self._client.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def wait_for_disconnect(self, timeout_sec: float = 2.0) -> bool:
        """Aguarda o QMP fechar e drena eventos já lidos pelo broker.

        O broker pode receber o último evento e EOF no mesmo ciclo do leitor.
        Nesse caso `get_event()` no adapter vê a conexão caída antes de expor a
        fila. Após a thread terminar, consumimos qualquer item restante da fila
        do próprio broker (que é independente do estado da conexão) para manter
        o contrato legado de processar o evento final antes de retornar.
        """
        if not self._thread:
            return True
        client = self._client
        self._thread.join(timeout=max(timeout_sec, 0.0))
        if not self._thread.is_alive() and client is not None:
            broker = getattr(client, "_broker", None)
            if broker is not None:
                while True:
                    try:
                        self._handle_event(broker.get_event(timeout=0))
                    except queue.Empty:
                        break
                    except QmpError:
                        break
        return not self._thread.is_alive()


class PowerSync:
    """Gerencia a sincronização de energia entre a VM macOS e o Host Linux."""

    @classmethod
    def request_host_action(cls, action: str) -> bool:
        """Solicita reboot ou desligamento do host e informa se foi aceito."""
        if action not in {"reboot", "poweroff"}:
            raise ValueError("ação de energia inválida")

        for command in (["systemctl", action], [action]):
            try:
                result = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                )
            except OSError:
                continue
            if result.returncode == 0:
                return True
        return False

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
