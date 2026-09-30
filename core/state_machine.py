"""
vHackintosh — State Machine & Installation Orchestrator
Monitora o ciclo de vida da VM, reboots intermediários da Apple e seleciona
automaticamente as partições de boot no OpenCore.
"""

from __future__ import annotations
import os
import time
import json
import socket
from enum import Enum
from pathlib import Path
from typing import Optional, Callable
from rich.console import Console

console = Console()


class VMState(str, Enum):
    IDLE = "IDLE"
    INICIANDO = "INICIANDO"
    EM_INSTALACAO = "EM_INSTALACAO"
    REINICIANDO_POS_INSTALACAO = "REINICIANDO_POS_INSTALACAO"
    INSTALACAO_CONCLUIDA = "INSTALACAO_CONCLUIDA"
    INICIANDO_MACOS = "INICIANDO_MACOS"
    ERRO_AO_INICIAR = "ERRO_AO_INICIAR"
    SHUTDOWN = "SHUTDOWN"


class QMPClient:
    """Cliente de comunicação QMP com a instância QEMU em execução."""

    def __init__(self, socket_path: str | Path):
        self.socket_path = Path(socket_path)
        self.sock: Optional[socket.socket] = None

    def connect(self, timeout_sec: float = 10.0) -> bool:
        start = time.time()
        while time.time() - start < timeout_sec:
            if self.socket_path.exists():
                try:
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.sock.connect(str(self.socket_path))
                    # Lê banner de abertura do QMP
                    self.sock.recv(1024)
                    # Habilita capacidades
                    self.execute("qmp_capabilities")
                    return True
                except Exception:
                    pass
            time.sleep(0.3)
        return False

    def execute(self, cmd: str, arguments: Optional[dict] = None) -> Optional[dict]:
        if not self.sock:
            return None
        payload = {"execute": cmd}
        if arguments:
            payload["arguments"] = arguments
        try:
            msg = json.dumps(payload) + "\n"
            self.sock.sendall(msg.encode("utf-8"))
            data = self.sock.recv(4096).decode("utf-8")
            return json.loads(data)
        except Exception:
            return None

    def send_key(self, qcode: str) -> bool:
        res = self.execute("send-key", {"keys": [{"type": "qcode", "data": qcode}]})
        return res is not None

    def send_boot_selection(self, entry_index: int = 1) -> None:
        """Envia navegação por teclas para o boot picker do OpenCore."""
        time.sleep(1.0)
        for _ in range(entry_index - 1):
            self.send_key("right")
            time.sleep(0.1)
        self.send_key("ret")

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


class InstallationStateMachine:
    """Máquina de estados que rastreia os reboots e o progresso do macOS."""

    def __init__(self, vm_id: str, is_new_install: bool = False):
        self.vm_id = vm_id
        self.is_new_install = is_new_install
        self.current_state = VMState.INICIANDO
        self.reboot_count = 0
        self.on_state_change: Optional[Callable[[VMState], None]] = None

    def set_state(self, state: VMState) -> None:
        if self.current_state != state:
            self.current_state = state
            if self.on_state_change:
                self.on_state_change(state)

    def handle_qemu_reset(self) -> None:
        """Chamado quando o QEMU reinicia internamente durante a instalação."""
        self.reboot_count += 1
        if self.is_new_install:
            if self.reboot_count == 1:
                self.set_state(VMState.REINICIANDO_POS_INSTALACAO)
            elif self.reboot_count >= 2:
                self.set_state(VMState.INSTALACAO_CONCLUIDA)
        else:
            self.set_state(VMState.INICIANDO_MACOS)

    def handle_panic_or_error(self) -> None:
        self.set_state(VMState.ERRO_AO_INICIAR)
