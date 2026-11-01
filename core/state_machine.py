"""
vHackintosh — State Machine & Installation Orchestrator
Monitora o ciclo de vida da VM, reboots intermediários da Apple e seleciona
automaticamente as partições de boot no OpenCore.
"""

from __future__ import annotations
import os
import time
from enum import Enum
from pathlib import Path
from typing import Optional, Callable
from rich.console import Console

from core.passthrough import QmpClient as _SharedQmpClient, QmpError

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
    """Compatibilidade da state machine sobre o broker QMP compartilhado."""

    def __init__(self, socket_path: str | Path):
        self.socket_path = Path(socket_path)
        self._client = _SharedQmpClient(self.socket_path, timeout=5.0)

    def connect(self, timeout_sec: float = 10.0) -> bool:
        # Preserva a espera pela criação inicial do socket pelo reims-ctl.
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if self.socket_path.exists():
                try:
                    self._client.connect()
                    return True
                except QmpError:
                    return False
            time.sleep(min(0.3, max(0, deadline - time.monotonic())))
        return False

    def execute(self, cmd: str, arguments: Optional[dict] = None) -> Optional[dict]:
        try:
            return self._client.execute(cmd, arguments)
        except QmpError:
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
        self._client.close()


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
