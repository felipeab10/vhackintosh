"""
vHackintosh — Integração com o reims-ctl (árvore nova do reims-vgpu)

O appliance é **frontend** do reims-ctl: ele provisiona discos e OpenCore
(`core/disk.py`), escreve o `vm.toml` no layout do reims-ctl, registra a VM em
`~/.config/reims-ctl/vms.toml` e delega o start do QEMU ao `reims-ctl start`.
O argv do QEMU é do upstream — o appliance não mantém cópia nenhuma dele, e o
QMP fica no socket fixo `<vmdir>/qmp.sock`, que o monitor já observa.

O que o appliance fixa no `vm.toml`:
- `cpus`/`memory_mib` vêm da sugestão por host (`core/hardware.py`);
- `cpu_model = "Skylake-Client-noTSX-IBRS"`, o default medido do upstream
  (docs/qemu.md, "x86-64 macOS guests: the CPU model") — model 94 que o XNU
  conhece, boota com ou sem o spoof de CPUID do OpenCore do template
  (`templates/README.md`); modelo fixo, quantidade por host;
- `extra_args = ["-action", "reboot=shutdown"]` preserva o contrato do
  appliance: um reboot do convidado **encerra** o QEMU e o laço da TUI
  relança (a alternativa, DeviceReset no lugar, é o que congela o host —
  ver o comentário longo em `ui/tui.py`);
- `installer` só existe na fase de instalação (imagem absoluta, in-place).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Optional

from core.config import VMConfig, CONFIG_DIR
from core.virt import cpu_flags as _nested_cpu_flags

# Baseline de CPU flags do q35-x86_64 do reims-ctl (vm.rs); o appliance só
# acrescenta +vmx/+svm quando o host tem nested habilitado (core/virt.py).
_BASE_CPU_FLAGS = "+ssse3,+sse4.2,+popcnt,+avx,+aes,+xsave,+xsaveopt,check"


def cpu_flags_str() -> str:
    return _nested_cpu_flags(_BASE_CPU_FLAGS)

# O mesmo diretório de `core/disk.py` (VM_STORAGE_BASE) — calculado aqui para
# não importar o disk.py (que puxa rich) neste módulo de launch.
VM_STORAGE_BASE = CONFIG_DIR / "vms"


def reims_ctl_bin() -> Optional[str]:
    """O executável do reims-ctl: PATH, depois o ~/.cargo/bin do install-ctl."""
    found = __import__("shutil").which("reims-ctl")
    if found:
        return found
    cargo_bin = Path(os.path.expanduser("~/.cargo/bin/reims-ctl"))
    return str(cargo_bin) if cargo_bin.is_file() else None


# A janela do reims-vgpu (winit) fala Wayland direto: sem conexão de display ela
# não abre. No appliance a TUI sobe num TTY (autologin tty1), então o start é
# envolvido numa sessão kiosk mínima (sway) criada pelo vhackintosh-waysession.
WAYSESSION_CANDIDATES = (
    "/usr/local/bin/vhackintosh-waysession",
    str(
        Path(__file__).resolve().parent.parent
        / "appliance" / "archiso" / "airootfs" / "usr" / "local" / "bin"
        / "vhackintosh-waysession"
    ),
)


def find_waysession() -> Optional[str]:
    for cand in WAYSESSION_CANDIDATES:
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def session_argv(argv: list[str], env: dict) -> tuple[list[str], bool]:
    """Envolve `argv` na sessão kiosk quando não há sessão gráfica alguma.

    Devolve `(argv_efetivo, sessao_faltando)` — `sessao_faltando` é True quando
    não há display nem waysession, caso em que a janela não vai abrir.
    """
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        return argv, False
    waysession = find_waysession()
    if waysession:
        return [waysession] + argv, False
    return argv, True


def registry_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "reims-ctl" / "vms.toml"


def vm_dir(vm: VMConfig) -> Path:
    return VM_STORAGE_BASE / vm.id


def qmp_socket(vm: VMConfig) -> Path:
    """O socket QMP fixo que o reims-ctl cria para a VM (paths.rs::vm.qmp_socket)."""
    return vm_dir(vm) / "qmp.sock"


def ensure_registered(vm: VMConfig) -> None:
    """Garante a entrada `[[vm]] name/dir` no registry do reims-ctl.

    O registry é o único caminho que o `reims-ctl start <nome>` conhece; o
    diretório continua sendo o do appliance (Entry.dir aceita qualquer caminho).
    Reescreve o arquivo preservando as outras VMs registradas.
    """
    path = registry_file()
    entries: list[dict[str, str]] = []
    if path.is_file():
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            entries = [dict(e) for e in data.get("vm", [])]
        except (tomllib.TOMLDecodeError, OSError):
            # Registro ilegível: preservar o que não for nosso vira difícil;
            # recomeçar com as entradas que ainda parsearem é aceitável aqui.
            entries = []

    name, directory = vm.id, str(vm_dir(vm))
    kept = [e for e in entries if e.get("name") != name]
    kept.append({"name": name, "dir": directory})

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for e in kept:
            fh.write("[[vm]]\n")
            fh.write(f"name = {json.dumps(e.get('name', ''))}\n")
            fh.write(f"dir = {json.dumps(e.get('dir', ''))}\n")
            fh.write("\n")
    os.replace(tmp, path)


def _random_mac() -> str:
    """MAC localmente administrado no padrão do QEMU (52:54:00:xx:xx:xx)."""
    import secrets

    return "52:54:00:" + ":".join(f"{b:02x}" for b in secrets.token_bytes(3))


def write_vm_toml(
    vm: VMConfig,
    *,
    installer: Optional[str] = None,
    resolution: str = "1920x1080",
    borderless: bool = True,
) -> Path:
    """Grava o `vm.toml` da VM no layout do reims-ctl e devolve o caminho.

    Preserva o MAC de rede e a seção `[gpu]` de escritas anteriores (o TOML é
    reescrito por completo a cada fase — instalação entra e sai do `installer`).
    `installer` é caminho absoluto da imagem; sem ele, a VM sobe com o disco
    instalado só.
    """
    d = vm_dir(vm)
    d.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    toml_path = d / "vm.toml"
    if toml_path.is_file():
        try:
            with open(toml_path, "rb") as fh:
                existing = tomllib.load(fh)
        except (tomllib.TOMLDecodeError, OSError):
            existing = {}

    network = existing.get("network") or {}
    mac = network.get("mac") or _random_mac()

    def s(v: str) -> str:
        # JSON e TOML concordam em strings básicas (escape \u00e9 etc. é válido
        # nos dois); ensure_ascii=False mantém UTF-8 legível como o upstream.
        return json.dumps(v, ensure_ascii=False)

    lines = [
        f"name = {s(vm.id)}",
        'profile = "q35-x86_64"',
        f"cpus = {int(vm.vcpus)}",
        f"memory_mib = {int(vm.ram_gb) * 1024}",
        'disk = "macos.qcow2"',
        'disk_format = "qcow2"',
        "stop_timeout_secs = 90",
        '# Reboot do convidado encerra o QEMU; a TUI relança (ver módulo).',
        'extra_args = ["-action", "reboot=shutdown"]',
        "",
        "[display]",
        "count = 1",
        f"resolution = {s(resolution)}",
        "hidpi = false",
        # No appliance (kiosk sway) borda fora aproxima o fullscreen que o
        # antigo REIMS_VGPU_FULLSCREEN=1 dava.
        f"borderless = {'true' if borderless else 'false'}",
        "",
        "[audio]",
        "enabled = true",
        "",
        "[network]",
        "enabled = true",
        f"mac = {s(mac)}",
        "",
        "[logs]",
        "enabled = true",
        'level = "info"',
        "",
        "[q35]",
        'opencore = "OpenCore.qcow2"',
        'opencore_format = "qcow2"',
        'firmware_code = "OVMF_CODE.fd"',
        'firmware_vars = "OVMF_VARS.fd"',
        # Skylake-Client-noTSX-IBRS: é o default medido do upstream
        # (docs/qemu.md, "x86-64 macOS guests: the CPU model") — model 94 que o
        # XNU conhece, boota com ou sem o spoof de CPUID do OpenCore, e não tem
        # AVX-512 (cujo estado o XNU bagunça em host AMD). O Haswell-noTSX que
        # antes fixávamos só boota atrás do spoof do ULTMOS.
        'cpu_model = "Skylake-Client-noTSX-IBRS"',
        # cpu_flags com +vmx/+svm quando o host tem nested (core/virt.py) — o
        # ponto único de launch é este arquivo (D6/D7).
        f"cpu_flags = {s(cpu_flags_str())}",
        "disk_port = 3",
    ]

    if installer:
        lines += [f"installer = {s(installer)}"]

    # Se o operador escolheu GPU via `reims-ctl set/gpus`, preserva.
    if isinstance(existing.get("gpu"), dict) and existing["gpu"]:
        lines += ["", "[gpu]"]
        for k, v in existing["gpu"].items():
            if isinstance(v, str):
                lines.append(f"{k} = {s(v)}")
            elif isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            else:
                lines.append(f"{k} = {v}")

    tmp = toml_path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, toml_path)
    return toml_path


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ReimsCtlSession:
    """O objeto que `run_rail_session` espera de `launch()`: `.wait()` bloqueia
    enquanto o QEMU da VM vive.

    `argv` é o comando de start completo (já envolvido pela sessão kiosk quando
    necessário). `reims-ctl start` é detached (retorna o pid do QEMU assim que
    o QMP responde — runtime::start só conclui com o socket acessível); este
    wrapper então espera o pid morrer. Se o start disser "already runs (pid N)"
    (corrida de relançamento), o pid é adotado em vez de tratado como erro.
    """

    _PID_RE = re.compile(r"pid (\d+)")

    def __init__(
        self,
        argv: list[str],
        env: Optional[dict] = None,
        on_ready: Optional[object] = None,
    ) -> None:
        self.argv = argv
        self.env = env
        # Chamado UMA vez, logo que o `reims-ctl start` conclui (QMP já
        # respondendo — o start só retorna com o socket acessível). Serve p/
        # auto-anexar USBs marcados (grupo 3); falhas aqui nunca derrubam a VM.
        self.on_ready = on_ready

    def wait(self) -> int:
        proc = subprocess.run(
            self.argv,
            capture_output=True,
            text=True,
            env=self.env,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        for line in out.splitlines():
            if line.strip():
                print(f"  reims-ctl: {line.strip()}")

        m = self._PID_RE.search(out)
        if not m:
            return proc.returncode if proc.returncode else 1
        if proc.returncode != 0 and "already runs" not in out:
            # Falha de preflight/build com um "pid" citado no erro: não adota.
            return proc.returncode

        if self.on_ready is not None:
            try:
                self.on_ready()
            except Exception as exc:  # noqa: BLE001 — anexo é melhor-esforço
                print(f"  aviso: pós-start falhou ({exc}); a VM segue normalmente")

        pid = int(m.group(1))
        while _pid_alive(pid):
            time.sleep(0.5)
        return 0
