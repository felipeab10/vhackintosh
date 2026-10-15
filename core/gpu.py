"""
vHackintosh — GPU Compatibility Checker & VFIO Helper
Identifica GPUs do sistema, verifica compatibilidade com Reims vGPU (Vulkan)
e analisa grupos IOMMU para suporte a VFIO Passthrough.
"""

from __future__ import annotations
import ctypes
import os
import re
import struct
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple


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
        elif vendor_id == "1234":
            vendor_name = "QEMU Standard VGA"
        elif vendor_id == "1b36":
            vendor_name = "Red Hat QEMU"
        elif vendor_id == "15ad":
            vendor_name = "VMware SVGA"
        elif vendor_id == "80ee":
            vendor_name = "VirtualBox Graphics"

        iommu_group = cls._get_iommu_group(pci_slot)
        is_vfio = cls._check_vfio_driver(pci_slot)

        # Compatibilidade com Reims vGPU (Vulkan):
        # NVIDIA GTX 900+, RTX series, AMD GCN 4+, Intel Gen9+ possuem suporte Vulkan 1.2+
        reims_compatible = False
        passthrough_compatible = False
        recommendation = "Incompatível com Reims vGPU (controlador gráfico sem suporte Vulkan 1.2+)"

        if vendor_name == "NVIDIA":
            reims_compatible = True
            passthrough_compatible = False  # macOS moderno não tem driver bare metal para Turing/Ampere/Ada
            recommendation = "Reims vGPU (Aceleração Metal sobre Vulkan em sessão Wayland nativa)"
        elif vendor_name == "AMD":
            reims_compatible = True
            # GPUs AMD Polaris/Navi suportam VFIO passthrough nativo no macOS
            passthrough_compatible = True
            recommendation = "Compatível com Reims vGPU e com Passthrough direto VFIO-PCI"
        elif vendor_name == "Intel":
            reims_compatible = True
            passthrough_compatible = False
            recommendation = "Reims vGPU (iGPU Vulkan)"
        elif vendor_id in ("1234", "1b36", "15ad", "80ee"):
            reims_compatible = False
            passthrough_compatible = False
            recommendation = "Controlador gráfico emulado/virtual — sem suporte a aceleração Vulkan de hardware"

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

    @classmethod
    def check_vulkan_host_readiness(cls) -> Tuple[bool, str]:
        """
        Verifica antecipadamente se o host possui suporte funcional à API Vulkan 1.2+
        e pelo menos uma GPU física capaz de atender ao Reims vGPU.
        
        Isso impede desperdiçar minutos compilando código quando o sistema estiver
        emulando vídeo básico (ex.: em testes aninhados no QEMU sem passthrough).
        """
        # 1. Carrega dinamicamente a biblioteca do loader Vulkan
        lib = None
        for name in ["libvulkan.so.1", "libvulkan.so"]:
            try:
                lib = ctypes.CDLL(name)
                break
            except Exception:
                pass

        if not lib:
            return False, "Driver/Loader Vulkan ausente (libvulkan.so.1 não encontrada no host)."

        class VkApplicationInfo(ctypes.Structure):
            _fields_ = [
                ("sType", ctypes.c_int),
                ("pNext", ctypes.c_void_p),
                ("pApplicationName", ctypes.c_char_p),
                ("applicationVersion", ctypes.c_uint32),
                ("pEngineName", ctypes.c_char_p),
                ("engineVersion", ctypes.c_uint32),
                ("apiVersion", ctypes.c_uint32),
            ]

        class VkInstanceCreateInfo(ctypes.Structure):
            _fields_ = [
                ("sType", ctypes.c_int),
                ("pNext", ctypes.c_void_p),
                ("flags", ctypes.c_uint32),
                ("pApplicationInfo", ctypes.POINTER(VkApplicationInfo)),
                ("enabledLayerCount", ctypes.c_uint32),
                ("ppEnabledLayerNames", ctypes.POINTER(ctypes.c_char_p)),
                ("enabledExtensionCount", ctypes.c_uint32),
                ("ppEnabledExtensionNames", ctypes.POINTER(ctypes.c_char_p)),
            ]

        # VK_STRUCTURE_TYPE_APPLICATION_INFO = 1, VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO = 10
        # Solicita API Vulkan 1.2 mínimo: (1 << 22) | (2 << 12) = 0x00402000
        min_vulkan_1_2 = (1 << 22) | (2 << 12)
        app_info = VkApplicationInfo(
            sType=1,
            pNext=None,
            pApplicationName=b"vhackintosh-preflight",
            applicationVersion=1,
            pEngineName=b"reims-preflight",
            engineVersion=1,
            apiVersion=min_vulkan_1_2,
        )
        inst_info = VkInstanceCreateInfo(
            sType=10,
            pNext=None,
            flags=0,
            pApplicationInfo=ctypes.pointer(app_info),
            enabledLayerCount=0,
            ppEnabledLayerNames=None,
            enabledExtensionCount=0,
            ppEnabledExtensionNames=None,
        )

        instance = ctypes.c_void_p()
        res = lib.vkCreateInstance(ctypes.byref(inst_info), None, ctypes.byref(instance))
        if res != 0:
            return False, f"Falha ao inicializar o subsistema Vulkan no host (vkCreateInstance: código {res})."

        try:
            gpu_count = ctypes.c_uint32(0)
            lib.vkEnumeratePhysicalDevices(instance, ctypes.byref(gpu_count), None)
            if gpu_count.value == 0:
                return False, "Nenhuma GPU física com suporte a Vulkan foi encontrada no host."

            devs = (ctypes.c_void_p * gpu_count.value)()
            lib.vkEnumeratePhysicalDevices(instance, ctypes.byref(gpu_count), devs)

            lib.vkGetPhysicalDeviceProperties.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            lib.vkGetPhysicalDeviceProperties.restype = None

            compatible_gpus: List[str] = []
            for i in range(gpu_count.value):
                raw_buf = (ctypes.c_char * 2048)()
                lib.vkGetPhysicalDeviceProperties(devs[i], ctypes.byref(raw_buf))
                api_ver, driver_ver, vendor_id, dev_id, dev_type = struct.unpack_from("<IIIII", raw_buf, 0)
                dev_name = raw_buf[20:276].split(b"\x00")[0].decode("utf-8", "replace").strip()

                major = api_ver >> 22
                minor = (api_ver >> 12) & 0x3ff
                patch = api_ver & 0xfff

                # Desconsidera emuladores por software de CPU (deviceType == 4 / VK_PHYSICAL_DEVICE_TYPE_CPU)
                is_cpu_software = dev_type == 4 or any(x in dev_name.lower() for x in ["llvmpipe", "lavapipe", "software"])
                if is_cpu_software:
                    continue

                if api_ver >= min_vulkan_1_2:
                    compatible_gpus.append(f"{dev_name} (Vulkan {major}.{minor}.{patch})")

            if not compatible_gpus:
                return False, "Nenhuma GPU física com suporte a Vulkan 1.2+ encontrada (detectada apenas emulação por software ou versão inferior)."

            return True, f"{len(compatible_gpus)} GPU(s) física(s) compatível(is) com Vulkan 1.2+ detectada(s): {', '.join(compatible_gpus)}."
        finally:
            lib.vkDestroyInstance(instance, None)

