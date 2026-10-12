"""
vHackintosh — Hotplug de dispositivos USB do host na VM (design D3)

O `reims-ctl` sobe o QEMU com o monitor QMP num socket fixo por VM:

    ~/.config/vhackintosh/vms/<id>/qmp.sock

Este módulo usa esse monitor para **anexar/desanexar em execução** (quente,
sem relaunch) dispositivos USB do host na VM, com `device_add`/`device_del` do
device QEMU `usb-host` (libusb). Câmera e sem-fio USB usam exatamente o mesmo
caminho — `list_attachable_extra()` só categoriza o que existe para a UI
mostrar.

Decisões do design:

- Conexões **curtas**: conecta, manda um comando, desconecta. O monitor de
  energia (`core/power.py`) mantém a conexão dele aberta o tempo todo; o QEMU
  aceita múltiplos clientes QMP simultâneos, então esta conexão curta convive
  com aquela — por isso este módulo tem o seu próprio cliente e não importa
  nada de `power.py`.
- **Sem `bus`/`addr` no `device_add`**: só `driver`, `vendorid`, `productid` e
  `id`. O QEMU acha o controlador USB que o próprio reims-ctl criou
  (`qdev_find_default_bus`) e posiciona o dispositivo sozinho.
- **Idempotência**: anexar o que já está anexado e desanexar o que não está
  são sucessos silenciosos — a UI pode chamar à vontade.
- **Timeout em todo caminho** (socket, `lsusb`, subprocess): nada de blocking
  infinito. Falha de socket vira `QmpError` com mensagem clara em pt-BR; o
  `desc` de um erro do QMP é repassado **intacto** (é o texto do QEMU — ex. um
  build sem `usb-host`/libusb — que o operador precisa ver).

CLI de smoke: `python3 -m core.passthrough list|attach|detach`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Cliente QMP (conexão curta)
# ---------------------------------------------------------------------------


class QmpError(Exception):
    """Falha do monitor QMP.

    `message` preserva o texto exato: para erro de comando é o `desc` que o
    QEMU devolveu (em inglês, repassado sem tradução nem truncamento); para
    falha de socket é a explicação em pt-BR do que deu errado.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class QmpClient:
    """Cliente QMP de conexão curta para o socket unix de uma VM.

    Uso::

        with QmpClient("/home/u/.config/vhackintosh/vms/tahoe/qmp.sock") as qmp:
            qmp.command("device_add", driver="usb-host", ...)

    No `connect()` lê o *greeting*, negocia `qmp_capabilities` e só então
    aceita comandos. Todo `connect`/`recv` usa `self.timeout` (5s por padrão):
    nenhum caminho bloqueia para sempre.

    O QEMU atende vários clientes QMP ao mesmo tempo, então esta conexão curta
    não disputa nada com o monitor de energia conectado em paralelo.
    """

    # Teto de mensagens lidas à espera de `return`/`error`. Eventos assíncronos
    # (`{"event": ...}`) são ignorados no meio, mas o laço jamais pode ser
    # infinito.
    _MAX_MSGS_PER_REPLY = 512

    def __init__(self, sock_path: str | Path, timeout: float = 5.0) -> None:
        self.sock_path = str(sock_path)
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buffer = b""

    # -- ciclo de vida --------------------------------------------------

    def connect(self) -> None:
        """Abre o socket, lê o greeting e negocia `qmp_capabilities`."""
        if self._sock is not None:
            return

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.sock_path)
        except OSError as exc:
            sock.close()
            raise QmpError(
                f"Sem conexão com o monitor QMP em {self.sock_path}: {exc}"
            ) from exc

        self._sock = sock
        self._buffer = b""
        try:
            greeting = self._read_greeting()
            if "QMP" not in greeting:
                raise QmpError(
                    f"Resposta inesperada do QMP em {self.sock_path}: "
                    f"greeting ausente ({_short(greeting)})"
                )
            # Negociação exigida pelo protocolo antes de qualquer comando.
            self._send({"execute": "qmp_capabilities"})
            self._read_reply()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Fecha o socket (idempotente)."""
        sock, self._sock = self._sock, None
        self._buffer = b""
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> QmpClient:
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # -- comandos -------------------------------------------------------

    def command(self, name: str, **args: Any) -> dict:
        """Manda o comando QMP `name` e devolve o campo `return` da resposta.

        Monta `{"execute": name, "arguments": args}` + linha terminada em
        `\\r\\n` e lê respostas até achar `return` ou `error`, ignorando os
        eventos assíncronos (`{"event": ...}`) que chegam no meio. Um `error`
        vira `QmpError` com o `desc` do QEMU intacto.

        O retorno documentado é `dict` (o que `device_add`/`device_del` e a
        maioria dos comandos devolvem); `human-monitor-command` devolve string
        — o valor é repassado como veio e quem chama normaliza.
        """
        self._send({"execute": name, "arguments": args})
        reply = self._read_reply()
        return reply["return"]

    # -- protocolo ------------------------------------------------------

    def _require(self) -> socket.socket:
        if self._sock is None:
            raise QmpError(
                f"Cliente QMP não conectado ao socket {self.sock_path}; "
                "chame connect() (ou use `with`) antes de comandos."
            )
        return self._sock

    def _send(self, payload: dict) -> None:
        sock = self._require()
        data = json.dumps(payload).encode("utf-8") + b"\r\n"
        try:
            sock.sendall(data)
        except OSError as exc:
            raise QmpError(
                f"Falha ao enviar comando ao QMP em {self.sock_path}: {exc}"
            ) from exc

    def _read_message(self) -> dict:
        """Lê e decodifica uma linha JSON do socket (timeout = `self.timeout`)."""
        sock = self._require()
        while True:
            if b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                line = line.strip()
                if line:
                    break
                continue
            try:
                chunk = sock.recv(4096)
            except socket.timeout as exc:
                raise QmpError(
                    f"Timeout de {self.timeout:g}s esperando resposta do QMP "
                    f"em {self.sock_path}"
                ) from exc
            except OSError as exc:
                raise QmpError(
                    f"Conexão com o QMP em {self.sock_path} falhou: {exc}"
                ) from exc
            if not chunk:
                raise QmpError(
                    f"O monitor QMP em {self.sock_path} fechou a conexão"
                )
            self._buffer += chunk

        try:
            msg = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise QmpError(
                f"Resposta inválida do QMP em {self.sock_path}: "
                f"{line[:200].decode('utf-8', errors='replace')!r}"
            ) from exc
        if not isinstance(msg, dict):
            raise QmpError(
                f"Resposta inesperada do QMP em {self.sock_path}: {_short(msg)}"
            )
        return msg

    def _read_greeting(self) -> dict:
        """Lê o greeting, pulando eventos que o QEMU possa mandar antes dele.

        Depois de um `device_del` o QEMU tem um `DEVICE_DELETED` na fila e já
        o solta na conexão nova — o protocolo diz que o greeting vem primeiro,
        mas na prática é preciso tolerar evento no meio.
        """
        for _ in range(self._MAX_MSGS_PER_REPLY):
            msg = self._read_message()
            if "event" in msg:
                continue
            return msg
        raise QmpError(
            f"O QMP em {self.sock_path} não enviou o greeting (só eventos)"
        )

    def _read_reply(self) -> dict:
        """Lê mensagens até `return`/`error`; eventos assíncronos são pulados."""
        for _ in range(self._MAX_MSGS_PER_REPLY):
            msg = self._read_message()
            if "event" in msg:
                continue
            if "error" in msg:
                raise QmpError(_error_desc(msg["error"]))
            if "return" in msg:
                return msg
        raise QmpError(
            f"O QMP em {self.sock_path} respondeu só eventos e nenhuma "
            "resposta de comando dentro do limite esperado"
        )


def _short(value: Any, limit: int = 200) -> str:
    """Texto curto e seguro para mensagem de erro."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit]


def _error_desc(error: Any) -> str:
    """O `desc` do erro QMP, intacto (fallback: a classe/objeto inteiro)."""
    if isinstance(error, dict):
        desc = error.get("desc") or error.get("class")
        if desc:
            return str(desc)
        return _short(error)
    return str(error)


# ---------------------------------------------------------------------------
# Dispositivos USB do host
# ---------------------------------------------------------------------------

_SYSFS_USB = Path("/sys/bus/usb/devices")
_LSUSB_TIMEOUT = 5.0

# `lsusb`: "Bus 003 Device 002: ID 1532:051e Razer USA, Ltd RC30-..."
_LSUSB_RE = re.compile(
    r"^Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+"
    r"([0-9a-fA-F]{4}):([0-9a-fA-F]{4})(?:\s+(.*))?$"
)
# `lsusb -t` (endpoint/driver): a linha do barramento traz `Bus 003.Port ...`
# e as linhas filhas trazem `Dev 002, If 0, Class=Video, Driver=uvcvideo, ...`
_LSUSB_T_BUS_RE = re.compile(r"Bus\s+(\d+)")
_LSUSB_T_DEV_RE = re.compile(r"Dev\s+(\d+)")
_LSUSB_T_VALUE_RE = re.compile(r"(?:Class|Driver)=([^,\s]+)")

_RE_CAMERA = re.compile(r"camera|webcam|uvc", re.IGNORECASE)
_RE_WIRELESS = re.compile(r"wifi|wireless|bluetooth|bt", re.IGNORECASE)

# Nomes de hub vindos do `lsusb -t` (`Class=root_hub`/`Class=Hub`/`Driver=hub`).
_HUB_TOKENS = frozenset({"hub", "root_hub"})
_HUB_NAMES = frozenset({"usb hub", "hub"})


def _run(cmd: list[str], timeout: float = _LSUSB_TIMEOUT) -> str:
    """Roda um comando curto devolvendo stdout; qualquer falha vira ""."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def _read_attr(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _norm_num(value: str) -> str:
    """`"003"` -> `"3"`: ids QMP e comparações usam a forma decimal."""
    value = value.strip()
    try:
        return str(int(value))
    except ValueError:
        return value


def _num(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return -1


def _sysfs_index() -> dict[tuple[int, int], dict[str, str]]:
    """`(busnum, devnum)` -> atributos de `/sys/bus/usb/devices/<dev>`.

    Usado tanto para enriquecer a saída do `lsusb` (product/manufacturer/
    serial reais do descritor, `bDeviceClass` para checagem de hub) quanto
    como fallback quando não existe `lsusb` no host.
    """
    index: dict[tuple[int, int], dict[str, str]] = {}
    try:
        entries = sorted(_SYSFS_USB.iterdir())
    except OSError:
        return index
    for entry in entries:
        if ":" in entry.name:
            continue  # interface (`3-5:1.0`), não dispositivo
        bus = _read_attr(entry / "busnum")
        addr = _read_attr(entry / "devnum")
        if not bus or not addr:
            continue
        try:
            key = (int(bus), int(addr))
        except ValueError:
            continue
        index[key] = {
            "sysfs": str(entry),
            "vendor_id": _read_attr(entry / "idVendor"),
            "product_id": _read_attr(entry / "idProduct"),
            "product": _read_attr(entry / "product"),
            "manufacturer": _read_attr(entry / "manufacturer"),
            "serial": _read_attr(entry / "serial"),
            "b_device_class": _read_attr(entry / "bDeviceClass"),
        }
    return index


def _lsusb_t_tokens() -> dict[tuple[int, int], set[str]]:
    """`(bus, dev)` -> valores `Class=`/`Driver=` vistos no `lsusb -t`.

    Dá o driver de cada interface (ex. `uvcvideo`, `btusb`) e o hub
    (`root_hub`/`hub`) mesmo quando o sysfs não está legível.
    """
    tokens: dict[tuple[int, int], set[str]] = {}
    raw = _run(["lsusb", "-t"])
    if not raw:
        return tokens
    bus: Optional[int] = None
    for line in raw.splitlines():
        bus_match = _LSUSB_T_BUS_RE.search(line)
        if bus_match:
            bus = int(bus_match.group(1))
        dev_match = _LSUSB_T_DEV_RE.search(line)
        if bus is None or not dev_match:
            continue
        bucket = tokens.setdefault((bus, int(dev_match.group(1))), set())
        bucket.update(v.lower() for v in _LSUSB_T_VALUE_RE.findall(line))
    return tokens


def _looks_hub(product: str, b_device_class: str, tokens: set[str]) -> bool:
    """Melhor esforço para descartar hubs (não têm como ser anexados úteis)."""
    if b_device_class == "09":
        return True
    if tokens & _HUB_TOKENS:
        return True
    name = product.strip().lower()
    return name in _HUB_NAMES or name.endswith(" root hub")


def _device_drivers(
    dev: "UsbDevice", tty: dict[tuple[int, int], set[str]]
) -> set[str]:
    """Drivers ligados ao dispositivo: sysfs (`<dev>:<if>/driver`) + `lsusb -t`."""
    drivers: set[str] = set()
    base = Path(dev.sysfs) if dev.sysfs else None
    if base is not None:
        try:
            entries = list(base.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            # o próprio dispositivo e as interfaces `<dev>:<if>`
            if entry.name != base.name and not entry.name.startswith(base.name + ":"):
                continue
            try:
                drivers.add(Path(os.readlink(entry / "driver")).name)
            except OSError:
                continue
    drivers |= tty.get((_num(dev.bus), _num(dev.addr)), set())
    return {d.lower() for d in drivers}


@dataclass
class UsbDevice:
    """Um dispositivo USB físico do host, elegível a hotplug na VM."""

    bus: str
    addr: str
    vendor_id: str  # 4 hex minúsculas, sem 0x
    product_id: str  # 4 hex minúsculas, sem 0x
    product: str
    manufacturer: str
    serial: str
    sysfs: str  # dir em /sys/bus/usb/devices ("" quando desconhecido)

    @property
    def qmp_id(self) -> str:
        """id único do device no QMP — o mesmo em `device_add` e `device_del`."""
        return f"usbhost-{self.bus}-{self.addr}"

    @property
    def short_label(self) -> str:
        """Rótulo de uma linha para a UI: `<product ou 'USB'> <ven>:<prod>`."""
        name = self.product.strip() or "USB"
        return f"{name} {self.vendor_id}:{self.product_id}"


def list_usb_devices() -> list[UsbDevice]:
    """Lista os USBs do host que podem ir para a VM (hubs descartados).

    Caminho principal: parse da saída do `lsusb`
    (`Bus XX Device YY: ID aaaa:bbbb Fabricante Produto`), enriquecida com o
    sysfs para os descritores reais e para `bDeviceClass`. Sem `lsusb` (ou
    com ele falhando), fallback puro lendo `/sys/bus/usb/devices/*/`.
    """
    return _list_devices(_sysfs_index(), _lsusb_t_tokens())


def _list_devices(
    sysfs_index: dict[tuple[int, int], dict[str, str]],
    tty: dict[tuple[int, int], set[str]],
) -> list[UsbDevice]:
    """Junta as duas fontes e devolve ordenado por (barramento, endereço)."""
    devices = _from_lsusb(sysfs_index, tty)
    if devices is None:
        devices = _from_sysfs(sysfs_index, tty)
    devices.sort(key=lambda d: (_num(d.bus), _num(d.addr), d.vendor_id))
    return devices


def _from_lsusb(
    sysfs_index: dict[tuple[int, int], dict[str, str]],
    tty: dict[tuple[int, int], set[str]],
) -> Optional[list[UsbDevice]]:
    """Parse do `lsusb`; `None` quando não dá para usar (aí cai no sysfs)."""
    if shutil.which("lsusb") is None:
        return None
    raw = _run(["lsusb"])
    if not raw.strip():
        return None

    devices: list[UsbDevice] = []
    for line in raw.splitlines():
        m = _LSUSB_RE.match(line.strip())
        if not m:
            continue
        bus_raw, addr_raw, vendor, product, rest = m.groups()
        bus, addr = _norm_num(bus_raw), _norm_num(addr_raw)
        info = sysfs_index.get((int(bus_raw), int(addr_raw)), {})
        product_name = info.get("product") or (rest or "").strip()
        if _looks_hub(
            product_name,
            info.get("b_device_class", ""),
            tty.get((int(bus_raw), int(addr_raw)), set()),
        ):
            continue
        devices.append(
            UsbDevice(
                bus=bus,
                addr=addr,
                vendor_id=vendor.lower(),
                product_id=product.lower(),
                # Sysfs tem os descritores reais; sem sysfs o `lsusb` só
                # entrega "fabricante produto" numa string só — fica no
                # product e o rótulo continua legível.
                product=product_name,
                manufacturer=info.get("manufacturer", ""),
                serial=info.get("serial", ""),
                sysfs=info.get("sysfs", ""),
            )
        )
    return devices


def _from_sysfs(
    sysfs_index: dict[tuple[int, int], dict[str, str]],
    tty: dict[tuple[int, int], set[str]],
) -> list[UsbDevice]:
    """Fallback sem `lsusb`: dispositivo a dispositivo pelo sysfs."""
    devices: list[UsbDevice] = []
    for (bus, addr), info in sorted(sysfs_index.items()):
        vendor, product = info.get("vendor_id", ""), info.get("product_id", "")
        if not vendor or not product:
            continue
        if _looks_hub(
            info.get("product", ""), info.get("b_device_class", ""),
            tty.get((bus, addr), set()),
        ):
            continue
        devices.append(
            UsbDevice(
                bus=str(bus),
                addr=str(addr),
                vendor_id=vendor.lower(),
                product_id=product.lower(),
                product=info.get("product", ""),
                manufacturer=info.get("manufacturer", ""),
                serial=info.get("serial", ""),
                sysfs=info.get("sysfs", ""),
            )
        )
    return devices


def list_attachable_extra() -> dict[str, list[UsbDevice]]:
    """Câmeras e sem-fio do host, agrupados para a UI mostrar.

    O attach é o mesmo caminho (`usb-host`) para tudo; aqui só categorizamos:

    - `"camera"`: product/manufacturer casando /camera|webcam|uvc/i ou driver
      `uvcvideo` no sysfs/`lsusb -t`;
    - `"wireless"`: casando /wifi|wireless|bluetooth|bt/i (ou driver `btusb`).

    As duas chaves sempre existem (lista vazia quando não há nada).
    """
    tty = _lsusb_t_tokens()
    extra: dict[str, list[UsbDevice]] = {"camera": [], "wireless": []}
    for dev in _list_devices(_sysfs_index(), tty):
        text = f"{dev.manufacturer} {dev.product}"
        drivers = _device_drivers(dev, tty)
        if _RE_CAMERA.search(text) or "uvcvideo" in drivers:
            extra["camera"].append(dev)
        if _RE_WIRELESS.search(text) or "btusb" in drivers:
            extra["wireless"].append(dev)
    return extra


# ---------------------------------------------------------------------------
# Attach / detach na VM viva
# ---------------------------------------------------------------------------

_ATTACH_DUP_TOKENS = ("duplicate", "already")
_DETACH_ABSENT_TOKENS = ("not found", "does not exist", "not existing")
_HMP_ID_RE = re.compile(r"usbhost-[A-Za-z0-9._-]+")


def attach(qmp_sock: str | Path, dev: UsbDevice) -> str:
    """Anexa `dev` à VM em execução (`device_add usb-host`); devolve o id.

    Sem `bus`/`addr` no comando: o QEMU posiciona o dispositivo sozinho no
    controlador USB que o reims-ctl criou. Se o id já está anexado ("Duplicate
    device ID"/"already") é sucesso idempotente com o mesmo id. Qualquer outro
    erro do QMP — inclusive um QEMU sem `usb-host`/libusb — é repassado
    intacto em `QmpError.message`.

    `vendorid`/`productid` vão como **inteiro** (a notação `0x<hex>` é a do
    HMP/QemuOpts; o QMP rejeita a string `"0x1234"` num prop uint).
    """
    dev_id = dev.qmp_id
    with QmpClient(qmp_sock) as qmp:
        try:
            qmp.command(
                "device_add",
                driver="usb-host",
                vendorid=int(dev.vendor_id, 16),
                productid=int(dev.product_id, 16),
                id=dev_id,
            )
        except QmpError as exc:
            msg = exc.message.lower()
            if any(token in msg for token in _ATTACH_DUP_TOKENS):
                return dev_id
            raise
    return dev_id


def detach(qmp_sock: str | Path, dev_or_id: UsbDevice | str) -> None:
    """Desanexa da VM em execução (`device_del`); idempotente.

    Aceita o `UsbDevice` ou o id (`usbhost-<bus>-<addr>`) direto. Um id que
    não existe ("not found") não é erro: nada a fazer, sem exceção.
    """
    dev_id = (
        dev_or_id.qmp_id if isinstance(dev_or_id, UsbDevice) else str(dev_or_id)
    )
    with QmpClient(qmp_sock) as qmp:
        try:
            qmp.command("device_del", id=dev_id)
        except QmpError as exc:
            msg = exc.message.lower()
            if any(token in msg for token in _DETACH_ABSENT_TOKENS):
                return
            raise


def attached_ids(qmp_sock: str | Path) -> set[str]:
    """Ids `usbhost-*` hoje anexados à VM.

    Sai de `human-monitor-command` + `info usb` (a saída traz `, ID: <id>`);
    qualquer falha — conexão, comando ou parse — devolve conjunto vazio para a
    UI degradar em vez de quebrar.
    """
    try:
        with QmpClient(qmp_sock) as qmp:
            # O QMP exige a chave hífenada `command-line`; kwargs não aceita
            # hífen direto, então o desempacotamento `**{...}` faz o caminho.
            raw = qmp.command("human-monitor-command", **{"command-line": "info usb"})
    except QmpError:
        return set()

    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, dict):
        text = str(raw.get("human-readable-text", ""))
    elif raw is None:
        text = ""
    else:
        text = str(raw)
    return set(_HMP_ID_RE.findall(text))


def probe_qmp(qmp_sock: str | Path) -> bool:
    """`True` quando o socket aceita conexão + `qmp_capabilities` (VM viva).

    Gate barato para a UI saber se dá para mexer em hotplug.
    """
    try:
        with QmpClient(qmp_sock):
            return True
    except QmpError:
        return False


# ---------------------------------------------------------------------------
# CLI de smoke
# ---------------------------------------------------------------------------

_USAGE = (
    "Uso:\n"
    "  python3 -m core.passthrough list\n"
    "  python3 -m core.passthrough attach <sock> <bus> <addr>\n"
    "  python3 -m core.passthrough detach <sock> <bus> <addr>\n"
)


def _find_device(bus: str, addr: str) -> Optional[UsbDevice]:
    """Acha o dispositivo do host por `bus`/`addr` (aceita com zero à esquerda)."""
    target = (_norm_num(bus), _norm_num(addr))
    for dev in list_usb_devices():
        if (dev.bus, dev.addr) == target:
            return dev
    return None


def main(argv: list[str]) -> int:
    """CLI de teste: `list`, `attach <sock> <bus> <addr>`, `detach ...`."""
    if not argv:
        print(_USAGE, end="", file=sys.stderr)
        return 2
    if argv[0] in ("-h", "--help"):
        print(_USAGE, end="")
        return 0

    cmd = argv[0]
    if cmd == "list":
        devices = list_usb_devices()
        if not devices:
            print("Nenhum dispositivo USB utilizável no host.")
        for dev in devices:
            print(f"{dev.qmp_id:<14} {dev.vendor_id}:{dev.product_id}  {dev.short_label}")
        extra = list_attachable_extra()
        for key in ("camera", "wireless"):
            ids = ", ".join(d.qmp_id for d in extra[key]) or "-"
            print(f"{key}: {ids}")
        return 0

    if cmd in ("attach", "detach"):
        if len(argv) != 4:
            print(_USAGE, end="", file=sys.stderr)
            return 2
        sock, bus, addr = argv[1], argv[2], argv[3]
        dev = _find_device(bus, addr)

        if cmd == "attach":
            if dev is None:
                print(
                    f"Dispositivo USB {bus}:{addr} não encontrado no host.",
                    file=sys.stderr,
                )
                return 1
            try:
                dev_id = attach(sock, dev)
            except QmpError as exc:
                print(f"Erro ao anexar: {exc.message}", file=sys.stderr)
                return 1
            print(f"Anexado: {dev_id}")
            return 0

        # detach: o id local basta; um device desconhecido do `lsusb` ainda
        # pode estar na VM de uma sessão anterior.
        dev_id = dev.qmp_id if dev else f"usbhost-{_norm_num(bus)}-{_norm_num(addr)}"
        try:
            detach(sock, dev_id)
        except QmpError as exc:
            print(f"Erro ao desanexar: {exc.message}", file=sys.stderr)
            return 1
        print(f"Desanexado: {dev_id}")
        return 0

    print(_USAGE, end="", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
