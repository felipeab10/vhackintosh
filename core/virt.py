"""
vHackintosh — Virtualização aninhada (change appliance-i18n…, grupo 6/D6)

O appliance precisa que a VM enxergue VT-x/AMD-V (para Docker/UTM dentro do
macOS). Duas metades:
- host: módulo KVM carregado com `nested=1` — a ISO traz
  `/etc/modprobe.d/vhackintosh-nested.conf`, e `apply_nested()` tenta recarregar
  o módulo quando ele subiu sem a flag (falha honesta quando em uso: pede reboot);
- guest: `cpu_flags()` devolve `+vmx`/`+svm` para o campo `cpu_flags` do
  `vm.toml` (o ponto único de launch — core/reims_ctl.py — os embute).

Readiness (core/readiness.py) consome `nested_status()` para distinguir
"sem KVM" (crítico) de "sem nested" (aviso acionável).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

_NESTED_PATHS = {
    "intel": Path("/sys/module/kvm_intel/parameters/nested"),
    "amd": Path("/sys/module/kvm_amd/parameters/nested"),
}


def cpu_vendor() -> Optional[str]:
    """'intel' | 'amd' a partir do /proc/cpuinfo; None quando não reconhecido."""
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "GenuineIntel" in cpuinfo or "vendor_id\t: GenuineIntel" in cpuinfo:
        return "intel"
    if "AuthenticAMD" in cpuinfo:
        return "amd"
    return None


def nested_status() -> dict:
    """Estado do nested no host.

    Devolve `{vendor, module_loaded, nested_enabled, can_enable, reason}`:
    `nested_enabled` é o que a VM precisa; `can_enable` diz se o appliance
    consegue ligar agora (módulo não em uso / parâmetro configurável).
    """
    vendor = cpu_vendor()
    status: dict = {
        "vendor": vendor,
        "module_loaded": False,
        "nested_enabled": False,
        "can_enable": False,
        "reason": "",
    }
    if vendor is None:
        status["reason"] = "processador não reconhecido (nem Intel nem AMD)"
        return status

    path = _NESTED_PATHS[vendor]
    if not path.exists():
        # Módulo do KVM do vendor não carregado: a appliance ainda não subiu VM.
        status["reason"] = (
            f"módulo kvm_{vendor} não está carregado (carrega sozinho ao iniciar "
            "a primeira VM; a ISO já traz nested=1 em modprobe.d)"
        )
        status["can_enable"] = True
        return status

    status["module_loaded"] = True
    try:
        value = path.read_text(encoding="utf-8").strip().upper()
    except OSError as exc:
        status["reason"] = f"sem leitura de {path}: {exc}"
        return status

    status["nested_enabled"] = value in ("Y", "1")
    if status["nested_enabled"]:
        status["can_enable"] = True
        status["reason"] = "nested habilitado"
        return status

    # Módulo carregado sem nested: dá para tentar recarregar, a menos que o
    # módulo esteja em uso (VM ligada — o rmmod falha e dizemos isso).
    status["can_enable"] = True
    status["reason"] = (
        f"kvm_{vendor} carregado com nested={value}; o appliance tenta recarregar "
        "com nested=1 (se recusar, reinicie com a ISO já instalada)"
    )
    return status


def apply_nested(timeout: float = 15.0) -> tuple[bool, str]:
    """Tenta ligar nested agora: recarrega o módulo do vendor com nested=1.

    Sucesso quando o parâmetro sai lendo Y. Recusa (rmmod em uso) devolve
    False com a razão — o módulo modprobe.d da ISO resolve no próximo boot.
    """
    status = nested_status()
    vendor = status.get("vendor")
    if not vendor:
        return False, status["reason"]
    if status["nested_enabled"]:
        return True, "nested já habilitado"

    module = f"kvm_{vendor}"
    try:
        unload = subprocess.run(
            ["modprobe", "-r", module], capture_output=True, text=True, timeout=timeout
        )
        if unload.returncode != 0:
            return False, (
                f"{module} está em uso (VM ligada?): "
                f"{(unload.stderr or unload.stdout).strip() or 'rmmod recusado'}"
            )
        load = subprocess.run(
            ["modprobe", module, "nested=1"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if load.returncode != 0:
            return False, f"falha ao recarregar {module}: {load.stderr.strip()}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"não foi possível recarregar {module}: {exc}"

    after = nested_status()
    if after["nested_enabled"]:
        return True, f"{module} recarregado com nested=1"
    return False, f"{module} recarregado, mas nested continua desligado neste host"


def cpu_flags(baseline: str) -> str:
    """`cpu_flags` do vm.toml com +vmx/+svm anexados quando o host tem nested.

    Serve a CPU fixa Haswell-noTSX: a flag de virtualização é um bit que a
    extensão do host oferece — sem nested no host, o bit não existe e o QEMU
    recusa expô-lo, então só anexamos quando o host confirma.
    """
    status = nested_status()
    vendor = status.get("vendor")
    flag = {"intel": "+vmx", "amd": "+svm"}.get(vendor)
    if not flag or not status["nested_enabled"]:
        return baseline
    if flag in baseline:
        return baseline
    return f"{baseline},{flag}"
