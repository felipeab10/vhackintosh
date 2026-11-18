"""macserial-backed, per-VM OpenCore identity generation and validation."""
from __future__ import annotations

import re
import secrets
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from core.config import SMBIOSConfig

MACSERIAL_CANDIDATES = (
    Path("/opt/vhackintosh/resources/macserial/macserial"),
    Path("/opt/vhackintosh/bin/macserial"),
    Path(__file__).resolve().parent.parent / "resources/macserial/macserial",
    Path(__file__).resolve().parent.parent / "bin" / "macserial",
)

# Models a user may pick when minting an identity for a VM. Each entry is the
# newest macOS the model can install, measured against Apple's compatibility
# pages (Tahoe 26, Sequoia 15, Sonoma 14, Ventura 13). A model absent from this
# table is refused instead of guessed: `macserial -a` lists 121 models back to
# MacBook1,1 (2006), so it is a serial generator, not an allowlist.
#
# The identity is now injected into the existing config.plist
# (`core/disk.py::inject_opencore_config`), so the EFI builder is no longer in
# this path and its `--model` list is not a constraint here. Two measurements
# still are, and a test enforces both:
#   1. `macserial -m <model> -g -n 1` returns a pair whose `--info` decode is the
#      same model (all nine below verified 2026-11-18);
#   2. Apple's current compatibility list still includes the model for the macOS
#      versions the appliance offers (floor Ventura 13).
# The only remaining `reims-util` constraint is the neutral ISO EFI, built with
# `--model iMac20,1` in `appliance/build_opencore.py:209`; it does not limit
# which model a single VM may claim.
EDIT_MODEL_MAX_MACOS: dict[str, str] = {
    "iMac18,3": "ventura",
    "iMac19,1": "sequoia",
    "iMac20,1": "tahoe",
    "iMac20,2": "tahoe",
    "iMacPro1,1": "sequoia",
    "MacBookPro15,1": "sequoia",
    "MacBookPro16,1": "tahoe",
    "Macmini8,1": "sequoia",
    "MacPro7,1": "tahoe",
}

# Ordered release ranks. macOS 26 (Tahoe) follows 15, so the numbers are not
# contiguous on purpose; only their order is used.
MACOS_VERSION_RANK: dict[str, int] = {
    "ventura": 13,
    "sonoma": 14,
    "sequoia": 15,
    "tahoe": 26,
}

# Factory prefixes macserial emits for the models above and that the appliance
# has shipped (C02/F5K/C07 measured; D25/G08/C17 from the vendored generator
# table in the ISO payload). A serial with another prefix is refused.
EDIT_FACTORY_CODES = frozenset({"C02", "C07", "D25", "F5K", "G08", "C17"})

# The neutral identity burned into the ISO EFI (appliance/build_opencore.py).
# Every VM starts from it, so it must never be applied as a per-VM identity.
NEUTRAL_SERIALS = frozenset({"C02VHA000001"})
NEUTRAL_MLBS = frozenset({"C02VHA00000000001"})
NIL_UUID = "00000000-0000-0000-0000-000000000000"

EDIT_FIELDS = ("model", "serial_number", "board_serial", "smuuid", "rom")


class SMBIOSError(RuntimeError):
    """Identity could not be safely generated or applied."""


class SMBIOSEditError(SMBIOSError):
    """A user-supplied identity cannot be applied to an existing VM."""


@dataclass(frozen=True)
class SMBIOSEditValidation:
    """Normalized identity plus what the caller should show and audit."""

    identity: SMBIOSConfig
    warnings: tuple[str, ...] = ()
    changed_fields: tuple[str, ...] = ()


class GenSMBIOS:
    RECOMMENDED_MODELS = {"tahoe": "iMac20,1", "sequoia": "iMac20,1", "ventura": "iMac20,1"}

    @classmethod
    def _macserial(cls) -> Path:
        for candidate in MACSERIAL_CANDIDATES:
            if candidate.is_file():
                if not candidate.stat().st_mode & 0o111:
                    try:
                        candidate.chmod(candidate.stat().st_mode | 0o755)
                    except OSError:
                        pass
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
        """Write the identity into an existing config.plist, in place.

        This is the whole identity edit when the EFI is not rebuilt: with
        `PlatformInfo.Automatic = true`, OpenCore derives Board-ID and
        FirmwareFeatures from `SystemProductName` and rewrites the platform
        NVRAM variables from `Generic`, so the five fields below are the only
        thing that has to change and `DataHub`/`PlatformNVRAM`/`SMBIOS` must only
        be cleaned of stale duplicates.

        `Generic` is created when the config does not have it yet (an EFI whose
        identity lived in the legacy sections); a missing `PlatformInfo` is
        still refused, because that is a config this code cannot reason about.

        `show_picker` is accepted for call-site compatibility and deliberately
        not applied: an identity edit must not change boot behavior, so
        `Misc > Boot > ShowPicker` and every other key stay as they are.
        """
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
        if "Generic" in platform and not isinstance(platform["Generic"], dict):
            raise SMBIOSError("OpenCore PlatformInfo.Generic is not a dictionary")
        generic = platform.setdefault("Generic", {})
        generic.update({
            "SystemProductName": smbios.model,
            "SystemSerialNumber": smbios.serial_number,
            "MLB": smbios.board_serial,
            "SystemUUID": smbios.smuuid,
            "ROM": bytes.fromhex(smbios.rom),
        })
        for section in ("DataHub", "PlatformNVRAM", "SMBIOS"):
            values = platform.get(section)
            if not isinstance(values, dict):
                continue
            for key in ("SystemSerialNumber", "SystemUUID", "MLB", "ROM", "BoardSerialNumber"):
                values.pop(key, None)
        tmp = path.with_name(path.name + ".tmp")
        try:
            with tmp.open("wb") as fh:
                plistlib.dump(data, fh, sort_keys=False)
            with tmp.open("rb") as fh:
                check = plistlib.load(fh).get("PlatformInfo", {}).get("Generic", {})
            if not (
                check.get("SystemProductName") == smbios.model
                and check.get("SystemSerialNumber") == smbios.serial_number
                and check.get("MLB") == smbios.board_serial
                and check.get("SystemUUID") == smbios.smuuid
                and check.get("ROM") == bytes.fromhex(smbios.rom)
            ):
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
        if path.is_symlink() or not path.is_file():
            return {"ok": False, "reason": "OpenCore EFI image missing"}
        # qcow2 is copied/extracted by the disk provisioner helper in normal use;
        # callers without guestfish receive an explicit unavailable result.
        if not shutil.which("guestfish"):
            return {"ok": False, "reason": "guestfish unavailable; EFI could not be inspected"}
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            plist_path = Path(td) / "config.plist"
            try:
                result = subprocess.run(
                    ["guestfish", "--ro", "-a", str(path), "-m", "/dev/sda1", "download", "/EFI/OC/config.plist", str(plist_path)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=20, check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return {"ok": False, "reason": "OpenCore config could not be read"}
            if result.returncode:
                return {"ok": False, "reason": "OpenCore config could not be read"}
            try:
                if plist_path.stat().st_size > 2 * 1024 * 1024:
                    return {"ok": False, "reason": "OpenCore config could not be read"}
                config = plistlib.loads(plist_path.read_bytes())
            except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
                return {"ok": False, "reason": "OpenCore config could not be read"}
        if not isinstance(config, dict):
            return {"ok": False, "reason": "OpenCore config could not be read"}
        platform = config.get("PlatformInfo")
        generic = platform.get("Generic") if isinstance(platform, dict) else None
        if not isinstance(generic, dict):
            return {"ok": False, "reason": "identity fields missing or differ; Apple activation remains unverified"}
        try:
            matches = all((
                generic.get("SystemProductName") == vm.smbios.model,
                generic.get("SystemSerialNumber") == vm.smbios.serial_number,
                generic.get("MLB") == vm.smbios.board_serial,
                generic.get("SystemUUID") == vm.smbios.smuuid,
                generic.get("ROM") == bytes.fromhex(vm.smbios.rom),
            ))
        except (TypeError, ValueError):
            matches = False
        return {"ok": matches, "reason": "local SMBIOS fields match" if matches else "identity fields missing or differ; Apple activation remains unverified"}

    @classmethod
    def require_supported_model(cls, model: str, macos_version: Optional[str] = None) -> str:
        """Refuse a model whose identity cannot be presented coherently.

        The table is the allowlist, not `macserial -a`: that command reports 121
        models since 2006 and generating a pair for MacBook1,1 would produce a
        serial that no current macOS installs on. A model may also be too old for
        the macOS version already chosen for the VM. Since the identity is
        injected in place, the EFI builder's model list plays no part here.
        """
        clean = (model or "").strip()
        if clean not in EDIT_MODEL_MAX_MACOS:
            raise SMBIOSEditError(
                f"Model {clean!r} is not on the supported edit list; "
                "add it only after macserial -l and Apple's current macOS compatibility list confirm it"
            )
        if macos_version:
            requested = MACOS_VERSION_RANK.get(macos_version.strip().lower())
            newest = MACOS_VERSION_RANK[EDIT_MODEL_MAX_MACOS[clean]]
            if requested is not None and requested > newest:
                raise SMBIOSEditError(
                    f"{clean} cannot run {macos_version}; its newest supported macOS is "
                    f"{EDIT_MODEL_MAX_MACOS[clean]}"
                )
        return clean

    @classmethod
    def generate_for_model(cls, model: str, macos_version: str = "sequoia") -> SMBIOSConfig:
        """Mint a fresh identity for a model the user picked (never persists it)."""
        cls.require_supported_model(model, macos_version)
        return cls.generate(macos_version, model_override=model.strip())

    @staticmethod
    def _normalize_identity(candidate: SMBIOSConfig) -> tuple[SMBIOSConfig, tuple[str, ...]]:
        """Uppercase/canonicalize the four editable strings; report what changed."""
        if not isinstance(candidate, SMBIOSConfig):
            raise SMBIOSEditError("SMBIOS identity must be an SMBIOSConfig")
        raw = {
            "model": candidate.model,
            "serial_number": candidate.serial_number,
            "board_serial": candidate.board_serial,
            "smuuid": candidate.smuuid,
            "rom": candidate.rom,
        }
        for name, value in raw.items():
            if not isinstance(value, str):
                raise SMBIOSEditError(f"SMBIOS {name} must be a string")
        identity = SMBIOSConfig(
            model=raw["model"].strip(),
            serial_number=raw["serial_number"].strip().upper(),
            board_serial=raw["board_serial"].strip().upper(),
            rom=raw["rom"].strip().upper().replace(":", "").replace("-", ""),
            smuuid="",
        )
        try:
            identity.smuuid = str(uuid.UUID(raw["smuuid"].strip())).upper()
        except (ValueError, AttributeError):
            raise SMBIOSEditError("SMBIOS UUID is missing or malformed") from None
        normalized = tuple(
            name
            for name in ("serial_number", "board_serial", "rom", "smuuid")
            if raw[name].strip() != getattr(identity, name)
        )
        warnings = tuple(f"{name} was normalized before applying" for name in normalized)
        return identity, warnings

    @classmethod
    def validate_for_edit(
        cls,
        candidate: SMBIOSConfig,
        *,
        previous: Optional[SMBIOSConfig] = None,
        macos_version: Optional[str] = None,
        other_identities: Iterable[SMBIOSConfig] = (),
    ) -> SMBIOSEditValidation:
        """Validate a user-typed identity for an existing VM. Pure: no I/O.

        This is shape and coherence only; it deliberately does not replace
        `validate`, which stays the syntax gate used by generation and
        injection. The serial<->model decode and the MLB checksum need the
        macserial binary, so they live in `verify_with_macserial`.
        """
        identity, warnings = cls._normalize_identity(candidate)

        model = cls.require_supported_model(identity.model, macos_version)

        serial = identity.serial_number
        if not re.fullmatch(r"[A-Z0-9]{12}", serial):
            raise SMBIOSEditError("SMBIOS serial must be 12 uppercase letters or digits")
        if serial[:3] not in EDIT_FACTORY_CODES:
            raise SMBIOSEditError(f"SMBIOS serial factory code {serial[:3]!r} is not a known Apple code")
        if serial in NEUTRAL_SERIALS:
            raise SMBIOSEditError("The ISO neutral serial cannot be applied to a VM")

        mlb = identity.board_serial
        if not re.fullmatch(r"[A-Z0-9]{17}", mlb):
            raise SMBIOSEditError("SMBIOS MLB must be 17 uppercase letters or digits")
        if mlb in NEUTRAL_MLBS:
            raise SMBIOSEditError("The ISO neutral MLB cannot be applied to a VM")
        if mlb == serial or len(set(mlb)) == 1:
            raise SMBIOSEditError("SMBIOS MLB is not a plausible board serial")
        if mlb[:3] != serial[:3]:
            raise SMBIOSEditError(
                "SMBIOS MLB must start with the same 3-character factory code as the serial"
            )

        if identity.smuuid == NIL_UUID:
            raise SMBIOSEditError("SMBIOS UUID cannot be the nil UUID")

        rom = identity.rom
        if not re.fullmatch(r"[0-9A-F]{12}", rom):
            raise SMBIOSEditError("SMBIOS ROM must be 6 bytes of uppercase hex")
        first_octet = int(rom[:2], 16)
        if rom == "000000000000" or rom == "FFFFFFFFFFFF":
            raise SMBIOSEditError("SMBIOS ROM cannot be all zero or the broadcast address")
        if first_octet & 0x01:
            raise SMBIOSEditError("SMBIOS ROM is a multicast/broadcast address")
        if not first_octet & 0x02:
            raise SMBIOSEditError("SMBIOS ROM must be a locally administered address")
        if first_octet != 0x02:
            warnings = warnings + (
                "ROM uses a locally administered prefix other than 02; the appliance generator always uses 02",
            )

        if previous is not None and previous.model != model and (previous.serial_number or "").strip().upper() == serial:
            raise SMBIOSEditError(
                "Changing the model requires a new serial/MLB pair; generate a fresh identity"
            )

        for other in other_identities:
            conflicts = [
                name
                for name in ("serial_number", "board_serial", "smuuid", "rom")
                if getattr(identity, name)
                and getattr(identity, name).lower() == (getattr(other, name) or "").lower()
            ]
            if conflicts:
                raise SMBIOSEditError(
                    "SMBIOS identity conflicts with another VM on " + ", ".join(conflicts)
                )

        changed: tuple[str, ...] = ()
        if previous is not None:
            changed = tuple(
                name
                for name in EDIT_FIELDS
                if (getattr(identity, name) or "").lower() != (getattr(previous, name) or "").strip().lower()
            )
        return SMBIOSEditValidation(identity=identity, warnings=warnings, changed_fields=changed)

    @classmethod
    def decode_serial(cls, serial: str) -> dict:
        """Decode a serial with macserial --info (the authoritative model check).

        macserial exits 0 even for a serial it flags as invalid, so the model is
        read from the decoded line instead of the return code.
        """
        tool = cls._macserial()
        try:
            result = subprocess.run(
                [str(tool), "--info", serial],
                capture_output=True, text=True, check=False, timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SMBIOSError("macserial could not decode the serial") from exc
        output = (result.stdout or "") + (result.stderr or "")
        decoded = re.search(r"^\s*Model:\s*(\S{1,6})\s*-\s*(.+?)\s*$", output, re.MULTILINE)
        system = re.search(r"^\s*SystemModel:\s*(.+?)\s*$", output, re.MULTILINE)
        if decoded is None:
            raise SMBIOSError("macserial did not decode the serial")
        model_code, model = decoded.group(1), decoded.group(2)
        return {
            "model_code": model_code,
            "model": None if model.lower().startswith("unknown") else model,
            "system_model": system.group(1) if system else None,
            "warnings": [line.strip() for line in output.splitlines() if line.strip().startswith("WARN")],
        }

    @classmethod
    def verify_with_macserial(cls, smbios: SMBIOSConfig) -> dict:
        """Check serial<->model and the MLB checksum with the pinned binary.

        Fail closed: an unavailable or unparseable macserial means `ok` is False,
        because the caller is about to rewrite a VM's Apple identity.
        """
        try:
            decoded = cls.decode_serial(smbios.serial_number)
        except SMBIOSError as exc:
            return {"ok": False, "model_matches": None, "mlb_checksum": "unavailable", "reason": str(exc)}
        model_matches = decoded["model"] == smbios.model and not decoded["warnings"]
        try:
            tool = cls._macserial()
            result = subprocess.run(
                [str(tool), "--verify", smbios.board_serial],
                capture_output=True, text=True, check=False, timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return {"ok": False, "model_matches": model_matches, "mlb_checksum": "unavailable",
                    "reason": "macserial could not verify the MLB"}
        output = ((result.stdout or "") + (result.stderr or "")).lower()
        if "invalid mlb checksum" in output:
            mlb_checksum = "invalid"
        elif "valid mlb checksum" in output:
            mlb_checksum = "valid"
        else:
            mlb_checksum = "unavailable"
        ok = bool(model_matches) and mlb_checksum == "valid"
        reason = "identity matches the decoded serial and the MLB checksum"
        if not model_matches:
            reason = f"serial decodes to {decoded['model']!r}, not {smbios.model!r}"
        elif mlb_checksum != "valid":
            reason = "MLB checksum is not valid"
        return {"ok": ok, "model_matches": model_matches, "mlb_checksum": mlb_checksum, "reason": reason}
