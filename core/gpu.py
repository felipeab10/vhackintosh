"""
vHackintosh — GPU Compatibility Checker & VFIO Helper
Identifica GPUs do sistema, verifica compatibilidade com Reims vGPU (Vulkan)
e analisa grupos IOMMU para suporte a VFIO Passthrough.
"""

from __future__ import annotations
import os
import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class GPUDevice:
    pci_slot: str
    vendor_id: str
    device_id: str
    vendor_name: str
    device_name: str
    iommu_group: Optional[int]
    is_vfio_stubbed: bool
    reims_vgpu_compatible: bool
    passthrough_compatible: bool
    recommendation: str


class GPUChecker:
    """Verifica GPUs disponíveis e compatibilidade com vGPU / VFIO."""

    @classmethod
    def list_gpus(cls) -> List[GPUDevice]:
        gpus: List[GPUDevice] = []
        try:
            lspci_out = subprocess.check_output(["lspci", "-Dnn"], text=True)
        except Exception:
            return gpus

        for line in lspci_out.splitlines():
            # Filtra controladores VGA e 3D
            if "VGA compatible controller" in line or "3D controller" in line:
                gpu = cls._parse_gpu_line(line)
                if gpu:
                    gpus.append(gpu)
        return gpus

    @classmethod
    def _parse_gpu_line(cls, line: str) -> Optional[GPUDevice]:
        # Ex: 0000:01:00.0 VGA compatible controller [0300]: NVIDIA Corporation GA107M [GeForce RTX 3050 Mobile] [10de:25a2] (rev a1)
        match = re.match(r"^([0-9a-fA-F:\.]+)\s+(?:VGA compatible controller|3D controller)\s+\[[0-9a-fA-F]+\]:\s+(.+?)\s+\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]", line)
        if not match:
            return None

        pci_slot = match.group(1)
        raw_name = match.group(2)
        vendor_id = match.group(3).lower()
        device_id = match.group(4).lower()

        vendor_name = "Desconhecido"
        if vendor_id == "10de":
            vendor_name = "NVIDIA"
        elif vendor_id == "1002":
            vendor_name = "AMD"
        elif vendor_id == "8086":
            vendor_name = "Intel"

        iommu_group = cls._get_iommu_group(pci_slot)
        is_vfio = cls._check_vfio_driver(pci_slot)

        # Compatibilidade com Reims vGPU (Vulkan):
        # NVIDIA GTX 900+, RTX series, AMD GCN 4+, Intel Gen9+ possuem suporte Vulkan 1.2+
        reims_compatible = True
        passthrough_compatible = False
        recommendation = ""

        if vendor_name == "NVIDIA":
            reims_compatible = True
            passthrough_compatible = False  # macOS moderno não tem driver bare metal para Turing/Ampere/Ada
            recommendation = "Reims vGPU (Aceleração Metal sobre Vulkan com modo X11/Xwayland recomendado)"
        elif vendor_name == "AMD":
            reims_compatible = True
            # GPUs AMD Polaris/Navi suportam VFIO passthrough nativo no macOS
            passthrough_compatible = True
            recommendation = "Compatível com Reims vGPU e com Passthrough direto VFIO-PCI"
        elif vendor_name == "Intel":
            reims_compatible = True
            passthrough_compatible = False
            recommendation = "Reims vGPU (iGPU Vulkan)"

        return GPUDevice(
            pci_slot=pci_slot,
            vendor_id=vendor_id,
            device_id=device_id,
            vendor_name=vendor_name,
            device_name=raw_name,
            iommu_group=iommu_group,
            is_vfio_stubbed=is_vfio,
            reims_vgpu_compatible=reims_compatible,
            passthrough_compatible=passthrough_compatible,
            recommendation=recommendation,
        )

    @staticmethod
    def _get_iommu_group(pci_slot: str) -> Optional[int]:
        iommu_path = f"/sys/bus/pci/devices/{pci_slot}/iommu_group"
        if os.path.exists(iommu_path) and os.path.islink(iommu_path):
            target = os.readlink(iommu_path)
            group_name = os.path.basename(target)
            if group_name.isdigit():
                return int(group_name)
        return None

    @staticmethod
    def _check_vfio_driver(pci_slot: str) -> bool:
        driver_path = f"/sys/bus/pci/devices/{pci_slot}/driver"
        if os.path.exists(driver_path) and os.path.islink(driver_path):
            target = os.readlink(driver_path)
            return os.path.basename(target) == "vfio-pci"
        return False
