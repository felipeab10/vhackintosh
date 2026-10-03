"""
vHackintosh — Ciclo de Vida da Instalação do macOS (Install Phase Tracking)

Resolve o problema crítico de a VM reiniciar/desligar o computador físico no meio
da instalação do macOS.

Contexto do bug
---------------
A heurística antiga decidia "macOS já instalado" pelo uso real do disco
(``>= 4 GB``). Porém a instalação do macOS é multi-estágio: logo na primeira
fase o instalador grava vários GB no disco de destino, e a heurística passava a
considerar o sistema como pronto. Com isso o vHackintosh:

  1. desacoplava a mídia ``BaseSystem.img`` cedo demais;
  2. passava ``-no-reboot`` para o QEMU;
  3. habilitava o *Host Power Sync*.

Resultado: um reboot intermediário do instalador encerrava o QEMU e o
``PowerSync`` reiniciava o computador físico no meio da instalação.

Estratégia
----------
A fase é um estado **explícito e persistido por VM** (``VMConfig.install_phase``),
com garantia conservadora: **o host SÓ sincroniza energia quando a fase é
``INSTALLED``**. Enquanto ``INSTALLING``/``PENDING``, reboots do guest são
absorvidos internamente pelo QEMU e o host nunca é reiniciado.

A conclusão da instalação é confirmada pelo usuário (prompt no TUI) a partir de
uma sonda de disco somente-leitura; a promoção automática nunca acontece sem
essa confirmação, pois um falso positivo reativaria exatamente o bug original.

Limitação conhecida: o ``libguestfs`` deste ambiente não monta APFS (não há
suporte a ``libfsapfs``), portanto a sonda consegue detectar a presença do
container APFS e o uso real do disco, mas **não** ler arquivos dentro do macOS.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from rich.console import Console

console = Console()


class InstallPhase(str, Enum):
    """Fase do ciclo de vida da instalação do macOS em uma VM."""

    PENDING = "pending"        # VM criada, nenhum sistema no disco ainda
    INSTALLING = "installing"  # instalação em andamento (inclui reboots da Apple)
    INSTALLED = "installed"    # macOS instalado e pronto para uso diário


# Uso real mínimo do qcow2 para considerar que existe um sistema completo.
# Uma instalação macOS moderna ocupa tipicamente 12-20 GB após a fase 2.
FULL_INSTALL_MIN_GB = 12.0

# Timeout para a sonda com libguestfs (o appliance demora alguns segundos para subir).
GUESTFISH_TIMEOUT_SEC = 90


@dataclass
class DiskProbe:
    """Resultado da inspeção somente-leitura do disco virtual de uma VM."""

    exists: bool = False
    disk_gb: float = 0.0
    virtual_gb: float = 0.0
    has_apfs: bool = False
    has_efi: bool = False
    probe_tool: str = "none"  # "guestfish" | "qemu-img" | "none"

    @property
    def looks_like_complete_install(self) -> bool:
        """Evidência forte (mas não definitiva) de instalação concluída."""
        return self.exists and self.has_apfs and self.disk_gb >= FULL_INSTALL_MIN_GB

    @property
    def has_any_system_data(self) -> bool:
        return self.exists and (self.has_apfs or self.disk_gb > 0.5)


@dataclass
class LaunchPolicy:
    """Decisão de como inicializar a VM (mídia, reboot e sincronização de energia)."""

    phase: InstallPhase
    attach_installer: bool
    enable_host_power_sync: bool
    reason: str

    @property
    def is_installing(self) -> bool:
        return self.phase is not InstallPhase.INSTALLED


def normalize_phase(value: Optional[str]) -> InstallPhase:
    """Converte string persistida em ``InstallPhase`` com fallback seguro."""
    if not value:
        return InstallPhase.PENDING
    try:
        return InstallPhase(str(value).strip().lower())
    except ValueError:
        return InstallPhase.PENDING


class InstallEvidence(str, Enum):
    """Força da evidência de instalação encontrada no disco (registrada na sonda)."""

    NONE = "none"          # nenhum dado de sistema no disco
    PARTIAL = "partial"    # há dados, mas a instalação parece incompleta
    COMPLETE = "complete"  # sistema macOS aparentemente completo (aguarda confirmação)


def classify_evidence(probe: DiskProbe) -> InstallEvidence:
    if not probe.exists or not probe.has_any_system_data:
        return InstallEvidence.NONE
    if probe.looks_like_complete_install:
        return InstallEvidence.COMPLETE
    return InstallEvidence.PARTIAL


def normalize_evidence(value: Optional[str]) -> InstallEvidence:
    if not value:
        return InstallEvidence.NONE
    try:
        return InstallEvidence(str(value).strip().lower())
    except ValueError:
        return InstallEvidence.NONE


class InstallProbe:
    """Sondagem somente-leitura do disco virtual (sem privilégios de root)."""

    @classmethod
    def probe(cls, disk_path: Optional[str | Path]) -> DiskProbe:
        if not disk_path:
            return DiskProbe()

        path = Path(disk_path)
        if not path.exists():
            return DiskProbe()

        probe = cls._qemu_img_info(path)
        probe.exists = True

        if shutil.which("guestfish"):
            fs_types, efi = cls._guestfish_filesystems(path)
            if fs_types is not None:
                probe.has_apfs = "apfs" in fs_types
                probe.has_efi = efi
                probe.probe_tool = "guestfish"
        return probe

    @staticmethod
    def _qemu_img_info(path: Path) -> DiskProbe:
        probe = DiskProbe(exists=True)
        try:
            out = subprocess.check_output(
                ["qemu-img", "info", "--output=json", str(path)],
                text=True,
                timeout=30,
                stderr=subprocess.DEVNULL,
            )
            data = json.loads(out)
            probe.virtual_gb = round(data.get("virtual-size", 0) / (1024 ** 3), 1)
            probe.disk_gb = round(data.get("actual-size", 0) / (1024 ** 3), 1)
            probe.probe_tool = "qemu-img"
        except Exception:
            pass
        return probe

    @staticmethod
    def _guestfish_filesystems(path: Path) -> tuple[Optional[list[str]], bool]:
        """
        Lista os tipos de sistema de arquivos das partições usando libguestfs.

        Retorna ``(tipos, tem_efi)`` ou ``(None, False)`` em caso de falha.
        Usa ``--ro`` para nunca tocar no disco da VM.
        """
        try:
            result = subprocess.run(
                ["guestfish", "--ro", "-a", str(path), "run", ":",
                 "list-filesystems"],
                capture_output=True,
                text=True,
                timeout=GUESTFISH_TIMEOUT_SEC,
            )
        except Exception:
            return None, False

        if result.returncode != 0:
            return None, False

        types: list[str] = []
        for line in result.stdout.splitlines():
            if ":" not in line:
                continue
            _, _, fs_type = line.partition(":")
            types.append(fs_type.strip().lower())

        return types, any(t == "vfat" for t in types)


class InstallPhaseResolver:
    """Decide a fase efetiva e a política de boot de uma VM."""

    @classmethod
    def resolve_phase(
        cls,
        vm,
        probe: Optional[DiskProbe] = None,
        force_installation: bool = False,
    ) -> InstallPhase:
        stored = normalize_phase(getattr(vm, "install_phase", None))

        # Pedido explícito de instalação sempre vence.
        if force_installation:
            return InstallPhase.INSTALLING

        # Estado terminal é "sticky": nunca regride automaticamente.
        if stored is InstallPhase.INSTALLED:
            return InstallPhase.INSTALLED

        # Fase já marcada como em andamento permanece até confirmação do usuário.
        if stored is InstallPhase.INSTALLING:
            return InstallPhase.INSTALLING

        # Sem estado persistido confiável: deduz da sonda de disco.
        probe = probe or InstallProbe.probe(getattr(vm, "disk_path", None))
        if not probe.exists or not probe.has_any_system_data:
            return InstallPhase.PENDING
        # Sabemos que há dados de sistema no disco, mas a promoção para
        # INSTALLED exige confirmação explícita do usuário (ver TUI), pois um
        # falso positivo reativaria o reboot do host no meio da instalação.
        return InstallPhase.INSTALLING

    @classmethod
    def build_policy(
        cls,
        vm,
        force_installation: bool = False,
        probe: Optional[DiskProbe] = None,
    ) -> LaunchPolicy:
        probe = probe or InstallProbe.probe(getattr(vm, "disk_path", None))
        phase = cls.resolve_phase(vm, probe=probe, force_installation=force_installation)

        if phase is InstallPhase.INSTALLED:
            return LaunchPolicy(
                phase=phase,
                attach_installer=False,
                enable_host_power_sync=True,
                reason="macOS instalado: mídia de instalação desacoplada e sincronização de energia do host ATIVA.",
            )

        return LaunchPolicy(
            phase=phase,
            attach_installer=True,
            enable_host_power_sync=False,
            reason="Instalação em andamento: reboots são absorvidos pelo QEMU e o host NÃO é reiniciado.",
        )


def summarize_probe(probe: DiskProbe) -> str:
    """Texto curto para exibição do resultado da sonda."""
    if not probe.exists:
        return "disco não encontrado"
    parts = [f"uso real {probe.disk_gb} GB"]
    parts.append("APFS detectado" if probe.has_apfs else "sem APFS")
    parts.append(f"via {probe.probe_tool}")
    return ", ".join(parts)


PHASE_LABELS = {
    InstallPhase.PENDING: "[dim]NÃO INSTALADO[/dim]",
    InstallPhase.INSTALLING: "[bold yellow]INSTALANDO[/bold yellow]",
    InstallPhase.INSTALLED: "[bold green]INSTALADO[/bold green]",
}

PHASE_SHORT = {
    InstallPhase.PENDING: "[dim]—[/dim]",
    InstallPhase.INSTALLING: "[bold yellow]⏳ INSTALANDO[/bold yellow]",
    InstallPhase.INSTALLED: "[bold green]✔ INSTALADO[/bold green]",
}


def phase_label(phase: InstallPhase) -> str:
    """Rótulo rico em cores para a fase da instalação."""
    return PHASE_LABELS.get(phase, "[dim]DESCONHECIDO[/dim]")


def phase_short_label(
    phase: InstallPhase,
    evidence: "InstallEvidence | str | None" = None,
) -> str:
    """
    Rótulo compacto para a tabela de VMs.

    Um sistema com evidência COMPLETA mas fase ainda não confirmada aparece como
    ``CONFIRMAR`` — sinaliza que a sincronização de energia do host está
    desativada e depende de confirmação do usuário (opção ``[i]``).
    """
    if phase is InstallPhase.INSTALLING and normalize_evidence(evidence) is InstallEvidence.COMPLETE:
        return "[bold yellow]⚠ CONFIRMAR[/bold yellow]"
    return PHASE_SHORT.get(phase, "[dim]—[/dim]")
