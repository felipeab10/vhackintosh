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

    for candidato in (
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
    try:
        proc = subprocess.run(
            [str(binario), "gpus"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    achados = []
    for linha in (proc.stdout or "").splitlines():
        # " 1. NVIDIA GeForce RTX 3050 Laptop GPU  [10de:25e2]  /caminho/icd.json"
        m = re.match(
            r"\s*\d+\.\s+(?P<name>.+?)\s+\[(?P<vd>[0-9a-fA-F]{4}:[0-9a-fA-F]{4})\]\s+(?P<icd>\S+\.json)\s*$",
            linha,
        )
        if not m:
            continue
        achados.append(
            {
                "vendor_device": m.group("vd").lower(),
                "icd": m.group("icd"),
                "name": m.group("name").strip(),
            }
        )
    return achados


def _vendor_device_do_slot(slot: str) -> Optional[str]:
    """`vendor:device` (como o `reims-ctl gpus` imprime) da GPU do slot PCI."""
    pontos = slot.split(":")
    if len(pontos) < 3:
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


def _icd_para_slot(slot: Optional[str]) -> Optional[str]:
    """O manifest ICD do Vulkan que dirige a GPU do slot PCI dado.

    O `reims-ctl` seleciona GPU por driver (`driver_file`, que vira
    `VK_DRIVER_FILES`), não por BDF. O wizard coleta o slot PCI; esta função
    faz a ponte consultando o próprio `reims-ctl gpus`, que enumera as GPUs de
    verdade. Quando qualquer elo falha devolve `None`: um palpite errado manda
    a VM para a placa errada, o que é pior do que cair no default.
    """
    if not slot or not isinstance(slot, str):
        return None
    alvo = _vendor_device_do_slot(slot)
    if not alvo:
        return None
    for gpu in _gpus_do_reims_ctl():
        if gpu["vendor_device"] == alvo:
            return gpu["icd"]
    return None


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
        icd = _icd_para_slot(slot)
        if icd:
            secao: dict = {"driver_file": icd}
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

    # O modo de validação nested é opt-in pelo cmdline injetado por
    # verify-iso.sh --vmware-svga. No uso normal omitimos graphics e deixamos
    # o default do reims-ctl (reims-vgpu) intacto.
    graphics_backend = _requested_test_graphics_backend()
    if graphics_backend:
        lines.insert(5, f"graphics = {s(graphics_backend)}")

    if installer:
        lines += [f"installer = {s(installer)}"]

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
        lines += ["", "[gpu]"]
        for k, v in gpu_section.items():
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

        # No appliance TTY, waysession não pode retornar quando `reims-ctl
        # start` termina: esse comando é detached e o QEMU ainda está rodando.
        # O cliente primário do Sway espera o PID do QEMU, mantendo a janela e
        # esta subprocess ativa até seu término.
        if Path(self.argv[0]).name == "vhackintosh-waysession":
            return proc.returncode if proc.returncode else 0

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
        # Teto de espera: antes era um `while _pid_alive(pid)` sem limite. Uma
        # VM que morre sem ser recolhida (ou um pid que nunca morre) prendia a
        # TUI para sempre — o operador via a tela voltar e achava que a VM
        # seguia rodando. O timeout do stop é 90s por padrão; 10 min de teto dá
        # folga larga para o desligamento educado e devolve o controle.
        limite = time.monotonic() + _ESPERA_MAXIMA_S
        while _pid_alive(pid):
            if time.monotonic() >= limite:
                print(
                    f"  aviso: o pid {pid} nao encerrou em {_ESPERA_MAXIMA_S // 60} min; "
                    "devolvendo o controle (a VM pode seguir rodando)"
                )
                break
            time.sleep(0.5)
        return 0
