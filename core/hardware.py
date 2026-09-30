"""
vHackintosh — Hardware Advisor
Analisa a topologia da CPU (P-Cores, E-Cores, Threads) e memória RAM do host
para sugerir a melhor alocação para a VM macOS.
"""

from __future__ import annotations
import os
import multiprocessing
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class CPUProfile:
    model_name: str
    total_threads: int
    physical_cores: int
    p_cores: int
    e_cores: int
    is_hybrid: bool
    suggested_vcpus: int


@dataclass
class RAMProfile:
    total_gb: float
    available_gb: float
    suggested_vm_ram_gb: int


@dataclass
class SystemHardwareProfile:
    cpu: CPUProfile
    ram: RAMProfile


class HardwareAdvisor:
    """Detecta a arquitetura de hardware e sugere parâmetros ideais para o macOS."""

    @classmethod
    def analyze(cls) -> SystemHardwareProfile:
        cpu = cls._analyze_cpu()
        ram = cls._analyze_ram()
        return SystemHardwareProfile(cpu=cpu, ram=ram)

    @classmethod
    def _analyze_cpu(cls) -> CPUProfile:
        total_threads = os.cpu_count() or multiprocessing.cpu_count() or 4
        model_name = "Intel/AMD Processor"
        physical_cores = total_threads // 2 if total_threads > 2 else total_threads

        # Lê /proc/cpuinfo
        try:
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if "model name" in line:
                        model_name = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass

        # Detecta P-cores e E-cores (Intel 12th+ Gen ou AMD heterogêneo) via sysfs
        p_cores = 0
        e_cores = 0
        is_hybrid = False

        core_types_dir = "/sys/devices/system/cpu/cpu0/topology"
        # Tenta verificar se há múltiplos tipos de cores (/sys/devices/cpu_core vs cpu_atom)
        if os.path.exists("/sys/devices/cpu_core") and os.path.exists("/sys/devices/cpu_atom"):
            is_hybrid = True
            try:
                with open("/sys/devices/cpu_core/cpus", "r") as f:
                    p_cores_str = f.read().strip()
                with open("/sys/devices/cpu_atom/cpus", "r") as f:
                    e_cores_str = f.read().strip()
                # Exemplo básico de contagem
                p_cores = len(cls._parse_cpu_range(p_cores_str))
                e_cores = len(cls._parse_cpu_range(e_cores_str))
            except Exception:
                pass

        if not is_hybrid:
            p_cores = physical_cores
            e_cores = 0

        # Cálculo de vCPUs sugeridas:
        # Se for sistema com 16 threads (ex: 12500H = 4P + 8E = 16t), sugerimos 12 vCPUs (deixando 4 para host Linux).
        # Se for 8 threads, sugerimos 6 vCPUs.
        # Se for 4 threads, sugerimos 2 ou 3 vCPUs.
        if total_threads >= 16:
            suggested_vcpus = total_threads - 4
        elif total_threads >= 12:
            suggested_vcpus = total_threads - 2
        elif total_threads >= 8:
            suggested_vcpus = total_threads - 2
        elif total_threads >= 4:
            suggested_vcpus = total_threads - 1
        else:
            suggested_vcpus = total_threads

        return CPUProfile(
            model_name=model_name,
            total_threads=total_threads,
            physical_cores=physical_cores,
            p_cores=p_cores,
            e_cores=e_cores,
            is_hybrid=is_hybrid,
            suggested_vcpus=max(2, suggested_vcpus),
        )

    @classmethod
    def _analyze_ram(cls) -> RAMProfile:
        total_gb = 8.0
        available_gb = 4.0

        try:
            with open("/proc/meminfo", "r") as f:
                meminfo = {}
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = parts[1].strip().split()[0]
                        meminfo[key] = int(val)
                total_kb = meminfo.get("MemTotal", 8 * 1024 * 1024)
                avail_kb = meminfo.get("MemAvailable", total_kb // 2)
                total_gb = round(total_kb / (1024 * 1024), 1)
                available_gb = round(avail_kb / (1024 * 1024), 1)
        except Exception:
            pass

        # Regra de ouro de alocação de RAM para o macOS sem disparar systemd-oomd no Linux:
        # Host >= 32GB -> VM 24GB
        # Host >= 16GB -> VM 8GB a 10GB (evita os 90% de limite do OOM)
        # Host >= 8GB  -> VM 4GB a 6GB
        if total_gb >= 30:
            suggested_ram = 24
        elif total_gb >= 24:
            suggested_ram = 16
        elif total_gb >= 14:
            suggested_ram = 8
        elif total_gb >= 8:
            suggested_ram = 4
        else:
            suggested_ram = 2

        return RAMProfile(
            total_gb=total_gb,
            available_gb=available_gb,
            suggested_vm_ram_gb=suggested_ram,
        )

    @staticmethod
    def _parse_cpu_range(range_str: str) -> list[int]:
        cpus = []
        for part in range_str.split(","):
            if "-" in part:
                start, end = part.split("-")
                cpus.extend(range(int(start), int(end) + 1))
            elif part.isdigit():
                cpus.append(int(part))
        return cpus
