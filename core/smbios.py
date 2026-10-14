"""macserial-backed, per-VM OpenCore identity generation and validation."""
from __future__ import annotations

import re
import secrets
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from core.config import SMBIOSConfig

MACSERIAL_CANDIDATES = (
    Path("/opt/vhackintosh/resources/macserial/macserial"),
    Path("/opt/vhackintosh/bin/macserial"),
    Path(__file__).resolve().parent.parent / "resources/macserial/macserial",
    Path(__file__).resolve().parent.parent / "bin" / "macserial",
)


class SMBIOSError(RuntimeError):
    """Identity could not be safely generated or applied."""


class GenSMBIOS:
    RECOMMENDED_MODELS = {"tahoe": "iMac20,1", "sequoia": "iMac20,1", "ventura": "iMac20,1"}

    @classmethod
    def _macserial(cls) -> Path:
        for candidate in MACSERIAL_CANDIDATES:
            if candidate.is_file():
                if not candidate.stat().st_mode & 0o111:
                    raise SMBIOSError(f"macserial is not executable: {candidate}")
                return candidate
        found = shutil.which("macserial")
        if found:
            return Path(found)
        raise SMBIOSError("macserial is unavailable; install the pinned OpenCorePkg tool")

    @classmethod
    def generate(cls, macos_version: str = "sequoia", model_override: Optional[str] = None) -> SMBIOSConfig:
        model = model_override or cls.RECOMMENDED_MODELS.get(macos_version.lower(), "iMac20,1")
        tool = cls._macserial()
        try:
            result = subprocess.run(
                [str(tool), "-m", model, "-g", "-n", "1"],
                capture_output=True, text=True, check=True, timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SMBIOSError("macserial failed to generate an identity") from exc
        records = []
        for line in result.stdout.splitlines():
            fields = [field.strip() for field in line.split("|")]
            if len(fields) >= 2:
                records.append(fields)
        if len(records) != 1:
            raise SMBIOSError(f"macserial did not return exactly one valid record for {model}")
        record = records[0]
        serial, mlb = record[0], record[1]
        if len(serial) < 10 or not re.fullmatch(r"[A-Z0-9]+", serial):
            raise SMBIOSError("macserial returned an invalid serial")
        if not re.fullmatch(r"[A-Z0-9]{17}", mlb):
            raise SMBIOSError("macserial returned an invalid MLB")
        # OpenCorePkg macserial generates serial+MLB; OpenCore requires a UUID
        # and ROM too. Generate those independently for this VM. Keep all fields
        # unique against identities already present in the store, retrying only
        # collisions before returning a candidate to the caller.
        from core.config import CONFIG_DIR, VMManagerStore
        # Avoid constructing a default store when running tests or embedded
        # callers that have not created the user config directory yet.
        existing = [vm.smbios for vm in VMManagerStore().list_vms()] if CONFIG_DIR.exists() else []
        for _ in range(8):
            identity = SMBIOSConfig(
                model=model,
                serial_number=serial,
                board_serial=mlb,
                smuuid=str(uuid.uuid4()).upper(),
                rom="02" + secrets.token_hex(5).upper(),
            )
            cls.validate(identity)
            if not any(cls._identities_conflict(identity, other) for other in existing):
                return identity
            result = subprocess.run(
                [str(tool), "-m", model, "-g", "-n", "1"],
                capture_output=True, text=True, check=True, timeout=20,
            )
            records = [line.split("|") for line in result.stdout.splitlines() if "|" in line]
            if len(records) != 1 or len(records[0]) < 2:
                raise SMBIOSError("macserial did not return exactly one valid record")
            serial, mlb = (field.strip() for field in records[0][:2])
            if not re.fullmatch(r"[A-Z0-9]{10,}", serial) or not re.fullmatch(r"[A-Z0-9]{17}", mlb):
                raise SMBIOSError("macserial returned a malformed identity")
        raise SMBIOSError("macserial repeatedly generated an identity already used by another VM")

    @staticmethod
    def _identities_conflict(left: SMBIOSConfig, right: SMBIOSConfig) -> bool:
        return any((
            left.serial_number and left.serial_number == right.serial_number,
            left.board_serial and left.board_serial == right.board_serial,
            left.smuuid and left.smuuid.lower() == right.smuuid.lower(),
            left.rom and left.rom.lower() == right.rom.lower(),
        ))

    @classmethod
    def validate(cls, smbios: SMBIOSConfig) -> None:
        if not smbios.model or not re.fullmatch(r"[A-Za-z0-9]+(?:,[0-9]+)?", smbios.model):
            raise SMBIOSError("SMBIOS model is missing or malformed")
        if not re.fullmatch(r"[A-Z0-9]{10,}", smbios.serial_number):
            raise SMBIOSError("SMBIOS serial is missing or malformed")
        if not re.fullmatch(r"[A-Z0-9]{17}", smbios.board_serial):
            raise SMBIOSError("SMBIOS MLB is missing or malformed")
        try:
            uuid.UUID(smbios.smuuid)
        except (ValueError, AttributeError) as exc:
            raise SMBIOSError("SMBIOS UUID is missing or malformed") from exc
        if not re.fullmatch(r"[0-9A-Fa-f]{12}", smbios.rom):
            raise SMBIOSError("SMBIOS ROM is missing or malformed")

    @classmethod
    def inject_into_config_plist(cls, config_plist_path: str | Path, smbios: SMBIOSConfig, show_picker: bool = False) -> bool:
        import plistlib
        cls.validate(smbios)
        path = Path(config_plist_path)
        if not path.exists():
            raise SMBIOSError("OpenCore config.plist was not found")
        with path.open("rb") as fh:
            data = plistlib.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("PlatformInfo"), dict):
            raise SMBIOSError("OpenCore plist has no PlatformInfo dictionary")
        platform = data["PlatformInfo"]
        platform["Automatic"] = True
        platform["UpdateDataHub"] = True
        platform["UpdateNVRAM"] = True
        platform["UpdateSMBIOS"] = True
        platform["UpdateSMBIOSMode"] = "Create"
        if not isinstance(platform.get("Generic"), dict):
            raise SMBIOSError("OpenCore PlatformInfo.Generic is not a dictionary")
        generic = platform["Generic"]
        generic.update({
            "SystemProductName": smbios.model,
            "SystemSerialNumber": smbios.serial_number,
            "MLB": smbios.board_serial,
            "SystemUUID": smbios.smuuid,
            "ROM": bytes.fromhex(smbios.rom),
        })
        for section in ("DataHub", "PlatformNVRAM", "SMBIOS"):
            values = platform.setdefault(section, {})
            for key in ("SystemSerialNumber", "SystemUUID", "MLB", "ROM", "BoardSerialNumber"):
                values.pop(key, None)
        tmp = path.with_name(path.name + ".tmp")
        try:
            with tmp.open("wb") as fh:
                plistlib.dump(data, fh, sort_keys=False)
            with tmp.open("rb") as fh:
                check = plistlib.load(fh)["PlatformInfo"]["Generic"]
            if check.get("SystemSerialNumber") != smbios.serial_number or check.get("MLB") != smbios.board_serial:
                raise SMBIOSError("OpenCore identity post-write validation failed")
            tmp.replace(path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return True

    @classmethod
    def inspect_vm(cls, vm, opencore_path: str | Path) -> dict:
        """Read-only local consistency check; never returns full identity values."""
        import plistlib
        path = Path(opencore_path)
        if not path.exists():
            return {"ok": False, "reason": "OpenCore EFI image missing"}
        # qcow2 is copied/extracted by the disk provisioner helper in normal use;
        # callers without guestfish receive an explicit unavailable result.
        if not shutil.which("guestfish"):
            return {"ok": False, "reason": "guestfish unavailable; EFI could not be inspected"}
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            plist_path = Path(td) / "config.plist"
            result = subprocess.run(["guestfish", "--ro", "-a", str(path), "-m", "/dev/sda1", "download", "/EFI/OC/config.plist", str(plist_path)], capture_output=True)
            if result.returncode:
                return {"ok": False, "reason": "OpenCore config could not be read"}
            config = plistlib.loads(plist_path.read_bytes())
        generic = config.get("PlatformInfo", {}).get("Generic", {})
        matches = all((
            generic.get("SystemProductName") == vm.smbios.model,
            generic.get("SystemSerialNumber") == vm.smbios.serial_number,
            generic.get("MLB") == vm.smbios.board_serial,
            generic.get("SystemUUID") == vm.smbios.smuuid,
            generic.get("ROM") == bytes.fromhex(vm.smbios.rom),
        ))
        return {"ok": matches, "reason": "local SMBIOS fields match" if matches else "identity fields missing or differ; Apple activation remains unverified"}
