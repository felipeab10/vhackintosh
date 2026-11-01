"""Persistent local activation request identity; contains no admin credentials."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_CODE_RE = re.compile(r"^VH-[A-F0-9]{4}(?:-[A-F0-9]{4}){5}$")
_LEGACY_CODE_RE = re.compile(r"^VH-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}$")
_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_inventory(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Inventário de hardware inválido.")
    expected = {"cpu_model", "memory_total_bytes", "gpus", "reims_vgpu_repository", "reims_vgpu_release"}
    if set(value) != expected:
        raise ValueError("O inventário de ativação aceita apenas CPU, memória, GPUs e release Reims.")
    cpu_model = str(value.get("cpu_model") or "desconhecido")[:160]
    memory = value.get("memory_total_bytes")
    if not cpu_model.strip() or not all(char.isprintable() for char in cpu_model):
        raise ValueError("Modelo do processador inválido.")
    if isinstance(memory, bool) or not isinstance(memory, int) or not 256 * 1024**2 <= memory <= 16 * 1024**4:
        raise ValueError("Memória total inválida.")
    repository = value.get("reims_vgpu_repository")
    release = value.get("reims_vgpu_release")
    if repository != "felipeab10/reims-vgpu":
        raise ValueError("Repositório Reims inválido.")
    if release is not None and (not isinstance(release, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", release)):
        raise ValueError("Release Reims inválida.")
    gpus = value.get("gpus", [])
    if not isinstance(gpus, list) or len(gpus) > 16:
        raise ValueError("Inventário de GPU inválido.")
    safe_gpus = []
    for item in gpus:
        if not isinstance(item, dict):
            continue
        vendor = "".join(char for char in str(item.get("vendor") or "GPU") if char.isprintable())[:80]
        model = "".join(char for char in str(item.get("model") or "Desconhecida") if char.isprintable())[:160]
        safe_gpus.append({"vendor": vendor or "GPU", "model": model or "Desconhecida"})
    return {
        "cpu_model": cpu_model,
        "memory_total_bytes": memory,
        "gpus": safe_gpus,
        "reims_vgpu_repository": repository,
        "reims_vgpu_release": release,
    }


class WebActivationStore:
    """Keep the appliance identity and portal submission state across reboots."""

    def __init__(self, path: str | os.PathLike[str] = "/var/lib/vhackintosh/web/activation.json"):
        self.path = Path(path)

    def _read(self) -> dict[str, Any] | None:
        try:
            info = self.path.lstat()
            if not self.path.is_file() or info.st_mode & 0o077 or info.st_size > 32 * 1024:
                raise PermissionError("Estado local de ativação precisa ser privado e limitado.")
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if (
            not isinstance(value, dict)
            or value.get("schema_version") not in {1, 2}
            or not isinstance(value.get("appliance_id"), str)
            or not _ID_RE.fullmatch(value["appliance_id"])
            or not isinstance(value.get("activation_code"), str)
            or not (_CODE_RE.fullmatch(value["activation_code"]) or _LEGACY_CODE_RE.fullmatch(value["activation_code"]))
            or value.get("status") not in {"not_submitted", "pending", "approved", "rejected"}
        ):
            raise ValueError("Estado local de ativação inválido.")
        return value

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o750)
        descriptor, temporary = tempfile.mkstemp(prefix=".activation.", dir=self.path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def get_or_create(self) -> dict[str, Any]:
        with _LOCK:
            value = self._read()
            if value is not None and value.get("schema_version") == 1:
                # Old console-issued values were bootstrap-only codes with less
                # entropy. Rotate them before using the new portal workflow.
                token = secrets.token_hex(12).upper()
                value.update({
                    "schema_version": 2,
                    "activation_code": "VH-" + "-".join(token[index:index + 4] for index in range(0, len(token), 4)),
                    "status": "not_submitted",
                    "updated_at": _now(),
                    "portal_request_id": None,
                    "inventory": None,
                })
                self._write(value)
            if value is None:
                token = secrets.token_hex(12).upper()
                value = {
                    "schema_version": 2,
                    "appliance_id": secrets.token_hex(16),
                    "activation_code": "VH-" + "-".join(token[index:index + 4] for index in range(0, len(token), 4)),
                    "status": "not_submitted",
                    "created_at": _now(),
                    "updated_at": _now(),
                    "portal_request_id": None,
                    "inventory": None,
                }
                self._write(value)
            return dict(value)

    def save_submission(self, inventory: dict[str, Any], *, portal_request_id: str | None = None) -> dict[str, Any]:
        safe = _safe_inventory(inventory)
        with _LOCK:
            value = self.get_or_create()
            value.update({"status": "pending", "updated_at": _now(), "portal_request_id": portal_request_id, "inventory": safe})
            self._write(value)
            return dict(value)

    def update_portal_status(self, status: str) -> dict[str, Any]:
        if status not in {"pending", "approved", "rejected"}:
            raise ValueError("Estado de ativação remoto inválido.")
        with _LOCK:
            value = self.get_or_create()
            value.update({"status": status, "updated_at": _now()})
            self._write(value)
            return dict(value)
