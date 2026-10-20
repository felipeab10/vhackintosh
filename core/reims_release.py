"""Atualização do reims-vgpu a partir das releases do GitHub.

O appliance nasce com o reims-vgpu **pré-compilado** dentro da ISO (ver
`appliance/build-iso.sh`), obtido de uma release do repositório
https://github.com/felipeab10/reims-vgpu/releases. Compilar o fork do QEMU dentro
da VM custava horas e exigia rede/ferramentas no live.

Este módulo dá a outra metade: trocar o binário empacotado por outro de uma
release mais nova, sem recompilar e sem reconstruir a ISO.

Portabilidade (medida no binário real da release v1.0.1): é um ELF x86-64
genérico, sem `-march=native`, sem caminhos do host de build, e carrega o Vulkan
em tempo de execução via `dlopen("libvulkan.so.1")`. Ou seja: o MESMO binário
serve para qualquer computador x64 cujo host tenha Vulkan 1.2+ e as libs de
sistema (`gtk3`, `libepoxy`, `pixman`, `libslirp`, `libpng`, `libjpeg-turbo`,
`alsa-lib`, `libpulse`, `zstd`, `ncurses`, `bzip2`). Quem decide se funciona é a
GPU do host, não a máquina onde o binário foi compilado.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

REIMS_REPO = "felipeab10/reims-vgpu"
RELEASE_ASSET = "reims-vgpu-linux-x86_64.tar.gz"
RELEASES_API = f"https://api.github.com/repos/{REIMS_REPO}/releases"

# Onde o appliance procura o reims-vgpu empacotado.
DEFAULT_REIMS_DIR = Path("/opt/reims-vgpu")

# Caminho, dentro da árvore, do binário que define "isto está pré-compilado".
# Importado de reims_build para haver uma única verdade sobre o layout; se o
# import falhar (uso isolado do módulo), cai no caminho novo.
try:
    from core.reims_build import PREBUILT_QEMU_RELS as _QEMU_RELS
    from core.reims_build import legacy_gop_rom, prebuilt_rom

    PREBUILT_QEMU_REL = _QEMU_RELS[0]
except Exception:  # pragma: no cover - só quando importado fora do pacote
    PREBUILT_QEMU_REL = Path("vendor/qemu/build/bin/qemu-system-x86_64")
    _QEMU_RELS = (PREBUILT_QEMU_REL, Path("vendor/qemu/build/qemu-system-x86_64"))

    def prebuilt_rom(reims_dir: Path) -> None:  # type: ignore[misc]
        return None

    def legacy_gop_rom(reims_dir: Path) -> None:  # type: ignore[misc]
        return None
MARKER_REL = Path(".release")

_TIMEOUT_DOWNLOAD = 900
_TIMEOUT_API = 30


class ReleaseError(RuntimeError):
    """Falha ao consultar ou baixar uma release."""


@dataclass
class Release:
    """Uma release do GitHub, com o asset que interessa ao appliance."""

    tag: str
    name: str
    published_at: str
    asset_url: str
    asset_size: int = 0
    prerelease: bool = False

    @property
    def size_mb(self) -> float:
        return round(self.asset_size / (1024 * 1024), 1) if self.asset_size else 0.0


@dataclass
class UpdateResult:
    ok: bool
    tag: str = ""
    previous: str = ""
    message: str = ""
    changed: bool = False


def _run(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def _has_curl() -> bool:
    return shutil.which("curl") is not None


def _has_wget() -> bool:
    return shutil.which("wget") is not None


def _qemu_bin(reims_dir: Path) -> Optional[Path]:
    """O binário do QEMU na árvore, aceitando o layout novo e o antigo."""
    for rel in _QEMU_RELS:
        candidato = reims_dir / rel
        if candidato.is_file():
            return candidato
    return None


# Notas de verificação da última troca, para quem chamou poder explicar por que
# uma release com a ROM de nome antigo foi aceita. Limpa a cada `_verify_tree`.
_VERIFY_NOTES: list[str] = []


def _device_of_this_binary(qemu: Path) -> Optional[str]:
    """Qual device de GPU virtual este binário traz: `"novo"`, `"antigo"`.

    O sinal é o NOME DO DEVICE compilado, e a razão de não usar o nome da ROM é
    que aquele sinal era APENAS NEGATIVO: a ausência de `ReimsVgpu.rom` era lida
    como "geração antiga", o que confundia dois casos bem diferentes — "é da
    geração antiga" e "não deu para saber". Medido nos binários que importam:

      v1.0.1 real (11.0.50)     `reims-vgpu-pci`, sem `apple-gfx-pci`
      atual (11.1.50)           `apple-gfx-pci`, sem `reims-vgpu-pci`
      atual depois de `strip`   `apple-gfx-pci` presente — o device sobrevive

    A terceira linha foi medida com `strip` GNU de verdade (286 MB → 66 MB): a
    string do `romfile` TAMBÉM sobrevive, porque é dado em `.rodata`, não
    símbolo. O `strip` remove símbolo e depuração, não conteúdo alocado — então
    o buraco nunca foi o `strip` apagando nada, era a falta de um sinal
    positivo. Com o nome do device a pergunta tem resposta afirmativa nos dois
    sentidos, que é o que faltava.

    Devolve `None` quando não dá para saber: nenhum dos dois nomes aparece, ou
    os dois aparecem (não escolhe por ordem de `if` — seria arbitrário). Quem
    chama decide o lado conservador.

    Limite real que sobra, e não é o `strip`: um binário empacotado de forma
    exótica (comprimido, ou com a `.rodata` removida por pós-processamento
    agressivo) cai em `None` → recusa. É falso alarme, não buraco silencioso.
    """
    try:
        with qemu.open("rb") as fh:
            dados = fh.read()
    except OSError:
        # Binário ilegível: sem como saber, os chamadores recusam.
        return None
    tem_novo = b"apple-gfx-pci" in dados
    tem_antigo = b"reims-vgpu-pci" in dados
    if tem_novo == tem_antigo:  # nenhum dos dois, ou os dois: ambíguo
        return None
    return "novo" if tem_novo else "antigo"


def _binary_wants_the_new_rom_name(qemu: Path) -> bool:
    """Este binário carrega `ReimsVgpu.rom` (e não a ROM antiga)?

    Pergunta direta de geração, respondida pelo device compilado — ver
    `_device_of_this_binary` para a medição e para o motivo de não usar o nome
    da ROM nem a versão do QEMU.

    Quando o device não é identificável (`None`), recusa: um firmware instalado
    no lugar errado sobe a VM com tela preta e nenhum erro, então o lado
    conservador é sempre o que aborta.
    """
    return _device_of_this_binary(qemu) == "novo"


def _version_says_new_generation(dados: bytes) -> Optional[bool]:
    """Lê a versão do QEMU nos bytes: `True` se for da leva do device novo.

    INFORMATIVO. NÃO entra na decisão de geração, e a razão é de categoria, não
    de precisão: a versão fala da BASE UPSTREAM do QEMU, não da geração do
    device. A branch do fork carrega o `apple-gfx-pci` sobre uma base
    `VERSION=11.0.50` — o rename `27c55651b3` está lá antes de o 11.1.0 entrar
    no merge `049fab62bb`, que foi quem levou a VERSION a 11.1.50. Então
    "device novo com versão antiga" existiu de fato, e decidir por versão
    rejeitaria esses builds. Fica exposto para diagnóstico e teste.
    """
    m = re.search(rb"QEMU emulator version (\d+)\.(\d+)", dados)
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2))) >= (11, 1)


def current_release(reims_dir: Path = DEFAULT_REIMS_DIR) -> Optional[str]:
    """A tag da release hoje instalada, ou None se a árvore veio das fontes."""
    marker = reims_dir / MARKER_REL
    if marker.is_file() and _qemu_bin(reims_dir) is not None:
        try:
            return marker.read_text(encoding="utf-8", errors="replace").strip() or None
        except OSError:
            return None
    return None


def is_prebuilt(reims_dir: Path = DEFAULT_REIMS_DIR) -> bool:
    """A árvore é pré-compilada (tem marca E binário presente)?"""
    return current_release(reims_dir) is not None


def vulkan_runtime_ok() -> tuple[bool, str]:
    """O host tem `libvulkan.so.1` carregável para o binário usar?

    O binário pré-compilado NÃO linka Vulkan: ele chama
    `dlopen("libvulkan.so.1")` em runtime. Por isso esta checagem é separada da
    de versão 1.2+ que vive em `core/gpu.py` — aqui só dizemos se a biblioteca
    está alcançável.
    """
    found = ctypes.util.find_library("vulkan") or "libvulkan.so.1"
    try:
        ctypes.CDLL(found)
    except OSError as exc:
        return False, (
            f"libvulkan.so.1 não pôde ser carregada ({exc}); "
            "o reims-vgpu pré-compilado precisa dela em runtime"
        )
    return True, f"libvulkan.so.1 carregável ({found})"


def list_releases(*, include_prerelease: bool = False) -> list[Release]:
    """Lista as releases que têm o asset do appliance, mais nova primeiro.

    Usa a API do GitHub; se ela recusar (rate-limit sem token), o erro é
    explicado e quem chama pode pedir uma tag explícita.
    """
    if not (_has_curl() or _has_wget()):
        raise ReleaseError("nem curl nem wget estão disponíveis para consultar as releases")

    if _has_curl():
        cmd = ["curl", "-fsSL", "-H", "Accept: application/vnd.github+json", RELEASES_API]
    else:
        cmd = ["wget", "-qO-", "--header=Accept: application/vnd.github+json", RELEASES_API]

    try:
        proc = _run(cmd, timeout=_TIMEOUT_API)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError(f"não foi possível consultar as releases: {exc}") from exc

    if proc.returncode != 0:
        raise ReleaseError(
            "a API do GitHub não respondeu "
            f"(rc={proc.returncode}); tente informar a tag explicitamente"
        )

    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"resposta inesperada da API do GitHub: {exc}") from exc

    if not isinstance(data, list):
        raise ReleaseError("resposta inesperada da API do GitHub: esperava uma lista")

    out: list[Release] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("draft"):
            continue
        if item.get("prerelease") and not include_prerelease:
            continue
        assets = item.get("assets") or []
        asset = next(
            (a for a in assets if isinstance(a, dict) and a.get("name") == RELEASE_ASSET),
            None,
        )
        if asset is None:
            # Release sem o tarball do appliance não serve.
            continue
        out.append(
            Release(
                tag=str(item.get("tag_name") or ""),
                name=str(item.get("name") or item.get("tag_name") or ""),
                published_at=str(item.get("published_at") or ""),
                asset_url=str(asset.get("browser_download_url") or ""),
                asset_size=int(asset.get("size") or 0),
                prerelease=bool(item.get("prerelease")),
            )
        )
    return out


def latest_release(*, include_prerelease: bool = False) -> Release:
    """A release mais recente com o asset do appliance."""
    releases = list_releases(include_prerelease=include_prerelease)
    if not releases:
        raise ReleaseError(f"nenhuma release de {REIMS_REPO} tem o asset {RELEASE_ASSET}")
    return releases[0]


def release_asset_url(tag: str) -> str:
    """URL do tarball para uma tag (`latest` resolve para a mais recente)."""
    if tag in ("", "latest"):
        return f"https://github.com/{REIMS_REPO}/releases/latest/download/{RELEASE_ASSET}"
    return f"https://github.com/{REIMS_REPO}/releases/download/{tag}/{RELEASE_ASSET}"


def _download(url: str, dest: Path, *, log: Callable[[str], None]) -> None:
    if _has_curl():
        cmd = ["curl", "-fL", "--progress-bar", "-o", str(dest), url]
    elif _has_wget():
        cmd = ["wget", "-q", "--show-progress", "-O", str(dest), url]
    else:
        raise ReleaseError("nem curl nem wget estão disponíveis para baixar a release")

    log(f"baixando {url}")
    try:
        proc = _run(cmd, timeout=_TIMEOUT_DOWNLOAD)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError(f"falha ao baixar a release: {exc}") from exc
    if proc.returncode != 0:
        raise ReleaseError(
            f"o download falhou (rc={proc.returncode}); confira a tag e a conexão"
        )
    if not dest.is_file() or dest.stat().st_size == 0:
        raise ReleaseError("o arquivo baixado está vazio")


def _extract_safely(tarball: Path, dest: Path, *, log: Callable[[str], None]) -> None:
    """Extrai o tarball recusando caminhos que escapariam de `dest`."""
    dest_resolved = dest.resolve()
    with tarfile.open(tarball, "r:gz") as tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if dest_resolved != target and dest_resolved not in target.parents:
                raise ReleaseError(f"caminho suspeito no tarball: {member.name}")
        log(f"extraindo em {dest}")
        # `filter="data"` recusa links/caminhos perigosos e é o padrão a partir do
        # Python 3.14; pedir explicitamente mantém o mesmo comportamento agora e
        # depois (sem ele, o 3.14 emite DeprecationWarning e muda o default).
        try:
            tar.extractall(dest, filter="data")
        except TypeError:  # Python < 3.11.4 não conhece o parâmetro
            tar.extractall(dest)


def _verify_tree(reims_dir: Path) -> None:
    """O que acabou de chegar é realmente um reims-vgpu utilizável?"""
    _VERIFY_NOTES.clear()
    qemu = _qemu_bin(reims_dir)
    if qemu is None:
        raise ReleaseError(
            f"a release não trouxe {PREBUILT_QEMU_REL} (nem o layout antigo); nada foi trocado"
        )
    # Sem a ROM o QEMU sobe sem a GPU virtual e o sintoma é tela preta, longe da
    # causa. Avisar agora vale mais do que descobrir na primeira VM.
    #
    # CUIDADO ao concluir "a ROM está errada": qual ROM é a certa depende do
    # BINÁRIO QUE VEIO NO TARBALL, não de uma convenção fixa. As releases até a
    # v1.0.1 trazem um QEMU 11.0.50 que carrega `reims-vgpu-gop.rom` (26112 B) e
    # que não conhece o nome novo (medido: `strings` no binário da v1.0.1 dá 0
    # ocorrências de `ReimsVgpu.rom`; o 11.1.50 atual dá 1). Rejeitar as duas
    # releases publicadas por "ROM errada" seria regressão: para aquele binário,
    # a ROM antiga É a certa, e vem no mesmo pacote.
    rom_nova = prebuilt_rom(reims_dir)
    rom_antiga = legacy_gop_rom(reims_dir)
    if rom_nova is None and rom_antiga is None:
        raise ReleaseError(
            "a release não trouxe a ROM do vGPU (nem ReimsVgpu.rom nem "
            "reims-vgpu-gop.rom); nada foi trocado"
        )
    # A pergunta certa é "O BINÁRIO CARREGA A ROM QUE VEIO?", nos dois sentidos.
    # A versão anterior só a fazia quando faltava a ROM nova, o que deixava
    # passar o caso simétrico em silêncio: ROM nova + binário antigo era ACEITA
    # ainda que aquele binário não saiba carregar `ReimsVgpu.rom` (medido: 0
    # ocorrências na v1.0.1). Mesmo modo de falha, na direção oposta.
    if rom_nova is not None and not _binary_wants_the_new_rom_name(qemu):
        raise ReleaseError(
            "a release traz ReimsVgpu.rom e um binário que não carrega esse "
            "nome pelo `romfile` (a geração antiga usa reims-vgpu-gop.rom); "
            "ele subiria sem GPU virtual. Nada foi trocado"
        )
    if rom_nova is None and rom_antiga is not None:
        # Nome antigo é aceitável só se o binário do pacote for da geração que o
        # usa. Se for o binário novo, ele procuraria ReimsVgpu.rom e falharia em
        # silêncio — aí sim é erro.
        if _binary_wants_the_new_rom_name(qemu):
            raise ReleaseError(
                f"a release traz {rom_antiga.name} e um binário que procura "
                "ReimsVgpu.rom pelo `romfile`; ele subiria sem GPU virtual. "
                "Nada foi trocado"
            )
        # A mensagem NÃO diz "a ROM deste binário": medimos que aquele binário
        # não tem preferência nenhuma (0 ocorrências de `romfile` no `.c` da
        # v1.0.1). Quem a passa é o script de boot da própria release, com
        # `romfile=`. Dizer "deste binário" seria falso.
        _VERIFY_NOTES.append(
            f"{rom_antiga.name} é a ROM da geração antiga (device "
            "reims-vgpu-pci); a release antiga a passa em `romfile=` no boot; "
            "aceita como está"
        )


def update_from_release(
    *,
    tag: str = "latest",
    reims_dir: Path = DEFAULT_REIMS_DIR,
    log: Optional[Callable[[str], None]] = None,
    force: bool = False,
) -> UpdateResult:
    """Troca o reims-vgpu empacotado pelo de uma release do GitHub.

    Monta tudo num diretório temporário e só então move para o lugar: uma falha
    no meio do caminho deixa o appliance exatamente como estava. Se a tag pedida
    já for a instalada e `force` for False, não faz nada.

    Falhas de rede/asset viram `ok=False` com `message` explicando, nunca
    exceção — quem chama decide o que mostrar.
    """
    emit = log or (lambda _msg: None)
    emit(f"reims-vgpu: verificando releases ({tag}) em {REIMS_REPO}")

    previous = current_release(reims_dir) or ""

    try:
        if tag in ("", "latest"):
            rel = latest_release()
            resolved = rel.tag
            url = rel.asset_url or release_asset_url(resolved)
        else:
            resolved = tag
            url = release_asset_url(tag)
    except ReleaseError as exc:
        return UpdateResult(ok=False, tag=tag, previous=previous, message=str(exc))

    if previous and previous == resolved and not force:
        return UpdateResult(
            ok=True,
            tag=resolved,
            previous=previous,
            changed=False,
            message=f"já está na release {resolved}",
        )

    tmp_root = Path(tempfile.mkdtemp(prefix="reims-release-"))
    try:
        tarball = tmp_root / RELEASE_ASSET
        _download(url, tarball, log=emit)

        staged = tmp_root / "staged"
        staged.mkdir()
        _extract_safely(tarball, staged, log=emit)
        _verify_tree(staged)
        for nota in _VERIFY_NOTES:
            emit(f"  {nota}")

        # Só agora mexemos no destino. Preservamos o que já estiver lá (fontes,
        # scripts do modo fontes) para não quebrar quem usa a árvore completa.
        reims_dir.mkdir(parents=True, exist_ok=True)
        for item in staged.iterdir():
            dest = reims_dir / item.name
            if item.is_dir() and not item.is_symlink():
                shutil.copytree(item, dest, dirs_exist_ok=True, symlinks=True)
            else:
                if dest.is_symlink() or dest.is_file():
                    dest.unlink()
                elif dest.is_dir():
                    shutil.rmtree(dest)
                shutil.move(str(item), str(dest))

        (reims_dir / MARKER_REL).write_text(f"{resolved}\n", encoding="utf-8")
        emit(f"reims-vgpu atualizado para {resolved}")
        return UpdateResult(
            ok=True,
            tag=resolved,
            previous=previous,
            changed=True,
            message=f"atualizado de {previous or 'nenhuma'} para {resolved}",
        )
    except (ReleaseError, OSError, tarfile.TarError) as exc:
        return UpdateResult(ok=False, tag=resolved, previous=previous, message=str(exc))
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
