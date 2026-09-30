"""
vHackintosh — GenSMBIOS Generator & OpenCore Injector
Gera seriais válidos da Apple e injeta automaticamente no config.plist do OpenCore.
"""

from __future__ import annotations
import uuid
import random
import string
import plistlib
from pathlib import Path
from typing import Dict, Optional
from core.config import SMBIOSConfig


class GenSMBIOS:
    """Gerador nativo de seriais da Apple compatível com OpenCore."""

    # Tabela de modelos ideais por versão do macOS
    RECOMMENDED_MODELS = {
        "tahoe": "MacPro7,1",
        "sequoia": "MacPro7,1",
        "sonoma": "MacPro7,1",
        "ventura": "MacPro7,1",
        "monterey": "iMacPro1,1",
        "bigsur": "iMacPro1,1",
    }

    # Prefixos de fábrica comuns (3 caracteres)
    FACTORY_CODES = ["C02", "F5K", "C07", "D25", "G08", "C17"]

    # Caracteres de ano/semana Apple
    YEAR_CHARS = "CDFGHJKLMNPQRSTVWXYZ"
    WEEK_CHARS = "123456789CDFGHJKLMNPQRSTVWXYZ"

    # Códigos de modelo de 4 caracteres para final do serial
    MODEL_CODES = {
        "MacPro7,1": "P7QM",
        "iMacPro1,1": "HX87",
        "MacBookPro16,1": "MD6N",
        "Macmini8,1": "JYVY",
    }

    @classmethod
    def generate(cls, macos_version: str = "sequoia", model_override: Optional[str] = None) -> SMBIOSConfig:
        model = model_override or cls.RECOMMENDED_MODELS.get(macos_version.lower(), "MacPro7,1")
        serial = cls._generate_serial(model)
        board_serial = cls._generate_mlb(serial)
        smuuid = str(uuid.uuid4()).upper()
        rom = cls._generate_rom()

        return SMBIOSConfig(
            model=model,
            serial_number=serial,
            board_serial=board_serial,
            smuuid=smuuid,
            rom=rom,
        )

    @classmethod
    def _generate_serial(cls, model: str) -> str:
        factory = random.choice(cls.FACTORY_CODES)
        year_week = random.choice(cls.YEAR_CHARS) + random.choice(cls.WEEK_CHARS)
        base_serial = factory + year_week
        random_part = "".join(random.choices(string.ascii_uppercase + string.digits, k=3))
        model_code = cls.MODEL_CODES.get(model, "P7QM")
        return f"{base_serial}{random_part}{model_code}"

    @classmethod
    def _generate_mlb(cls, serial: str) -> str:
        # MLB (Board Serial) tem 17 caracteres
        # Primeiros 3: fábrica, seguidos de código de placa (ex: 902, 001) + serial parcial
        factory = serial[:3] if len(serial) >= 3 else "C02"
        mid = f"{random.randint(10, 99)}0{random.randint(100, 999)}"
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        mlb = f"{factory}{mid}{suffix}"[:17]
        return mlb

    @classmethod
    def _generate_rom(cls) -> str:
        # MAC Address em hex (12 dígitos)
        mac_bytes = [random.randint(0x00, 0xFF) for _ in range(6)]
        # Define bit unicast localmente administrado
        mac_bytes[0] = (mac_bytes[0] & 0xFE) | 0x02
        return "".join(f"{b:02X}" for b in mac_bytes)

    @classmethod
    def inject_into_config_plist(cls, config_plist_path: str | Path, smbios: SMBIOSConfig) -> bool:
        """Injeta a estrutura SMBIOS gerada diretamente no config.plist do OpenCore."""
        path = Path(config_plist_path)
        if not path.exists():
            return False

        try:
            with open(path, "rb") as f:
                plist_data = plistlib.load(f)

            if "PlatformInfo" not in plist_data:
                plist_data["PlatformInfo"] = {}

            plist_data["PlatformInfo"]["Automatic"] = True
            plist_data["PlatformInfo"]["UpdateDataHub"] = True
            plist_data["PlatformInfo"]["UpdateNVRAM"] = True
            plist_data["PlatformInfo"]["UpdateSMBIOS"] = True
            plist_data["PlatformInfo"]["UpdateSMBIOSMode"] = "Create"

            if "Generic" not in plist_data["PlatformInfo"]:
                plist_data["PlatformInfo"]["Generic"] = {}

            generic = plist_data["PlatformInfo"]["Generic"]
            generic["SystemProductName"] = smbios.model
            generic["SystemSerialNumber"] = smbios.serial_number
            generic["MLB"] = smbios.board_serial
            generic["SystemUUID"] = smbios.smuuid
            generic["ROM"] = bytes.fromhex(smbios.rom) if smbios.rom else bytes([0] * 6)
            generic["AdviseFeatures"] = True
            generic["MaxBIOSVersion"] = False
            generic["ProcessorType"] = 0
            generic["SpoofVendor"] = True

            with open(path, "wb") as f:
                plistlib.dump(plist_data, f)

            return True
        except Exception:
            return False
