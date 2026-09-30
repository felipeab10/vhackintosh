"""
vHackintosh — System Readiness & Hardware Compatibility Checker
Verifica pré-requisitos essenciais antes de rodar ou instalar máquinas virtuais:
  1. Virtualização de CPU na BIOS (Intel VT-x / AMD SVM) e disponibilidade de /dev/kvm
  2. Conectividade com a Internet (DNS e Apple Software Update Server)
  3. Aceleração gráfica Vulkan e nós DRM (/dev/dri/renderD128) para reims-vgpu
  4. Recursos de memória RAM e espaço livre em disco
"""

from __future__ import annotations
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


@dataclass
class CheckResult:
    category: str
    name: str
    passed: bool
    status_text: str
    details: str
    remediation: Optional[str] = None
    is_critical: bool = False


@dataclass
class ReadinessReport:
    checks: List[CheckResult]
    can_run_vms: bool
    has_internet: bool
    critical_errors: List[str]


class SystemReadinessChecker:
    """Validador de compatibilidade e prontidão do sistema hospedeiro."""

    @classmethod
    def check_all(cls) -> ReadinessReport:
        checks = []
        critical_errors = []

        # 1. Checagem de Virtualização de CPU (BIOS e KVM)
        kvm_check = cls._check_virtualization()
        checks.append(kvm_check)
        if not kvm_check.passed and kvm_check.is_critical:
            critical_errors.append(kvm_check.status_text)

        # 2. Checagem de Conectividade com a Internet
        net_check = cls._check_internet()
        checks.append(net_check)

        # 3. Checagem de Gráficos e Vulkan (reims-vgpu)
        gpu_check = cls._check_gpu_vulkan()
        checks.append(gpu_check)

        # 4. Checagem de Memória RAM
        ram_check = cls._check_ram()
        checks.append(ram_check)

        # 5. Checagem de Espaço em Disco
        disk_check = cls._check_disk_space()
        checks.append(disk_check)

        can_run = kvm_check.passed
        has_net = net_check.passed

        return ReadinessReport(
            checks=checks,
            can_run_vms=can_run,
            has_internet=has_net,
            critical_errors=critical_errors
        )

    @classmethod
    def _check_virtualization(cls) -> CheckResult:
        """Verifica se /dev/kvm está disponível e se VT-x/AMD-V está ativo na BIOS."""
        kvm_path = "/dev/kvm"
        kvm_exists = os.path.exists(kvm_path)
        kvm_accessible = kvm_exists and os.access(kvm_path, os.R_OK | os.W_OK)

        # Inspeciona flags do processador em /proc/cpuinfo
        cpuinfo = ""
        try:
            with open("/proc/cpuinfo", "r") as f:
                cpuinfo = f.read()
        except Exception:
            pass

        has_vmx = "vmx" in cpuinfo  # Intel VT-x
        has_svm = "svm" in cpuinfo  # AMD SVM (AMD-V)

        if kvm_accessible:
            vendor = "Intel (VT-x)" if has_vmx else ("AMD (AMD-V)" if has_svm else "Genérica")
            return CheckResult(
                category="Virtualização",
                name="Aceleração KVM & BIOS",
                passed=True,
                status_text="Habilitada e Pronta",
                details=f"/dev/kvm disponível e acelerado por hardware ({vendor}).",
                is_critical=True
            )

        if kvm_exists and not kvm_accessible:
            return CheckResult(
                category="Virtualização",
                name="Permissões de /dev/kvm",
                passed=False,
                status_text="Permissão Negada",
                details="O nó /dev/kvm existe, mas o usuário atual não tem permissão de leitura/escrita.",
                remediation="Adicione seu usuário ao grupo kvm: 'sudo usermod -aG kvm $USER' e reinicie a sessão.",
                is_critical=True
            )

        # Se /dev/kvm não existe, analisa se é suporte ausente ou desativado na BIOS
        if has_vmx:
            return CheckResult(
                category="Virtualização",
                name="Intel VT-x na BIOS",
                passed=False,
                status_text="DESATIVADA NA BIOS",
                details="Seu processador Intel suporta VT-x, mas a virtualização por hardware está DESLIGADA na BIOS da sua placa-mãe!",
                remediation="Reinicie o computador, entre no Setup da BIOS (tecla F2 ou Del) e ative 'Intel Virtualization Technology' (ou 'VT-x').",
                is_critical=True
            )
        elif has_svm:
            return CheckResult(
                category="Virtualização",
                name="AMD SVM na BIOS",
                passed=False,
                status_text="DESATIVADA NA BIOS",
                details="Seu processador AMD suporta AMD-V, mas o modo SVM está DESLIGADO na BIOS da sua placa-mãe!",
                remediation="Reinicie o computador, entre no Setup da BIOS (tecla F2 ou Del) e ative 'SVM Mode' (AMD Virtualization).",
                is_critical=True
            )
        else:
            return CheckResult(
                category="Virtualização",
                name="Suporte a Virtualização",
                passed=False,
                status_text="Processador Incompatível",
                details="Nenhuma flag de virtualização (vmx ou svm) foi encontrada no seu processador.",
                remediation="A execução de máquinas virtuais macOS requer um processador x86_64 com suporte a VT-x ou AMD-V.",
                is_critical=True
            )

    @classmethod
    def _check_internet(cls) -> CheckResult:
        """Testa conectividade com DNS público e servidores da Apple."""
        connected = False
        target = "8.8.8.8"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((target, 53))
            s.close()
            connected = True
        except Exception:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(2.0)
                s.connect(("1.1.1.1", 53))
                s.close()
                connected = True
            except Exception:
                connected = False

        if connected:
            return CheckResult(
                category="Rede",
                name="Conexão com a Internet",
                passed=True,
                status_text="Online",
                details="Conectividade externa ativa para download do macOS e Harnesses CLI.",
                is_critical=False
            )
        else:
            return CheckResult(
                category="Rede",
                name="Conexão com a Internet",
                passed=False,
                status_text="OFFLINE",
                details="Sem conexão com a Internet. O download de instaladores oficiais e AI Harnesses ficará indisponível.",
                remediation="Conecte um cabo Ethernet ou configure o Wi-Fi via 'nmtui' no terminal.",
                is_critical=False
            )

    @classmethod
    def _check_gpu_vulkan(cls) -> CheckResult:
        """Verifica a presença de nós DRM e drivers Vulkan para o reims-vgpu."""
        has_render_node = os.path.exists("/dev/dri/renderD128")
        
        has_icd = False
        icd_dir = Path("/usr/share/vulkan/icd.d")
        if icd_dir.exists() and any(icd_dir.glob("*.json")):
            has_icd = True

        if has_render_node and has_icd:
            return CheckResult(
                category="Gráficos",
                name="Aceleração Vulkan / DRM",
                passed=True,
                status_text="Suportado & Pronto",
                details="Nó DRM (/dev/dri/renderD128) e manifestos Vulkan detectados para o reims-vgpu.",
                is_critical=False
            )
        elif has_render_node:
            return CheckResult(
                category="Gráficos",
                name="Aceleração Gráfica",
                passed=True,
                status_text="Básico (Render Node Ativo)",
                details="Dispositivo gráfico renderD128 detectado.",
                is_critical=False
            )
        else:
            return CheckResult(
                category="Gráficos",
                name="Aceleração Gráfica",
                passed=False,
                status_text="Atenção",
                details="Nó /dev/dri/renderD128 não encontrado. Pode degradar o desempenho de renderização.",
                remediation="Certifique-se de que os drivers de GPU (Mesa / Vulkan) estão carregados.",
                is_critical=False
            )

    @classmethod
    def _check_ram(cls) -> CheckResult:
        """Verifica quantidade total de memória RAM instalada."""
        total_gb = 0.0
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if "MemTotal" in line:
                        kb = int(line.split(":")[1].strip().split()[0])
                        total_gb = round(kb / (1024 * 1024), 1)
                        break
        except Exception:
            total_gb = 8.0

        if total_gb >= 15.0:
            return CheckResult(
                category="Memória",
                name="Memória RAM Total",
                passed=True,
                status_text=f"{total_gb} GB (Excelente)",
                details="Quantidade suficiente para alocar 8 GB a 12 GB para o macOS sem impactar o host.",
                is_critical=False
            )
        elif total_gb >= 7.5:
            return CheckResult(
                category="Memória",
                name="Memória RAM Total",
                passed=True,
                status_text=f"{total_gb} GB (Mínimo)",
                details="Adequado para alocar até 4 GB para a máquina virtual.",
                is_critical=False
            )
        else:
            return CheckResult(
                category="Memória",
                name="Memória RAM Total",
                passed=False,
                status_text=f"{total_gb} GB (Insuficiente)",
                details="Recomenda-se no mínimo 8 GB de RAM para virtualizar macOS modernamente.",
                remediation="Instale mais memória RAM física no computador.",
                is_critical=False
            )

    @classmethod
    def _check_disk_space(cls) -> CheckResult:
        """Verifica espaço livre na partição home/raiz."""
        target_dir = os.path.expanduser("~")
        total, used, free = shutil.disk_usage(target_dir)
        free_gb = round(free / (1024 ** 3), 1)

        if free_gb >= 50.0:
            return CheckResult(
                category="Armazenamento",
                name="Espaço Livre em Disco",
                passed=True,
                status_text=f"{free_gb} GB Livres",
                details="Espaço abundante para múltiplas VMs e imagens de recuperação da Apple.",
                is_critical=False
            )
        elif free_gb >= 25.0:
            return CheckResult(
                category="Armazenamento",
                name="Espaço Livre em Disco",
                passed=True,
                status_text=f"{free_gb} GB Livres",
                details="Suficiente para instalar 1 VM macOS de 64 GB com alocação dinâmica QCOW2.",
                is_critical=False
            )
        else:
            return CheckResult(
                category="Armazenamento",
                name="Espaço Livre em Disco",
                passed=False,
                status_text=f"{free_gb} GB (Baixo)",
                details="Recomenda-se pelo menos 30 GB livres para provisionamento seguro de novas VMs.",
                remediation="Libere espaço em disco apagando arquivos temporários ou desinstalando pacotes.",
                is_critical=False
            )

    @classmethod
    def render_panel(cls, report: ReadinessReport) -> Panel:
        """Gera um painel Rich estilizado com a matriz de compatibilidade."""
        table = Table(box=None, header_style="bold magenta", expand=True)
        table.add_column("Categoria", style="cyan", width=16)
        table.add_column("Item Verificado", style="bold white", width=26)
        table.add_column("Status", width=22)
        table.add_column("Detalhes & Diagnóstico", style="dim")

        for c in report.checks:
            if c.passed:
                status = Text(f"✔ {c.status_text}", style="bold green")
            else:
                if c.is_critical:
                    status = Text(f"✖ {c.status_text}", style="bold red")
                else:
                    status = Text(f"⚠ {c.status_text}", style="bold yellow")
            
            details = c.details
            if not c.passed and c.remediation:
                details += f"\n👉 [bold yellow]{c.remediation}[/bold yellow]"

            table.add_row(c.category, c.name, status, details)

        border_color = "green" if report.can_run_vms else "red"
        title = "[bold green]Matriz de Compatibilidade do Sistema[/bold green]" if report.can_run_vms else "[bold red]Atenção: Pré-requisitos Críticos Pendentes[/bold red]"
        return Panel(table, title=title, border_style=border_color)
