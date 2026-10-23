"""Testes do modelo pré-compilado: release do GitHub em vez de compilar na VM.

Contexto: o appliance é uma imagem live onde compilar o fork do QEMU levava
horas. A decisão foi voltar a empacotar o reims-vgpu **já compilado**, vindo de
uma release de https://github.com/felipeab10/reims-vgpu/releases, com uma opção
para atualizá-lo depois. O binário é x86-64 genérico (sem `-march=native`, sem
caminhos do host) e carrega o Vulkan em runtime via `dlopen`.

Nada aqui toca a rede: todo download é substituído por um tarball sintético.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from core import reims_build, reims_release

#: O script de montagem da ISO. Só o caminho: ele é lido por inspeção, para
#: travar contratos que atravessam a fronteira Python/shell.
BUILD_ISO = Path(__file__).resolve().parent.parent / "appliance" / "build-iso.sh"


def _make_release_tree(root: Path, *, with_qemu: bool = True, with_rom: bool = True) -> Path:
    """Monta uma árvore no formato da release real (raiz `./`).

    O layout é o do prefixo do QEMU instalado — `bin/` mais `share/qemu/` — que
    é o que o `release.sh` publica e o que o binário espera para achar a ROM
    pelo `romfile ReimsVgpu.rom`.

    O binário falso carrega o sinal de verdade (`ReimsVgpu.rom` nos bytes), como
    o `apple-gfx-pci` real: sem ele, a verificação passa a acusar — com razão —
    que há uma `ReimsVgpu.rom` numa árvore cujo binário não sabe carregá-la.
    """
    if with_qemu:
        build = root / "vendor" / "qemu" / "build"
        (build / "bin").mkdir(parents=True, exist_ok=True)
        qemu = build / "bin" / "qemu-system-x86_64"
        # Um script com o device da geração nova nos bytes, como o binário real.
        qemu.write_bytes(
            b"#!/bin/sh\n# QEMU emulator version 11.1.50\n"
            b"\x00apple-gfx-pci\x00ReimsVgpu.rom\x00\necho novo\n"
        )
        qemu.chmod(0o755)
        (build / "share" / "qemu").mkdir(parents=True, exist_ok=True)
        if with_rom:
            (build / "share" / "qemu" / "ReimsVgpu.rom").write_bytes(b"rom-do-vgpu")
        pc_bios = root / "vendor" / "qemu" / "pc-bios"
        pc_bios.mkdir(parents=True, exist_ok=True)
        (pc_bios / "pxe-virtio.rom").write_text("rom", encoding="utf-8")
    return root


def _pack(tree: Path, dest: Path) -> Path:
    with tarfile.open(dest, "w:gz") as tar:
        for path in sorted(tree.rglob("*")):
            tar.add(path, arcname=str(path.relative_to(tree)))
    return dest


class LegacyRomIsNotMistakenForTheVgpuRomTests(unittest.TestCase):
    """A ROM GOP das releases antigas não pode virar `ReimsVgpu.rom`.

    Medido no tarball real da v1.0.1: ele traz
    `crates/reims-vgpu-efi/out/reims-vgpu-gop.rom`, de 26112 bytes e sha256
    02cf046f…, enquanto a ROM que o QEMU procura (`ReimsVgpu.rom`) tem 5632
    bytes e sha256 2165aa4c…. São firmwares diferentes. O QEMU aceita as duas
    como `romfile` sem reclamar, então renomear a errada no lugar da certa
    produziria uma VM com o firmware errado e nenhum erro na tela.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reims = Path(self.tmp.name) / "reims-vgpu"
        self.reims.mkdir(parents=True)

    def test_install_prebuilt_copies_every_file_of_the_qemu_rom_dirs(self):
        """`install_prebuilt` tem de levar TODOS os arquivos soltos de ROM.

        Este teste existe por um defeito real que passou despercebido: um
        `if False:` no lugar da condição do loop deixou a instalação levando só
        o binário, sem `bios-256k.bin` — e sem ele o QEMU nem inicia. A suíte
        ficou verde porque `test_pc_bios_is_carried_over` exercita `build()`,
        outro caminho de código, enquanto o defeito estava em
        `install_prebuilt()`. Nenhum teste ligava `install_prebuilt` ao pc-bios.

        Aqui os dois diretórios de ROM são preenchidos e a asserção é sobre
        todos os arquivos, nos dois destinos — não sobre um arquivo só.
        """
        _make_release_tree(self.reims)
        build = self.reims / "vendor" / "qemu" / "build"
        # Conteúdo de share/qemu (a ROM do vGPU já está lá) mais o resto do pc-bios.
        (build / "share" / "qemu" / "bios-256k.bin").write_bytes(b"BIOS")
        (build / "share" / "qemu" / "efi-e1000e.rom").write_bytes(b"EFI")
        # pc-bios separado, com um arquivo que só existe nele.
        pb = self.reims / "vendor" / "qemu" / "pc-bios"
        (pb / "vgabios-stdvga.rom").write_bytes(b"VGA")

        destino = Path(self.tmp.name) / "dados-do-usuario"
        with patch.object(reims_build, "qemu_data_dir", return_value=destino):
            with contextlib.redirect_stdout(io.StringIO()):
                ok = reims_build.install_prebuilt(self.reims)
        self.assertTrue(ok)

        prefixo = destino / "qemu" / "prebuilt"
        # Sem bios-256k.bin o QEMU aborta ao subir: é o arquivo crítico.
        self.assertTrue(
            (prefixo / "share" / "qemu" / "bios-256k.bin").is_file(),
            f"bios-256k.bin não foi copiado; achei: "
            f"{sorted(p.relative_to(prefixo).as_posix() for p in prefixo.rglob('*') if p.is_file())}",
        )
        self.assertTrue((prefixo / "share" / "qemu" / "efi-e1000e.rom").is_file())
        self.assertTrue((prefixo / "share" / "qemu" / "ReimsVgpu.rom").is_file())
        self.assertTrue((prefixo / "pc-bios" / "vgabios-stdvga.rom").is_file())

    def test_install_copies_the_rom_from_outside_the_pc_bios_dir(self):
        """A ROM tem de ser instalada mesmo estando FORA de `share/qemu`.

        Existe um segundo caminho que copia `vendor/qemu/build/share/qemu/*` e
        `vendor/qemu/pc-bios/*` para o prefixo. Se a ROM estiver dentro de um
        desses diretórios, ela chega lá por esse caminho também, e desligar a
        cópia explícita (`rom_src = None`) não muda nada — foi o que aconteceu
        ao mutar o código: a suíte continuava verde e parecia um buraco de
        teste, quando na verdade era a mutação sendo inofensiva.

        Pondo a ROM só em `vendor/qemu/build/ReimsVgpu.rom` (que o loop NÃO
        varre), o único jeito de ela aparecer no prefixo é pela cópia explícita
        — e aí o teste passa a medir o que diz medir.
        """
        _make_release_tree(self.reims, with_rom=False)
        build = self.reims / "vendor" / "qemu" / "build"
        (build / "ReimsVgpu.rom").write_bytes(b"rom-fora-do-pc-bios")
        # Garante a premissa: a ROM não está em nenhum diretório que o loop varre.
        self.assertFalse((build / "share" / "qemu" / "ReimsVgpu.rom").exists())
        self.assertFalse((self.reims / "vendor" / "qemu" / "pc-bios" / "ReimsVgpu.rom").exists())

        destino = Path(self.tmp.name) / "dados-do-usuario"
        with patch.object(reims_build, "qemu_data_dir", return_value=destino):
            with contextlib.redirect_stdout(io.StringIO()):
                ok = reims_build.install_prebuilt(self.reims)
        self.assertTrue(ok)

        binarios = list(destino.rglob("qemu-system-x86_64"))
        self.assertTrue(binarios, "o binário deveria ter sido instalado")
        rom_esperada = binarios[0].parent.parent / "share" / "qemu" / "ReimsVgpu.rom"
        self.assertTrue(
            rom_esperada.is_file(),
            f"a cópia explícita da ROM não aconteceu; achados: "
            f"{sorted(p.relative_to(destino).as_posix() for p in destino.rglob('*.rom'))}",
        )
        self.assertEqual(rom_esperada.read_bytes(), b"rom-fora-do-pc-bios")

    def test_install_puts_the_vgpu_rom_as_a_sibling_of_the_binary(self):
        """A ROM certa tem de ir para `share/qemu/` ao lado de `bin/`.

        Sem este teste, `install_prebuilt` podia (a) não copiar ROM nenhuma ou
        (b) copiá-la para `share/reims-vgpu-qemu/share/qemu` — que é justamente
        o erro de prefixo já corrigido no `build-iso.sh` — e a suíte inteira
        continuava verde. Medido por mutação: as duas passavam.

        O QEMU resolve o `romfile` relativo ao próprio executável, subindo um
        nível de `bin/`: `<dir do binário>/../share/qemu/`. Por isso a asserção
        é sobre a RELAÇÃO entre os caminhos, reproduzida em disco, e não sobre
        a presença de strings no código.
        """
        _make_release_tree(self.reims)  # com a ROM certa (nome atual)

        destino = Path(self.tmp.name) / "dados-do-usuario"
        with patch.object(reims_build, "qemu_data_dir", return_value=destino):
            with contextlib.redirect_stdout(io.StringIO()):
                ok = reims_build.install_prebuilt(self.reims)
        self.assertTrue(ok)

        binarios = list(destino.rglob("qemu-system-x86_64"))
        self.assertTrue(binarios, "o binário deveria ter sido instalado")
        binario = binarios[0]

        # A ROM que o QEMU procura, derivada SÓ da posição do binário.
        rom_esperada = binario.parent.parent / "share" / "qemu" / "ReimsVgpu.rom"
        self.assertTrue(
            rom_esperada.is_file(),
            f"a ROM do vGPU deveria estar em {rom_esperada} (irmã de bin/); "
            f"achados: {sorted(p.relative_to(destino) for p in destino.rglob('*.rom'))}",
        )
        # E tem de ser a ROM da release, não um arquivo qualquer.
        self.assertEqual(rom_esperada.read_bytes(), b"rom-do-vgpu")
        # O prefixo é um só: bin/ e share/qemu/ irmãos sob a mesma raiz.
        self.assertEqual(binario.parent.name, "bin")
        self.assertEqual(rom_esperada.parent.parent.parent, binario.parent.parent)

    def test_install_does_not_nest_the_prefix_twice(self):
        """Não pode nascer `share/reims-vgpu-qemu/share/qemu` (bug de prefixo)."""
        _make_release_tree(self.reims)

        destino = Path(self.tmp.name) / "dados-do-usuario"
        with patch.object(reims_build, "qemu_data_dir", return_value=destino):
            with contextlib.redirect_stdout(io.StringIO()):
                reims_build.install_prebuilt(self.reims)

        roms = sorted(p.relative_to(destino).as_posix() for p in destino.rglob("*.rom"))
        for caminho in roms:
            self.assertNotIn(
                "reims-vgpu-qemu/share/qemu", caminho,
                f"prefixo aninhado de novo: {caminho}",
            )

    def test_prebuilt_rom_ignores_the_legacy_gop_rom(self):
        antiga = self.reims / "crates" / "reims-vgpu-efi" / "out"
        antiga.mkdir(parents=True)
        (antiga / "reims-vgpu-gop.rom").write_bytes(b"\x55\xaa" + b"gop antiga")
        self.assertIsNone(
            reims_build.prebuilt_rom(self.reims),
            "a ROM GOP antiga não pode ser devolvida como a ROM do vGPU",
        )
        self.assertIsNotNone(reims_build.legacy_gop_rom(self.reims))

    def test_install_does_not_install_the_legacy_rom_renamed(self):
        """O binário instala, mas a ROM errada não é copiada para o prefixo.

        Isolado num destino próprio: `install_prebuilt` escreve em
        `qemu_data_dir()`, que é o diretório real do usuário. Sem isolar, outro
        teste que já instalou a ROM certa ali faria este passar (ou falhar) pelo
        motivo errado — foi o que aconteceu quando o diretório estava sujo.
        """
        antiga = self.reims / "crates" / "reims-vgpu-efi" / "out"
        antiga.mkdir(parents=True)
        (antiga / "reims-vgpu-gop.rom").write_bytes(b"gop antiga")
        _make_release_tree(self.reims, with_rom=False)

        destino = Path(self.tmp.name) / "dados-do-usuario"
        buf = io.StringIO()
        with patch.object(reims_build, "qemu_data_dir", return_value=destino):
            with contextlib.redirect_stdout(buf):
                ok = reims_build.install_prebuilt(self.reims)
        self.assertTrue(ok)

        # O prefixo montado tem de ter o binário, e NÃO pode ter a ROM errada.
        binarios = list(destino.rglob("qemu-system-x86_64"))
        self.assertTrue(binarios, "o binário deveria ter sido instalado")
        rom_dest = binarios[0].parent.parent / "share" / "qemu" / "ReimsVgpu.rom"
        self.assertFalse(
            rom_dest.is_file(),
            "a ROM GOP antiga não pode ser instalada renomeada como ReimsVgpu.rom",
        )

    def test_verify_tree_accepts_the_old_rom_with_the_old_binary(self):
        """A ROM de nome antigo é VÁLIDA quando o binário do pacote é da geração dela.

        Este teste já afirmou o contrário, e afirmar o contrário era uma
        regressão: as releases v1.0.0 e v1.0.1 publicadas trazem um QEMU 11.0.50
        que carrega `reims-vgpu-gop.rom` e que **não conhece** o nome novo —
        medido com `strings` no binário real: 0 ocorrências de `ReimsVgpu.rom`
        (o 11.1.50 atual tem 1). Rejeitá-las dizendo "ROM errada" tornava
        ininstalável a última release estável.

        Aqui o binário da árvore é deliberadamente SEM a string nova, imitando o
        da v1.0.1, e a verificação tem de passar.
        """
        build = self.reims / "vendor" / "qemu" / "build"
        (build / "bin").mkdir(parents=True, exist_ok=True)
        # Binário "antigo": não contém ReimsVgpu.rom, como o da v1.0.1.
        (build / "bin" / "qemu-system-x86_64").write_bytes(b"ELF-fake-sem-o-nome-novo")
        antiga = self.reims / "crates" / "reims-vgpu-efi" / "out"
        antiga.mkdir(parents=True)
        (antiga / "reims-vgpu-gop.rom").write_bytes(b"gop antiga")

        reims_release._verify_tree(self.reims)  # não pode levantar
        self.assertTrue(
            any("reims-vgpu-gop.rom" in n for n in reims_release._VERIFY_NOTES),
            f"a aceitação deveria ser explicada; notas: {reims_release._VERIFY_NOTES}",
        )

    def test_verify_tree_refuses_the_old_rom_with_the_new_binary(self):
        """A combinação perigosa: ROM antiga + binário que procura o nome novo.

        É o caso que gera tela preta sem erro — o binário novo procuraria
        `ReimsVgpu.rom` e não acharia. Este é o erro que vale recusar.
        """
        build = self.reims / "vendor" / "qemu" / "build"
        (build / "bin").mkdir(parents=True, exist_ok=True)
        # O binário novo é identificado pelo DEVICE compilado, não pela string
        # do `romfile` — ver `_device_of_this_binary`.
        (build / "bin" / "qemu-system-x86_64").write_bytes(
            b"ELF\x00apple-gfx-pci\x00ReimsVgpu.rom\x00"
        )
        antiga = self.reims / "crates" / "reims-vgpu-efi" / "out"
        antiga.mkdir(parents=True)
        (antiga / "reims-vgpu-gop.rom").write_bytes(b"gop antiga")

        with self.assertRaises(reims_release.ReleaseError) as ctx:
            reims_release._verify_tree(self.reims)
        mensagem = str(ctx.exception)
        self.assertIn("reims-vgpu-gop.rom", mensagem)
        self.assertIn("ReimsVgpu.rom", mensagem)

    def test_verify_tree_refuses_when_there_is_no_rom_at_all(self):
        """Sem ROM nenhuma, a mensagem tem de citar os dois nomes possíveis."""
        _make_release_tree(self.reims, with_rom=False)

        with self.assertRaises(reims_release.ReleaseError) as ctx:
            reims_release._verify_tree(self.reims)
        mensagem = str(ctx.exception)
        self.assertIn("ReimsVgpu.rom", mensagem)
        self.assertIn("reims-vgpu-gop.rom", mensagem)

    def test_verify_tree_still_accepts_the_real_rom(self):
        """O contraponto: com a ROM certa, a release é aceita."""
        _make_release_tree(self.reims)
        reims_release._verify_tree(self.reims)

    def test_verify_tree_refuses_the_new_rom_inside_an_old_binary(self):
        """O sentido SIMÉTRICO, que ficou sem checagem por muito tempo.

        A verificação só examinava o binário quando FALTAVA a `ReimsVgpu.rom`.
        Com a ROM nova presente, o binário nunca era lido — então uma árvore de
        binário antigo (v1.0.1, 0 ocorrências de `ReimsVgpu.rom`) carregando a
        ROM nova era aceita em silêncio, embora esse binário não saiba carregá-la.
        Mesmo modo de falha do caso já barrado, na direção oposta.
        """
        _make_release_tree(self.reims)
        # Troca o binário por um da geração antiga, mantendo a ROM nova no lugar.
        # O device ANTIGO é o sinal: é o que o `reims-vgpu-pci` da v1.0.1 traz.
        qemu = self.reims / "vendor" / "qemu" / "build" / "bin" / "qemu-system-x86_64"
        qemu.write_bytes(
            b"#!/bin/sh\n# QEMU emulator version 11.0.50\n"
            b"\x00reims-vgpu-pci\x00\necho antigo\n"
        )

        with self.assertRaises(reims_release.ReleaseError) as ctx:
            reims_release._verify_tree(self.reims)

        mensagem = str(ctx.exception)
        self.assertIn("ReimsVgpu.rom", mensagem)
        self.assertIn("reims-vgpu-gop.rom", mensagem)

    def test_verify_tree_accepts_the_new_rom_with_the_new_binary(self):
        """A precondição do teste acima: com o binário novo, a mesma árvore passa.

        Sem este contraponto, o teste do sentido simétrico poderia estar passando
        por qualquer outro motivo (a árvore estar malformada, por exemplo).
        """
        _make_release_tree(self.reims)
        reims_release._verify_tree(self.reims)  # não levanta


class BinaryGenerationQuadrantTests(unittest.TestCase):
    """Os 4 quadrantes de `_binary_wants_the_new_rom_name`.

    Estes testes existem porque o cruzamento string×versão ficou SEM cobertura
    quando foi escrito: a mutação que apagava o cruzamento inteiro deixava a
    suíte verde, porque todo teste então existente montava binários sintéticos
    sem a linha `QEMU emulator version` e caía sempre no ramo `None`. Um teste
    que nunca chega ao ramo novo não protege o ramo novo.

    Os binários aqui são curtos de propósito, mas contêm os sinais de verdade
    (o nome do device compilado e a linha de versão), para exercitar de fato a
    decisão.

    O sinal é o NOME DO DEVICE, não o nome da ROM: aquele sinal era apenas
    negativo (ausência de `ReimsVgpu.rom` = "geração antiga"), e confundia "é da
    geração antiga" com "não deu para saber". O verifier mediu com `strip` GNU
    de verdade que a string do `romfile` sobrevive (é dado em `.rodata`, não
    símbolo) — o buraco nunca foi o `strip` apagando nada, era a falta de um
    sinal positivo.
    """

    def _binario(
        self, *, device: Optional[str], versao: Optional[str] = None,
        romfile: bool = False,
    ) -> Path:
        """Um binário falso com o device (e opcionalmente a string do romfile)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        p = d / "qemu-system-x86_64"
        corpo = b""
        if versao:
            corpo += f"QEMU emulator version {versao}\n".encode()
        corpo += b"\x00"
        if device:
            corpo += device.encode() + b"\x00"
        if romfile:
            corpo += b"ReimsVgpu.rom\x00"
        corpo += b"\x00"
        p.write_bytes(corpo)
        return p

    def test_quadrante_novo_sem_ambiguidade(self):
        """Device novo: geração nova."""
        self.assertTrue(
            reims_release._binary_wants_the_new_rom_name(
                self._binario(device="apple-gfx-pci", versao="11.1.50")
            )
        )

    def test_quadrante_antigo_sem_ambiguidade(self):
        """Device antigo (o caso v1.0.1): geração antiga."""
        self.assertFalse(
            reims_release._binary_wants_the_new_rom_name(
                self._binario(device="reims-vgpu-pci", versao="11.0.50")
            )
        )

    def test_device_novo_com_a_string_da_rom_removida_e_a_geracao_nova(self):
        """O ACHADO B do verifier, como teste: o `strip` NÃO engana mais.

        Um binário novo cuja tabela QOM perdeu `ReimsVgpu.rom` (o que um `strip`
        faz) mantém `apple-gfx-pci`, e por isso continua sendo geração nova. Com
        o sinal antigo (o nome da ROM) ele era classificado como antigo, e a
        árvore com `reims-vgpu-gop.rom` era ACEITA em silêncio: binário novo,
        ROM antiga, tela preta sem erro.
        """
        self.assertTrue(
            reims_release._binary_wants_the_new_rom_name(
                self._binario(device="apple-gfx-pci", versao="11.1.50", romfile=False)
            )
        )

    def test_device_desconhecido_recusa(self):
        """Sem nenhum nome de device não dá para saber: o lado seguro é recusar."""
        self.assertFalse(
            reims_release._binary_wants_the_new_rom_name(
                self._binario(device=None, versao="11.1.50", romfile=True)
            )
        )

    def test_os_dois_devices_juntos_e_ambiguo_e_recusa(self):
        """Com os DOIS nomes presentes não se escolhe por ordem de `if`.

        Alcançar `apple-gfx-pci` primeiro viraria "novo" por acidente de
        implementação. Não existe binário real assim (medido: o antigo tem 0
        `apple-gfx-pci`, o novo tem 0 `reims-vgpu-pci`), mas escolher por ordem
        seria arbitrário — melhor dizer que não sabe e recusar.
        """
        p = self._binario(device="apple-gfx-pci", versao="11.1.50")
        p.write_bytes(p.read_bytes() + b"\x00reims-vgpu-pci\x00")
        self.assertIsNone(reims_release._device_of_this_binary(p))
        self.assertFalse(reims_release._binary_wants_the_new_rom_name(p))

    def test_o_strip_nao_muda_a_deteccao(self):
        """O device é dado em `.rodata`: apagar símbolos não o remove.

        Medido com `strip` GNU real (286 MB → 66 MB): `apple-gfx-pci` continua
        presente e `ReimsVgpu.rom` também. O que quebra é só o pós-processamento
        que remova a `.rodata` inteira — e aí a resposta é `None`, que recusa.
        """
        p = self._binario(device="apple-gfx-pci", versao="11.1.50", romfile=True)
        # Simula a remoção da tabela de símbolos/depuração: o conteúdo alocado
        # (os nomes) permanece, como no `strip` de verdade.
        self.assertEqual(reims_release._device_of_this_binary(p), "novo")

    def test_string_vence_a_versao(self):
        """A versão antiga NÃO muda a decisão sobre o device.

        Medido no submódulo: o rename `27c55651b3` é anterior ao bump 11.1 e
        ainda era `VERSION=11.0.50`. Existe uma faixa de builds com o device novo
        e versão antiga — todos geração nova.
        """
        self.assertTrue(
            reims_release._binary_wants_the_new_rom_name(
                self._binario(device="apple-gfx-pci", versao="11.0.50", romfile=True)
            )
        )

    def test_binario_antigo_real_nao_e_confundido(self):
        """O contraexemplo que REPROVOU o veto por versão.

        Um veto do tipo "sem a string e versão < 11.1 -> recusa" parece
        conservador e é justamente o errado: o binário REAL da v1.0.1 é 11.0.50
        e não tem a string, então esse veto recusaria as duas releases
        publicadas. Este teste fixa o comportamento correto para esse par.
        """
        p = self._binario(device="reims-vgpu-pci", versao="11.0.50")
        self.assertFalse(reims_release._binary_wants_the_new_rom_name(p))

    def test_sem_versao_o_device_decide_sozinho(self):
        """Sem a linha de versão, quem decide é o device — nos dois sentidos."""
        self.assertTrue(
            reims_release._binary_wants_the_new_rom_name(
                self._binario(device="apple-gfx-pci", versao=None)
            )
        )
        self.assertFalse(
            reims_release._binary_wants_the_new_rom_name(
                self._binario(device="reims-vgpu-pci", versao=None)
            )
        )

    def test_versao_nao_veta(self):
        """A versão é informativa: não entra na decisão.

        A tentação de usar a versão como rede de segurança foi testada e
        reprovada — ela rejeita as releases boas (v1.0.1 é 11.0.50 sem a
        string). Estes dois pares provam que a versão não muda o resultado.
        """
        for versao in ("11.0.50", "11.1.50", None):
            self.assertTrue(
                reims_release._binary_wants_the_new_rom_name(
                    self._binario(device="apple-gfx-pci", versao=versao)
                ),
                f"com o device novo, versão {versao} não pode mudar a decisão",
            )
        for versao in ("11.0.50", "11.1.50", None):
            self.assertFalse(
                reims_release._binary_wants_the_new_rom_name(
                    self._binario(device="reims-vgpu-pci", versao=versao)
                ),
                f"com o device antigo, versão {versao} não pode mudar a decisão",
            )

    def test_fronteira_da_versao_e_o_11_1(self):
        """A fronteira é 11.1: 11.0 veta menos, 11.1 veta."""
        self.assertIs(reims_release._version_says_new_generation(b"QEMU emulator version 11.0.50"), False)
        self.assertIs(reims_release._version_says_new_generation(b"QEMU emulator version 11.1.50"), True)
        self.assertIs(reims_release._version_says_new_generation(b"QEMU emulator version 12.0.0"), True)
        self.assertIsNone(reims_release._version_says_new_generation(b"sem versao aqui"))

    def test_binario_ilegivel_recusa(self):
        """Ilegível: sem como saber, escolhe o erro barulhento."""
        self.assertFalse(
            reims_release._binary_wants_the_new_rom_name(Path("/nao/existe/qemu"))
        )


class PrebuiltDetectionTests(unittest.TestCase):
    """`prebuilt_release` só diz sim com marca E binário."""

    def setUp(self) -> None:
        # `TemporaryDirectory` com `addCleanup`, e não `mkdtemp()`: as árvores
        # aqui carregam cópias do QEMU (274 MB cada) e o `mkdtemp` sem limpeza
        # deixava ~1 GB por execução em `/tmp`. Num tmpfs de 24 GB isso
        # transborda depois de algumas rodadas, e o sintoma são falhas
        # ALEATÓRIAS de escrita em testes sem relação nenhuma com o assunto.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.reims = self.root / "opt" / "reims-vgpu"
        self.reims.mkdir(parents=True)

    def _add_qemu(self) -> Path:
        build = self.reims / "vendor" / "qemu" / "build"
        build.mkdir(parents=True, exist_ok=True)
        qemu = build / "qemu-system-x86_64"
        qemu.write_text("#!/bin/sh\n", encoding="utf-8")
        qemu.chmod(0o755)
        return qemu

    def test_marker_and_binary_is_prebuilt(self):
        self._add_qemu()
        (self.reims / ".release").write_text("v1.0.1\n", encoding="utf-8")
        self.assertEqual(reims_build.prebuilt_release(self.reims), "v1.0.1")
        self.assertEqual(reims_release.current_release(self.reims), "v1.0.1")
        self.assertTrue(reims_release.is_prebuilt(self.reims))

    def test_marker_without_binary_is_not_prebuilt(self):
        """Marca sozinha não basta: não prometer binário que não existe."""
        (self.reims / ".release").write_text("v1.0.1\n", encoding="utf-8")
        self.assertIsNone(reims_build.prebuilt_release(self.reims))
        self.assertFalse(reims_release.is_prebuilt(self.reims))

    def test_binary_without_marker_is_not_prebuilt(self):
        """Um `build/` local não é uma release; não confundir os dois."""
        self._add_qemu()
        self.assertIsNone(reims_build.prebuilt_release(self.reims))

    def test_source_tree_is_not_prebuilt(self):
        (self.reims / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
        (self.reims / "crates").mkdir()
        (self.reims / "vendor" / "qemu").mkdir(parents=True)
        (self.reims / "vendor" / "qemu" / "meson.build").write_text("", encoding="utf-8")
        self.assertIsNone(reims_build.prebuilt_release(self.reims))

    def test_provenance_is_parsed(self):
        (self.reims / ".release").write_text("v1.0.1\n", encoding="utf-8")
        (self.reims / "SOURCE-PROVENANCE.txt").write_text(
            "mode=prebuilt\nrelease=v1.0.1\nrepo=felipeab10/reims-vgpu\n",
            encoding="utf-8",
        )
        prov = reims_build.prebuilt_provenance(self.reims)
        self.assertEqual(prov["mode"], "prebuilt")
        self.assertEqual(prov["release"], "v1.0.1")

    def test_provenance_missing_is_empty_dict(self):
        self.assertEqual(reims_build.prebuilt_provenance(self.reims), {})


class BuildSkipsCompilationTests(unittest.TestCase):
    """`build()` no modo pré-compilado instala o binário e NÃO compila."""

    def setUp(self) -> None:
        # `TemporaryDirectory` com `addCleanup`, e não `mkdtemp()`: as árvores
        # aqui carregam cópias do QEMU (274 MB cada) e o `mkdtemp` sem limpeza
        # deixava ~1 GB por execução em `/tmp`. Num tmpfs de 24 GB isso
        # transborda depois de algumas rodadas, e o sintoma são falhas
        # ALEATÓRIAS de escrita em testes sem relação nenhuma com o assunto.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.reims = self.root / "opt" / "reims-vgpu"
        _make_release_tree(self.reims)
        (self.reims / ".release").write_text("v1.0.1\n", encoding="utf-8")
        (self.reims / "SOURCE-PROVENANCE.txt").write_text(
            "mode=prebuilt\nrelease=v1.0.1\n", encoding="utf-8"
        )
        self.data = self.root / "data"

    def test_build_installs_without_running_any_stage(self):
        with patch.object(reims_build, "qemu_data_dir", lambda: self.data), patch.object(
            reims_build, "_run_stage", side_effect=AssertionError("não deveria compilar")
        ):
            ok = reims_build.build(self.reims)
        self.assertTrue(ok)

        link = self.data / "qemu" / "current"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "v1.0.1")

        installed = link / "bin" / "qemu-system-x86_64"
        self.assertTrue(installed.is_file())
        self.assertTrue(os.access(installed, os.X_OK))

    def test_pc_bios_is_carried_over(self):
        with patch.object(reims_build, "qemu_data_dir", lambda: self.data), patch.object(
            reims_build, "_run_stage", side_effect=AssertionError("não deveria compilar")
        ):
            reims_build.build(self.reims)
        self.assertTrue((self.data / "qemu" / "current" / "pc-bios" / "pxe-virtio.rom").is_file())

    def test_missing_binary_returns_false_without_raising(self):
        (self.reims / "vendor" / "qemu" / "build" / "bin" / "qemu-system-x86_64").unlink()
        with patch.object(reims_build, "qemu_data_dir", lambda: self.data):
            self.assertFalse(reims_build.install_prebuilt(self.reims))

    def test_directory_where_symlink_belongs_is_not_destroyed(self):
        """Nunca apagar um diretório de usuário para criar o symlink `current`.

        Verifica também o *motivo* da recusa: se a guarda explícita sumir, o
        `unlink()` de um diretório levanta OSError e o teste ainda veria False —
        passaria pelo motivo errado. Por isso a mensagem é conferida.
        """
        import io as _io
        from contextlib import redirect_stdout

        (self.data / "qemu" / "current").mkdir(parents=True)
        keep = self.data / "qemu" / "current" / "meu-arquivo"
        keep.write_text("importante", encoding="utf-8")
        buf = _io.StringIO()
        with patch.object(reims_build, "qemu_data_dir", lambda: self.data):
            with redirect_stdout(buf):
                ok = reims_build.install_prebuilt(self.reims)
        self.assertFalse(ok)
        self.assertTrue(keep.is_file())
        self.assertIn("é um diretório", buf.getvalue())

    def test_symlink_pointing_at_a_directory_is_repointed_not_refused(self):
        """`current` symlink-para-diretório deve ser repontado, não recusado.

        Distinto de `test_directory_where_symlink_belongs_is_not_destroyed`:
        ali `current` é um diretório de verdade (recusar é o certo); aqui é um
        *symlink* apontando para um diretório. Uma guarda que só testasse
        `link.is_dir()` — sem o `and not link.is_symlink()` — trataria este caso
        como o outro e recusaria a instalação, quebrando a troca de release.
        """
        old_tree = self.root / "arvore-antiga"
        old_tree.mkdir()
        (old_tree / "bin").mkdir()
        (old_tree / "bin" / "qemu-system-x86_64").write_text("binário velho")
        (self.data / "qemu").mkdir(parents=True)
        (self.data / "qemu" / "current").symlink_to(old_tree, target_is_directory=True)

        with patch.object(reims_build, "qemu_data_dir", lambda: self.data):
            ok = reims_build.install_prebuilt(self.reims)

        self.assertTrue(ok, "um symlink para diretório tem de ser repontado")
        link = self.data / "qemu" / "current"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (self.data / "qemu" / "v1.0.1").resolve())
        # A árvore antiga continua existindo: repontar não apaga o que havia.
        self.assertTrue((old_tree / "bin" / "qemu-system-x86_64").is_file())

    def test_existing_symlink_is_replaced_to_point_at_new_release(self):
        """Trocar de release deve repontar `current`, não falhar."""
        with patch.object(reims_build, "qemu_data_dir", lambda: self.data):
            self.assertTrue(reims_build.install_prebuilt(self.reims))
            old = self.data / "qemu" / "current"
            self.assertEqual(os.readlink(old), "v1.0.1")

            (self.reims / ".release").write_text("v2.0.0\n", encoding="utf-8")
            self.assertTrue(reims_build.install_prebuilt(self.reims))
            self.assertEqual(os.readlink(self.data / "qemu" / "current"), "v2.0.0")

    def test_evaluate_reports_release_reason(self):
        state = reims_build.evaluate(self.reims)
        self.assertFalse(state.needs_build)
        self.assertIn("v1.0.1", state.reason)
    def test_evaluate_with_sources_still_wants_a_build(self):
        """O modo fontes não pode ter regredido."""
        (self.reims / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
        (self.reims / "scripts").mkdir()
        (self.reims / "scripts" / "install-ctl.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.reims / "crates").mkdir()
        (self.reims / "vendor" / "qemu" / "meson.build").write_text("", encoding="utf-8")
        (self.reims / ".release").unlink()
        state = reims_build.evaluate(self.reims)
        self.assertTrue(state.needs_build)


class PrebuiltMotorInstallTests(unittest.TestCase):
    """A release tem de entregar o motor `reims-ctl`, e ele tem de ir para o lugar.

    O appliance **não tem motor de reserva**: sem `reims-ctl` o QEMU instalado não
    sobe VM nenhuma, porque `decide_reimsctl()` recusa o lançamento. Instalar o
    QEMU e esquecer o motor reproduz exatamente o sintoma do usuário — a ISO
    instala a compilação e ainda assim a VM não inicia.

    O destino é `reims_ctl_install_dir()`, o mesmo diretório que
    `core.reims_ctl.reims_ctl_bin()` consulta. Não é `/usr/local/bin`: aquele
    caminho vem antes no PATH e uma cópia embutida na ISO *sombrearia* toda
    atualização feita em runtime.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.reims = self.root / "opt" / "reims-vgpu"
        _make_release_tree(self.reims)
        (self.reims / ".release").write_text("v1.0.1\n", encoding="utf-8")
        self.data = self.root / "data"
        self.home = self.root / "home"
        self.home.mkdir()

    def _com_motor(self, rel: str = "bin/reims-ctl") -> Path:
        motor = self.reims / rel
        motor.parent.mkdir(parents=True, exist_ok=True)
        motor.write_text("#!/bin/sh\necho motor\n", encoding="utf-8")
        # 0644 de propósito: é assim que os arquivos chegam da ISO, porque o
        # `mkarchiso` só aplica permissões ao que está listado no `profiledef.sh`.
        # Quem instala tem de dar o bit de execução — `copy2` preserva o modo de
        # origem, então uma origem 0755 esconderia a falta do `chmod`.
        motor.chmod(0o644)
        return motor

    def test_the_motor_from_the_bundle_is_installed_where_the_app_finds_it(self):
        self._com_motor()
        with patch.object(reims_build, "qemu_data_dir", lambda: self.data), patch.dict(
            os.environ, {"HOME": str(self.home)}
        ):
            self.assertTrue(reims_build.install_prebuilt(self.reims))

        destino = self.home / ".cargo" / "bin" / "reims-ctl"
        self.assertTrue(destino.is_file(), "o motor não foi instalado")
        self.assertTrue(os.access(destino, os.X_OK), "o motor ficou sem bit de execução")
        self.assertEqual(destino.read_text(encoding="utf-8"), "#!/bin/sh\necho motor\n")

        # E o resolvedor que o appliance usa encontra o motor por esse caminho.
        from core import reims_ctl

        with patch.dict(os.environ, {"HOME": str(self.home)}), patch(
            "shutil.which", return_value=None
        ):
            self.assertEqual(reims_ctl.reims_ctl_bin(), str(destino))

    def test_the_motor_at_the_tarball_root_is_also_accepted(self):
        """Tolerância de layout: `reims-ctl` na raiz do tarball também vale."""
        self._com_motor("reims-ctl")
        with patch.object(reims_build, "qemu_data_dir", lambda: self.data), patch.dict(
            os.environ, {"HOME": str(self.home)}
        ):
            self.assertTrue(reims_build.install_prebuilt(self.reims))
        self.assertTrue((self.home / ".cargo" / "bin" / "reims-ctl").is_file())

    def test_switching_release_updates_the_matching_reims_ctl_too(self):
        """Rollback deve trocar o controlador junto com a versão do QEMU."""
        ctl = self._com_motor()
        ctl_dest = self.home / ".cargo" / "bin" / "reims-ctl"
        with patch.object(reims_build, "qemu_data_dir", lambda: self.data), patch.dict(
            os.environ, {"HOME": str(self.home)}
        ):
            self.assertTrue(reims_build.install_prebuilt(self.reims))
            self.assertEqual(ctl_dest.read_bytes(), ctl.read_bytes())
            qemu_v101 = self.data / "qemu" / "v1.0.1" / "bin" / "qemu-system-x86_64"
            self.assertTrue(qemu_v101.is_file())

            (self.reims / ".release").write_text("v1.0.0\n", encoding="utf-8")
            ctl.write_text("#!/bin/sh\necho motor anterior\n", encoding="utf-8")
            self.assertTrue(reims_build.install_prebuilt(self.reims))

        self.assertEqual(ctl_dest.read_bytes(), ctl.read_bytes())
        self.assertEqual(os.readlink(self.data / "qemu" / "current"), "v1.0.0")
        self.assertTrue(qemu_v101.is_file(), "a versão anterior precisa continuar instalada")
        qemu_v100 = self.data / "qemu" / "v1.0.0" / "bin" / "qemu-system-x86_64"
        self.assertTrue(qemu_v100.is_file())

    def test_a_bundle_without_the_motor_is_warned_about(self):
        """Sem motor no bundle: instala o QEMU, mas avisa em vez de ficar quieto."""
        from contextlib import redirect_stdout

        from core import reims_ctl

        buf = io.StringIO()
        with patch.object(reims_build, "qemu_data_dir", lambda: self.data), patch.dict(
            os.environ, {"HOME": str(self.home)}
        ), patch.object(reims_ctl, "reims_ctl_bin", return_value=None):
            with redirect_stdout(buf):
                ok = reims_build.install_prebuilt(self.reims)
        self.assertTrue(ok)
        self.assertIn("não trouxe o motor reims-ctl", buf.getvalue())

    def test_a_motor_already_on_the_system_is_not_warned_about(self):
        """Contraprova do aviso: com um motor utilizável, nada é dito.

        Sem isto, um aviso incondicional passaria no teste acima.
        """
        from contextlib import redirect_stdout

        from core import reims_ctl

        buf = io.StringIO()
        with patch.object(reims_build, "qemu_data_dir", lambda: self.data), patch.dict(
            os.environ, {"HOME": str(self.home)}
        ), patch.object(
            reims_ctl, "reims_ctl_bin", return_value="/usr/local/bin/reims-ctl"
        ):
            with redirect_stdout(buf):
                self.assertTrue(reims_build.install_prebuilt(self.reims))
        self.assertNotIn("não trouxe o motor", buf.getvalue())


class PrebuiltInstallIsIdempotentTests(unittest.TestCase):
    """Instalar de novo não pode recopiar a árvore.

    O "Iniciar VM" chama `install_prebuilt` a cada lançamento e o destino é o
    tmpfs da ISO live: sem a saída de idempotência, cada clique recopiava ~100 MB
    para a RAM para produzir exatamente o mesmo prefixo.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.reims = self.root / "opt" / "reims-vgpu"
        _make_release_tree(self.reims)
        (self.reims / ".release").write_text("v1.0.1\n", encoding="utf-8")
        self.data = self.root / "data"
        self.destino = self.data / "qemu" / "v1.0.1" / "bin" / "qemu-system-x86_64"

    def test_the_second_call_does_not_copy_anything(self):
        """O sinal é a QUANTIDADE de cópias, não que o copy2 tenha sido chamado.

        A primeira versão deste teste media `copy2` com `AssertionError` como
        efeito. Isso se anulava sozinho: o `install_prebuilt` engole `OSError`,
        mas a exceção levantada dentro do `with` voltava pelo `finally`, e o
        `assertTrue` — que é a medida — nunca rodava. Medido por mutação: apagar
        o bloco de idempotência inteiro passava neste teste (escapou). Agora o
        contador vive fora da função, então não há como a falha ser engolida.
        """
        copias = []
        real = reims_build.shutil.copy2

        def contando(*a, **kw):
            copias.append(a[0] if a else kw.get("src"))
            return real(*a, **kw)

        with patch.object(reims_build, "qemu_data_dir", lambda: self.data), patch.object(
            reims_build.shutil, "copy2", contando
        ):
            self.assertTrue(reims_build.install_prebuilt(self.reims))
            self.assertTrue(self.destino.is_file(), "a primeira chamada não instalou")
            assert copias, "a primeira chamada não copiou nada (pré-condição)"
            antes = len(copias)
            self.assertTrue(reims_build.install_prebuilt(self.reims))

        self.assertEqual(
            antes, len(copias),
            f"a segunda chamada recopiou {len(copias) - antes} arquivo(s) para o "
            "tmpfs, com o prefixo já íntegro e a mesma release",
        )

    def test_the_install_is_not_skipped_for_a_different_release(self):
        """Controle negativo do contador: release diferente TEM de recopiar.

        Sem este par, "nunca copiar" também passaria no teste acima.
        """
        copias = []
        real = reims_build.shutil.copy2

        def contando(*a, **kw):
            copias.append(a[0] if a else kw.get("src"))
            return real(*a, **kw)

        outra = self.root / "opt" / "reims-vgpu-nova"
        _make_release_tree(outra)
        (outra / ".release").write_text("v1.0.2\n", encoding="utf-8")

        with patch.object(reims_build, "qemu_data_dir", lambda: self.data), patch.object(
            reims_build.shutil, "copy2", contando
        ):
            self.assertTrue(reims_build.install_prebuilt(self.reims))
            antes = len(copias)
            self.assertTrue(reims_build.install_prebuilt(outra))

        self.assertGreater(
            len(copias), antes,
            "a release nova reaproveitou o prefixo da anterior",
        )
        self.assertTrue(
            (self.data / "qemu" / "v1.0.2" / "bin" / "qemu-system-x86_64").is_file()
        )

    def test_a_deleted_binary_is_reinstalled_even_with_the_right_symlink(self):
        """Contraprova: `current` certo com o arquivo apagado tem de reinstalar.

        Uma idempotência que olhasse só o symlink passaria no teste acima e
        deixaria a VM apontando para um binário que não existe.
        """
        # A volta do `current` roda DENTRO do patch: no ramo mutante ela levanta
        # OSError, o `install_prebuilt` engole e devolve True — medido fora, essa
        # falha virava "passou". Aqui a instalação de volta tem de completar.
        with patch.object(reims_build, "qemu_data_dir", lambda: self.data):
            self.assertTrue(reims_build.install_prebuilt(self.reims))
            self.destino.unlink()
            reinstalou = reims_build.install_prebuilt(self.reims)
            self.assertTrue(
                reinstalou, "o prefixo mutilado não foi reconstruído"
            )
            self.assertTrue(
                self.destino.is_file(), "o binário apagado não voltou ao prefixo"
            )
            self.assertTrue(os.access(self.destino, os.X_OK))

    def test_a_missing_motor_reinstalls_even_with_the_qemu_in_place(self):
        """A árvore já instalada mas sem o motor não pode devolver "pronto"."""
        motor = self.reims / "bin" / "reims-ctl"
        motor.parent.mkdir(parents=True, exist_ok=True)
        motor.write_text("#!/bin/sh\necho motor\n", encoding="utf-8")
        motor.chmod(0o755)
        home = self.root / "home"
        home.mkdir()
        with patch.object(reims_build, "qemu_data_dir", lambda: self.data), patch.dict(
            os.environ, {"HOME": str(home)}
        ):
            self.assertTrue(reims_build.install_prebuilt(self.reims))
            ctl = home / ".cargo" / "bin" / "reims-ctl"
            self.assertTrue(ctl.is_file())
            # O binário do QEMU continua lá; só o motor some.
            ctl.unlink()
            self.assertTrue(reims_build.install_prebuilt(self.reims))
        self.assertTrue(ctl.is_file())

    def test_the_installed_prefix_survives_a_missing_source_tree(self):
        """Origem removida + prefixo íntegro: "já está pronto", não erro.

        O prefixo é o que a VM executa; exigir a árvore de origem aqui trocaria um
        sucesso por uma recusa.
        """
        import shutil as _shutil

        with patch.object(reims_build, "qemu_data_dir", lambda: self.data):
            self.assertTrue(reims_build.install_prebuilt(self.reims))
            _shutil.rmtree(self.reims)
            self.assertTrue(
                reims_build.install_prebuilt(self.reims),
                "o prefixo já instalado foi recusado só porque a origem sumiu",
            )


class EvaluateResolvesTheInstalledPrefixTests(unittest.TestCase):
    """No modo pré-compilado `evaluate` tem de achar o binário do prefixo.

    Antes ele caía no caminho plano (`vendor/qemu/build/qemu-system-x86_64`), que
    a release **não** publica: `binary_path` voltava `None` e o "Iniciar VM"
    recusava mesmo com a compilação instalada ao lado.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.reims = self.root / "opt" / "reims-vgpu"
        _make_release_tree(self.reims)
        (self.reims / ".release").write_text("v1.0.1\n", encoding="utf-8")
        self.data = self.root / "data"

    def test_binary_path_points_at_the_in_tree_prefixed_layout(self):
        """O defeito: `evaluate` procurava o binário no caminho PLANO.

        A release publica `vendor/qemu/build/bin/qemu-system-x86_64` (layout de
        prefixo). O caminho plano, `vendor/qemu/build/qemu-system-x86_64`, existe
        só nas árvores compiladas no host. Enquanto `evaluate` olhava o plano,
        `binary_path` voltava `None` no modo pré-compilado.
        """
        with patch.object(reims_build, "qemu_data_dir", lambda: self.data):
            state = reims_build.evaluate(self.reims)
        self.assertIsNotNone(state.binary_path, "o binário pré-compilado não foi resolvido")
        self.assertEqual(
            state.binary_path, self.reims / "vendor" / "qemu" / "build" / "bin" / "qemu-system-x86_64"
        )
        self.assertNotEqual(
            state.binary_path, self.reims / "vendor" / "qemu" / "build" / "qemu-system-x86_64"
        )

    def test_binary_path_prefers_the_installed_prefix_over_the_tree(self):
        """Com a compilação instalada, quem manda é o prefixo do runtime."""
        with patch.object(reims_build, "qemu_data_dir", lambda: self.data):
            self.assertTrue(reims_build.install_prebuilt(self.reims))
            state = reims_build.evaluate(self.reims)
        self.assertEqual(
            state.binary_path, self.data / "qemu" / "current" / "bin" / "qemu-system-x86_64"
        )

    def test_binary_path_is_none_when_no_layout_has_a_binary(self):
        """Contraprova: sem binário nenhum o caminho não pode ser inventado."""
        import shutil as _shutil

        _shutil.rmtree(self.reims / "vendor")
        with patch.object(reims_build, "qemu_data_dir", lambda: self.data):
            state = reims_build.evaluate(self.reims)
        self.assertIsNone(state.binary_path)


class EnsureReimsBuiltInstallsFirstTests(unittest.TestCase):
    """`_ensure_reims_built` instala a compilação da ISO ANTES de decidir.

    O ramo pré-compilado devolvia o estado avaliado **antes** da instalação: a VM
    era recusada por "binário ausente" mesmo com o binário empacotado ali do lado.
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import ui.tui as tui  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(f"UI indisponível: {exc}") from exc

    def setUp(self) -> None:
        import ui.tui as tui

        self.tui = tui
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.reims = self.root / "opt" / "reims-vgpu"
        self.reims.mkdir(parents=True)
        self.binario = self.data_path = (
            self.root / "data" / "qemu" / "current" / "bin" / "qemu-system-x86_64"
        )
        self.inst = tui.VMTUI.__new__(tui.VMTUI)

    @staticmethod
    def _estado(reims, binary_path):
        from types import SimpleNamespace

        return SimpleNamespace(
            reims_dir=reims,
            sources_present=False,
            needs_build=False,
            reason="release v1.0.1",
            binary_path=binary_path,
        )

    def _patch_tui(self, **extra):
        from contextlib import nullcontext
        from unittest.mock import patch as _patch

        patches = [
            _patch.object(self.tui, "ensure_executable_bits", lambda _d: None),
            _patch.object(self.tui.console, "status", lambda *a, **_k: nullcontext()),
        ]
        for nome, valor in extra.items():
            patches.append(_patch.object(self.tui, nome, **valor))
        return patches

    def test_the_bundled_build_is_installed_and_the_new_state_is_used(self):
        """Uma chamada basta: instala e reavalia, com o estado **novo**."""
        self.binario.parent.mkdir(parents=True, exist_ok=True)
        self.binario.write_text("#!/bin/sh\n", encoding="utf-8")
        self.binario.chmod(0o755)
        avaliacoes = []

        def avaliar(explicit=None):
            avaliacoes.append(explicit)
            return self._estado(self.reims, None) if len(avaliacoes) == 1 else self._estado(
                self.reims, self.binario
            )

        with contextlib.ExitStack() as pilha:
            for p in self._patch_tui(
                evaluate_reims_build={"side_effect": avaliar},
                install_prebuilt={"return_value": True},
            ):
                pilha.enter_context(p)
            ok = self.inst._ensure_reims_built(str(self.reims))

        self.assertTrue(ok, "a VM foi recusada com a compilação da ISO instalada")
        self.assertEqual(len(avaliacoes), 2, "o estado não foi reavaliado após instalar")

    def test_the_install_is_called_with_the_tree_and_before_the_verdict(self):
        self.binario.parent.mkdir(parents=True, exist_ok=True)
        self.binario.write_text("#!/bin/sh\n", encoding="utf-8")
        self.binario.chmod(0o755)
        with contextlib.ExitStack() as pilha:
            for p in self._patch_tui(
                evaluate_reims_build={
                    "side_effect": lambda explicit=None: self._estado(self.reims, self.binario)
                },
                install_prebuilt={"return_value": True},
            ):
                pilha.enter_context(p)
            instalador = pilha.enter_context(
                patch.object(self.tui, "install_prebuilt", return_value=True)
            )
            self.inst._ensure_reims_built(str(self.reims))
        instalador.assert_called_once_with(self.reims)

    def test_a_failed_install_refuses_instead_of_launching_nothing(self):
        """Contraprova: instalação falha ⇒ False, e NÃO se decide com o estado novo.

        O estado seguinte tem binário: se a falha fosse ignorada, a função
        devolveria True e o lançamento seguiria com uma instalação que não
        aconteceu. Por isso o segundo estado aqui tem binário de verdade — é ele
        que faz o teste reprovar um `if` que não devolve False.
        """
        self.binario.parent.mkdir(parents=True, exist_ok=True)
        self.binario.write_text("#!/bin/sh\n", encoding="utf-8")
        self.binario.chmod(0o755)
        avaliacoes = []

        def avaliar(explicit=None):
            avaliacoes.append(explicit)
            return self._estado(self.reims, None) if len(avaliacoes) == 1 else self._estado(
                self.reims, self.binario
            )

        with contextlib.ExitStack() as pilha:
            for p in self._patch_tui(
                evaluate_reims_build={"side_effect": avaliar},
                install_prebuilt={"return_value": False},
            ):
                pilha.enter_context(p)
            self.assertFalse(self.inst._ensure_reims_built(str(self.reims)))
        self.assertEqual(
            len(avaliacoes), 1, "decidiu com um estado que não foi instalado"
        )

    def test_the_source_mode_still_compiles_and_is_not_touched(self):
        """O modo fontes não pode ter regredido com a mudança do ramo pré-compilado."""
        from types import SimpleNamespace

        com_fontes = SimpleNamespace(
            reims_dir=self.reims,
            sources_present=True,
            needs_build=False,
            reason="fontes atualizadas",
            binary_path=self.binario,
        )
        with contextlib.ExitStack() as pilha:
            for p in self._patch_tui(
                evaluate_reims_build={"return_value": com_fontes},
                install_prebuilt={"return_value": True},
            ):
                pilha.enter_context(p)
            instalador = pilha.enter_context(
                patch.object(self.tui, "install_prebuilt", return_value=True)
            )
            self.assertTrue(self.inst._ensure_reims_built(str(self.reims)))
        self.assertFalse(
            instalador.called,
            "o modo fontes não pode instalar o pré-compilado",
        )


class ReimsReleaseMenuItemWiringTests(unittest.TestCase):
    """O item "A" existe no laço principal e chama a rotina certa.

    Um teste de texto ("a chave existe no i18n") não liga a opção ao código: a
    linha podia aparecer no menu e a tecla não fazer nada. Aqui a resposta "A" é
    aceita pelo próprio `Prompt.ask` (as escolhas são conferidas) e o método é
    chamado.
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import ui.tui as tui  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(f"UI indisponível: {exc}") from exc

    def setUp(self) -> None:
        import ui.tui as tui
        from unittest.mock import MagicMock

        self.tui = tui
        self.inst = tui.VMTUI.__new__(tui.VMTUI)
        self.inst.store = MagicMock()
        self.inst.store.list_vms.return_value = []
        self.inst.lock = MagicMock()
        self.inst.lock.get_running_vm_info.return_value = None
        self.inst.system_mode = "installed"
        self.inst.installer_available = False

    def _rodar_menu(self, respostas):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest.mock import patch as _patch

        vistos = []

        def perguntar(*_a, **kw):
            escolhas = list(kw.get("choices") or [])
            resposta = respostas[len(vistos)]
            # A resposta simulada tem de ser uma das oferecidas: é isto que liga
            # a linha impressa no menu à tecla que o usuário digita.
            self.assertIn(resposta, escolhas, f'"{resposta}" não está nas escolhas do menu')
            vistos.append(resposta)
            return resposta

        saida = io.StringIO()
        with _patch.object(self.tui.VMTUI, "_language_screen", lambda s: None), _patch.object(
            self.tui.VMTUI, "_backfill_install_phases", lambda s: None
        ), _patch.object(self.tui.VMTUI, "_render_vm_table", lambda s, vms: None), _patch.object(
            self.tui.SystemReadinessChecker,
            "check_all",
            return_value=SimpleNamespace(can_run_vms=True, has_internet=True, checks=[]),
        ), _patch.object(
            self.tui.console, "status", lambda *a, **_k: nullcontext()
        ), _patch.object(
            self.tui.Prompt, "ask", side_effect=perguntar
        ), contextlib.redirect_stdout(
            saida
        ):
            self.inst.run_main_loop()
        return saida.getvalue(), vistos

    def test_choice_a_is_offered_and_runs_the_release_update(self):
        chamadas = []
        with patch.object(
            self.tui.VMTUI, "_menu_reims_release", lambda s: chamadas.append("A")
        ):
            saida, vistos = self._rodar_menu(["A", "0"])
        self.assertEqual(vistos, ["A", "0"])
        self.assertEqual(chamadas, ["A"], 'a tecla "A" não chamou a rotina da release')
        self.assertIn("Atualizar reims-vgpu", saida, "a opção não aparece no menu")

    def test_lowercase_a_also_works(self):
        chamadas = []
        with patch.object(
            self.tui.VMTUI, "_menu_reims_release", lambda s: chamadas.append("a")
        ):
            self._rodar_menu(["a", "0"])
        self.assertEqual(chamadas, ["a"])


class ReleaseUrlTests(unittest.TestCase):
    def test_latest_uses_latest_download(self):
        url = reims_release.release_asset_url("latest")
        self.assertEqual(
            url,
            "https://github.com/felipeab10/reims-vgpu/releases/latest/download/"
            "reims-vgpu-linux-x86_64.tar.gz",
        )

    def test_tag_uses_download_path(self):
        url = reims_release.release_asset_url("v1.0.1")
        self.assertEqual(
            url,
            "https://github.com/felipeab10/reims-vgpu/releases/download/v1.0.1/"
            "reims-vgpu-linux-x86_64.tar.gz",
        )

    def test_empty_tag_is_latest(self):
        self.assertEqual(
            reims_release.release_asset_url(""), reims_release.release_asset_url("latest")
        )


class UpdateFromReleaseTests(unittest.TestCase):
    """`update_from_release` troca o binário, preserva o resto e nunca destrói."""

    def setUp(self) -> None:
        # `TemporaryDirectory` com `addCleanup`, e não `mkdtemp()`: as árvores
        # aqui carregam cópias do QEMU (274 MB cada) e o `mkdtemp` sem limpeza
        # deixava ~1 GB por execução em `/tmp`. Num tmpfs de 24 GB isso
        # transborda depois de algumas rodadas, e o sintoma são falhas
        # ALEATÓRIAS de escrita em testes sem relação nenhuma com o assunto.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.reims = self.root / "opt" / "reims-vgpu"
        build = self.reims / "vendor" / "qemu" / "build"
        (build / "bin").mkdir(parents=True)
        self.qemu = build / "bin" / "qemu-system-x86_64"
        self.qemu.write_text("#!/bin/sh\necho velho\n", encoding="utf-8")
        self.qemu.chmod(0o755)
        (build / "share" / "qemu").mkdir(parents=True)
        (build / "share" / "qemu" / "ReimsVgpu.rom").write_bytes(b"rom-antiga")
        (self.reims / ".release").write_text("v1.0.0\n", encoding="utf-8")
        (self.reims / "scripts").mkdir()
        (self.reims / "scripts" / "install-ctl.sh").write_text("#!/bin/sh\n", encoding="utf-8")

        tree = _make_release_tree(self.root / "payload")
        self.tarball = _pack(tree, self.root / "rel.tar.gz")

    @staticmethod
    def _fake_download(src: Path):
        import shutil

        def _dl(url, dest, log=None):  # noqa: ARG001
            shutil.copy2(src, dest)

        return _dl

    def _patch(self, tag: str):
        return patch.multiple(
            reims_release,
            _download=self._fake_download(self.tarball),
            latest_release=lambda **kw: reims_release.Release(
                tag=tag, name=tag, published_at="", asset_url=""
            ),
        )

    def test_download_failure_leaves_tree_untouched(self):
        def boom(url, dest, log=None):  # noqa: ARG001
            raise reims_release.ReleaseError("sem rede")

        with patch.object(reims_release, "_download", boom):
            result = reims_release.update_from_release(
                tag="v9.9.9", reims_dir=self.reims, log=lambda _m: None
            )
        self.assertFalse(result.ok)
        self.assertIn("sem rede", result.message)
        self.assertEqual(reims_release.current_release(self.reims), "v1.0.0")
        self.assertIn("velho", self.qemu.read_text(encoding="utf-8"))

    def test_release_without_qemu_is_rejected_and_tree_intact(self):
        tree = _make_release_tree(self.root / "incomplete", with_qemu=False)
        tree.mkdir(parents=True, exist_ok=True)
        (tree / "README.md").write_text("oi", encoding="utf-8")
        bad = _pack(tree, self.root / "bad.tar.gz")

        with patch.object(reims_release, "_download", self._fake_download(bad)):
            result = reims_release.update_from_release(
                tag="v9.9.9", reims_dir=self.reims, log=lambda _m: None
            )
        self.assertFalse(result.ok)
        self.assertIn("nada foi trocado", result.message)
        self.assertIn("velho", self.qemu.read_text(encoding="utf-8"))
        self.assertEqual(reims_release.current_release(self.reims), "v1.0.0")

    def test_same_tag_without_force_is_a_noop(self):
        with self._patch("v1.0.0"):
            result = reims_release.update_from_release(reims_dir=self.reims, log=lambda _m: None)
        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertIn("v1.0.0", result.message)

    def test_successful_update_replaces_binary_and_keeps_sources(self):
        with self._patch("v1.0.2"):
            result = reims_release.update_from_release(reims_dir=self.reims, log=lambda _m: None)
        self.assertTrue(result.ok)
        self.assertTrue(result.changed)
        self.assertEqual(reims_release.current_release(self.reims), "v1.0.2")
        self.assertIn("novo", self.qemu.read_text(encoding="utf-8"))
        # O modo fontes continua funcionando: nada de apagar scripts/.
        self.assertTrue((self.reims / "scripts" / "install-ctl.sh").is_file())

    def test_path_traversal_in_tarball_is_refused(self):
        evil = self.root / "evil.tar.gz"
        with tarfile.open(evil, "w:gz") as tar:
            info = tarfile.TarInfo("../../etc/pwned")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"evil"))

        with patch.object(reims_release, "_download", self._fake_download(evil)):
            result = reims_release.update_from_release(
                tag="v9.9.9", reims_dir=self.reims, log=lambda _m: None
            )
        self.assertFalse(result.ok)
        self.assertIn("suspeito", result.message)
        self.assertFalse((self.root / "etc" / "pwned").exists())


class ListReleasesTests(unittest.TestCase):
    """A listagem filtra release sem o asset e respeita prerelease/draft."""

    def test_filters_assets_and_prereleases(self):
        payload = [
            {"tag_name": "v2.0.0-rc1", "prerelease": True,
             "assets": [{"name": reims_release.RELEASE_ASSET, "size": 10,
                         "browser_download_url": "u1"}]},
            {"tag_name": "v1.0.1", "prerelease": False,
             "assets": [{"name": reims_release.RELEASE_ASSET, "size": 20,
                         "browser_download_url": "u2"}]},
            {"tag_name": "v1.0.0", "prerelease": False,
             "assets": [{"name": "outro.tar.gz", "size": 30,
                         "browser_download_url": "u3"}]},
            {"tag_name": "rascunho", "draft": True,
             "assets": [{"name": reims_release.RELEASE_ASSET, "size": 40,
                         "browser_download_url": "u4"}]},
        ]
        import json
        import subprocess

        fake = subprocess.CompletedProcess(
            args=["curl"], returncode=0, stdout=json.dumps(payload), stderr=""
        )
        with patch.object(reims_release, "_run", return_value=fake):
            releases = reims_release.list_releases()
        self.assertEqual([r.tag for r in releases], ["v1.0.1"])
        self.assertEqual(releases[0].asset_size, 20)

        with patch.object(reims_release, "_run", return_value=fake):
            with_prerelease = reims_release.list_releases(include_prerelease=True)
        self.assertEqual([r.tag for r in with_prerelease], ["v2.0.0-rc1", "v1.0.1"])

    def test_api_failure_raises_release_error(self):
        import subprocess

        fake = subprocess.CompletedProcess(args=["curl"], returncode=22, stdout="", stderr="")
        with patch.object(reims_release, "_run", return_value=fake):
            with self.assertRaises(reims_release.ReleaseError):
                reims_release.list_releases()

    def test_requests_up_to_one_hundred_releases_for_rollback_choices(self):
        import json
        import subprocess

        fake = subprocess.CompletedProcess(
            args=["curl"], returncode=0, stdout=json.dumps([]), stderr=""
        )
        with patch.object(reims_release, "_run", return_value=fake) as run:
            reims_release.list_releases()
        self.assertIn("per_page=100&page=1", run.call_args.args[0][-1])

    def test_release_list_follows_additional_api_pages(self):
        import json
        import subprocess

        first_page = [
            {
                "tag_name": f"v{index}",
                "assets": [{"name": reims_release.RELEASE_ASSET, "size": 1}],
            }
            for index in range(100)
        ]
        second_page = [{
            "tag_name": "v-older",
            "assets": [{"name": reims_release.RELEASE_ASSET, "size": 1}],
        }]
        responses = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(data), stderr="")
            for data in (first_page, second_page)
        ]
        with patch.object(reims_release, "_run", side_effect=responses) as run:
            releases = reims_release.list_releases()
        self.assertEqual(len(releases), 101)
        self.assertEqual(releases[-1].tag, "v-older")
        self.assertIn("page=2", run.call_args_list[1].args[0][-1])

    def test_malformed_json_raises_release_error(self):
        import subprocess

        fake = subprocess.CompletedProcess(
            args=["curl"], returncode=0, stdout="não é json", stderr=""
        )
        with patch.object(reims_release, "_run", return_value=fake):
            with self.assertRaises(reims_release.ReleaseError):
                reims_release.list_releases()


class PortabilityClaimTests(unittest.TestCase):
    """O binário não pode passar a exigir o host de build."""

    def test_module_documents_dlopen_vulkan(self):
        """A porta de entrada do Vulkan é dlopen, então a lib não é linkada."""
        text = Path(reims_release.__file__).read_text(encoding="utf-8")
        self.assertIn("dlopen", text)
        self.assertIn("x86-64", text)

    def test_no_hardcoded_build_host_path(self):
        text = Path(reims_release.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/home/felipeab10/reims-vgpu", text)

    def test_vulkan_runtime_ok_reports_reason(self):
        ok, message = reims_release.vulkan_runtime_ok()
        self.assertIn("libvulkan", message)
        self.assertIsInstance(ok, bool)


class ReleaseScriptContractTests(unittest.TestCase):
    """O script de release do fork tem de casar com o que o appliance consome.

    `scripts/release.sh` vive no fork reims-vgpu, não neste repositório, então
    estes testes pulam quando ele não está disponível (ex.: CI sem o fork).
    """

    SCRIPT = Path("/home/felipeab10/reims-vgpu/scripts/release.sh")

    def setUp(self) -> None:
        if not self.SCRIPT.is_file():
            self.skipTest("fork reims-vgpu ausente neste host")

    def test_asset_name_matches_the_appliance_expectation(self):
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn(reims_release.RELEASE_ASSET, text)
        self.assertIn(reims_release.REIMS_REPO, text)

    def test_packages_the_path_the_appliance_verifies(self):
        """O tarball REAL tem de conter o caminho que `_verify_tree` exige.

        O teste anterior asseria `"vendor/qemu/build/qemu-system-x86_64" in
        text`, e o verifier mediu que isso é satisfeito pela linha 199 do
        release.sh — que é só o DEFAULT de `QEMU_BIN`, não o que é empacotado
        (o `cp` está ~125 linhas abaixo, em `${DIST_TMP}/vendor/qemu/build/bin/`).
        Passava verde com o `cp` removido, com o `cp` apontando para o layout
        antigo, e até sem binário nenhum no pacote.

        Então aqui o `release.sh` RODA de verdade (num repo descartável, com
        `gh` falso e um QEMU de mentira no cache) e o TARBALL resultante é
        aberto: o caminho tem de estar lá DENTRO, e ser um dos que
        `PREBUILT_QEMU_RELS` aceita.
        """
        import stat
        import subprocess as sp
        import tarfile

        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            repo = raiz / "repo"
            (repo / "scripts").mkdir(parents=True)
            script = repo / "scripts" / "release.sh"
            script.write_text(self.SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
            script.chmod(0o755)

            build = repo / "vendor" / "qemu" / "build"
            (build / "bin").mkdir(parents=True)
            qemu = build / "bin" / "qemu-system-x86_64"
            # Um binário com o device da geração nova, como o real: sem isso a
            # verificação do appliance recusaria a árvore por outro motivo.
            qemu.write_bytes(
                b"#!/bin/sh\n# QEMU emulator version 11.1.50\n"
                b"\x00apple-gfx-pci\x00ReimsVgpu.rom\x00\necho novo\n"
            )
            qemu.chmod(0o755)
            (build / "share" / "qemu").mkdir(parents=True)
            (build / "share" / "qemu" / "ReimsVgpu.rom").write_bytes(b"rom-do-vgpu")
            (repo / "vendor" / "qemu" / "pc-bios").mkdir(parents=True)
            (repo / "vendor" / "qemu" / "pc-bios" / "bios-256k.bin").write_bytes(b"b")
            (repo / "vm").mkdir()
            (repo / "vm" / "boot-x86.sh").write_text("#!/bin/sh\n", encoding="utf-8")

            def git(*args: str) -> None:
                sp.run(
                    ["git", *args], cwd=str(repo), check=True,
                    capture_output=True, text=True,
                    env={**os.environ,
                         "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
                )

            git("init", "-q", "-b", "master")
            git("add", "-A")
            git("commit", "-q", "-m", ":new: base")
            git("tag", "v1.0.1")

            fakebin = raiz / "bin"
            fakebin.mkdir()
            gh = fakebin / "gh"
            gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            env = dict(os.environ)
            env["PATH"] = f"{fakebin}:{env.get('PATH', '')}"
            env["TMPDIR"] = str(raiz / "tmpdir")
            (raiz / "tmpdir").mkdir()

            proc = sp.run(
                ["bash", str(script), "--yes", "--skip-push", "--tag", "v1.0.2"],
                capture_output=True, text=True, env=env, cwd=str(repo), timeout=300,
            )
            combinado = proc.stdout + proc.stderr
            tarball = repo / "reims-vgpu-linux-x86_64.tar.gz"
            self.assertTrue(
                tarball.is_file(),
                f"o release.sh não produziu o pacote; o teste mediria nada:\n{combinado[-700:]}",
            )

            with tarfile.open(tarball, "r:gz") as tar:
                # `getnames()` traz o prefixo `./` da release real; normalizo
                # para comparar com os caminhos relativos que o appliance usa,
                # mas guardo o nome ORIGINAL para poder extrair.
                nomes = {n.lstrip("./"): n for n in tar.getnames()}

                # O caminho tem de estar DENTRO do pacote — não basta aparecer
                # no script. É isto que o `_verify_tree` vai procurar na árvore
                # extraída, e sem ele o appliance recusa a release.
                aceitos = [str(rel) for rel in reims_build.PREBUILT_QEMU_RELS]
                presentes = [a for a in aceitos if a in nomes]
                self.assertTrue(
                    presentes,
                    "o pacote não traz nenhum caminho que o appliance aceita "
                    f"(procurei {aceitos}); o release.sh empacotou outra coisa:\n"
                    + "\n".join(n for n in nomes if "qemu" in n)[:800],
                )
                # E o binário precisa ser REALMENTE o do cache, não um placeholder.
                conteudo = tar.extractfile(nomes[presentes[0]]).read()
            self.assertIn(b"apple-gfx-pci", conteudo, "empacotou um binário que não é o do QEMU")
            self.assertIn(b"ReimsVgpu.rom", conteudo)

    def test_generates_next_version_from_tags(self):
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("git tag -l 'v[0-9]*'", text)
        for component in ("major", "minor", "patch"):
            self.assertIn(component, text)

    def test_rolls_back_the_tag_when_publishing_fails(self):
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("git tag -d", text)
        self.assertIn("trap cleanup", text)

    def test_syntax_is_valid(self):
        import subprocess

        proc = subprocess.run(
            ["bash", "-n", str(self.SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_is_not_world_unreadable(self):
        """0755 como os vizinhos: o script é lido e empacotado por outros."""
        mode = self.SCRIPT.stat().st_mode & 0o777
        self.assertTrue(mode & 0o044, f"release.sh sem leitura: {oct(mode)}")
        self.assertTrue(mode & 0o100, f"release.sh não é executável: {oct(mode)}")

    def test_update_sources_can_extract_over_an_existing_tree(self):
        """`--update-sources` tem de funcionar quando a árvore JÁ existe.

        É o uso normal da opção: atualizar fontes que já estão lá. O script
        usava `tar -xkzf`, e no `tar` o `k` é `--keep-old-files`, que PROÍBE
        sobrescrever — a extração morria com "Arquivo existe" (status 2) na
        primeira colisão. Pior: o erro não era checado, então o script seguia e
        gravava o `.release`, anunciando sucesso sobre uma árvore pela metade.

        Medido neste host (GNU tar 1.35): `-xzf` sobre arquivo existente dá
        rc=0 e o conteúdo do pacote vence; `-xkzf` dá rc=2 e mantém o antigo.
        """
        import subprocess as sp

        texto = BUILD_ISO.read_text(encoding="utf-8")
        self.assertNotIn(
            "tar -xkzf",
            texto,
            "o `k` (--keep-old-files) voltou: a extração vai morrer na 1a colisão",
        )
        # A falha da extração tem de ser tratada, não engolida.
        self.assertIn('if ! tar -xzf "${_UPD_TAR}"', texto)

        # Comportamento, não só texto: prova a semântica dos dois modos.
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            (raiz / "a.txt").write_text("antigo", encoding="utf-8")
            pacote = raiz / "p.tgz"
            origem = raiz / "origem"
            origem.mkdir()
            (origem / "a.txt").write_text("novo", encoding="utf-8")
            sp.run(["tar", "-czf", str(pacote), "-C", str(origem), "."], check=True)
            (raiz / "a.txt").write_text("antigo", encoding="utf-8")

            com_k = sp.run(
                ["tar", "-xzkf", str(pacote), "-C", str(raiz)],
                capture_output=True, text=True,
            )
            self.assertNotEqual(com_k.returncode, 0, "`-k` deveria recusar")
            self.assertEqual((raiz / "a.txt").read_text(encoding="utf-8"), "antigo")

            sem_k = sp.run(
                ["tar", "-xzf", str(pacote), "-C", str(raiz)],
                capture_output=True, text=True,
            )
            self.assertEqual(sem_k.returncode, 0, sem_k.stderr)
            self.assertEqual((raiz / "a.txt").read_text(encoding="utf-8"), "novo")

    def test_build_iso_refuses_to_rename_the_legacy_rom(self):
        """O shell e o Python têm de dar o MESMO veredito sobre a ROM antiga.

        Achado do verifier, e era real: `prebuilt_rom` (Python) ignora de
        propósito a `reims-vgpu-gop.rom`, porque instalá-la renomeada faria o
        QEMU carregar o firmware errado em silêncio. Mas o `build-iso.sh` caía
        num fallback que a encontrava pelo `find` e a instalava **sempre com o
        nome de destino `ReimsVgpu.rom`** — exatamente o "renomeada" proibido.
        Os dois caminhos discordavam na mesma árvore.

        Não basta exigir que o texto não mencione a GOP: o que precisa ser
        verdade é que ela nunca vire o nome de destino.

        Este teste EXECUTA o bloco da ROM do script contra uma árvore que só
        tem a GOP antiga e mede o efeito: `REIMS_ROM` tem de sair vazio.

        A primeira versão deste teste fazia `assertNotIn` da linha literal do
        achado C e o verifier derrubou com uma medição: ela pegava **exatamente**
        aquela forma textual e mais nada. Reintroduzir o furo com outro nome de
        variável, ou com `find` de aspas duplas, ou sem o `-type f`, passava com
        a suíte verde — o furo voltava inteiro. Teste de texto guarda contra
        repetir o mesmo diff, não contra o erro voltar; e a distância entre o
        que o docstring prometia ("o par `find`/`install`") e o que o código
        fazia (uma palavra exata) era justamente o que o mutante explorava.
        Aqui o que se mede é o valor que o `install` usaria como origem.

        ESTE é o teste que pega o fallback do achado C quando ele volta para
        dentro do `if [ -z "${REIMS_ROM}" ]` — o único lugar onde ele pode agir.
        O teste irmão (`..._install_block_writes_the_new_rom_...`) roda a árvore
        COM a ROM nova, então nele o `if` não entra e o fallback nunca executa;
        ele tem de pegar o outro ramo. Medi os dois: com o fallback dentro do
        `if`, este cai e o irmão passa; com ele fora, os dois caem. Nenhum
        sozinho cobre o caminho — juntos, cobrem.
        """
        # Recorta o bloco da ROM por marcadores de texto e roda em `bash`, com
        # uma árvore sintética que só traz a GOP antiga — o formato das releases
        # v1.0.0/v1.0.1.
        texto = BUILD_ISO.read_text(encoding="utf-8")
        inicio = texto.index('REIMS_ROM="$(find')
        fim = texto.index("# O QEMU resolve", inicio)
        bloco = texto[inicio:fim]
        self.assertIn("install", texto[fim : fim + 4000], "o bloco mudou de lugar")

        raiz = self._arvore_so_com_gop()
        destino = raiz / "crates" / "reims-vgpu-efi" / "out"
        script = raiz / "trecho.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'REIMS_DEST="{destino.parent.parent.parent}"\n'
            'REIMS_ROM=""\n'
            # As cores vêm do script, não inventadas aqui: o bloco as usa no
            # aviso e sem elas o `set -u` derruba o trecho por motivo errado.
            'RED=""; YELLOW=""; GREEN=""; BLUE=""; CYAN=""; NC=""\n'
            'REIMS_RELEASE="v1.0.1"\n'
            f"{bloco}\n"
            'printf "ROM=%s\\n" "${REIMS_ROM}"\n',
            encoding="utf-8",
        )
        saida = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, check=False
        )
        self.assertEqual(saida.returncode, 0, saida.stderr)
        # O efeito: a GOP antiga NÃO pode virar a origem da instalação. Se ela
        # aparecer aqui, o `install` a renomeia para `ReimsVgpu.rom` e o QEMU
        # carrega firmware errado em silêncio.
        #
        # Olha só a linha `ROM=`, e não a saída toda: o aviso cita o nome da GOP
        # de propósito (é o texto que explica o usuário), e proibi-lo na saída
        # inteira transformaria a explicação correta em falha de teste.
        linha_rom = [
            l for l in saida.stdout.splitlines() if l.startswith("ROM=")
        ]
        self.assertEqual(len(linha_rom), 1, saida.stdout)
        self.assertEqual(
            linha_rom[0], "ROM=",
            f"a GOP antiga virou origem da instalação: {linha_rom[0]!r}",
        )
        # E o Python concorda: os dois caminhos na mesma árvore.
        self.assertIsNone(
            reims_build.prebuilt_rom(raiz),
            "prebuilt_rom passou a instalar a ROM da geração antiga",
        )
        # Precondição: a árvore sintética TEM a GOP. Sem isso o teste passaria
        # por não achar nada, e não por recusar — verde pelo motivo errado.
        self.assertTrue((destino / "reims-vgpu-gop.rom").is_file())

    def _arvore_so_com_gop(self) -> Path:
        """Uma árvore como a das releases v1.0.0/v1.0.1: só a GOP antiga."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        raiz = Path(tmp.name)
        destino = raiz / "crates" / "reims-vgpu-efi" / "out"
        destino.mkdir(parents=True)
        (destino / "reims-vgpu-gop.rom").write_bytes(b"gop antiga de 26112 B")
        return raiz

    def test_the_rom_block_installs_the_new_rom_when_it_exists(self):
        """O outro sentido do achado C: com a ROM NOVA, o bloco a escolhe mesmo.

        O teste acima prova que a GOP antiga é recusada. Falta o contraponto, e
        sem ele a "correção" mais simples — esvaziar `REIMS_ROM` sempre — passaria
        com a suíte verde enquanto nenhuma ISO jamais levaria a ROM do vGPU.
        Recusar tudo não é o comportamento correto; recusar SÓ o errado é.

        Roda o MESMO bloco extraído do script (699 bytes, medidos) contra uma
        árvore com `share/qemu/ReimsVgpu.rom`, o layout que o `release.sh`
        publica, e exige que a origem escolhida seja exatamente o arquivo novo.

        ATÉ ONDE ESTE TESTE VAI, medido com o fallback do achado C recolocado
        (o verifier e eu chegamos a números opostos aqui, e os dois estavam
        certos sobre mutações de NOME igual mas POSIÇÃO diferente):

          fallback na linha 348, imediatamente antes do `if [ -z ... ]`
            -> este teste FALHA. Ali `REIMS_GOP_ANTIGA` ainda não existe (é
               definida na 349, DENTRO do `if`), então `REIMS_ROM` fica vazio,
               o `if` ENTRA, e o fallback interno executa. É mutação alcançável.
          fallback antes do `find` da ROM nova (linha 347)
            -> este teste PASSA, e NENHUM OUTRO PODE PEGAR. O `find` sobrescreve
               a atribuição 1 linha depois. É MUTANTE EQUIVALENTE: não muda
               comportamento, então não há cobertura possível. Foi isso que o
               verifier mediu, e ele está certo nisso.

        Ou seja: "o teste positivo não pega o fallback" só vale para a posição
        equivalente, que não é um escape — é uma mutação sem efeito. Para a
        posição que REALMENTE muda comportamento, este teste cai.
        """
        texto = BUILD_ISO.read_text(encoding="utf-8")
        inicio = texto.index('REIMS_ROM="$(find')
        fim = texto.index("# O QEMU resolve", inicio)
        bloco = texto[inicio:fim]

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        raiz = Path(tmp.name)
        rom = raiz / "vendor" / "qemu" / "build" / "share" / "qemu" / "ReimsVgpu.rom"
        rom.parent.mkdir(parents=True)
        rom.write_bytes(b"ROM nova, 5632 B")

        script = raiz / "trecho.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'REIMS_DEST="{raiz}"\n'
            'REIMS_ROM=""\n'
            'RED=""; YELLOW=""; GREEN=""; BLUE=""; CYAN=""; NC=""\n'
            'REIMS_RELEASE="v1.0.2"\n'
            f"{bloco}\n"
            'printf "ROM=%s\\n" "${REIMS_ROM}"\n',
            encoding="utf-8",
        )
        saida = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, check=False
        )
        self.assertEqual(saida.returncode, 0, saida.stderr)
        linha = [l for l in saida.stdout.splitlines() if l.startswith("ROM=")]
        self.assertEqual(len(linha), 1, saida.stdout)
        self.assertEqual(linha[0], f"ROM={rom}", "a ROM nova não foi escolhida")
        # E o Python escolhe a MESMA: os dois caminhos concordam no positivo.
        self.assertEqual(
            reims_build.prebuilt_rom(raiz), rom,
            "prebuilt_rom e o shell discordam sobre qual ROM instalar",
        )

    def test_the_install_block_writes_the_new_rom_under_the_expected_name(self):
        """A ponta que faltava: o `install` de verdade, com as DUAS ROMs presentes.

        Os testes acima medem a ESCOLHA da origem. Nenhum exercitava o `install`
        — e é ele que produz o arquivo que o QEMU vai carregar. Um `install` que
        gravasse a origem com o nome de origem, ou que escolhesse a GOP na hora
        de copiar, passaria pelos dois.

        O caso é deliberadamente adversário: a árvore tem a ROM nova E a GOP
        antiga, que é a situação de uma release de transição. O destino tem de
        receber o conteúdo da NOVA, com o nome `ReimsVgpu.rom` — 26112 bytes de
        firmware antigo renomeado sob o nome novo é exatamente o que o achado C
        descrevia, e o QEMU aceita os dois sem reclamar.

        Limite MEDIDO deste teste, que fica registrado para não virar promessa
        maior do que ele cumpre: trocar o destino do `install` pelo nome da
        origem (MUT V) NÃO o derruba, e não é furo. O `find` procura só por
        `-name 'ReimsVgpu.rom'`, então a origem sempre chega com esse nome e o
        `basename` dá no mesmo. Quem defende a escolha é o `find`, não o
        `install`.

        O RECORTE É O BLOCO INTEIRO, e o achado H do verifier rendeu uma lição
        melhor do que a correção que ele propôs.

        A primeira versão fatiava dois trechos separados (a escolha e o
        `install`). O verifier mediu que um fallback restaurado fora da fatia não
        derrubava o teste, e reproduzi — mas a medição completa corrigiu os dois
        lados:

          fallback ANTES do `if`            -> este teste PEGA
                                               (o verifier previu que não)
          fallback DENTRO do `if`, depois
            de a variável ser definida      -> este teste NÃO pega, e NÃO PODE
                                               pegá-lo: com a ROM nova presente
                                               `REIMS_ROM` não é vazio, o
                                               `if [ -z ]` não entra e o
                                               fallback nunca executa.
                                               Quem o pega é o teste NEGATIVO,
                                               que roda a árvore só-GOP — e ali
                                               ele cai. Na suíte inteira:
                                               1 failed.
          fallback junto ao `install`       -> este teste PEGA

        Ou seja: nenhum dos dois testes pega tudo sozinho, porque cada um
        exercita UM ramo do `if`. Isso não é falha de nenhum deles, e sim o
        motivo de os dois existirem. Eu quase "consertei" isso perseguindo uma
        mutação que este teste não tem como ver — o conserto certo era o recorte
        contíguo (que pegou o Y1, o verifier previa o contrário) mais a divisão
        de trabalho entre os dois testes, que já cobria todo o caminho.
        """
        linhas = BUILD_ISO.read_text(encoding="utf-8").splitlines()
        i = next(
            n for n, l in enumerate(linhas)
            if l.strip().startswith('REIMS_ROM="$(find')
        )
        # Recorte CONTÍGUO: da escolha da ROM até o fim do `install`, sem pular
        # nenhuma linha. Fatiar em dois trechos deixou um fallback introduzido
        # entre eles invisível ao teste — e era o achado C inteiro de volta.
        k = next(
            n for n, l in enumerate(linhas)
            if n > i and l.strip().startswith('if [ -n "${REIMS_ROM}" ]')
        )
        fim = next(n for n, l in enumerate(linhas) if n > k and l.strip() == "fi")
        bloco = "\n".join(linhas[i : fim + 1])
        # Precondição do recorte: ele tem de conter os dois momentos. Se um
        # marcador sumir, o teste mediria um trecho qualquer e passaria.
        self.assertIn('REIMS_ROM="$(find', bloco)
        self.assertIn("install -m 0644", bloco)
        self.assertIn("ReimsVgpu.rom", bloco)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        raiz = Path(tmp.name)
        dest = raiz / "arvore"
        prefixo = raiz / "prefixo"
        rom_nova = dest / "vendor" / "qemu" / "build" / "share" / "qemu" / "ReimsVgpu.rom"
        rom_gop = dest / "crates" / "reims-vgpu-efi" / "out" / "reims-vgpu-gop.rom"
        rom_nova.parent.mkdir(parents=True)
        rom_gop.parent.mkdir(parents=True)
        (prefixo / "share" / "qemu").mkdir(parents=True)
        rom_nova.write_bytes(b"ROM-NOVA-5632")
        rom_gop.write_bytes(b"GOP-ANTIGA-26112")

        script = raiz / "t.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            # O recorte contíguo usa estas variáveis, todas definidas pelo
            # script fora dele. Sem elas o trecho morre de `set -u` por motivo
            # errado — falha que parece cobertura e não é.
            f'REIMS_DEST="{dest}"\n'
            f'REIMS_QEMU_PREFIX="{prefixo}"\n'
            f'ARCHISO_PROFILE="{raiz / "perfil"}"\n'
            f'REIMS_QEMU_BIN="{dest}/vendor/qemu/build/bin/qemu-system-x86_64"\n'
            'REIMS_ROM=""\n'
            'RED=""; YELLOW=""; GREEN=""; BLUE=""; CYAN=""; NC=""\n'
            'REIMS_RELEASE="v1.0.2"\n'
            f"{bloco}\n",
            encoding="utf-8",
        )
        saida = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, check=False
        )
        self.assertEqual(saida.returncode, 0, saida.stderr)
        # O destino não é o prefixo que eu passei: o próprio trecho o REDEFINE
        # para dentro do ARCHISO_PROFILE (é assim que o layout bin/ + share/qemu/
        # irmãos vai para a ISO). Asserir o prefixo de entrada mediria o lugar
        # errado — e foi o que a primeira versão contígua deste teste fez.
        instalado = (
            raiz / "perfil" / "airootfs" / "usr" / "local" / "lib"
            / "reims-vgpu-qemu" / "share" / "qemu" / "ReimsVgpu.rom"
        )
        self.assertTrue(instalado.is_file(), f"nada instalado. stdout={saida.stdout!r}")
        self.assertEqual(
            instalado.read_bytes(), b"ROM-NOVA-5632",
            "o destino recebeu firmware que não é a ROM nova — é o achado C "
            "renomeando a GOP antiga para o nome novo",
        )
        # E a GOP antiga não pode ter sido instalada em lugar nenhum da árvore.
        for achado in raiz.rglob("ReimsVgpu.rom"):
            self.assertEqual(
                achado.read_bytes(), b"ROM-NOVA-5632",
                f"{achado} recebeu a GOP antiga sob o nome novo",
            )

    def test_skip_push_does_not_publish_a_release(self):
        """`--skip-push` não pode publicar: o nome promete "não toque no remoto".

        Antes, a flag só pulava o `git push` e seguia para o `gh release
        create`, criando Releases reais. Num teste isso vira release pública de
        verdade e, pior, sequestra o `latest` — que é o que o appliance baixa
        por padrão.

        Hermético de propósito: monta um repositório e uma árvore de artefatos
        descartáveis, com um `gh` falso no PATH. Se `gh` for chamado, a
        sentinela aparece. Rodar contra o fork real dependeria do estado dele
        (tags, artefatos) e o teste passaria sem exercitar nada.

        ESTE É O TESTE QUE ENCONTROU O VAZAMENTO DE 481 MB, e por acidente.
        Ele rodava o `release.sh` de verdade e, a cada execução, deixava um
        `/tmp/tmp.XXXXXXXXXX` órfão com o binário do QEMU (286 MB) mais as OVMF
        (227 MB) copiados. O `du -sh /tmp/tmp.*` mostrou 481 MB por rodada; num
        tmpfs de 24 GB isso transbordava depois de ~40 execuções, e o sintoma
        eram falhas ALEATÓRIAS de escrita em testes sem relação nenhuma — o
        tipo de "instabilidade" que se atribui a flakiness e é vazamento.

        Causa: `release.sh` faz `DIST_TMP="$(mktemp -d)"` e limpava tudo num
        único `cleanup`, que o `--skip-push` desarmava com `trap - EXIT` para
        preservar o tarball. Desarmava junto a remoção do `DIST_TMP`.
        """
        import stat
        import subprocess as sp

        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            repo = raiz / "repo"
            (repo / "scripts").mkdir(parents=True)
            script = repo / "scripts" / "release.sh"
            script.write_text(
                self.SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
            )
            script.chmod(0o755)

            # Artefatos que o preflight exige, no layout novo.
            build = repo / "vendor" / "qemu" / "build"
            (build / "bin").mkdir(parents=True)
            qemu = build / "bin" / "qemu-system-x86_64"
            qemu.write_text("#!/bin/sh\necho qemu\n", encoding="utf-8")
            qemu.chmod(0o755)
            (build / "share" / "qemu").mkdir(parents=True)
            (build / "share" / "qemu" / "ReimsVgpu.rom").write_bytes(b"rom")
            (repo / "vendor" / "qemu" / "pc-bios").mkdir(parents=True)
            (repo / "vendor" / "qemu" / "pc-bios" / "bios-256k.bin").write_bytes(b"b")
            (repo / "vm").mkdir()
            (repo / "vm" / "boot-x86.sh").write_text("#!/bin/sh\n", encoding="utf-8")

            def git(*args: str) -> None:
                sp.run(
                    ["git", *args], cwd=str(repo), check=True,
                    capture_output=True, text=True,
                    env={**os.environ,
                         "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
                )

            git("init", "-q", "-b", "master")
            git("add", "-A")
            git("commit", "-q", "-m", ":new: base")
            git("tag", "v1.0.1")

            fakebin = raiz / "bin"
            fakebin.mkdir()
            sentinela = raiz / "gh-chamado"
            gh = fakebin / "gh"
            gh.write_text(f'#!/bin/sh\ntouch "{sentinela}"\nexit 0\n', encoding="utf-8")
            gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            env = dict(os.environ)
            env["PATH"] = f"{fakebin}:{env.get('PATH', '')}"
            # O `DIST_TMP` do release.sh é `mktemp -d`, que honra o TMPDIR.
            # Apontando-o para um dir nosso, o vazamento de 481 MB fica
            # observável DENTRO do teste em vez de poluir o `/tmp` do host — e
            # asserir sobre ele passa a ser possível. Antes disso o teste só
            # podia olhar o `/tmp` global, que outros testes usam.
            tmp_do_script = raiz / "tmpdir"
            tmp_do_script.mkdir()
            env["TMPDIR"] = str(tmp_do_script)
            proc = sp.run(
                ["bash", str(script), "--yes", "--skip-push", "--tag", "v1.0.2"],
                capture_output=True, text=True, env=env, cwd=str(repo), timeout=300,
            )
            combinado = proc.stdout + proc.stderr

            # Se o script não chegou ao ponto de decidir sobre o remoto, este
            # teste não está exercitando o que promete — falhar é o certo.
            self.assertIn(
                "--skip-push", combinado,
                "o script parou antes de tratar --skip-push; o teste não "
                f"exercitou o caminho de publicação:\n{combinado[-700:]}",
            )
            self.assertFalse(
                sentinela.exists(),
                f"--skip-push chamou o `gh` e publicaria um Release:\n{combinado[-700:]}",
            )
            self.assertNotIn("Publicando o Release", combinado)

            # A árvore de empacotamento tem de ter sido descartada. Era aqui
            # que ficavam os 481 MB: o `trap - EXIT` que preserva o tarball
            # desarmava também a remoção do `DIST_TMP`.
            self.assertEqual(
                [], sorted(tmp_do_script.iterdir()),
                "o release.sh deixou a árvore de empacotamento para trás — "
                "cada execução vaza ~481 MB (binário do QEMU + OVMF)",
            )
            # E o tarball, esse sim, continua preservado: é o que o
            # `--skip-push` promete ("Pacote: ... mantido neste diretório").
            self.assertTrue(
                (repo / "reims-vgpu-linux-x86_64.tar.gz").is_file(),
                "o --skip-push preservava o tarball para inspeção",
            )

    def test_publishing_path_still_calls_gh(self):
        """O contraponto: sem --skip-push o Release É publicado.

        Sem isto, um script que nunca publicasse nada passaria no teste acima.
        """
        import stat
        import subprocess as sp

        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            repo = raiz / "repo"
            (repo / "scripts").mkdir(parents=True)
            script = repo / "scripts" / "release.sh"
            script.write_text(self.SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
            script.chmod(0o755)
            build = repo / "vendor" / "qemu" / "build"
            (build / "bin").mkdir(parents=True)
            qemu = build / "bin" / "qemu-system-x86_64"
            qemu.write_text("#!/bin/sh\necho qemu\n", encoding="utf-8")
            qemu.chmod(0o755)
            (build / "share" / "qemu").mkdir(parents=True)
            (build / "share" / "qemu" / "ReimsVgpu.rom").write_bytes(b"rom")
            (repo / "vendor" / "qemu" / "pc-bios").mkdir(parents=True)
            (repo / "vendor" / "qemu" / "pc-bios" / "bios-256k.bin").write_bytes(b"b")
            (repo / "vm").mkdir()
            (repo / "vm" / "boot-x86.sh").write_text("#!/bin/sh\n", encoding="utf-8")

            def git(*args: str) -> None:
                sp.run(
                    ["git", *args], cwd=str(repo), check=True,
                    capture_output=True, text=True,
                    env={**os.environ,
                         "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
                )

            git("init", "-q", "-b", "master")
            git("add", "-A")
            git("commit", "-q", "-m", ":new: base")
            git("tag", "v1.0.1")

            fakebin = raiz / "bin"
            fakebin.mkdir()
            for nome in ("gh", "git"):
                if nome == "gh":
                    f = fakebin / nome
                    f.write_text('#!/bin/sh\nexit 0\n', encoding="utf-8")
                    f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            # `git push` precisa funcionar: um remotinho local descartável.
            sp.run(["git", "init", "-q", "--bare", str(raiz / "remote")], check=True,
                   capture_output=True)
            git("remote", "add", "origin", str(raiz / "remote"))

            env = dict(os.environ)
            env["PATH"] = f"{fakebin}:{env.get('PATH', '')}"
            proc = sp.run(
                ["bash", str(script), "--yes", "--tag", "v1.0.2"],
                capture_output=True, text=True, env=env, cwd=str(repo), timeout=300,
            )
            combinado = proc.stdout + proc.stderr
            self.assertIn("Publicando o Release", combinado)


class QemuPrefixLayoutTests(unittest.TestCase):
    """O binário e as ROMs têm de ficar em bin/ e share/qemu/ IRMÃOS.

    O QEMU resolve `romfile` relativo ao próprio executável, procurando em
    `<dir do binário>/../share/qemu/`. Isto foi medido com o binário real:
    ROM em `share/qemu/` ao lado de `bin/` carrega; a mesma ROM um nível
    abaixo faz o QEMU dizer "could not load PC BIOS". Como o sintoma de uma
    ROM não encontrada é tela preta — longe da causa — o layout fica travado
    por teste em cada produtor e consumidor.
    """

    RAIZ = Path(__file__).resolve().parents[1]
    BUILD_ISO = RAIZ / "appliance" / "build-iso.sh"
    RELEASE_SH = RAIZ / ".." / "reims-vgpu" / "scripts" / "release.sh"

    def test_build_iso_keeps_the_runtime_bundle_and_skips_duplicate_install(self):
        """O bundle de release é a única cópia do QEMU durante a montagem."""
        if not self.BUILD_ISO.is_file():
            self.skipTest("build-iso.sh ausente")
        texto = self.BUILD_ISO.read_text(encoding="utf-8")
        self.assertIn('"${REIMS_DEST}/vendor/qemu/build/bin/qemu-system-x86_64"', texto)
        self.assertIn("install_prebuilt()", texto)
        self.assertIn("--exclude='*/vm/ovmf/*'", texto)
        self.assertNotIn("REIMS_QEMU_PREFIX=", texto)
        self.assertNotIn("REIMS_QEMU_SHORTCUT=", texto)
        self.assertNotIn("/usr/local/lib/reims-vgpu-qemu", texto)

    def test_release_sh_packages_the_same_sibling_layout(self):
        if not self.RELEASE_SH.is_file():
            self.skipTest("fork reims-vgpu ausente neste host")
        texto = self.RELEASE_SH.read_text(encoding="utf-8")
        self.assertIn("vendor/qemu/build/bin/qemu-system-x86_64", texto)
        self.assertIn("vendor/qemu/build/share/qemu/ReimsVgpu.rom", texto)

    def test_release_sh_never_packages_the_legacy_gop_rom(self):
        """O produtor não pode publicar a ROM antiga renomeada.

        A `reims-vgpu-gop.rom` tem 26112 bytes e sha256 02cf046f…; a ROM que o
        QEMU carrega tem 5632 bytes e sha256 2165aa4c…. O QEMU aceita as duas
        como `romfile`, então publicar a errada daria uma release silenciosamente
        defeituosa. Ela pode aparecer no script só como diagnóstico, nunca como
        candidata a `ROM_BIN`.
        """
        if not self.RELEASE_SH.is_file():
            self.skipTest("fork reims-vgpu ausente neste host")
        texto = self.RELEASE_SH.read_text(encoding="utf-8")

        # O bloco que escolhe a ROM não pode listar a antiga como candidata.
        i = texto.index("ROM_BIN=\"\"")
        j = texto.index("done", i)
        bloco = texto[i:j]
        self.assertNotIn(
            "crates/reims-vgpu-efi/out/reims-vgpu-gop.rom", bloco,
            "a ROM GOP antiga não pode ser candidata a ROM_BIN",
        )
        # Se o script a mencionar, tem de ser num aviso que diga que não serve.
        if "reims-vgpu-gop.rom" in texto:
            self.assertIn("não é a", texto)

    def test_a_missing_pc_bios_is_an_error_not_a_warning(self):
        """Sem o pc-bios o QEMU nem inicia — avisar e seguir geraria ISO inútil."""
        if not self.BUILD_ISO.is_file():
            self.skipTest("build-iso.sh ausente")
        texto = self.BUILD_ISO.read_text(encoding="utf-8")
        self.assertIn('"${REIMS_DEST}/vendor/qemu/build/share/qemu/bios-256k.bin"', texto)
        self.assertIn('"${REIMS_DEST}/vendor/qemu/pc-bios/bios-256k.bin"', texto)
        # O preflight confere no bundle de origem; o runtime instala essas ROMs
        # junto com o QEMU no diretório usado por reims-ctl.
        i = texto.index("não trouxe bios-256k.bin no diretório de ROMs do QEMU")
        trecho = texto[max(0, i - 260):i + 400]
        self.assertIn("exit 1", trecho, "pc-bios ausente deveria abortar o build")
        self.assertNotIn("YELLOW", trecho, "pc-bios ausente não é aviso amarelo")

    def test_build_does_not_duplicate_qemu_outside_the_release_bundle(self):
        """O ISO guarda o bundle; install_prebuilt instala o QEMU no primeiro uso."""
        if not self.BUILD_ISO.is_file():
            self.skipTest("build-iso.sh ausente")
        texto = self.BUILD_ISO.read_text(encoding="utf-8")
        self.assertIn('"${REIMS_DEST}/vendor/qemu/build/bin/qemu-system-x86_64"', texto)
        self.assertNotIn('REIMS_QEMU_PREFIX=', texto)
        self.assertNotIn('REIMS_QEMU_SHORTCUT=', texto)
        self.assertNotIn('/usr/local/lib/reims-vgpu-qemu', texto)
        self.assertIn("install_prebuilt()", texto)

    def test_profiledef_does_not_require_local_reims_sources(self):
        """A ISO usa uma release pré-compilada, sem depender de fontes locais."""
        profile = self.RAIZ / "appliance" / "archiso" / "profiledef.sh"
        if not profile.is_file():
            self.skipTest("profiledef.sh ausente")
        text = profile.read_text(encoding="utf-8")
        self.assertNotRegex(
            text,
            r'^\s*\["/opt/reims-vgpu/(?:scripts|crates)/',
            "a ISO não pode exigir uma árvore de fontes do Reims",
        )


class BuildIsoFlagSemanticsTests(unittest.TestCase):
    """As flags do `build-iso.sh` não podem mentir sobre o que vão fazer.

    O padrão é pré-compilado; `--source` e `--from-source` pedem o modo fontes.
    Se `--source` fosse ignorado em silêncio, o usuário acharia que compilou
    quando na verdade baixou um binário de release.
    """

    SCRIPT = Path(__file__).resolve().parents[1] / "appliance" / "build-iso.sh"

    def setUp(self) -> None:
        if not self.SCRIPT.is_file():
            self.skipTest("build-iso.sh ausente")
        self.texto = self.SCRIPT.read_text(encoding="utf-8")

    def test_default_is_prebuilt(self):
        self.assertRegex(self.texto, r"(?m)^REIMS_PREBUILT=1$")

    def test_source_flag_turns_off_prebuilt(self):
        bloco = self.texto.split("-s|--source)", 1)
        self.assertEqual(len(bloco), 2, "--source desapareceu do parser")
        # O `--source` tem de desligar o pré-compilado no seu próprio bloco.
        self.assertIn("REIMS_PREBUILT=0", bloco[1].split(";;", 1)[0])

    def test_help_documents_every_mode(self):
        for termo in ("--release", "--from-source", "--update-sources", "--prebuilt"):
            self.assertIn(termo, self.texto, f"--help não documenta {termo}")


class ReleaseAssetContractTests(unittest.TestCase):
    """Os TRÊS lugares que falam do asset têm de concordar.

    Um layout divergente publica uma release que o appliance baixa e recusa —
    exatamente o tipo de falha que só aparece na hora de usar. Aqui o contrato
    é verificado estaticamente, sem rede.
    """

    FORK_SCRIPT = Path("/home/felipeab10/reims-vgpu/scripts/release.sh")
    WORKFLOW = (
        Path(__file__).resolve().parents[1] / ".." / "reims-vgpu"
        / ".github" / "workflows" / "release.yml"
    ).resolve()

    # (caminho empacotado, trecho que PROVA que ele é copiado para o pacote).
    # Exigir a cópia — e não só a menção do caminho — importa: um preflight que
    # apenas *checa* `vendor/qemu/pc-bios` continuaria casando a string mesmo
    # depois de o `cp` correspondente ser removido, e o teste passaria com o
    # asset incompleto (foi verificado por mutação).
    CAMINHOS_OBRIGATORIOS = (
        ("vendor/qemu/build/bin/qemu-system-x86_64", 'cp -a "${QEMU_BIN}" "${DIST_TMP}/vendor/qemu/build/bin/qemu-system-x86_64"'),
        ("vendor/qemu/build/share/qemu/ReimsVgpu.rom", 'cp -a "${ROM_BIN}" "${DIST_TMP}/vendor/qemu/build/share/qemu/ReimsVgpu.rom"'),
        ("vendor/qemu/pc-bios", 'cp -a vendor/qemu/pc-bios/. "${DIST_TMP}/vendor/qemu/build/share/qemu/"'),
        ("vm/ovmf", 'cp -a vm/ovmf/. "${DIST_TMP}/vm/ovmf/"'),
        ("vm/boot-x86.sh", 'cp -a vm/boot-x86.sh "${DIST_TMP}/vm/"'),
    )

    def setUp(self) -> None:
        if not self.FORK_SCRIPT.is_file():
            self.skipTest("fork reims-vgpu ausente neste host")

    def test_producers_agree_on_the_asset_name(self):
        self.assertIn(reims_release.RELEASE_ASSET, self.FORK_SCRIPT.read_text("utf-8"))
        if self.WORKFLOW.is_file():
            self.assertIn(
                reims_release.RELEASE_ASSET, self.WORKFLOW.read_text(encoding="utf-8")
            )

    def test_release_script_actually_copies_every_required_path(self):
        texto = self.FORK_SCRIPT.read_text(encoding="utf-8")
        for caminho, prova in self.CAMINHOS_OBRIGATORIOS:
            self.assertIn(prova, texto, f"release.sh não empacota {caminho}")

    def test_workflow_is_disabled_so_it_cannot_race_the_release_script(self):
        """O workflow precisa estar desativado, não só corrigido.

        Quando ele estava ativo, `push: tags: 'v*'` fazia o `release.sh` — que
        empurra a tag e só depois publica — acordar um segundo produtor do
        MESMO Release. Além disso ele chamava dois scripts que o commit
        98fe2a3b removeu (`crates/reims-vgpu-efi/...` e
        `scripts/qemu-build/qemu-build.sh`), então todo push de tag ficava
        vermelho. O caminho local é o produtor canônico.
        """
        pastas = Path(__file__).resolve().parents[1] / ".." / "reims-vgpu" / ".github" / "workflows"
        if not pastas.is_dir():
            self.skipTest("fork reims-vgpu ausente neste host")
        ativos = sorted(p.name for p in pastas.glob("*.yml")) + sorted(
            p.name for p in pastas.glob("*.yaml")
        )
        self.assertEqual(
            [], ativos, f"workflow ativo publicaria release em paralelo: {ativos}"
        )
        self.assertTrue(
            (pastas / "release.yml.disabled").is_file(),
            "o workflow deve continuar no repositório como referência, desativado",
        )

    def test_workflow_reference_warns_about_the_removed_tree(self):
        """A referência desativada precisa avisar por que não pode ser reativada.

        O arquivo mantém os comandos antigos de propósito (é histórico), então o
        que impede alguém de reativar uma armadilha é o cabeçalho explicando que
        aqueles scripts foram removidos pelo commit 98fe2a3b e que o workflow
        competiria com o `release.sh`.
        """
        caminho = (
            Path(__file__).resolve().parents[1] / ".." / "reims-vgpu"
            / ".github" / "workflows" / "release.yml.disabled"
        )
        if not caminho.is_file():
            self.skipTest("workflow de referência ausente neste host")
        texto = caminho.read_text(encoding="utf-8")
        self.assertIn("DESATIVADO", texto)
        self.assertIn("98fe2a3b", texto, "deve dizer qual commit removeu a árvore")
        self.assertIn("install-ctl.sh", texto, "deve apontar o construtor atual")
        self.assertIn("release.sh", texto, "deve avisar do conflito de produtores")

    def test_appliance_consumes_the_same_paths(self):
        """O build-iso.sh confere exatamente o binário que os produtores põem."""
        build_iso = (
            Path(__file__).resolve().parents[1] / "appliance" / "build-iso.sh"
        )
        texto = build_iso.read_text(encoding="utf-8")
        self.assertIn(reims_release.RELEASE_ASSET, texto)
        self.assertIn(
            "vendor/qemu/build/qemu-system-x86_64", texto,
            "build-iso.sh não valida o binário que a release traz",
        )

    # Dois recortes do release.sh, com o fim marcado por um comentário REAL do
    # arquivo (não por regex preguiçoso: um `.*?` casaria no primeiro `#`).
    _BLOCO_MOTOR_FIM = "# A ROM do vGPU, com o nome que o binário pede"
    _BLOCO_EMBALAGEM_FIM = "\n# OVMF: do repositório, se houver; senão do sistema."

    def _roda_o_empacotamento(self, tmp: Path) -> Path:
        """Executa o empacotamento do release.sh com fontes falsas.

        Texto não é contrato: `assertIn('cp -a ...')` aceita o `cp` comentado,
        dentro de um `if false`, ou com o arquivo indo para outro lugar — só
        olha a forma da string. Aqui as linhas REAIS de empacotamento rodam
        contra um motor e um QEMU de mentira, e o que se mede é a árvore que
        sobrou em disco, que é exatamente o que o `tar` publicaria.
        """
        texto = self.FORK_SCRIPT.read_text(encoding="utf-8")
        i = texto.index(self._BLOCO_MOTOR_FIM)
        j = texto.index(self._BLOCO_EMBALAGEM_FIM)
        # PARAR ANTES do OVMF de propósito: aquele trecho copia de
        # `vendor/qemu/pc-bios` e `vm/ovmf`, caminhos RELATIVOS do repositório do
        # fork, e num diretório de teste ele copiaria o OVMF do sistema para
        # dentro do teste. Até ali está tudo o que decide o pacote do motor.
        # (Cuidado que custou caro: um recorte que termine em `[[ -f vm/boot-x86.sh ]]`
        # termina com um teste FALSO, que sob `set -e` aborta o script inteiro.)
        k = texto.rindex('mkdir -p "${DIST_TMP}/vendor/qemu/build/bin"', 0, i)
        bloco = texto[k:j]

        qemu = tmp / "qemu-falso"
        qemu.write_bytes(b"\x7fELF qemu falso")
        motor = tmp / "reims-ctl-falso"
        motor.write_text("#!/bin/sh\necho motor falso\n", encoding="utf-8")
        # 0644 de propósito: é assim que o motor sai do `cargo install` no cache.
        # Com a origem 0755, o `cp -a` preserva o bit e o teste passaria mesmo
        # sem o `chmod 0755` do release.sh (medido: essa mutação escapava).
        motor.chmod(0o644)
        rom = tmp / "ReimsVgpu.rom"
        rom.write_bytes(b"\x55\xaa rom falsa")

        dist = tmp / "dist"
        script = tmp / "empacota.sh"
        script.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            # `say` e as cores vêm de antes no release.sh.
            'say() { :; }\n'
            'RED=""; YELLOW=""; NC=""; GREEN=""; CYAN=""; BLUE=""\n'
            f'DIST_TMP="{dist}"\n'
            f'QEMU_BIN="{qemu}"\n'
            f'CTL_BIN="{motor}"\n'
            f'ROM_BIN="{rom}"\n'
            f"{bloco}\n",
            encoding="utf-8",
        )
        r = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=120
        )
        self.assertEqual(
            0, r.returncode,
            f"o empacotamento do release.sh abortou:\n{r.stdout}\n{r.stderr}",
        )
        return dist

    def test_the_release_puts_the_motor_in_the_packaged_tree(self):
        """O `cp` do motor roda de verdade e o destino é o que o app procura.

        Medido por mutação: apagar o `mkdir -p "${DIST_TMP}/bin"` ou comentar o
        `cp` tem de reprovar aqui. Um `assertIn` sobre o texto do script não
        distingue nada disso.
        """
        with tempfile.TemporaryDirectory() as td:
            dist = self._roda_o_empacotamento(Path(td))

            rel = reims_build.PREBUILT_REIMS_CTL_RELS[0]
            destino = dist / rel
            self.assertTrue(
                destino.is_file(),
                f"o pacote não traz {rel}; tem: {sorted(p.name for p in dist.iterdir())}",
            )
            self.assertEqual(
                "#!/bin/sh\necho motor falso\n", destino.read_text(encoding="utf-8"),
                "o arquivo empacotado não é o motor",
            )
            self.assertTrue(
                os.access(destino, os.X_OK),
                f"{rel} saiu do pacote sem bit de execução — o `chmod 0755` do "
                "release.sh é o que garante isso, porque a origem vem 0644",
            )
            # A árvore do QEMU continua indo para o seu próprio lugar.
            self.assertTrue(
                (dist / "vendor/qemu/build/bin/qemu-system-x86_64").is_file()
            )
            self.assertTrue(
                (dist / "vendor/qemu/build/share/qemu/ReimsVgpu.rom").is_file()
            )
            # E o layout é o mesmo que `prebuilt_reims_ctl` aceita.
            self.assertEqual(reims_build.prebuilt_reims_ctl(dist), destino)

    def test_the_packaged_motor_is_not_a_copy_of_the_qemu(self):
        """Controle negativo: o motor empacotado tem de ser o MOTOR.

        Sem isto, um `cp` que apontasse para o binário do QEMU passaria no teste
        acima (o arquivo existiria e seria executável).
        """
        with tempfile.TemporaryDirectory() as td:
            dist = self._roda_o_empacotamento(Path(td))
            rel = reims_build.PREBUILT_REIMS_CTL_RELS[0]
            conteudo = (dist / rel).read_bytes()
            self.assertNotEqual(b"\x7fELF qemu falso", conteudo)
            self.assertIn(b"motor falso", conteudo)

    def test_the_release_keeps_the_motor_inside_the_tarball_sanity_list(self):
        """A sanidade é textual porque é uma conferência sobre o tarball.

        O que importa é o caminho conferido ser EXATAMENTE o que `install_prebuilt`
        procura — conferir `bin/reims-ctl-antigo` deixaria o pacote sair sem motor
        e ainda assim "passar".
        """
        texto = self.FORK_SCRIPT.read_text(encoding="utf-8")
        rel = reims_build.PREBUILT_REIMS_CTL_RELS[0]
        i = texto.index("for esperado in")
        j = texto.index("done", i)
        laco = texto[i:j]
        self.assertIn(f"'{rel}'", laco, "a sanidade do tarball não exige o motor")
        self.assertIn(
            "die ", laco,
            "a sanidade lista o motor mas não aborta quando ele falta",
        )

    def test_the_release_refuses_to_publish_without_the_motor(self):
        """Sem motor não há publicação: o preflight entra em MISSING e aborta.

        Medido pela EXECUÇÃO do trecho que decide, não pela presença da string:
        `MISSING+=` dentro de um comentário passaria num `assertIn`.
        """
        texto = self.FORK_SCRIPT.read_text(encoding="utf-8")
        i = texto.index('CTL_BIN=""')
        # Até DEPOIS do `fi` que fecha o `if [[ "${#MISSING[@]}" -gt 0 ]]`: o
        # `exit 1` está dentro dele, e cortar ali deixaria o `if` sem fechar.
        # Depois deste ponto o script publica de verdade.
        j = texto.index("exit 1", i)
        j = texto.index("fi\n", j) + len("fi\n")
        trecho = texto[i:j]
        # `MISSING`, não `die`: o script real acumula o que falta e sai com 1 —
        # é ISSO que tem de acontecer. Um `die` inventado aqui mediria o teste,
        # não o script. As demais variáveis vêm de antes dele no script.
        trecho = (
            'QEMU_BIN=""\nROM_BIN=""\nBUILD_PLATFORM="linux-x86_64"\n'
            'NEXT_TAG="v0.0.1"\n' + trecho
        )
        trabalho = Path(tempfile.mkdtemp(prefix="release-preflight-"))
        self.addCleanup(shutil.rmtree, trabalho, True)
        script = trabalho / "preflight.sh"
        script.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            "cd /tmp\n"
            # Mesmo `say` do release.sh: as mensagens do preflight saem por ele.
            # Com `say() { :; }` o teste veria stdout vazio e não teria como
            # conferir QUAL artefato faltou.
            'say() { printf "%s\\n" "$*"; }\n'
            # `reims-vgpu` falso sem os candidatos do motor.
            f'CACHE_DIR="{trabalho}/cache"\n'
            'RED=""; YELLOW=""; NC=""; GREEN=""; CYAN=""; BLUE=""\n'
            f'CTL_BIN_OVERRIDE=""\n'
            f"{trecho}\n"
            # O script real chega aqui só com tudo presente; o trecho acima já
            # saiu com 1 se faltar o motor.
            'exit 0\n',
            encoding="utf-8",
        )
        r = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=120
        )
        self.assertNotEqual(
            0, r.returncode,
            "o release.sh publicaria um pacote sem o motor: o preflight não abortou",
        )
        self.assertIn(
            "o motor reims-ctl", r.stdout + r.stderr,
            "abortou, mas não disse que o motor era o que faltava",
        )

    def test_the_iso_reports_a_bundle_that_arrived_without_the_motor(self):
        """O aviso em si é comportamental: ver BundleMotorCheckTests.

        Aqui só se confere que a ISO não avisa duas vezes nem por conta própria —
        o `build-iso.sh` delega para a lib, que é exercitada nos dois ramos.
        """
        texto = (
            Path(__file__).resolve().parents[1] / "appliance" / "build-iso.sh"
        ).read_text(encoding="utf-8")
        self.assertNotIn(
            'echo -e "${YELLOW}Aviso: o pacote', texto,
            "aviso duplicado inline no build-iso.sh (já existe na lib)",
        )


class BundleMotorCheckTests(unittest.TestCase):
    """A conferência do motor roda de verdade, nos dois ramos.

    O aviso vivia inline no `build-iso.sh`, que exige root e rede: nenhum teste
    o alcançava, e um teste que só conferisse o texto aceitava
    `if [ -f ... ] || true;` (medido por mutação — escapou). Por isso a
    conferência é uma função em `appliance/lib/bundle_checks.sh`, chamada aqui
    em subprocesso com uma árvore de mentira.
    """

    LIB = Path(__file__).resolve().parents[1] / "appliance" / "lib" / "bundle_checks.sh"

    def setUp(self) -> None:
        if not self.LIB.is_file():
            self.skipTest("appliance/lib/bundle_checks.sh ausente")
        self.tmp = Path(tempfile.mkdtemp(prefix="bundle-check-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _chama(self, destino: Path, release: str = "v1.1.0"):
        """Source a lib e chama a conferência. Devolve (código, saída)."""
        script = (
            f'GREEN=""; YELLOW=""; NC=""; . "{self.LIB}"; '
            f'checa_motor_do_pacote "{destino}" "{release}"'
        )
        r = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=60
        )
        return r.returncode, r.stdout + r.stderr

    def _pacote(self, *, com_motor: bool) -> Path:
        """Árvore no formato do tarball real (raiz `./`)."""
        destino = self.tmp / "opt" / "reims-vgpu"
        (destino / "vendor" / "qemu" / "build" / "bin").mkdir(parents=True)
        (destino / "vendor" / "qemu" / "build" / "bin" / "qemu-system-x86_64").write_bytes(b"\x7fELF")
        (destino / "vendor" / "qemu" / "build" / "share" / "qemu").mkdir(parents=True)
        (destino / "vendor" / "qemu" / "build" / "share" / "qemu" / "ReimsVgpu.rom").write_bytes(b"\x55\xaa")
        if com_motor:
            (destino / "bin").mkdir()
            # 0644 de propósito: é assim que o arquivo sai do tarball e entra na
            # ISO. Quem instala é que faz o chmod 0755; exigir o bit de execução
            # na conferência acusaria toda release legítima.
            (destino / "bin" / "reims-ctl").write_bytes(b"#!/bin/sh\n")
            os.chmod(destino / "bin" / "reims-ctl", 0o644)
        return destino

    def test_a_bundle_that_carries_the_motor_is_accepted(self):
        codigo, saida = self._chama(self._pacote(com_motor=True))
        self.assertEqual(0, codigo, f"recusou um pacote completo:\n{saida}")
        self.assertIn("presente", saida)
        self.assertNotIn("Aviso", saida, "avisou mesmo com o motor presente")

    def test_a_bundle_without_the_motor_is_refused_with_the_reason(self):
        codigo, saida = self._chama(self._pacote(com_motor=False), release="v1.1.0")
        self.assertNotEqual(0, codigo, "aceitou silenciosamente um pacote sem motor")
        self.assertIn("não traz bin/reims-ctl", saida)
        self.assertIn("v1.1.0", saida, "o aviso não diz QUAL pacote veio sem o motor")
        self.assertIn(
            "release.sh", saida, "o aviso não diz como corrigir (empacotar o motor)"
        )

    def test_the_qemu_binary_alone_does_not_count_as_the_motor(self):
        """Controle negativo: o resto do pacote presente não pode mascarar a falta."""
        destino = self._pacote(com_motor=False)
        self.assertTrue((destino / "vendor/qemu/build/bin/qemu-system-x86_64").is_file())
        codigo, _ = self._chama(destino)
        self.assertNotEqual(0, codigo)

    def test_a_directory_named_like_the_motor_does_not_count(self):
        destino = self._pacote(com_motor=False)
        (destino / "bin" / "reims-ctl").mkdir(parents=True)
        codigo, _ = self._chama(destino)
        self.assertNotEqual(0, codigo, "um diretório com o nome do motor foi aceito")

    def test_the_path_checked_is_the_path_the_appliance_installs(self):
        """O caminho não é literal solto: sai da lib e casa com o do Python."""
        script = f'. "{self.LIB}"; printf "%s" "$REIMS_CTL_BUNDLE_REL"'
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual("bin/reims-ctl", r.stdout)
        self.assertEqual(
            Path(r.stdout), reims_build.PREBUILT_REIMS_CTL_RELS[0],
            "a lib e install_prebuilt() discordam de onde o motor está",
        )

    def test_build_iso_calls_the_check_after_extracting(self):
        texto = BUILD_ISO.read_text(encoding="utf-8")
        self.assertIn('. "${SCRIPT_DIR}/lib/bundle_checks.sh"', texto,
                      "build-iso.sh não carrega a conferência")
        self.assertIn('checa_motor_do_pacote "${REIMS_DEST}" "${REIMS_RELEASE}"', texto,
                      "build-iso.sh extrai o pacote e não confere o motor")


class ReleaseUpdateInTuiTests(unittest.TestCase):
    """O item de menu "A" troca o reims-vgpu por uma release do fork.

    `VMTUI._menu_reims_release` é o equivalente pré-compilado do antigo
    "atualizar as fontes do reims-vgpu": baixa a última release e **instala** no
    prefixo que a VM executa. Falha de rede, API fora do ar ou release sem o
    asset apenas avisam; nada aqui pode levantar.
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import ui.tui as tui  # noqa: F401
        except ImportError as exc:  # rich/questionary ausentes
            raise unittest.SkipTest(f"UI indisponível: {exc}") from exc

    def setUp(self) -> None:
        import ui.tui as tui
        from unittest.mock import MagicMock

        self.tui = tui
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.reims = self.root / "reims-vgpu"
        _make_release_tree(self.reims)
        (self.reims / ".release").write_text("v1.0.1\n", encoding="utf-8")
        self.instalacoes: list[Path] = []

        self.inst = tui.VMTUI.__new__(tui.VMTUI)
        # `console` é um objeto real do rich: `Panel`/mensagens vão para o stdout
        # capturado pelo `_rodar`, o que valida o texto que o usuário vê.
        self.inst.console = tui.Console(force_terminal=False, width=200)
        import contextlib

        self.inst.console.status = lambda *a, **_k: contextlib.nullcontext()

    def _rodar(
        self, *, latest, update=None, respostas=(True, True), instala=True,
        releases=None, escolha=1, live=False,
    ):
        """Executa o item de menu com as releases dubladas e devolve o texto."""
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch

        def fake_list(**kwargs):
            if releases is not None:
                return releases
            release = latest(**kwargs)
            return [release] if release is not None else []

        class FakeRR:
            ReleaseError = reims_release.ReleaseError
            DEFAULT_REIMS_DIR = self.reims
            REIMS_REPO = "felipeab10/reims-vgpu"
            current_release = staticmethod(lambda _d: "v1.0.1")
            list_releases = staticmethod(fake_list)
            update_from_release = staticmethod(
                update
                if update is not None
                else (lambda **_k: self.fail("não deveria chamar update"))
            )
            vulkan_runtime_ok = staticmethod(lambda: (True, "libvulkan ok"))

        it = iter(respostas)

        def proxima(*_a, **_k):
            try:
                return next(it)
            except StopIteration:
                return True

        def fake_install(reims_dir):
            self.instalacoes.append(reims_dir)
            return instala

        buf = io.StringIO()
        with patch.dict("sys.modules", {"core.reims_release": FakeRR}), patch.object(
            self.tui, "find_reims_dir", return_value=self.reims
        ), patch.object(
            self.tui, "install_prebuilt", side_effect=fake_install
        ), patch.object(
            self.tui.Confirm, "ask", side_effect=proxima
        ), patch.object(
            self.tui.Prompt, "ask", return_value=str(escolha)
        ), patch.object(
            self.tui, "is_live_media", return_value=live
        ), redirect_stdout(buf):
            self.inst._menu_reims_release()
        return buf.getvalue()

    @staticmethod
    def _rel(tag):
        return reims_release.Release(tag=tag, name=tag, published_at="", asset_url="")

    def test_newer_release_is_installed_when_accepted(self):
        out = self._rodar(
            latest=lambda **_k: self._rel("v2.0.0"),
            update=lambda **_k: reims_release.UpdateResult(
                ok=True, changed=True, tag="v2.0.0", message="ok"
            ),
        )
        self.assertIn("v2.0.0", out)
        self.assertIn("alterada", out)

    def test_an_older_release_can_be_selected_for_rollback(self):
        received = []

        def update(**kwargs):
            received.append(kwargs["tag"])
            return reims_release.UpdateResult(
                ok=True, changed=True, tag=kwargs["tag"], message="ok"
            )

        out = self._rodar(
            latest=lambda **_k: self._rel("v2.0.0"),
            releases=[self._rel("v2.0.0"), self._rel("v1.0.0")],
            escolha=2,
            update=update,
        )
        self.assertEqual(received, ["v1.0.0"])
        self.assertEqual(self.instalacoes, [self.reims])
        self.assertIn("v1.0.0", out)
        self.assertIn("v2.0.0", out)

    def test_the_downloaded_release_is_installed_into_the_vm_prefix(self):
        """Baixar não basta: a árvore tem de ir para o prefixo que a VM executa.

        Este é o defeito que motivou o fluxo novo — `update_from_release` só
        escrevia no diretório do reims-vgpu, e a VM continuava na compilação
        antiga. Aqui a asserção é sobre a **chamada de instalação**, não sobre o
        texto: sem ela, "baixou e não instalou" passaria.
        """
        out = self._rodar(
            latest=lambda **_k: self._rel("v2.0.0"),
            update=lambda **_k: reims_release.UpdateResult(
                ok=True, changed=True, tag="v2.0.0", message="ok"
            ),
        )
        self.assertEqual(
            self.instalacoes,
            [self.reims],
            "a release baixada não foi instalada no prefixo da VM",
        )
        self.assertIn("pronto para a VM", out)
        self.assertIn("próxima vez", out)

    def test_installed_system_does_not_claim_the_update_is_temporary(self):
        out = self._rodar(
            latest=lambda **_k: self._rel("v2.0.0"),
            update=lambda **_k: reims_release.UpdateResult(
                ok=True, changed=True, tag="v2.0.0", message="ok"
            ),
            live=False,
        )
        self.assertNotIn("Vale para esta sessão", out)

    def test_live_iso_warns_that_the_update_will_not_persist(self):
        out = self._rodar(
            latest=lambda **_k: self._rel("v2.0.0"),
            update=lambda **_k: reims_release.UpdateResult(
                ok=True, changed=True, tag="v2.0.0", message="ok"
            ),
            live=True,
        )
        self.assertIn("Vale para esta sessão", out)

    def test_a_failed_install_is_reported_instead_of_silently_kept(self):
        """`install_prebuilt` devolvendo False não pode virar "sucesso"."""
        out = self._rodar(
            latest=lambda **_k: self._rel("v2.0.0"),
            update=lambda **_k: reims_release.UpdateResult(
                ok=True, changed=True, tag="v2.0.0", message="ok"
            ),
            instala=False,
        )
        self.assertIn("instalação no prefixo da VM falhou", out)
        self.assertNotIn("pronto para a VM", out)

    def test_nothing_is_installed_when_the_user_declines_the_download(self):
        """Contraprova: recusar o download não pode instalar nada.

        Sem isto, um "instale sempre" passaria nos testes positivos.
        """
        out = self._rodar(latest=lambda **_k: self._rel("v2.0.0"), respostas=(True, False))
        self.assertIn("Troca cancelada", out)
        self.assertEqual(self.instalacoes, [])

    def test_nothing_is_installed_when_the_download_fails(self):
        out = self._rodar(
            latest=lambda **_k: self._rel("v2.0.0"),
            update=lambda **_k: reims_release.UpdateResult(
                ok=False, changed=False, tag="v2.0.0", message="download quebrou"
            ),
        )
        self.assertIn("download quebrou", out)
        self.assertEqual(self.instalacoes, [])

    def test_nothing_is_installed_when_the_tag_is_already_current(self):
        out = self._rodar(latest=lambda **_k: self._rel("v1.0.1"))
        self.assertIn("já está selecionada", out)
        self.assertEqual(self.instalacoes, [])

    def test_same_tag_says_up_to_date_and_never_updates(self):
        """`update` levanta se for chamado: já em dia não pode baixar de novo."""
        out = self._rodar(latest=lambda **_k: self._rel("v1.0.1"))
        self.assertIn("já está selecionada", out)

    def test_network_failure_does_not_raise(self):
        def boom(**_k):
            raise reims_release.ReleaseError("sem rede")

        out = self._rodar(latest=boom)
        self.assertIn("sem rede", out)
        self.assertEqual(self.instalacoes, [])

    def test_update_failure_does_not_raise(self):
        out = self._rodar(
            latest=lambda **_k: self._rel("v2.0.0"),
            update=lambda **_k: reims_release.UpdateResult(
                ok=False, changed=False, tag="v2.0.0", message="download quebrou"
            ),
        )
        self.assertIn("download quebrou", out)

    def test_declining_the_download_keeps_current_binary(self):
        out = self._rodar(latest=lambda **_k: self._rel("v2.0.0"), respostas=(True, False))
        self.assertIn("Troca cancelada", out)

    def test_declining_the_check_never_hits_the_network(self):
        """Dizer não à consulta não pode chamar a listagem de releases."""
        out = self._rodar(
            latest=lambda **_k: self.fail("não deveria consultar"),
            respostas=(False,),
        )
        self.assertIn("Mantendo", out)

    def test_no_release_with_the_asset_is_reported(self):
        out = self._rodar(latest=lambda **_k: None)
        self.assertIn("Nenhuma release", out)


class ReleaseFlowDocumentationTests(unittest.TestCase):
    """O script de release do fork é documentado para quem for usá-lo."""

    def test_readme_mentions_the_release_workflow(self):
        readme = Path(__file__).resolve().parents[1] / "README.md"
        text = readme.read_text(encoding="utf-8")
        for termo in ("--release", "--from-source", "--update-sources"):
            self.assertIn(termo, text, f"README não documenta {termo}")


class ReimsCtlResolutionOrderTests(unittest.TestCase):
    """O motor que a release instalou tem de vencer um `reims-ctl` do PATH.

    `reims_ctl_bin()` começava por `shutil.which("reims-ctl")`. Bastava existir
    um motor ANTIGO em /usr/local/bin para o appliance ignorar o que a release
    acabara de instalar em `~/.cargo/bin` e subir a VM com o antigo — o oposto
    do que "Iniciar VM deve usar a compilação atual" promete. O motor do PACOTE
    é o que casa com o QEMU do MESMO pacote, então ele tem de ganhar.

    Medido antes da correção: com um motor velho no PATH e o novo já instalado,
    a resolução devolvia o velho.
    """

    RAIZ = Path(__file__).resolve().parents[1]

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="reims-ctl-order-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _resolve(self, *, com_instalado: bool, com_path: bool) -> Optional[str]:
        """Roda a resolução num HOME e PATH isolados, em subprocesso.

        Isolar é obrigatório: a `HOME` do processo de teste tem o motor REAL do
        host em `~/.cargo/bin`, e ele venceria os dois candidatos que o caso
        quer distinguir.
        """
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        if com_instalado:
            inst = home / ".cargo" / "bin" / "reims-ctl"
            inst.parent.mkdir(parents=True, exist_ok=True)
            inst.write_text("#!/bin/sh\necho instalado\n")
            inst.chmod(0o755)
        path_dir = self.tmp / "path"
        path_dir.mkdir(exist_ok=True)
        if com_path:
            velho = path_dir / "reims-ctl"
            velho.write_text("#!/bin/sh\necho velho\n")
            velho.chmod(0o755)
        script = (
            "import sys; sys.path.insert(0, " + repr(str(self.RAIZ)) + ");"
            "from core.reims_ctl import reims_ctl_bin;"
            "print(reims_ctl_bin() or '')"
        )
        r = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=120,
            env={
                "HOME": str(home),
                "PATH": f"{path_dir}:/usr/bin:/bin",
                "PYTHONPATH": str(self.RAIZ),
                # Sem isto o `which` acharia o motor real do host.
                "XDG_DATA_HOME": str(home / ".local/share"),
            },
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip() or None

    def test_the_installed_motor_wins_over_one_in_the_path(self):
        achado = self._resolve(com_instalado=True, com_path=True)
        self.assertIsNotNone(achado)
        self.assertEqual(Path(achado).parent, self.tmp / "home/.cargo/bin", (
            "com um motor ANTIGO no PATH e o do pacote em ~/.cargo/bin, a "
            f"resolução tem de devolver o do pacote; devolveu {achado!r}"
        ))

    def test_a_motor_in_the_path_is_still_used_as_a_fallback(self):
        """Contraprova: quem instalou por fora não pode ficar sem motor."""
        achado = self._resolve(com_instalado=False, com_path=True)
        self.assertIsNotNone(achado)
        self.assertEqual(Path(achado).parent, self.tmp / "path")

    def test_without_any_motor_the_result_is_none(self):
        self.assertIsNone(self._resolve(com_instalado=False, com_path=False))

    def test_unreadable_root_candidate_does_not_explode(self):
        """`is_file()` sob /root LEVANTA para quem não é root.

        O candidato `/root/.cargo/bin/reims-ctl` é conferido em toda resolução.
        Sem tratar o OSError, um usuário comum recebia `PermissionError` no
        lugar do caminho do PATH — um erro de travessia de diretório virando
        falha de resolução do motor.

        Roda num HOME isolado E com o `which` neutralizado, senão o motor real
        deste host (em `~/.cargo/bin`) apareceria e o caso não seria medido.
        """
        home = self.tmp / "home-vazio"
        home.mkdir(exist_ok=True)
        vazio = self.tmp / "path-vazio"
        vazio.mkdir(exist_ok=True)
        script = (
            "import sys; sys.path.insert(0, " + repr(str(self.RAIZ)) + ");"
            "from pathlib import Path;"
            "real = Path.is_file;"
            "Path.is_file = lambda self, *a, **k: "
            "  (_ for _ in ()).throw(PermissionError(13, 'Permission denied', str(self)))"
            "  if str(self).startswith('/root/') else real(self, *a, **k);"
            "from core.reims_ctl import reims_ctl_bin;"
            "print(reims_ctl_bin() or '')"
        )
        r = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=120,
            env={
                "HOME": str(home),
                "PATH": str(vazio),
                "PYTHONPATH": str(self.RAIZ),
                "XDG_DATA_HOME": str(home / ".local/share"),
            },
        )
        self.assertEqual(
            r.returncode, 0,
            "o acesso negado tem de virar fallback, não exceção: " + r.stderr,
        )
        self.assertEqual(r.stdout.strip(), "")


class AArvorePreCompiladaEhEncontradaTests(unittest.TestCase):
    """`find_reims_dir` tem de enxergar o payload que a própria ISO instala.

    O defeito medido numa ISO real: o appliance gravava o payload da release em
    `/opt/reims-vgpu` — `.release`, `bin/reims-ctl` e `vendor/qemu/build/` — e
    `find_reims_dir()` devolvia `None`, porque exigia um script de COMPILAÇÃO
    (`scripts/install-ctl.sh` ou `scripts/qemu-build/qemu-build.sh`) que uma
    release binária não tem e não deve ter.

    O resultado na tela: "A VM não foi iniciada: o reims-vgpu está
    indisponível / Motivo: sem diretório do reims-vgpu" — enquanto o menu
    "Atualizar reims-vgpu", na mesma sessão, dizia "instalado: v1.2.0". Os dois
    discordavam porque só o menu tinha fallback para `/opt/reims-vgpu`.

    A suíte estava verde com a VM inutilizável porque o único teste que tocava
    nisto **mockava** `find_reims_dir`; ninguém exercitava a função real contra
    o layout real. Estes testes exercitam a função.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        # `find_reims_dir` sem argumento varre REIMS_SEARCH_PATHS e o ambiente
        # do host; isolar os dois é o que torna a asserção sobre `None`
        # significativa (senão ela passaria por achar o checkout do
        # desenvolvedor em /home/felipeab10/reims-vgpu).
        self.addCleanup(os.environ.pop, "REIMS_SRC", None)
        self.addCleanup(os.environ.pop, "REIMS_VGPU_DIR", None)
        os.environ.pop("REIMS_SRC", None)
        os.environ.pop("REIMS_VGPU_DIR", None)
        self._paths_originais = reims_build.REIMS_SEARCH_PATHS
        reims_build.REIMS_SEARCH_PATHS = (str(self.base / "inexistente"),)
        self.addCleanup(setattr, reims_build, "REIMS_SEARCH_PATHS",
                        self._paths_originais)

    def _arvore_pre_compilada(self, release: str = "v1.2.0") -> Path:
        """O layout EXATO que a ISO instala em /opt/reims-vgpu."""
        arvore = self.base / "reims-vgpu"
        _make_release_tree(arvore)
        (arvore / "bin").mkdir(parents=True, exist_ok=True)
        motor = arvore / "bin" / "reims-ctl"
        motor.write_bytes(b"#!/bin/sh\necho reims-ctl 0.1.0\n")
        motor.chmod(0o755)
        (arvore / ".release").write_text(f"{release}\n", encoding="utf-8")
        (arvore / "SOURCE-PROVENANCE.txt").write_text(
            f"mode=prebuilt\nrelease={release}\n", encoding="utf-8"
        )
        return arvore

    def test_the_prebuilt_payload_is_recognized(self):
        """O ponto exato do defeito: sem script de build, ainda é válido."""
        arvore = self._arvore_pre_compilada()
        self.assertFalse(
            (arvore / reims_build.INSTALL_CTL_REL).exists(),
            "a release não traz scripts de compilação; se trouxesse, o teste "
            "não estaria cobrindo o defeito",
        )
        self.assertTrue(
            reims_build.is_valid_reims_tree(arvore),
            "payload da release recusado como árvore do reims-vgpu: é este "
            "`False` que produz 'sem diretório do reims-vgpu' na tela",
        )
        self.assertEqual(reims_build.find_reims_dir(arvore), arvore)

    def test_it_is_found_by_the_search_paths_too(self):
        """Não basta aceitar o caminho explícito: a varredura tem de achá-lo."""
        arvore = self._arvore_pre_compilada()
        reims_build.REIMS_SEARCH_PATHS = (str(arvore),)
        self.assertEqual(
            reims_build.find_reims_dir(), arvore,
            "a varredura padrão não encontra uma instalação pré-compilada — "
            "que é o caso de TODO usuário da ISO pronta",
        )

    def test_a_checkout_de_fontes_continua_valendo(self):
        """A correção não pode trocar um caso pelo outro."""
        for rel in (reims_build.INSTALL_CTL_REL, reims_build.QEMU_BUILD_LEGACY_REL):
            with self.subTest(marcador=str(rel)):
                raiz = self.base / f"fontes-{rel.parent.name}"
                (raiz / rel).parent.mkdir(parents=True, exist_ok=True)
                (raiz / rel).write_text("#!/bin/sh\n", encoding="utf-8")
                self.assertTrue(reims_build.is_valid_reims_tree(raiz))
                self.assertEqual(reims_build.find_reims_dir(raiz), raiz)

    def test_a_diretorio_vazio_nao_e_arvore(self):
        """O critério não pode ficar tão frouxo que aceite qualquer diretório."""
        for nome in ("vazio", "so-release", "so-motor", "so-qemu"):
            with self.subTest(caso=nome):
                arvore = self.base / nome
                arvore.mkdir(parents=True, exist_ok=True)
                if nome == "so-release":
                    (arvore / ".release").write_text("v1.2.0\n", encoding="utf-8")
                elif nome == "so-motor":
                    (arvore / "bin").mkdir(exist_ok=True)
                    (arvore / "bin" / "reims-ctl").write_bytes(b"x")
                elif nome == "so-qemu":
                    q = arvore / "vendor" / "qemu" / "build" / "bin"
                    q.mkdir(parents=True, exist_ok=True)
                    (q / "qemu-system-x86_64").write_bytes(b"x")
                self.assertFalse(
                    reims_build.is_valid_reims_tree(arvore),
                    f"{nome}: diretório incompleto não pode passar por "
                    "instalação do reims-vgpu",
                )

    def test_the_two_screens_agree_about_an_installed_release(self):
        """Lançamento e menu têm de responder a MESMA coisa sobre o payload.

        Este é o teste que faltava, e a asserção é a IGUALDADE — porque o
        defeito não estava em nenhum dos dois lados isoladamente: o menu
        aplicava fallback para `DEFAULT_REIMS_DIR` e o lançamento não, então
        cada tela dava uma resposta diferente para o mesmo disco.
        """
        arvore = self._arvore_pre_compilada()
        reims_build.REIMS_SEARCH_PATHS = (str(arvore),)

        achado = reims_build.find_reims_dir()
        self.assertIsNotNone(
            achado, "o menu diria 'instalado' e o lançamento 'sem diretório'"
        )

        # O que cada tela faz com o resultado, conforme ui/tui.py.
        do_lancamento: Optional[str] = str(achado) if achado else None
        do_menu = str(achado) if achado else str(reims_release.DEFAULT_REIMS_DIR)
        self.assertEqual(
            do_lancamento, do_menu,
            "lançamento e menu discordam sobre a instalação: era exatamente "
            "isto que produzia 'sem diretório do reims-vgpu' com o menu "
            "dizendo 'já está na versão mais recente'",
        )

    def test_the_release_marker_is_readable_from_the_payload(self):
        """A versão que o menu exibe vem do `.release` gravado no payload."""
        arvore = self._arvore_pre_compilada("v1.2.0")
        self.assertTrue(reims_build.is_valid_reims_tree(arvore))
        self.assertEqual(reims_release.current_release(arvore), "v1.2.0")


if __name__ == "__main__":
    unittest.main()
