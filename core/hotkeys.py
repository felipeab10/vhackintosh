"""
vHackintosh — Hotkeys XF86 do notebook (brilho e volume) via evdev

Por que este módulo lê ``/dev/input`` direto em vez de keybindings do
compositor (decisão **D4** do design): a sessão do appliance é um sway kiosk
mínimo — ou um TTY — e o *identity* do compositor não está fixado. Lendo os
eventos no nível evdev, as teclas ``XF86MonBrightnessUp/Down`` e
``XF86AudioRaiseVolume/LowerVolume/Mute`` funcionam em qualquer sessão, sem
depender de configuração de keybinding de compositor nenhum.

Spec de aceitação (task 4.x do change ``appliance-i18n-and-device-integration``):

* brilho +/- ajusta o brilho do **host** (``brightnessctl`` → sysfs);
* volume +/-/mute ajusta o volume do **host** (``wpctl`` → ``pactl`` →
  ``amixer``), inclusive com a VM em execução (é áudio do host, não do guest);
* máquina sem hardware de brilho (desktop): as teclas viram *no-ops*, a sessão
  continua normalmente e **nenhuma** mensagem/erro aparece na tela;
* sem permissão ou sem dispositivo: o processo simplesmente sai (código 0) — o
  launcher do appliance pode reiniciar quando quiser.

Execução::

    python3 -m core.hotkeys [--verbose] [--device /dev/input/eventN]

Padrão é silêncio total: sem stdout e sem stderr. ``--verbose`` apenas anota
linhas em ``/tmp/vhackintosh-hotkeys.log`` (nunca polui a sessão gráfica).

Dependências: stdlib + ``python-evdev`` (Arch: pacote ``python-evdev``).
"""

from __future__ import annotations

import argparse
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

# Import tolerante: evdev ausente/quebrado nunca pode derrubar a sessão com
# traceback — o processo só registra uma linha (com --verbose) e sai com 0.
try:
    import evdev
    from evdev import InputDevice, categorize, ecodes
except Exception as _exc:  # pragma: no cover — só em host sem python-evdev
    evdev = None  # type: ignore[assignment]
    InputDevice = None  # type: ignore[assignment]
    categorize = None  # type: ignore[assignment]
    ecodes = None  # type: ignore[assignment]
    _EDEV_IMPORT_ERROR: Optional[BaseException] = _exc
else:
    _EDEV_IMPORT_ERROR = None

LOG_PATH = "/tmp/vhackintosh-hotkeys.log"

# Teclas tratadas (as demais são ignoradas). Os nomes são resolvidos em
# ``ecodes`` na runtime com getattr: versões antigas do evdev podem não ter
# todos — a que faltar vira no-op silencioso.
WANTED_KEYS = (
    "KEY_VOLUMEUP",
    "KEY_VOLUMEDOWN",
    "KEY_MUTE",
    "KEY_BRIGHTNESSUP",
    "KEY_BRIGHTNESSDOWN",
)

# Apenas press (value==1) e repeat (value==2): repeat dá o ajuste contínuo
# segurando a tecla. value==0 (release) é sempre ignorado.
KEY_PRESS = 1
KEY_REPEAT = 2

POLL_TIMEOUT_S = 1.0  # janela do select: percebe SIGTERM e sai em <= 1s
ACTION_TIMEOUT_S = 2.0  # nenhum comando pode travar o loop de eventos
SYSFS_STEP_PCT = 10  # sysfs: passo de 10% do max_brightness (spec)


class VolumeBackend(NamedTuple):
    """Um backend de áudio do host: nome do binário + um comando por ação."""

    name: str
    up: str
    down: str
    toggle: str


# Ordem de fallback do volume (item 4). O primeiro instalado no PATH define o
# backend escolhido na inicialização; se falhar em runtime, tenta-se o próximo
# silenciosamente. Os comandos são embutidos aqui (sem shell) — 5% de passo.
VOLUME_BACKENDS = (
    VolumeBackend(
        "wpctl",
        "wpctl set-volume -l 1.5 @DEFAULT_AUDIO_SINK@ 5%+",
        "wpctl set-volume -l 1.5 @DEFAULT_AUDIO_SINK@ 5%-",
        "wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle",
    ),
    VolumeBackend(
        "pactl",
        "pactl set-sink-volume @DEFAULT_SINK@ +5%",
        "pactl set-sink-volume @DEFAULT_SINK@ -5%",
        "pactl set-sink-mute @DEFAULT_SINK@ toggle",
    ),
    VolumeBackend(
        "amixer",
        "amixer set Master 5%+",
        "amixer set Master 5%-",
        "amixer set Master toggle",
    ),
)

# brightnessctl já implementa o delta percentual (+5% / 5%-) e escolhe o
# device padrão; é sempre a primeira tentativa de brilho (item 5).
BRIGHTNESSCTL_UP = "brightnessctl set +5%"
BRIGHTNESSCTL_DOWN = "brightnessctl set 5%-"

# Flags globais: o handler de sinal só marca a parada (rodar sys.exit dentro de
# handler interromperia a limpeza dos dispositivos); o loop percebe o flag na
# próxima iteração do select(1s).
_VERBOSE = False
_STOP = False


def _log(message: str) -> None:
    """Anota uma linha no log — somente com ``--verbose``.

    Nunca escreve em stdout/stderr: a TUI do appliance pode estar na mesma
    tty e qualquer poluição aí suja a sessão (spec: silêncio absoluto).
    """
    if not _VERBOSE:
        return
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass  # log indisponível nunca pode derrubar o serviço


def _run_command(command: str) -> bool:
    """Executa um comando (sem shell, sem saída visível) e reporta sucesso."""
    try:
        result = subprocess.run(
            shlex.split(command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=ACTION_TIMEOUT_S,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        # binário ausente, timeout, EPIPE... tudo vira "falhou" e o chamador
        # procura o próximo backend sem derrubar o loop.
        return False


class VolumeController:
    """Volume/mudo do host com backend fixado na inicialização + fallback."""

    def __init__(self) -> None:
        # Primeiro backend cujo binário existe no PATH define o backend inicial.
        self._index = next(
            (i for i, backend in enumerate(VOLUME_BACKENDS) if shutil.which(backend.name)),
            -1,
        )
        if self._index < 0:
            _log("volume: nenhum backend (wpctl/pactl/amixer) disponível; teclas viram no-op")
        else:
            _log(f"volume: backend inicial '{VOLUME_BACKENDS[self._index].name}'")

    def adjust(self, action: str) -> None:
        """Aplica ``"up"``/``"down"``/``"toggle"``; nunca propaga exceção."""
        if self._index < 0:
            return  # nenhum backend: no-op silencioso
        for offset in range(len(VOLUME_BACKENDS) - self._index):
            index = self._index + offset
            backend = VOLUME_BACKENDS[index]
            if not shutil.which(backend.name):
                continue  # sumiu do PATH depois do boot: pula
            if action == "up":
                command = backend.up
            elif action == "down":
                command = backend.down
            else:
                command = backend.toggle
            if _run_command(command):
                if index != self._index:
                    _log(f"volume: migrado para backend '{backend.name}'")
                    self._index = index
                return
            _log(f"volume: '{backend.name}' falhou; tentando o próximo backend silenciosamente")
        # Todos falharam nesta tecla: mantém o índice atual e tenta de novo no
        # próximo press (áudio pode simplesmente estar parado agora).


class BrightnessController:
    """Brilho do host: ``brightnessctl`` com fallback sysfs; sem hardware → no-op."""

    def __init__(self) -> None:
        self._enabled = True
        self._use_ctl = shutil.which("brightnessctl") is not None
        self._sysfs: Optional[Path] = self._find_sysfs()
        if not self._use_ctl and self._sysfs is None:
            # Desktop sem nenhum backlight: desativa já no boot da sessão.
            self._disable("nenhum backlight e brightnessctl indisponível")
        elif not self._use_ctl:
            _log("brilho: brightnessctl ausente; usando sysfs direto")

    @staticmethod
    def _find_sysfs() -> Optional[Path]:
        """Primeiro ``/sys/class/backlight/*`` com ``brightness`` gravável."""
        try:
            entries = sorted(Path("/sys/class/backlight").glob("*"))
        except OSError:
            return None
        for entry in entries:
            brightness = entry / "brightness"
            try:
                if brightness.is_file() and os.access(brightness, os.W_OK):
                    return entry
            except OSError:
                continue
        return None

    def _disable(self, reason: str) -> None:
        """Desliga o tratamento de brilho (uma única linha no log, se verbose)."""
        self._enabled = False
        _log(f"brilho: desativado ({reason}); teclas de brilho viram no-op")

    def adjust(self, direction: int) -> None:
        """``+1`` aumenta / ``-1`` diminui; nunca propaga exceção."""
        if not self._enabled:
            return
        if self._use_ctl:
            command = BRIGHTNESSCTL_UP if direction > 0 else BRIGHTNESSCTL_DOWN
            if _run_command(command):
                return
            # brightnessctl falhou (ou não existia): cai para sysfs. Se não
            # houver backlight nenhum, desativa e a sessão segue normalmente —
            # é exatamente o cenário "desktop" da spec.
            _log("brilho: brightnessctl falhou; tentando fallback sysfs")
            self._use_ctl = False
        if self._sysfs is None:
            self._sysfs = self._find_sysfs()
        if self._sysfs is None:
            self._disable("nenhum /sys/class/backlight gravável")
            return
        self._sysfs_adjust(direction)

    def _sysfs_adjust(self, direction: int) -> None:
        """Ajusta o backlight escrevendo em ``.../brightness`` (clamp 0..max)."""
        entry = self._sysfs
        if entry is None:
            return
        try:
            max_brightness = self._read_int(entry / "max_brightness")
            # Preferimos actual_brightness (o valor realmente exibido pelo
            # driver); brightness é o fallback quando o driver não o expõe.
            current_path = entry / "actual_brightness"
            if not current_path.is_file():
                current_path = entry / "brightness"
            current = self._read_int(current_path)
            step = max(1, max_brightness * SYSFS_STEP_PCT // 100)
            target = max(0, min(max_brightness, current + direction * step))
            (entry / "brightness").write_text(str(target), encoding="ascii")
            _log(f"brilho: sysfs {current} -> {target} (max {max_brightness})")
        except (OSError, ValueError) as exc:
            # Dispositivo de backlight volátil/permissão perdida: desliga em
            # silêncio em vez de tentar (e falhar) em toda tecla.
            self._disable(f"sysfs falhou ({exc})")

    @staticmethod
    def _read_int(path: Path) -> int:
        return int(path.read_text(encoding="ascii").strip())


def _device_paths(only: Optional[str]) -> List[str]:
    """Lista ``/dev/input/event*`` com KEY_VOLUMEUP ou KEY_BRIGHTNESSUP.

    ``only`` restringe a um único caminho (``--device /dev/input/eventN``).
    Dispositivo sem permissão ou volátil é ignorado em silêncio.
    """
    if only:
        return [only]
    paths: List[str] = []
    try:
        candidates: Sequence[str] = evdev.list_devices()
    except Exception as exc:  # /dev/input inacessível
        _log(f"list_devices falhou: {exc!r}")
        return []
    for path in candidates:
        dev = None
        try:
            dev = InputDevice(path)
            keys = dev.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_VOLUMEUP in keys or ecodes.KEY_BRIGHTNESSUP in keys:
                paths.append(path)
        except Exception:
            continue  # EACCES/ENOENT/removido: passa reto, sem barulho
        finally:
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass
    return paths


def _open_devices(paths: Sequence[str]) -> List[Any]:
    """Abre os dispositivos elegíveis; falha de abertura vira apenas log."""
    opened: List[Any] = []
    for path in paths:
        try:
            opened.append(InputDevice(path))
        except Exception as exc:
            _log(f"sem acesso ao dispositivo {path}: {exc!r}")
    return opened


def _handled_key_codes() -> Dict[int, str]:
    """Mapa código de tecla → nome, tolerando ecodes sem o nome (getattr)."""
    codes: Dict[int, str] = {}
    for name in WANTED_KEYS:
        code = getattr(ecodes, name, None)
        if code is not None:
            codes[code] = name
    return codes


def _dispatch(name: str, volume: VolumeController, brightness: BrightnessController) -> None:
    """Encaminha a tecla tratada para o controlador correspondente."""
    if name == "KEY_VOLUMEUP":
        volume.adjust("up")
    elif name == "KEY_VOLUMEDOWN":
        volume.adjust("down")
    elif name == "KEY_MUTE":
        volume.adjust("toggle")
    elif name == "KEY_BRIGHTNESSUP":
        brightness.adjust(1)
    elif name == "KEY_BRIGHTNESSDOWN":
        brightness.adjust(-1)


def _event_loop(devices: List[Any], volume: VolumeController, brightness: BrightnessController) -> None:
    """Loop de leitura: select com timeout + tratamento tolerante por evento."""
    codes = _handled_key_codes()
    if not codes:
        _log("ecodes não expõe as teclas XF86 esperadas; nada a monitorar")
        return
    while not _STOP:
        try:
            readable, _, _ = select.select(list(devices), [], [], POLL_TIMEOUT_S)
        except InterruptedError:
            continue  # select interrompido por sinal; o flag é checado no topo
        except (OSError, ValueError):
            # ENODEV/EIO quando um teclado é desconectado: prefere sair com 0
            # para o launcher reiniciar e re-escanear os dispositivos.
            _log("select falhou (dispositivo removido?); encerrando")
            return
        for dev in readable:
            try:
                for event in dev.read():
                    if event.type != ecodes.EV_KEY:
                        continue
                    name = codes.get(event.code)
                    if name is None or event.value not in (KEY_PRESS, KEY_REPEAT):
                        continue  # release (0) e códigos não tratados
                    if name == "KEY_MUTE" and event.value != KEY_PRESS:
                        continue  # mute não repete: só o press inicial
                    _log(f"evento {name} value={event.value} ({categorize(event)})")
                    try:
                        _dispatch(name, volume, brightness)
                    except Exception as exc:  # nenhuma ação derruba o loop
                        _log(f"ação '{name}' falhou e foi ignorada: {exc!r}")
            except OSError:
                # Teclado desconectado durante a leitura (ENODEV/EIO).
                _log("dispositivo perdido durante a leitura; encerrando")
                return
            except Exception as exc:
                _log(f"erro ignorado ao ler {dev}: {exc!r}")


def _request_stop(signum: int, frame: Optional[Any]) -> None:
    """SIGTERM/SIGINT: só marcam a parada (o select de 1s percebe o flag)."""
    global _STOP
    _STOP = True


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 -m core.hotkeys",
        description="Ajusta brilho/volume do host pelas teclas XF86 (evdev).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=f"registra o que faz em {LOG_PATH} (padrão: silêncio, sem stdout)",
    )
    parser.add_argument(
        "--device",
        metavar="/dev/input/eventN",
        help="restringe a leitura a um único dispositivo evdev",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Ponto de entrada — sempre retorna 0; nenhum caminho gera traceback."""
    global _VERBOSE, _STOP
    args = _parse_args(argv)
    _VERBOSE = bool(args.verbose)
    _STOP = False

    if _EDEV_IMPORT_ERROR is not None:
        _log(f"python-evdev indisponível ({_EDEV_IMPORT_ERROR!r}); saindo em silêncio")
        return 0

    # Instalado cedo para que SIGTERM durante a inicialização também saia limpo.
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    paths = _device_paths(args.device)
    if not paths:
        _log("nenhum teclado acessível com KEY_VOLUMEUP/KEY_BRIGHTNESSUP; saindo em silêncio")
        return 0
    devices = _open_devices(paths)
    if not devices:
        _log("sem permissão/dispositivo evdev; saindo em silêncio")
        return 0

    volume = VolumeController()
    brightness = BrightnessController()
    _log("monitorando " + ", ".join(str(dev.path) for dev in devices))
    try:
        _event_loop(devices, volume, brightness)
    except Exception as exc:  # última rede: nenhum traceback não tratado
        _log(f"erro fatal ignorado: {exc!r}")
    finally:
        for dev in devices:
            try:
                dev.close()
            except Exception:
                pass
    if _STOP:
        _log("sinal de parada recebido; encerrando")
    return 0


if __name__ == "__main__":
    sys.exit(main())
