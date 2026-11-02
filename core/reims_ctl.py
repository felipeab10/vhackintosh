"""
vHackintosh — Integração com o reims-ctl (árvore nova do reims-vgpu)

O appliance é **frontend** do reims-ctl: ele provisiona discos e OpenCore
(`core/disk.py`), escreve o `vm.toml` no layout do reims-ctl, registra a VM em
`~/.config/reims-ctl/vms.toml` e delega o start do QEMU ao `reims-ctl start`.
O argv do QEMU é do upstream — o appliance não mantém cópia nenhuma dele, e o
QMP fica no socket fixo `<vmdir>/qmp.sock`, que o monitor já observa.

O que o appliance projeta no `vm.toml`:
- `cpus`/`memory_mib` vêm da configuração salva (sugerida por host na criação);
- `q35.cpu_model` vem do modelo QEMU validado no inventário; o default seguro é
  `Skylake-Client-noTSX-IBRS`, recomendado pelo upstream para macOS x86-64;
- `network.enabled`, `network.mac` e `audio.backend` são opções estruturadas
  do Reims; o launcher Reims v1.4.0+ cria VirtIO Sound automaticamente quando
  o áudio está habilitado (e HDA adicional no perfil x86-64);
- `extra_args = ["-action", "reboot=shutdown"]` preserva o contrato do
  appliance: um reboot do convidado **encerra** o QEMU e o laço da TUI
  relança (a alternativa, DeviceReset no lugar, é o que congela o host —
  ver o comentário longo em `ui/tui.py`);
- `q35.installer` só existe na fase de instalação (imagem absoluta, in-place).
"""

from __future__ import annotations

import json
import base64
import os
import subprocess
import re
import selectors
import signal
import stat
import tempfile
import time
import tomllib
import datetime as _datetime
from pathlib import Path
from typing import Optional

from core.config import VMConfig, CONFIG_DIR
from core.display import validate_display_options
from core.qemu_args import validate_qemu_extra_args
from core.gpu import smbios_oem_string_present
from core.virt import cpu_flags as _nested_cpu_flags

# Baseline de CPU flags do q35-x86_64 do reims-ctl (vm.rs); o appliance só
# acrescenta +vmx/+svm quando o host tem nested habilitado (core/virt.py).
_BASE_CPU_FLAGS = "+ssse3,+sse4.2,+popcnt,+avx,+aes,+xsave,+xsaveopt,check"


def cpu_flags_str() -> str:
    return _nested_cpu_flags(_BASE_CPU_FLAGS)

# O mesmo diretório de `core/disk.py` (VM_STORAGE_BASE) — calculado aqui para
# não importar o disk.py (que puxa rich) neste módulo de launch.
VM_STORAGE_BASE = CONFIG_DIR / "vms"
VMWARE_SVGA_CMDLINE = "vhack_graphics=vmware-svga"
TEST_GRAPHICS_ENV = "VHACK_TEST_GRAPHICS_BACKEND"


def _graphics_backend_from_cmdline(cmdline: str) -> Optional[str]:
    return "vmware-svga" if VMWARE_SVGA_CMDLINE in cmdline.split() else None


def _requested_test_graphics_backend() -> Optional[str]:
    """Retorna o backend alternativo solicitado pelo kernel ou SMBIOS OEM."""
    try:
        cmdline = Path("/proc/cmdline").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    return _graphics_backend_from_cmdline(cmdline) or (
        "vmware-svga"
        if smbios_oem_string_present("vhack_graphics=vmware-svga")
        else None
    )


def apply_test_graphics_environment(env: dict) -> Optional[str]:
    """Propaga o backend de teste somente à sessão que iniciará a VM."""
    backend = _requested_test_graphics_backend()
    if backend:
        env[TEST_GRAPHICS_ENV] = backend
    else:
        env.pop(TEST_GRAPHICS_ENV, None)
    return backend


class GPUSelectionError(ValueError):
    """A GPU explicitamente escolhida não pôde ser mapeada a um ICD Vulkan."""


def _toml_key(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:
            return "nan"
        if value == float("inf"):
            return "inf"
        if value == float("-inf"):
            return "-inf"
        return repr(value)
    if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = (f"{_toml_key(str(key))} = {_toml_value(item)}" for key, item in value.items())
        return "{ " + ", ".join(parts) + " }"
    raise TypeError(f"TOML value of type {type(value).__name__} cannot be preserved")


def _toml_dumps(document: dict) -> str:
    """Serialize TOML values parsed by tomllib without dropping unknown fields."""
    lines: list[str] = []

    def table_values(values: dict, prefix: tuple[str, ...], *, array_table: bool = False) -> None:
        if prefix:
            header = ".".join(_toml_key(part) for part in prefix)
            lines.append(("[[" if array_table else "[") + header + ("]]" if array_table else "]"))
        children = []
        for key, value in values.items():
            key_text = _toml_key(str(key))
            if isinstance(value, dict):
                children.append((key, value, False))
            elif isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
                children.append((key, value, True))
            else:
                lines.append(f"{key_text} = {_toml_value(value)}")
        for key, value, is_array in children:
            if lines and lines[-1] != "":
                lines.append("")
            if is_array:
                for item in value:
                    table_values(item, (*prefix, str(key)), array_table=True)
            else:
                table_values(value, (*prefix, str(key)))

    table_values(document, ())
    return "\n".join(lines).rstrip() + "\n"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        # vm.toml can contain extra_env values; keep the projection private even
        # when replacing a file created with permissive legacy permissions.
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _remove_durable(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _project_qemu_args(existing: object, configured: list[str]) -> list[str]:
    """Preserve supported Reims tokens and refuse to erase unknown arguments."""
    if existing is None:
        previous: list[str] = []
    elif isinstance(existing, list) and all(isinstance(item, str) for item in existing):
        previous = list(existing)
    else:
        raise ValueError("extra_args existente não é uma lista de tokens; arquivo preservado.")

    preserved: list[str] = []
    index = 0
    while index < len(previous):
        if previous[index] == "-action":
            if index + 1 >= len(previous) or previous[index + 1] != "reboot=shutdown":
                raise ValueError("extra_args contém uma política de reboot não gerenciada; arquivo preservado.")
            index += 2
            continue
        preserved.append(previous[index])
        if index + 1 < len(previous):
            preserved.append(previous[index + 1])
        index += 2

    preserved = validate_qemu_extra_args(preserved)
    configured = validate_qemu_extra_args(configured)
    overridden = set(configured[::2])
    merged = [token for offset in range(0, len(preserved), 2) if preserved[offset] not in overridden for token in preserved[offset:offset + 2]]
    return ["-action", "reboot=shutdown", *validate_qemu_extra_args([*merged, *configured])]


def reims_ctl_bin() -> Optional[str]:
    """Resolve o binário do runtime ou a instalação padrão do appliance.

    A ORDEM importa. O motor instalado pela release em `~/.cargo/bin` é o que
    casa com o QEMU que a MESMA release instalou, então ele tem precedência
    sobre um `reims-ctl` qualquer achado no PATH.

    O contrário (PATH primeiro) é um bug de verdade: bastava existir um
    `reims-ctl` antigo em /usr/local/bin para o appliance ignorar o motor que
    acabara de instalar e subir a VM com o antigo — o oposto do que "Iniciar VM
    deve usar a compilação atual" promete. Foi medido: com um motor velho no
    PATH, a resolução devolvia o velho mesmo com o novo já instalado.

    O PATH continua sendo considerado, mas DEPOIS, para não quebrar quem tem o
    motor instalado por fora (ex.: `cargo install` manual num prefixo próprio).
    """
    def utilizavel(caminho: Path) -> bool:
        # `is_file()` LEVANTA PermissionError quando o caminho está sob um
        # diretório sem permissão de travessia — é o caso de /root/.cargo/bin
        # para quem não é root. Sem este `except`, um usuário comum receberia
        # uma exceção no lugar do fallback.
        try:
            return caminho.is_file() and os.access(caminho, os.X_OK)
        except OSError:
            return False

    cargo_home = os.environ.get("CARGO_HOME")
    cargo_root = Path(cargo_home).expanduser() if cargo_home else None
    cargo_bin = (
        cargo_root / "bin"
        if cargo_root is not None and cargo_root.is_absolute()
        else Path(os.path.expanduser("~/.cargo/bin"))
    )
    for candidato in (
        cargo_bin / "reims-ctl",
        Path(os.path.expanduser("~/.cargo/bin/reims-ctl")),
        # Como root o `~` é /root, mas o appliance já rodou com HOME
        # diferente: mantém o caminho explícito para não depender do
        # expanduser nem de o usuário conseguir atravessar o diretório.
        Path("/root/.cargo/bin/reims-ctl"),
    ):
        if utilizavel(candidato):
            return str(candidato)
    return __import__("shutil").which("reims-ctl")


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
SHARED_DISPLAY_UNIT = Path("/etc/systemd/system/vhackintosh-display.service")
SHARED_DISPLAY_UNIT_CANDIDATES = (
    SHARED_DISPLAY_UNIT,
    Path("/usr/lib/systemd/system/vhackintosh-display.service"),
    Path("/lib/systemd/system/vhackintosh-display.service"),
)


def find_waysession() -> Optional[str]:
    for cand in WAYSESSION_CANDIDATES:
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def session_argv(argv: list[str], env: dict) -> tuple[list[str], bool]:
    """Use the appliance's shared compositor, starting it for TUI launches.

    The shared Sway owns the physical DRM device once; all Reims SDL windows
    connect to its Wayland socket and can use the same physical Vulkan GPU.
    Outside an installed appliance, retain the development kiosk fallback.
    """
    shared_unit = SHARED_DISPLAY_UNIT
    shared_runtime = "/run/vhackintosh-display"
    display_name = env.get("WAYLAND_DISPLAY")
    runtime_dir = env.get("XDG_RUNTIME_DIR")
    socket_path = (
        Path(display_name)
        if display_name and display_name.startswith("/")
        else Path(runtime_dir or "") / (display_name or "")
    )

    # An existing X11/Wayland session is usable as-is, except for the shared
    # appliance socket whose service may have gone idle since the last TUI run.
    if env.get("DISPLAY"):
        return argv, False
    if (
        display_name
        and runtime_dir != shared_runtime
        and socket_path.exists()
    ):
        return argv, False

    if any(unit.is_file() for unit in (shared_unit, *SHARED_DISPLAY_UNIT_CANDIDATES[1:])):
        systemctl = "/usr/bin/systemctl"
        if not os.path.exists(systemctl):
            raise RuntimeError("systemctl não está disponível para iniciar o display compartilhado.")
        try:
            result = subprocess.run(
                [systemctl, "start", "vhackintosh-display.service"],
                capture_output=True,
                text=True,
                timeout=135,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Não foi possível iniciar o display compartilhado: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                "Não foi possível iniciar o display compartilhado do appliance"
                + (f": {detail}" if detail else ".")
            )
        env["XDG_RUNTIME_DIR"] = shared_runtime
        env["WAYLAND_DISPLAY"] = "wayland-0"
        if not (Path(shared_runtime) / "wayland-0").is_socket():
            raise RuntimeError("O compositor compartilhado iniciou sem publicar o socket Wayland.")
        return argv, False

    if display_name and socket_path.exists():
        return argv, False

    waysession = find_waysession()
    if waysession:
        return [waysession] + argv, False
    return argv, True


def registry_file() -> Path:
    explicit = os.environ.get("VHACKINTOSH_REIMS_REGISTRY")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise ValueError("VHACKINTOSH_REIMS_REGISTRY precisa ser um caminho absoluto.")
        return path
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "reims-ctl" / "vms.toml"


def vm_dir(vm: VMConfig) -> Path:
    """Return a registered VM's actual directory, or its managed first-run path."""
    managed = VM_STORAGE_BASE / vm.id
    path = registry_file()
    if path.is_symlink() or not path.is_file():
        return managed
    try:
        with path.open("rb") as handle:
            entries = tomllib.load(handle).get("vm", [])
        matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("name") == vm.id]
        if len(matches) != 1:
            return managed
        directory_value = matches[0].get("dir")
        if not isinstance(directory_value, str) or not directory_value:
            return managed
        directory = Path(directory_value).expanduser()
        if not directory.is_absolute():
            return managed
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or directory.resolve(strict=True) != Path(os.path.abspath(directory)):
            return managed
        if not (directory / "vm.toml").is_file() or (directory / "vm.toml").is_symlink():
            return managed
        return directory
    except (OSError, TypeError, ValueError, tomllib.TOMLDecodeError):
        return managed


def qmp_socket(vm: VMConfig) -> Path:
    """Socket QMP no diretório efetivamente registrado para a VM.

    O `reims-ctl` aceita VMs cujo diretório não seja o padrão do appliance e
    move sockets longos para o runtime dir para caber no limite de `sockaddr_un`.
    Consultar o registro evita que uma segunda sessão de terminal suponha um
    caminho diferente daquele usado pelo processo QEMU.
    """
    # Read the locally maintained registry instead of calling `reims-ctl
    # list` here. Resolving a socket path must stay side-effect free and cannot
    # depend on a second subprocess after stop has already completed.
    record = None
    try:
        with registry_file().open("rb") as handle:
            registry = tomllib.load(handle)
        record = next(
            (entry for entry in registry.get("vm", []) if entry.get("name") == vm.id),
            None,
        )
    except (OSError, tomllib.TOMLDecodeError, AttributeError, TypeError):
        record = None
    directory = record.get("dir") if isinstance(record, dict) else None
    vm_path = Path(directory) if isinstance(directory, str) and directory else vm_dir(vm)
    own = vm_path / "qmp.sock"
    if len(os.fsencode(own)) < 100:
        return own

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and Path(runtime_dir).is_absolute():
        base = Path(runtime_dir) / "reims-ctl"
    else:
        base = Path(os.environ.get("TMPDIR") or "/tmp") / f"reims-ctl-{os.getuid()}"
    return base / f"{vm.id}.qmp"


def ensure_registered(vm: VMConfig) -> None:
    """Garante a entrada `[[vm]] name/dir` no registry do reims-ctl.

    O registry é o único caminho que o `reims-ctl start <nome>` conhece; o
    diretório continua sendo o do appliance (Entry.dir aceita qualquer caminho).
    Reescreve o arquivo preservando as outras VMs registradas.
    """
    path = registry_file()
    registry: dict = {}
    if path.is_symlink():
        raise ValueError(f"O registro Reims é um link simbólico; arquivo preservado: {path}")
    if path.is_file():
        try:
            with open(path, "rb") as fh:
                registry = tomllib.load(fh)
        except (tomllib.TOMLDecodeError, OSError) as exc:
            raise ValueError(f"Não foi possível preservar o registro Reims: {path}") from exc
    if not isinstance(registry, dict):
        raise ValueError(f"O registro Reims não contém uma tabela válida: {path}")

    entries = registry.get("vm", [])
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError(f"A lista de VMs do registro Reims é inválida: {path}")
    if any(not isinstance(entry.get("name"), str) or not isinstance(entry.get("dir"), str) for entry in entries):
        raise ValueError(f"Uma entrada do registro Reims não tem nome/diretório válidos: {path}")

    name, directory = vm.id, str(vm_dir(vm))
    matching = [entry for entry in entries if entry.get("name") == name]
    if len(matching) > 1:
        raise ValueError(f"O registro Reims contém entradas duplicadas para {name}; arquivo preservado.")
    if matching:
        matching[0]["dir"] = directory
    else:
        entries.append({"name": name, "dir": directory})
    registry["vm"] = entries
    _atomic_write_text(path, _toml_dumps(registry))


def _random_mac() -> str:
    """MAC localmente administrado no padrão do QEMU (52:54:00:xx:xx:xx)."""
    import secrets

    return "52:54:00:" + ":".join(f"{b:02x}" for b in secrets.token_bytes(3))


_GPU_LINE_RE = re.compile(
    r"\s*(?P<number>\d+)\.\s+(?P<name>.+?)\s+"
    r"\[(?P<vd>[0-9a-fA-F]{4}:[0-9a-fA-F]{4})\]\s+"
    r"(?P<icd>.+?\.json)\s*"
)
_MAX_REIMS_GPU_OUTPUT = 64 * 1024
_REIMS_GPU_TIMEOUT = 30.0


def _parse_reims_gpus(stdout: str) -> list[dict]:
    """Interpreta somente as linhas de GPU emitidas pelo formato atual da CLI."""
    achados = []
    for linha in stdout.splitlines():
        m = _GPU_LINE_RE.fullmatch(linha)
        if not m:
            continue
        vendor_device = m.group("vd").lower()
        vendor, device = vendor_device.split(":", 1)
        achados.append(
            {
                "vendor_device": vendor_device,
                "vendor_id": vendor,
                "device_id": int(device, 16),
                "icd": m.group("icd").strip(),
                "name": m.group("name").strip(),
            }
        )
    return achados


def _run_reims_gpus_bounded(binary: str, *arguments: str) -> Optional[str]:
    """Run a bounded Reims JSON/text listing with a hard output/time limit."""
    try:
        proc = subprocess.Popen(
            [str(binary), *(arguments or ("gpus",))],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.stdout is None:
        proc.kill()
        proc.wait()
        return None
    output = bytearray()
    selector = selectors.DefaultSelector()
    os.set_blocking(proc.stdout.fileno(), False)
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + _REIMS_GPU_TIMEOUT
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            for key, _ in selector.select(min(remaining, 0.1)):
                chunk = os.read(key.fd, min(8192, _MAX_REIMS_GPU_OUTPUT + 1 - len(output)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > _MAX_REIMS_GPU_OUTPUT:
                    raise OverflowError
        try:
            return_code = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise TimeoutError from None
        if return_code != 0:
            return None
        return bytes(output).decode("utf-8", errors="replace")
    except (TimeoutError, OverflowError):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.wait()
        return None
    finally:
        selector.close()
        proc.stdout.close()


def _gpus_do_reims_ctl() -> list:
    """As GPUs que o reims-ctl enumera: lista de dicts com `vendor_device`,
    `icd`, `name` e `kind`, na ordem de preferência dele.

    O reims-ctl enumera as GPUs de verdade, subindo um probe Vulkan com
    `VK_DRIVER_FILES` apontado para cada manifest ICD. Reproduzir isso aqui
    seria pior: num host com dois manifests Intel (o `intel_icd.json` do Alder
    Lake e o `intel_hasvk_icd.json`, que é Haswell), casar por nome de arquivo
    escolhe o errado — medido. Então perguntamos a quem já sabe.

    A saída humana tem a forma:
        By default the library takes the first discrete GPU, ...
         1. NVIDIA GeForce RTX 3050 Laptop GPU  [10de:25e2]  /usr/.../nvidia_icd.json
         2. Intel(R) Iris(R) Xe Graphics (ADL GT2)  [8086:46a6]  /usr/.../intel_icd.json

    A ordem da lista É significativa: é a ordem em que o reims-ctl escolheria,
    e é o que permite dizer qual GPU ele usaria sem `[gpu]` no TOML.
    """
    binario = reims_ctl_bin()
    if not binario:
        return []
    output = _run_reims_gpus_bounded(str(binario))
    return _parse_reims_gpus(output) if output is not None else []


_PCI_BDF_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


def _slots_do_vendor_device(vendor_device: str, *, sysfs_root: Path = Path("/sys")) -> list[str]:
    """Return all display-controller BDFs carrying this exact PCI ID pair."""
    root = sysfs_root / "bus/pci/devices"
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name.lower())
    except OSError:
        return []
    matches = []
    for entry in entries:
        if not _PCI_BDF_RE.fullmatch(entry.name):
            continue
        try:
            class_id = (entry / "class").read_text(encoding="ascii").strip().lower()
            vendor = (entry / "vendor").read_text(encoding="ascii").strip().removeprefix("0x").lower()
            device = (entry / "device").read_text(encoding="ascii").strip().removeprefix("0x").lower()
        except OSError:
            continue
        if class_id.startswith("0x03") and f"{vendor}:{device}" == vendor_device:
            matches.append(entry.name.lower())
    return matches


def _vendor_device_do_slot(slot: str) -> Optional[str]:
    """`vendor:device` (como o `reims-ctl gpus` imprime) da GPU do slot PCI."""
    if not isinstance(slot, str) or not _PCI_BDF_RE.fullmatch(slot):
        return None
    base = Path(f"/sys/bus/pci/devices/{slot}")
    try:
        vendor = (base / "vendor").read_text().strip().removeprefix("0x").lower()
        device = (base / "device").read_text().strip().removeprefix("0x").lower()
    except OSError:
        return None
    if len(vendor) != 4 or len(device) != 4:
        return None
    return f"{vendor}:{device}"


def _gpu_config_para_slot(slot: Optional[str]) -> Optional[dict]:
    """O manifest ICD do Vulkan que dirige a GPU do slot PCI dado.

    O `reims-ctl` seleciona GPU por driver (`driver_file`, que vira
    `VK_DRIVER_FILES`), não por BDF. O wizard coleta o slot PCI; esta função
    faz a ponte consultando o próprio `reims-ctl gpus`, que enumera as GPUs de
    verdade. Quando qualquer elo falha devolve `None`: um palpite errado manda
    a VM para a placa errada, o que é pior do que cair no default.
    """
    if not slot or not isinstance(slot, str) or not _PCI_BDF_RE.fullmatch(slot):
        return None
    alvo = _vendor_device_do_slot(slot)
    if not alvo:
        return None
    bdfs = _slots_do_vendor_device(alvo)
    if len(bdfs) != 1 or bdfs[0] != slot.lower():
        return None
    gpus = _gpus_do_reims_ctl()
    compatíveis = [gpu for gpu in gpus if gpu["vendor_device"] == alvo]
    if len(compatíveis) != 1:
        return None
    selecionada = compatíveis[0]
    mesmo_manifest = [gpu for gpu in gpus if gpu["icd"] == selecionada["icd"]]
    resultado = {"driver_file": selecionada["icd"]}
    if len(mesmo_manifest) > 1:
        # VK_LOADER_DEVICE_ID_FILTER only accepts deviceID; it cannot distinguish
        # same-ID devices inside one manifest, even when their BDFs differ.
        mesmos_ids = [gpu for gpu in mesmo_manifest if gpu["device_id"] == selecionada["device_id"]]
        if len(mesmos_ids) != 1:
            return None
        resultado["device_id"] = selecionada["device_id"]
    return resultado


def _icd_para_slot(slot: Optional[str]) -> Optional[str]:
    """Compatibility helper returning the measured Vulkan manifest only."""
    config = _gpu_config_para_slot(slot)
    return config["driver_file"] if config else None


def _gpu_section(vm: VMConfig, existing: dict) -> dict:
    """A seção `[gpu]` do `vm.toml`, ou `{}` quando não há o que afirmar.

    O reims-ctl, sem `[gpu]`, escolhe "the first discrete GPU, then the first
    integrated one" (medido: `reims-ctl gpus`). Num notebook com NVIDIA +
    Intel isso significa a NVIDIA mesmo que o operador tenha escolhido a Intel
    no wizard — foi o que produziu a tela preta de 19/10. Então a escolha do
    wizard tem de virar TOML, e não só um campo do `config.json`.
    """
    slot = (vm.selected_gpu or "").strip()
    if slot:
        try:
            from core.runtime_inventory import inspect_runtime

            features = inspect_runtime().get("reims_schema_features", [])
        except Exception:
            features = []
        if isinstance(features, list) and {"gpu_identity", "gpu_inventory_json"}.issubset(set(features)):
            # On identity-capable Reims, project the exact identity only after
            # matching its current JSON inventory to the host PCI BDF.  An
            # unavailable, changed, or ambiguous mapping must fail closed.
            from core.web_gpu import inventory_gpus

            matches = [
                item.as_dict() for item in inventory_gpus()
                if item.pci_bdf == slot.lower() and item.selection_supported
            ]
            if len(matches) != 1:
                raise GPUSelectionError(
                    f"A identidade Reims da GPU {slot} está indisponível ou ambígua; escolha foi preservada."
                )
            item = matches[0]
            if vm.selected_gpu_uuid and item.get("vulkan_uuid") != vm.selected_gpu_uuid:
                raise GPUSelectionError(
                    f"O UUID Vulkan da GPU {slot} mudou desde que foi configurada; atualize o inventário."
                )
            selected_identity = (
                vm.selected_gpu_device_uuid,
                vm.selected_gpu_pci_address,
                vm.selected_gpu_driver_file,
            )
            current_identity = (
                item.get("reims_device_uuid"), item.get("reims_pci_address"), item.get("driver_file"),
            )
            if any(value is not None for value in selected_identity) and selected_identity != current_identity:
                raise GPUSelectionError(
                    f"A identidade Reims da GPU {slot} mudou desde que foi configurada; recarregue o inventário."
                )
            if not all(isinstance(value, str) and value for value in current_identity):
                raise GPUSelectionError(f"O Reims não informou uma identidade completa para a GPU {slot}.")
            return {
                "device_uuid": current_identity[0],
                "pci_address": current_identity[1],
                "driver_file": current_identity[2],
                **({"name": vm.selected_gpu_name} if vm.selected_gpu_name else {}),
            }
        selection = _gpu_config_para_slot(slot)
        if selection:
            secao: dict = dict(selection)
            if vm.selected_gpu_name:
                secao["name"] = vm.selected_gpu_name
            return secao
        # Nunca omitir a seleção explícita: sem [gpu], reims-ctl aplica o seu
        # default (normalmente a dGPU NVIDIA) e a VM inicia com a placa errada.
        raise GPUSelectionError(
            f"Não foi possível mapear {vm.selected_gpu_name or slot} ({slot}) "
            "para um ICD Vulkan. A escolha foi recusada para evitar fallback "
            "silencioso para outra GPU."
        )

    # Nada escolhido no wizard: preserva o que um `reims-ctl set --gpu` gravou.
    if isinstance(existing.get("gpu"), dict) and existing["gpu"]:
        return dict(existing["gpu"])
    return {}


def write_vm_toml(
    vm: VMConfig,
    *,
    installer: Optional[str] = None,
    resolution: str | None = None,
    borderless: bool | None = None,
) -> Path:
    """Grava o `vm.toml` da VM no layout do reims-ctl e devolve o caminho.

    Atualiza os campos pertencentes ao vHackintosh e preserva os demais valores
    TOML já existentes (inclusive campos futuros do Reims). A publicação é
    atômica; se o arquivo existente estiver inválido ou contiver um tipo TOML
    que não possa ser serializado, a escrita falha sem substituí-lo.
    `installer` é caminho absoluto da imagem e pertence à tabela `[q35]` do
    esquema Reims; sem ele, a VM sobe somente com os discos configurados.
    """
    d = vm_dir(vm)
    d.mkdir(parents=True, exist_ok=True)

    toml_path = d / "vm.toml"
    if toml_path.is_symlink():
        raise ValueError(f"O vm.toml é um link simbólico; arquivo preservado: {toml_path}")
    if toml_path.is_file():
        try:
            with open(toml_path, "rb") as fh:
                existing = tomllib.load(fh)
        except (tomllib.TOMLDecodeError, OSError) as exc:
            raise ValueError(f"Não foi possível preservar o vm.toml existente: {toml_path}") from exc
    else:
        existing = {}
    if not isinstance(existing, dict):
        raise ValueError(f"O vm.toml existente não contém uma tabela válida: {toml_path}")

    def section(name: str) -> dict:
        value = existing.setdefault(name, {})
        if not isinstance(value, dict):
            raise ValueError(f"A seção [{name}] de {toml_path} não é uma tabela; arquivo preservado.")
        return value

    effective_borderless = vm.fullscreen if borderless is None else borderless
    effective_resolution = resolution or vm.display_resolution or "1920x1080"
    effective_resolution = validate_display_options(
        effective_resolution, vm.display_hidpi, vm.display_refresh_hz,
    )
    existing.update({
        "name": vm.id,
        "profile": "q35-x86_64",
        "cpus": int(vm.vcpus),
        "memory_mib": int(vm.ram_gb) * 1024,
        # Existing absolute/external disk paths and image formats belong to
        # Reims; this panel does not yet expose a disk replacement editor.
        "disk": existing.get("disk") or (vm.disk_path or "macos.qcow2"),
        "extra_args": _project_qemu_args(existing.get("extra_args"), vm.qemu_extra_args),
    })
    existing.setdefault("disk_format", "qcow2")
    existing.setdefault("stop_timeout_secs", 90)
    if vm.additional_disks is not None:
        if len(vm.additional_disks) > 15:
            raise ValueError("São permitidos no máximo 15 discos adicionais.")
        projected_disks = []
        for order, disk in enumerate(vm.additional_disks):
            if (
                disk.order != order
                or disk.format not in {"qcow2", "raw"}
                or disk.controller not in {"ahci", "nvme"}
                or not Path(disk.path).is_absolute()
            ):
                raise ValueError("Configuração de disco adicional inválida; vm.toml preservado.")
            projected_disks.append({
                "path": disk.path,
                "format": disk.format,
                "controller": disk.controller,
                "order": order,
            })
        if projected_disks:
            existing["additional_disks"] = projected_disks
        else:
            existing.pop("additional_disks", None)

    display = section("display")
    display.update({
        "resolution": effective_resolution,
        "hidpi": bool(vm.display_hidpi),
        # No appliance (kiosk sway) borda fora aproxima o fullscreen que o
        # antigo REIMS_VGPU_FULLSCREEN=1 dava.
        "borderless": bool(effective_borderless),
    })
    if vm.display_count is not None:
        if isinstance(vm.display_count, bool) or not isinstance(vm.display_count, int) or not 1 <= vm.display_count <= 8:
            raise ValueError("Quantidade de displays inválida; use de 1 a 8.")
        previous_count = display.get("count", 1)
        if previous_count != vm.display_count:
            try:
                from core.runtime_inventory import inspect_runtime

                runtime = inspect_runtime()
            except Exception:
                runtime = {}
            if runtime.get("status") != "available" or "--displays" not in runtime.get("reims_set_options", []):
                raise ValueError("O runtime Reims não confirma alteração da quantidade de displays; vm.toml preservado.")
        display["count"] = vm.display_count
    display.setdefault("count", 1)
    if vm.display_refresh_hz is not None:
        display["refresh"] = int(vm.display_refresh_hz)
    else:
        display.pop("refresh", None)

    # The Reims schema fixes the guest audio device model, but allows selecting
    # one host backend that this QEMU build advertises. `auto` removes the
    # override so Reims chooses its native backend.
    audio = section("audio")
    audio["enabled"] = bool(vm.audio_enabled)
    if vm.audio_backend and vm.audio_backend != "auto":
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", vm.audio_backend):
            raise ValueError("Backend de áudio salvo inválido; configuração preservada.")
        audio["backend"] = vm.audio_backend
    else:
        audio.pop("backend", None)
    network = section("network")
    network["enabled"] = bool(vm.network_enabled)
    if vm.network_mac:
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", vm.network_mac) or int(vm.network_mac[:2], 16) & 1:
            raise ValueError("MAC salvo inválido; configuração preservada.")
        network["mac"] = vm.network_mac
    network.setdefault("mac", _random_mac())
    if vm.network_ssh_port is not None:
        if isinstance(vm.network_ssh_port, bool) or not isinstance(vm.network_ssh_port, int) or not 0 <= vm.network_ssh_port <= 65535:
            raise ValueError("Porta SSH encaminhada inválida; vm.toml preservado.")
        if vm.network_ssh_port == 0:
            network.pop("ssh_port", None)
        else:
            if not vm.network_enabled:
                raise ValueError("A porta SSH não pode ser encaminhada com a rede da VM desabilitada.")
            network["ssh_port"] = vm.network_ssh_port
    logs = section("logs")
    logs["enabled"] = True
    logs.setdefault("level", "info")

    q35 = section("q35")
    q35.update({
        # Skylake-Client-noTSX-IBRS: é o default medido do upstream
        # (docs/qemu.md, "x86-64 macOS guests: the CPU model") — model 94 que o
        # XNU conhece, boota com ou sem o spoof de CPUID do OpenCore, e não tem
        # AVX-512 (cujo estado o XNU bagunça em host AMD). O Haswell-noTSX que
        # antes fixávamos só boota atrás do spoof do ULTMOS.
        "cpu_model": vm.cpu_model or "Skylake-Client-noTSX-IBRS",
    })
    # CPU topology is a gated Reims schema extension. Do not infer support
    # from QEMU help: when the field is unowned, keep the existing TOML value;
    # when owned, only project/remove it if this exact runtime advertises it.
    if vm.cpu_topology is not None:
        vm.cpu_topology.validate(vcpus=vm.vcpus)
        default_topology = (1, vm.vcpus, 1)
        requested_topology = (vm.cpu_topology.sockets, vm.cpu_topology.cores, vm.cpu_topology.threads)
        current_topology = q35.get("cpu_topology")
        try:
            from core.runtime_inventory import inspect_runtime

            features = inspect_runtime().get("reims_schema_features", [])
        except Exception:
            features = []
        supports_topology = isinstance(features, list) and "cpu_topology" in features
        current_factors = None
        if isinstance(current_topology, dict):
            values = (current_topology.get("sockets"), current_topology.get("cores"), current_topology.get("threads"))
            if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
                current_factors = values
        if requested_topology == default_topology:
            if supports_topology:
                q35.pop("cpu_topology", None)
            elif current_factors != requested_topology:
                raise ValueError("O runtime Reims não anuncia cpu_topology; configuração preservada.")
        elif supports_topology:
            q35["cpu_topology"] = {
                "sockets": vm.cpu_topology.sockets,
                "cores": vm.cpu_topology.cores,
                "threads": vm.cpu_topology.threads,
            }
        elif current_factors != requested_topology:
            raise ValueError("O runtime Reims não anuncia cpu_topology; configuração preservada.")
    q35.setdefault("opencore", "OpenCore.qcow2")
    q35.setdefault("opencore_format", "qcow2")
    q35.setdefault("firmware_code", "OVMF_CODE.fd")
    q35.setdefault("firmware_vars", "OVMF_VARS.fd")
    # Preserve advanced Reims fields that are not exposed by the safe editor.
    q35.setdefault("cpu_flags", cpu_flags_str())
    q35.setdefault("disk_port", 3)

    # O modo de validação nested é opt-in pelo cmdline injetado por
    # verify-iso.sh --vmware-svga. No uso normal omitimos graphics e deixamos
    # o default do reims-ctl (reims-vgpu) intacto.
    graphics_backend = _requested_test_graphics_backend()
    if graphics_backend:
        existing["graphics"] = graphics_backend

    if installer:
        q35["installer"] = installer
    else:
        q35.pop("installer", None)
    # Older vHackintosh builds wrote this Reims-owned field at the TOML root,
    # where reims-ctl ignores it. Remove that stale projection while retaining
    # all other unowned root fields.
    existing.pop("installer", None)

    # Seção `[gpu]`: sem ela o reims-ctl pega "the first discrete GPU, then the
    # first integrated one" — num notebook com RTX + Iris Xe isso é a NVIDIA, e
    # a escolha do wizard virava fumaça. Duas fontes, nesta ordem:
    #
    #   1. `vm.selected_gpu` (o slot PCI que o operador escolheu no wizard);
    #   2. a seção `[gpu]` já gravada, para não perdermos um `reims-ctl set --gpu`.
    #
    # A escolha explícita vence a preservação: se o operador trocou a placa no
    # wizard, gravar a antiga de novo seria justamente o bug.
    gpu_section = {} if graphics_backend == "vmware-svga" else _gpu_section(vm, existing)
    if gpu_section:
        previous = existing.get("gpu") if isinstance(existing.get("gpu"), dict) else {}
        existing["gpu"] = {**previous, **gpu_section}
    elif graphics_backend == "vmware-svga":
        existing.pop("gpu", None)

    _atomic_write_text(toml_path, _toml_dumps(existing))
    return toml_path


def _projection_journal_path(vm: VMConfig) -> Path:
    return vm_dir(vm) / ".vhackintosh-reims-projection.json"


def _projection_snapshot(path: Path) -> str | None:
    if path.is_symlink():
        raise ValueError(f"Não é seguro projetar configuração sobre link simbólico: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"O destino da projeção não é um arquivo regular: {path}")
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _restore_projection_snapshot(path: Path, encoded: str | None) -> None:
    if encoded is None:
        _remove_durable(path)
        return
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Snapshot inválido no journal de projeção Reims.") from exc
    _atomic_write_bytes(path, content)


def _recover_vm_configuration(vm: VMConfig, journal_path: Path, registry_path: Path) -> None:
    if journal_path.is_symlink():
        raise ValueError(f"Journal de projeção é link simbólico; arquivo preservado: {journal_path}")
    if not journal_path.exists():
        return
    try:
        data = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Journal de projeção inválido; arquivos preservados: {journal_path}") from exc
    expected_toml = vm_dir(vm) / "vm.toml"
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or data.get("vm_id") != vm.id
        or data.get("vm_toml") != str(expected_toml)
        or data.get("registry") != str(registry_path)
        or not isinstance(data.get("before"), dict)
        or not isinstance(data.get("committed"), bool)
    ):
        raise ValueError(f"Journal de projeção não corresponde à configuração atual: {journal_path}")
    if not data["committed"]:
        before = data["before"]
        if set(before) != {"vm_toml", "registry"}:
            raise ValueError(f"Snapshots incompletos no journal de projeção: {journal_path}")
        _restore_projection_snapshot(expected_toml, before["vm_toml"])
        _restore_projection_snapshot(registry_path, before["registry"])
    _remove_durable(journal_path)


def write_vm_configuration(
    vm: VMConfig,
    *,
    installer: Optional[str] = None,
    resolution: str | None = None,
    borderless: bool | None = None,
) -> Path:
    """Publica `vm.toml` e o registro Reims como uma projeção recuperável.

    Um journal privado guarda as duas versões anteriores antes da primeira
    troca. Se o processo cair no meio, a próxima projeção restaura ambas antes
    de tentar novamente. O marcador committed é escrito somente após as duas
    publicações; nesse ponto uma queda posterior apenas limpa o journal.
    """
    directory = vm_dir(vm)
    directory.mkdir(parents=True, exist_ok=True)
    journal_path = _projection_journal_path(vm)
    registry_path = registry_file()
    _recover_vm_configuration(vm, journal_path, registry_path)

    vm_toml_path = directory / "vm.toml"
    before = {
        "vm_toml": _projection_snapshot(vm_toml_path),
        "registry": _projection_snapshot(registry_path),
    }
    journal = {
        "version": 1,
        "vm_id": vm.id,
        "vm_toml": str(vm_toml_path),
        "registry": str(registry_path),
        "before": before,
        "committed": False,
    }
    _atomic_write_text(journal_path, json.dumps(journal, separators=(",", ":")) + "\n")
    try:
        result = write_vm_toml(
            vm, installer=installer, resolution=resolution, borderless=borderless,
        )
        ensure_registered(vm)
        journal["committed"] = True
        _atomic_write_text(journal_path, json.dumps(journal, separators=(",", ":")) + "\n")
    except Exception as exc:
        try:
            _restore_projection_snapshot(vm_toml_path, before["vm_toml"])
            _restore_projection_snapshot(registry_path, before["registry"])
            _remove_durable(journal_path)
        except Exception as recovery_exc:
            raise RuntimeError(
                f"Falha ao projetar a VM ({exc}); journal preservado para recuperação em {journal_path}: {recovery_exc}"
            ) from exc
        raise
    _remove_durable(journal_path)
    return result


def _pid_alive(pid: int) -> bool:
    """Se o processo ainda roda de verdade.

    Um zumbi (`/proc/<pid>/stat` em estado `Z`) NÃO conta como vivo: o
    `kill(pid, 0)` funciona nele, então a checagem ingênua dizia "vivo" e o
    laço de espera do `ReimsCtlSession.wait` girava para sempre — medido em
    19/10: a VM morria, a tela voltava e parecia que ela seguia em execução
    com um pid. O QEMU já tinha terminado; só ninguém o havia recolhido.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # `kill` não distingue zumbi de processo vivo; o /proc distingue.
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            campos = fh.read().split(b") ", 1)
        if len(campos) == 2 and campos[1][:1] == b"Z":
            return False
    except OSError:
        # Sem /proc (ou o processo sumiu entre as duas chamadas): fica com o
        # veredito do `kill`.
        pass
    return True


def vms_rodando() -> dict:
    """`vm.id` -> pid, para as VMs que o reims-ctl considera rodando.

    Usa `reims-ctl list --json`, que traz `{name, dir, state, pid}` — o mesmo
    `runtime::pid` que decide o "already runs (pid N)" do `start`. A saída
    humana NÃO serve: medido, ela imprime `macos-tahoe stopped …`, com o nome
    na primeira coluna e o estado como palavra, não o pid.
    """
    binario = reims_ctl_bin()
    if not binario:
        return {}
    try:
        proc = subprocess.run(
            [str(binario), "list", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    try:
        dados = json.loads(proc.stdout or "[]")
    except (ValueError, TypeError):
        return {}
    rodando: dict = {}
    if isinstance(dados, dict):
        dados = dados.get("vms", [])
    if not isinstance(dados, list):
        return {}
    for entrada in dados:
        if not isinstance(entrada, dict):
            continue
        pid = entrada.get("pid")
        nome = entrada.get("name")
        if nome and isinstance(pid, int) and pid > 0:
            rodando[nome] = pid
    return rodando


def vm_runtime_snapshot() -> Optional[dict[str, int]]:
    """Return a read-only, complete Reims VM snapshot or ``None`` if unknown.

    Unlike :func:`vms_rodando`, this helper distinguishes a confirmed empty
    running set from an unavailable or malformed runtime listing. It is used
    before importing legacy entries so the web manager never assumes that an
    unobserved guest is stopped.
    """
    binario = reims_ctl_bin()
    if not binario:
        return None
    try:
        proc = subprocess.run(
            [str(binario), "list", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "[]")
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        data = data.get("vms")
    if not isinstance(data, list):
        return None
    running: dict[str, int] = {}
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            return None
        name, pid = item.get("name"), item.get("pid")
        if not isinstance(name, str) or not name or name in seen:
            return None
        seen.add(name)
        if pid is None:
            continue
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None
        running[name] = pid
    return running


def vm_gpu_identity_snapshot() -> dict[str, dict] | None:
    """Return exact configured/effective GPU identities only when Reims advertises them."""
    try:
        from core.runtime_inventory import inspect_runtime

        features = inspect_runtime().get("reims_schema_features", [])
    except Exception:
        return None
    if not isinstance(features, list) or "gpu_identity" not in features:
        return None
    binary = reims_ctl_bin()
    if not binary:
        return None
    output = _run_reims_gpus_bounded(str(binary), "list", "--json")
    if output is None:
        return None
    try:
        document = json.loads(output)
    except (ValueError, TypeError):
        return None
    if isinstance(document, dict):
        document = document.get("vms")
    if not isinstance(document, list) or len(document) > 256:
        return None
    result: dict[str, dict] = {}
    for entry in document:
        if not isinstance(entry, dict):
            return None
        name = entry.get("name")
        identity = entry.get("gpu_identity")
        if not isinstance(name, str) or not name or name in result or not isinstance(identity, dict):
            return None
        configured = identity.get("configured")
        effective = identity.get("effective")
        if configured is not None:
            if not isinstance(configured, dict):
                return None
            uuid = configured.get("device_uuid")
            pci = configured.get("pci_address")
            driver = configured.get("driver_file")
            if (
                not isinstance(uuid, str) or not uuid or len(uuid) > 128
                or not isinstance(pci, str) or not _PCI_BDF_RE.fullmatch(pci)
                or not isinstance(driver, str) or not driver.startswith("/") or len(driver) > 4096
            ):
                return None
            configured = {"device_uuid": uuid, "pci_address": pci.lower(), "driver_file": driver}
        if effective is not None:
            if not isinstance(effective, dict):
                return None
            uuid, pci = effective.get("device_uuid"), effective.get("pci_address")
            if (
                not isinstance(uuid, str) or not uuid or len(uuid) > 128
                or not isinstance(pci, str) or not _PCI_BDF_RE.fullmatch(pci)
            ):
                return None
            effective = {"device_uuid": uuid, "pci_address": pci.lower()}
        result[name] = {"configured": configured, "effective": effective}
    return result


def vm_record(name: str) -> Optional[dict]:
    """Retorna o registro completo de uma VM conhecida pelo reims-ctl.

    O caminho `dir` é necessário para recuperar VMs que foram criadas pelo
    reims-ctl antes de o vHackintosh manter seu próprio catálogo. Em caso de
    erro de comando ou JSON inválido, devolve None sem inventar um caminho.
    """
    binario = reims_ctl_bin()
    if not binario:
        return None
    try:
        proc = subprocess.run(
            [str(binario), "list", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        dados = json.loads(proc.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    if proc.returncode != 0:
        return None
    if isinstance(dados, dict):
        dados = dados.get("vms", [])
    if not isinstance(dados, list):
        return None
    return next(
        (entry for entry in dados if isinstance(entry, dict) and entry.get("name") == name),
        None,
    )


def parar_vm(vm: VMConfig, *, force: bool = False) -> tuple:
    """Solicita desligamento ACPI ou força o corte da VM.

    O comando `reims-ctl stop` força o desligamento quando seu timeout vence,
    então o caminho normal envia `system_powerdown` diretamente pelo QMP e
    espera um período curto. Se o convidado não sair, devolve o controle para
    a TUI oferecer a ação forçada separadamente.
    """
    binario = reims_ctl_bin()
    if not binario:
        return False, "reims-ctl não instalado"
    if force:
        try:
            proc = subprocess.run(
                [str(binario), "stop", vm.id, "--force"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return False, "o reims-ctl stop --force excedeu o tempo limite"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        saida = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode == 0:
            from core.passthrough import close_qmp_brokers_for_vm

            close_qmp_brokers_for_vm(vm.id)
        return proc.returncode == 0, saida

    pid = vms_rodando().get(vm.id)
    if not pid:
        return False, "a VM não aparece como ativa no reims-ctl"
    try:
        from core.passthrough import QmpClient, QmpError

        with QmpClient(qmp_socket(vm), timeout=5.0) as qmp:
            qmp.command("system_powerdown")
    except (OSError, QmpError) as exc:
        return False, f"não foi possível enviar o desligamento ACPI pelo QMP: {exc}"

    prazo = time.monotonic() + _DESLIGAMENTO_ACPI_ESPERA_S
    while _pid_alive(pid):
        if time.monotonic() >= prazo:
            return False, "pedido enviado, mas o macOS não desligou dentro do prazo"
        time.sleep(0.5)
    from core.passthrough import close_qmp_brokers_for_vm

    close_qmp_brokers_for_vm(vm.id)
    return True, "o macOS desligou após o pedido ACPI"


def visualizar_vm(vm: VMConfig) -> tuple[bool, str]:
    """Abre o viewer de terminal para uma VM já em execução.

    O viewer herda stdin/stdout para que `reims-ctl view` possa controlar a
    tela interativamente. Ctrl-] encerra somente o viewer; a VM continua viva.
    """
    binario = reims_ctl_bin()
    if not binario:
        return False, "reims-ctl não instalado"
    try:
        proc = subprocess.run([str(binario), "view", vm.id], check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode:
        return False, f"reims-ctl view encerrou com código {proc.returncode}"
    return True, ""


_ESPERA_MAXIMA_S = 600  # teto do laço de espera do `ReimsCtlSession.wait`
_DESLIGAMENTO_ACPI_ESPERA_S = 20


class ReimsCtlSession:
    """O objeto que `run_rail_session` espera de `launch()`: `.wait()` bloqueia
    enquanto o QEMU da VM vive.

    `argv` é o comando de start completo (já envolvido pela sessão kiosk quando
    necessário). Sem kiosk, `reims-ctl start` retorna o pid do QEMU assim que o
    QMP responde e este wrapper espera o pid morrer. Com kiosk, o próprio
    `vhackintosh-waysession` mantém o Sway vivo até o QEMU sair, então o fim do
    comando da sessão encerra a espera. Se o start disser "already runs (pid N)"
    (corrida de relançamento), o pid é adotado em vez de tratado como erro.
    """

    _PID_RE = re.compile(r"pid (\d+)")

    def _release_qmp_if_vm_ended(self) -> None:
        """Libera somente o monitor da VM iniciada por esta sessão."""
        try:
            start_index = self.argv.index("start")
            vm_id = self.argv[start_index + 1]
        except (ValueError, IndexError):
            return
        from core.passthrough import close_qmp_brokers_for_vm

        close_qmp_brokers_for_vm(vm_id)

    def __init__(
        self,
        argv: list[str],
        env: Optional[dict] = None,
        on_ready: Optional[object] = None,
        wait_timeout_sec: Optional[float] = _ESPERA_MAXIMA_S,
    ) -> None:
        self.argv = argv
        self.env = env
        if wait_timeout_sec is not None and wait_timeout_sec <= 0:
            raise ValueError("wait_timeout_sec deve ser positivo ou None")
        self.wait_timeout_sec = wait_timeout_sec
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

        # No appliance TTY, waysession não pode retornar quando `reims-ctl
        # start` termina: esse comando é detached e o QEMU ainda está rodando.
        # O cliente primário do Sway espera o PID do QEMU, mantendo a janela e
        # esta subprocess ativa até seu término.
        if Path(self.argv[0]).name == "vhackintosh-waysession":
            self._release_qmp_if_vm_ended()
            return proc.returncode if proc.returncode else 0

        m = self._PID_RE.search(out)
        if not m:
            self._release_qmp_if_vm_ended()
            return proc.returncode if proc.returncode else 1
        if proc.returncode != 0 and "already runs" not in out:
            # Falha de preflight/build com um "pid" citado no erro: não adota.
            self._release_qmp_if_vm_ended()
            return proc.returncode

        if self.on_ready is not None:
            try:
                self.on_ready()
            except Exception as exc:  # noqa: BLE001 — anexo é melhor-esforço
                print(f"  aviso: pós-start falhou ({exc}); a VM segue normalmente")

        pid = int(m.group(1))
        # Teto de espera: antes era um `while _pid_alive(pid)` sem limite. Uma
        # VM que morre sem ser recolhida (ou um pid que nunca morre) prendia a
        # TUI para sempre — o operador via a tela voltar e achava que a VM
        # seguia rodando. O timeout do stop é 90s por padrão; 10 min de teto dá
        # folga larga para o desligamento educado e devolve o controle.
        limite = (
            time.monotonic() + self.wait_timeout_sec
            if self.wait_timeout_sec is not None
            else None
        )
        while _pid_alive(pid):
            if limite is not None and time.monotonic() >= limite:
                print(
                    f"  aviso: o pid {pid} nao encerrou em {self.wait_timeout_sec:g} s; "
                    "devolvendo o controle"
                )
                break
            time.sleep(0.5)
        if not _pid_alive(pid):
            self._release_qmp_if_vm_ended()
        return 0
