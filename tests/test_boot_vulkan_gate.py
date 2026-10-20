"""O gate de Vulkan do boot honra o MESMO escape do `core/gpu.py`.

Defeito medido numa ISO real: a tela "BLOQUEIO CRÍTICO: GPU / HARDWARE
INCOMPATÍVEL" aparecia num guest sem GPU 1.2+ e desligava a máquina, sem
oferecer saída nenhuma. O `VHACK_SKIP_VULKAN_CHECK=1` já existia e já era
respeitado por `core/gpu.check_vulkan_host_readiness()` — e o
`appliance/archiso/.../profile.d/vhackintosh.sh` já o usava no caminho
`vhack_build_test=1`. Só o BOOT NORMAL não consultava a variável: chamava
`vhackintosh check-vulkan` direto e, no erro, entrava no `while true` de
desligar.

Ou seja, a variável de escape do projeto existia, funcionava no
`core/gpu.py` e no modo de build automatizado, e mesmo assim não destravava
o boot. Nada disso tinha teste: nenhum arquivo em `tests/` mencionava
`profile.d/vhackintosh.sh`, e foi assim que a divergência passou.

Estes testes medem o COMPORTAMENTO do script (sourceando só as funções, sem
o menu interativo) e não o texto dele: um `assertIn` no nome da variável
aceitaria tanto `if vhack_vulkan_check_skipped; then` (certo) quanto
`if false; then` com a variável citada num comentário (errado).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PROFILE = (
    RAIZ / "appliance" / "archiso" / "airootfs" / "etc" / "profile.d" / "vhackintosh.sh"
)
GRUB_CFG = RAIZ / "appliance" / "archiso" / "grub" / "grub.cfg"
LOOPBACK_CFG = RAIZ / "appliance" / "archiso" / "grub" / "loopback.cfg"
CMD_DO_ESCAPE = "vhack_skip_vulkan=1"

# A variável vem do módulo de produção, e não é redigitada aqui: se ela for
# renomeada num lado só, o teste acusa em vez de concordar sozinho.
sys.path.insert(0, str(RAIZ))
from core.gpu import (  # noqa: E402
    VULKAN_SKIP_CMDLINE,
    VULKAN_SKIP_ENV,
    vulkan_check_skipped,
)


def _so_as_funcoes(destino: Path, cmdline: Path | None = None) -> Path:
    """Extrai o arquivo até o começo do menu interativo.

    Sourcear o profile inteiro abriria o menu (e, num host sem Vulkan,
    desligaria a máquina de verdade). O menu começa no `if` que testa a tty;
    tudo antes disso é definição de função e os hooks de cmdline.

    `cmdline` redireciona APENAS a leitura de /proc/cmdline para um arquivo de
    mentira — é a única forma de exercitar a via do parâmetro de kernel sem
    reiniciar numa máquina com o cmdline trocado. A troca é feita na string do
    caminho; todo o resto do `grep` (a expressão regular, o `=1`) continua
    sendo o código real do arquivo, que é o que precisa ser testado.
    """
    linhas = PROFILE.read_text(encoding="utf-8").splitlines(keepends=True)
    corte = len(linhas)
    for i, linha in enumerate(linhas):
        if linha.startswith('if [ "$(tty)" = "/dev/tty1" ]') and "vhack_build_test=1" in linha:
            corte = i
            break
    texto = "".join(linhas[:corte])
    if cmdline is not None:
        self_antes = texto.count("/proc/cmdline")
        texto = texto.replace("/proc/cmdline", str(cmdline))
        # Se o arquivo deixar de ler o cmdline, o teste falha aqui em vez de
        # medir um helper que nunca leu nada.
        assert self_antes >= 1, "o profile não lê mais /proc/cmdline"
    destino.write_text(texto, encoding="utf-8")
    return destino


class VulkanSkipFlagTests(unittest.TestCase):
    """`vhack_vulkan_check_skipped` decide igual ao `core/gpu.py`."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="boot-vulkan-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.funcs = _so_as_funcoes(self.tmp / "funcs.sh")

    def _shell(self, valor: str | None) -> bool:
        """Pergunta ao helper do shell se ele manda pular."""
        env = dict(os.environ)
        if valor is None:
            env.pop("VHACK_SKIP_VULKAN_CHECK", None)
        else:
            env["VHACK_SKIP_VULKAN_CHECK"] = valor
        r = subprocess.run(
            ["bash", "-c",
             f'. "{self.funcs}"; vhack_vulkan_check_skipped && echo SIM || echo NAO'],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip() == "SIM"

    def _python(self, valor: str | None) -> bool:
        """A MESMA pergunta para a implementação de referência."""
        env = dict(os.environ)
        if valor is None:
            env.pop("VHACK_SKIP_VULKAN_CHECK", None)
        else:
            env["VHACK_SKIP_VULKAN_CHECK"] = valor
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]);"
             "from core.gpu import vulkan_check_skipped as v;"
             "print('SIM' if v() else 'NAO')", str(RAIZ)],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip() == "SIM"

    def test_the_shell_and_python_agree_on_every_accepted_value(self):
        """Contrato cruzado: as duas implementações não podem divergir.

        O appliance tem DUAS cópias da política (o profile do shell e o
        `core/gpu.py` embutido na ISO). Se elas discordarem, o boot e o menu
        tomam decisões diferentes para o mesmo ambiente.
        """
        for valor in ["1", "true", "TRUE", "True", "yes", "YES", "on", "ON",
                      "sim", "SIM", " yes ", "1 ", " 1", "Sim"]:
            with self.subTest(valor=valor):
                self.assertTrue(self._shell(valor), f"shell devia pular com {valor!r}")
                self.assertTrue(self._python(valor), f"python devia pular com {valor!r}")

    def test_values_that_do_not_mean_yes_keep_the_gate_closed(self):
        """Contraprova: o padrão continua sendo BLOQUEAR.

        Nada de "verdadeiro aproximado": só os valores da tabela liberam. Um
        valor desconhecido tem de manter o gate fechado — afrouxar aqui seria
        transformar um typo (`VHACK_SKIP_VULKAN_CHECK=si`) num boot sem
        verificação nenhuma, silenciosamente.
        """
        for valor in ["0", "", " ", "nao", "não", "false", "no", "off", "2",
                      "sim_nao", "true1", "disabled", "si", "y", "t"]:
            with self.subTest(valor=valor):
                self.assertFalse(self._shell(valor), f"shell não devia pular com {valor!r}")
                self.assertFalse(self._python(valor), f"python não devia pular com {valor!r}")

    def test_surrounding_spaces_are_ignored_on_both_sides(self):
        """`" 1 "` vale tanto quanto `"1"`, nos dois lados.

        O `core/gpu.py` faz `.strip()`; o shell precisa fazer o mesmo, senão
        `VHACK_SKIP_VULKAN_CHECK=" 1"` liberaria no menu e travaria no boot —
        a mesma divergência que este arquivo existe para impedir.
        """
        for valor in [" 1", "1 ", " 1 ", "\ttrue\t", " yes "]:
            with self.subTest(valor=valor):
                self.assertTrue(self._shell(valor), f"shell devia pular com {valor!r}")
                self.assertTrue(self._python(valor), f"python devia pular com {valor!r}")

    def test_unset_keeps_the_gate_closed(self):
        self.assertFalse(self._shell(None))
        self.assertFalse(self._python(None))


def _bloco_de_decisao() -> str:
    """Extrai do script o trecho que decide `VULKAN_OK`, sem redigitá-lo.

    Vai da linha `VULKAN_OUTPUT=""` (início do bloco 4, o gate de Vulkan) até
    a linha que abre o `if [ "$VULKAN_OK" -ne 1 ]`. O `poweroff` fica de fora
    de propósito: aqui só interessa se o caminho de bloqueio é alcançado.

    Se a âncora sumir (alguém reescreveu o gate), o teste FALHA em vez de
    passar em silêncio com um bloco vazio — um extrator que devolve "" faria
    `VULKAN_OK` ficar 1 e todo teste de bloqueio daria "não desligou".
    """
    linhas = PROFILE.read_text(encoding="utf-8").splitlines()
    inicio = fim = None
    for i, linha in enumerate(linhas):
        if inicio is None and linha.strip() == 'VULKAN_OUTPUT=""':
            inicio = i
            continue
        if inicio is not None and linha.strip() == 'if [ "$VULKAN_OK" -ne 1 ]; then':
            fim = i
            break
    if inicio is None or fim is None:
        raise AssertionError(
            "não achei o bloco de decisão de Vulkan em "
            f"{PROFILE} (inicio={inicio}, fim={fim}); o gate foi reescrito e "
            "este teste precisa acompanhar"
        )
    return "\n".join(linhas[inicio:fim])


class GrubMenuEntryTests(unittest.TestCase):
    """O menu do GRUB oferece a entrada de teste em VM.

    Sem ela, quem testa a ISO num guest sem GPU precisa editar a linha do
    GRUB a cada boot — ou seja, o escape existe mas dá trabalho. A entrada
    dedicada é o que torna o teste numa VM possível de verdade.

    Os testes leem os `menuentry` SEPARADOS, com o comentário descartado.
    Medido por mutação: a primeira versão procurava o parâmetro no arquivo
    inteiro e deixava escapar a mutação que tirava o parâmetro da ENTRADA —
    o comentário acima dela continuava satisfazendo o `assertIn`. Procurar
    texto solto num arquivo grande é o mesmo erro que o `assertIn` de
    `VHACK_SKIP_VULKAN_CHECK` que esta suíte já corrigiu.
    """

    @staticmethod
    def _entradas(cfg: Path) -> dict:
        """Mapeia id -> corpo do `menuentry`, sem comentários.

        Comentário que cita a entrada não é a entrada. Um `#` dentro da linha
        `linux` também não existe neste arquivo, então cortar por `#` é seguro
        e é o que impede o comentário de mascara o corpo.
        """
        entradas, atual, linhas = {}, None, []
        for bruta in cfg.read_text(encoding="utf-8").splitlines():
            linha = bruta.split("#", 1)[0] if not bruta.lstrip().startswith("#") else ""
            if linha.startswith("menuentry "):
                atual = re.search(r"--id '([^']+)'", linha)
                atual = atual.group(1) if atual else linha
                linhas = []
            elif atual is not None:
                if linha.strip() == "}":
                    entradas[atual] = "\n".join(linhas)
                    atual = None
                else:
                    linhas.append(linha)
        if atual is not None:
            entradas[atual] = "\n".join(linhas)
        return entradas

    def test_both_boot_templates_offer_the_vm_entry(self):
        for cfg in (GRUB_CFG, LOOPBACK_CFG):
            with self.subTest(cfg=cfg.name):
                entradas = self._entradas(cfg)
                self.assertIn("vhackintosh-vm-test", entradas)
                self.assertIn(
                    CMD_DO_ESCAPE, entradas["vhackintosh-vm-test"],
                    f"{cfg.name}: a entrada de VM não passa {CMD_DO_ESCAPE}",
                )

    def test_the_vm_entry_uses_the_parameter_the_profile_reads(self):
        """O parâmetro do GRUB é o MESMO que `vhack_vulkan_check_skipped` lê.

        Se um dos dois for renomeado, a entrada passa a bootar bloqueando e a
        pessoa fica presa numa tela que desliga a máquina — o defeito original.
        """
        texto = PROFILE.read_text(encoding="utf-8")
        helper = texto[texto.find("vhack_vulkan_check_skipped() {"):]
        chave = CMD_DO_ESCAPE.split("=")[0]
        self.assertIn(chave, helper, "o helper não lê o parâmetro que o GRUB usa")
        for cfg in (GRUB_CFG, LOOPBACK_CFG):
            with self.subTest(cfg=cfg.name):
                self.assertIn(chave, self._entradas(cfg)["vhackintosh-vm-test"])

    def test_the_vm_entry_is_not_the_default(self):
        """Testar em VM é opt-in; o boot normal fica com a checagem."""
        for cfg in (GRUB_CFG, LOOPBACK_CFG):
            with self.subTest(cfg=cfg.name):
                linhas = cfg.read_text(encoding="utf-8").splitlines()
                default = [l for l in linhas if l.startswith("set default=")]
                self.assertEqual(len(default), 1)
                self.assertNotIn("vm-test", default[0])

    def test_every_boot_entry_has_an_initrd(self):
        """Toda entrada que carrega kernel precisa do initrd.

        Vale para a entrada de VM TAMBÉM: ela é a exceção das outras
        asserções, e sem este teste um `initrd` faltando nela passaria
        despercebido — exatamente a mutação G7, que escapou.
        """
        for cfg in (GRUB_CFG, LOOPBACK_CFG):
            entradas = self._entradas(cfg)
            self.assertTrue(entradas, f"{cfg.name}: nenhuma entrada encontrada")
            for nome, corpo in entradas.items():
                if "/vmlinuz-linux" not in corpo:
                    continue
                with self.subTest(cfg=cfg.name, entrada=nome):
                    self.assertIn(
                        "initrd /%INSTALL_DIR%/boot/%ARCH%/initramfs-linux.img",
                        corpo,
                        f"{cfg.name}/{nome}: entrada de boot sem initrd",
                    )

    def test_the_original_entries_are_untouched(self):
        """Nenhuma entrada de BOOT fora da de VM pode pular a checagem.

        "Entrada de boot" = a que carrega kernel; `Reiniciar`/`Desligar` e o
        menu de firmware não carregam, então não se cobra initrd deles. O
        kiosk é o que roda no hardware real e a proteção vale para ele.
        """
        for cfg in (GRUB_CFG, LOOPBACK_CFG):
            entradas = self._entradas(cfg)
            for nome, corpo in entradas.items():
                if nome == "vhackintosh-vm-test" or "/vmlinuz-linux" not in corpo:
                    continue
                with self.subTest(cfg=cfg.name, entrada=nome):
                    self.assertNotIn(
                        CMD_DO_ESCAPE, corpo,
                        f"{cfg.name}/{nome}: entrada não-VM pula a checagem",
                    )

    def test_the_kiosk_entry_still_boots_with_the_check_on(self):
        """Contraprova: o kiosk REAL não foi afetado pela novidade."""
        kiosk = self._entradas(GRUB_CFG)["vhackintosh"]
        self.assertNotIn(CMD_DO_ESCAPE, kiosk)
        for marcador in ("%INSTALL_DIR%", "%ARCHISO_UUID%", "cow_spacesize"):
            with self.subTest(marcador=marcador):
                self.assertIn(marcador, kiosk)


class KernelParameterTests(unittest.TestCase):
    """`vhack_skip_vulkan=1` no cmdline também destrava o boot.

    É a via que o usuário REALMENTE consegue usar dentro da ISO: o gate roda
    no login, então não há como exportar a variável de ambiente antes dele.
    Com o parâmetro de kernel dá para acrescentar `vhack_skip_vulkan=1` com
    `e` no GRUB e bootar — mesmo mecanismo do `vhack_build_test=1`.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="boot-cmdline-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cmdline = self.tmp / "cmdline"
        self.funcs = _so_as_funcoes(self.tmp / "funcs.sh", cmdline=self.cmdline)

    def _pula(self, cmdline: str, *, env_valor: str | None = None) -> bool:
        self.cmdline.write_text(cmdline, encoding="utf-8")
        env = dict(os.environ)
        env.pop("VHACK_SKIP_VULKAN_CHECK", None)
        if env_valor is not None:
            env["VHACK_SKIP_VULKAN_CHECK"] = env_valor
        r = subprocess.run(
            ["bash", "-c",
             f'. "{self.funcs}"; vhack_vulkan_check_skipped && echo SIM || echo NAO'],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip() == "SIM"

    def test_the_parameter_skips_the_gate(self):
        self.assertTrue(self._pula("root=... quiet vhack_skip_vulkan=1"))

    def test_the_parameter_works_alone_at_start_and_end(self):
        """Como fica depois do `e` no GRUB: pode ir em qualquer posição."""
        self.assertTrue(self._pula("vhack_skip_vulkan=1 console=tty0"))
        self.assertTrue(self._pula("console=tty0 vhack_skip_vulkan=1"))

    def test_it_coexists_with_the_build_test_parameter(self):
        """Os dois parâmetros juntos, como no `verify-iso.sh --build-test`."""
        self.assertTrue(self._pula("vhack_build_test=1 vhack_skip_vulkan=1"))

    def test_other_values_of_the_parameter_do_not_skip(self):
        """Só `=1` liga. Um typo não pode destravar o boot em silêncio."""
        for cmdline in ["vhack_skip_vulkan=0", "vhack_skip_vulkan=true",
                        "vhack_skip_vulkan=", "vhack_skip_vulkan=10",
                        "vhack_skip_vulkan=1abc", "vhack_skip_vulkan",
                        "meu_vhack_skip_vulkan=1"]:
            with self.subTest(cmdline=cmdline):
                self.assertFalse(self._pula(cmdline))

    def test_an_empty_cmdline_does_not_skip(self):
        self.assertFalse(self._pula(""))
        self.assertFalse(self._pula("console=tty0 quiet"))

    def test_a_broken_parameter_does_not_break_the_gate_for_others(self):
        """Sem o parâmetro, o gate continua fechado (contraprova)."""
        self.assertFalse(self._pula("console=tty0"))
        self.assertTrue(self._pula("console=tty0", env_valor="1"),
                        "a variável de ambiente continua valendo")


class GrubToHelperEndToEndTests(unittest.TestCase):
    """A cadeia inteira: linha `linux` do GRUB -> helper real do profile.

    Os testes acima checam as peças separadas (o GRUB tem o parâmetro, o
    helper entende o parâmetro). Este faz a ligação: pega a linha `linux` de
    CADA entrada do template, resolve os `%PLACEHOLDER%` como o mkarchiso
    resolve, e roda o helper EXTRAÍDO DO ARQUIVO. Assim, se alguém renomear o
    parâmetro em um lado só, aqui aparece.

    É a mesma prova que rodei à mão contra a ISO 1.1.1 antes de escrever isto:
    hoje `vhackintosh` e `vhackintosh-verbose` bloqueiam e
    `vhackintosh-vm-test` destrava.
    """

    # Como o mkarchiso resolve o template (medido na ISO publicada: ela não
    # tem mais nenhum `%` cru).
    RESOLVE = {
        "%INSTALL_DIR%": "arch",
        "%ARCH%": "x86_64",
        "%ARCHISO_UUID%": "2026-10-18-10-02-56-00",
    }

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="e2e-grub-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cmdline = self.tmp / "cmdline"
        self.funcs = _so_as_funcoes(self.tmp / "funcs.sh", cmdline=self.cmdline)

    def _entradas(self, cfg: Path) -> dict:
        entradas, atual, linhas = {}, None, []
        for bruta in cfg.read_text(encoding="utf-8").splitlines():
            linha = "" if bruta.lstrip().startswith("#") else bruta.split("#", 1)[0]
            if linha.startswith("menuentry "):
                achou = re.search(r"--id '([^']+)'", linha)
                atual = achou.group(1) if achou else linha
                linhas = []
            elif atual is not None:
                if linha.strip() == "}":
                    entradas[atual] = "\n".join(linhas)
                    atual = None
                else:
                    linhas.append(linha)
        return entradas

    def _pula(self, cmdline: str) -> bool:
        self.cmdline.write_text(cmdline, encoding="utf-8")
        env = dict(os.environ)
        env.pop("VHACK_SKIP_VULKAN_CHECK", None)
        r = subprocess.run(
            ["bash", "-c",
             f'. "{self.funcs}"; vhack_vulkan_check_skipped && echo SIM || echo NAO'],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip() == "SIM"

    def test_only_the_vm_entry_reaches_the_menu_unblocked(self):
        """A entrada de VM destrava; as outras três continuam bloqueando."""
        esperado = {
            "vhackintosh": False,
            "vhackintosh-verbose": False,
            "vhackintosh-vm-test": True,
        }
        for arq in (GRUB_CFG, LOOPBACK_CFG):
            for nome, corpo in self._entradas(arq).items():
                achou = re.search(r"^    linux \S+ (.*)$", corpo, re.M)
                if achou is None:
                    continue  # Reiniciar/Desligar/firmware não carregam kernel
                with self.subTest(cfg=arq.name, entrada=nome):
                    self.assertIn(nome, esperado,
                                  f"entrada nova sem expectativa declarada: {nome}")
                    params = achou.group(1)
                    for chave, valor in self.RESOLVE.items():
                        params = params.replace(chave, valor)
                    self.assertNotIn("%", params,
                                     "sobrou placeholder: o mkarchiso não resolveria")
                    self.assertEqual(
                        self._pula(params), esperado[nome],
                        f"{arq.name}/{nome}: cmdline decidiu errado",
                    )

    def test_the_resolution_table_matches_the_real_iso(self):
        """Os placeholders que eu resolvo são os que o template usa.

        Se o template ganhar um `%NOVO%` que o mkarchiso substitui e eu não,
        o teste acima passaria com um `%` cru no cmdline — que na ISO real
        viraria valor nenhum. Aqui o conjunto tem de bater.
        """
        usados = set()
        for arq in (GRUB_CFG, LOOPBACK_CFG):
            usados |= set(re.findall(r"%[A-Z_]+%", arq.read_text(encoding="utf-8")))
        self.assertEqual(
            usados, set(self.RESOLVE),
            "a tabela de resolução não cobre (ou sobra) placeholders",
        )


class BootGateHonoursTheSkipTests(unittest.TestCase):
    """O boot não pode desligar a máquina quando o usuário pediu o pulo.

    Este é o defeito da imagem: a tela de bloqueio aparecia e o `while true`
    de desligar era inevitável.

    O bloco de decisão é EXTRAÍDO DO ARQUIVO, não redigitado aqui. A primeira
    versão deste teste montava o `if` à mão, e a mutação provou que isso não
    serve: reintroduzir o defeito no script (trocar o teste do escape por
    `if false; then`) deixava o teste passando, porque ele exercitava a cópia
    do teste e não o script. Medido: B1 ESCAPOU.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="boot-gate-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.funcs = _so_as_funcoes(self.tmp / "funcs.sh")
        self.bloco = _bloco_de_decisao()

    def _roda_o_boot(self, *, skip: bool, vulkan_disponivel: bool) -> bool:
        """Devolve True se o boot DESLIGOU a máquina (caminho de bloqueio).

        `vulkan_disponivel=False` replica o guest sem GPU 1.2+ da imagem: o
        `vhackintosh check-vulkan` sai != 0, que é exatamente o que fazia a
        tela de bloqueio aparecer.
        """
        rc_vulkan = 0 if vulkan_disponivel else 1
        script = f'''
set -u
. "{self.funcs}"
vhackintosh() {{
    if [ "$1" = "check-vulkan" ]; then
        echo "ERRO: Nenhuma GPU física com suporte a Vulkan foi encontrada no host."
        return {rc_vulkan}
    fi
    return 0
}}
{self.bloco}
if [ "$VULKAN_OK" -ne 1 ]; then
    echo "DESLIGOU=1"
else
    echo "DESLIGOU=0"
fi
'''
        env = dict(os.environ)
        if skip:
            env["VHACK_SKIP_VULKAN_CHECK"] = "1"
        else:
            env.pop("VHACK_SKIP_VULKAN_CHECK", None)
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return "DESLIGOU=1" in r.stdout

    def test_the_case_from_the_screenshot_is_unblocked(self):
        """O caso relatado: guest sem GPU + pedido explícito de pular."""
        self.assertFalse(
            self._roda_o_boot(skip=True, vulkan_disponivel=False),
            "com VHACK_SKIP_VULKAN_CHECK=1 o boot NÃO pode desligar a máquina",
        )

    def test_without_the_flag_the_gate_still_fires(self):
        """Contraprova: sem a variável, quem não tem GPU continua bloqueado."""
        self.assertTrue(
            self._roda_o_boot(skip=False, vulkan_disponivel=False),
            "sem o escape, um host sem Vulkan 1.2+ tem de ser bloqueado",
        )

    def test_a_host_with_vulkan_is_never_blocked(self):
        self.assertFalse(self._roda_o_boot(skip=False, vulkan_disponivel=True))

    def test_the_flag_does_not_hide_a_working_gpu(self):
        self.assertFalse(self._roda_o_boot(skip=True, vulkan_disponivel=True))


class TheGateWiringIsPresentTests(unittest.TestCase):
    """O script de boot e o `core/gpu.py` falam do MESMO escape.

    Sem esta amarração, alguém poderia "consertar" o boot inventando uma
    variável própria (`VHACK_IGNORA_VULKAN`, digamos) e a documentação, o
    `core/gpu.py` e o menu passariam a apontar para outra coisa.

    A amarração é feita no HELPERS e não no arquivo inteiro: medido, a
    variável aparece 4x no arquivo (comentário, chamada do
    `verify-build-in-guest.sh`, mensagem e o helper). Um `assertIn` no texto
    todo passava mesmo depois de trocar a variável do helper — mutação B5
    ESCAPOU. Aqui a checagem é sobre a função que DECIDE.
    """

    def _corpo_do_helper(self) -> str:
        texto = PROFILE.read_text(encoding="utf-8")
        inicio = texto.find("vhack_vulkan_check_skipped() {")
        self.assertNotEqual(inicio, -1, "o helper do escape sumiu do profile")
        fim = texto.find("\n}", inicio)
        self.assertNotEqual(fim, -1, "o helper do escape ficou sem fechar")
        return texto[inicio:fim]

    def test_the_profile_reads_the_same_variable_as_the_core(self):
        self.assertIn(VULKAN_SKIP_ENV, self._corpo_do_helper())

    def test_the_helper_does_not_invent_its_own_variable(self):
        """Só o nome oficial pode aparecer no helper."""
        corpo = self._corpo_do_helper()
        usados = set(re.findall(r"[A-Z][A-Z0-9_]{6,}", corpo))
        inventados = {n for n in usados if "VHACK" in n and n != VULKAN_SKIP_ENV}
        self.assertEqual(
            inventados, set(),
            f"o helper do escape usa variável(ns) própria(s): {sorted(inventados)}",
        )

    def test_the_two_sides_name_the_same_kernel_parameter(self):
        """O parâmetro do shell e o do Python têm de ser o mesmo texto.

        Foi exatamente esta costura que se rompeu: `vhack_skip_vulkan=1` era
        lido só pelo shell. Os dois nomes agora vêm das constantes de produção.
        """
        corpo = self._corpo_do_helper()
        self.assertIn(VULKAN_SKIP_CMDLINE, corpo,
                      "o helper do shell não lê o parâmetro que o Python usa")
        self.assertEqual(VULKAN_SKIP_CMDLINE, CMD_DO_ESCAPE,
                         "o parâmetro do boot mudou de nome num lado só")
        # E o Python precisa consultá-lo de fato.
        fonte = (RAIZ / "core" / "gpu.py").read_text(encoding="utf-8")
        self.assertIn("VULKAN_SKIP_CMDLINE", fonte)
        self.assertIn("/proc/cmdline", fonte)

    def test_the_blocking_loop_is_still_reachable(self):
        """O gate não pode ter sido simplesmente APAGADO.

        Pular sob pedido é diferente de não ter gate: o `while true` de
        desligar precisa continuar existindo para quem não pediu o pulo.
        """
        texto = PROFILE.read_text(encoding="utf-8")
        self.assertIn("BLOQUEIO CRÍTICO", texto)
        self.assertIn("poweroff", texto)
        self.assertIn('if [ "$VULKAN_OK" -ne 1 ]', texto)

    def test_the_blocking_screen_teaches_the_way_out(self):
        """A tela que bloqueia tem de DIZER como destravar.

        Quem cai ali está numa tela sem saída (Enter desliga, 'r' reinicia
        para a mesma tela). O parâmetro de kernel resolve, mas só se a pessoa
        souber que ele existe: um escape indescobrível é o mesmo que não ter
        escape. Este teste amarra a instrução ao parâmetro REAL, para os dois
        não divergirem num rename futuro.
        """
        texto = PROFILE.read_text(encoding="utf-8")
        trecho_bloqueio = texto[texto.find("BLOQUEIO CRÍTICO"):]

        self.assertIn("vhack_skip_vulkan=1", trecho_bloqueio,
                      "a tela de bloqueio não ensina o parâmetro que destrava")
        self.assertIn("VHACK_SKIP_VULKAN_CHECK=1", trecho_bloqueio,
                      "a tela de bloqueio não menciona a variável de ambiente")

    def test_the_documented_parameter_is_the_one_that_actually_works(self):
        """A instrução da tela não pode apontar para um parâmetro morto."""
        texto = PROFILE.read_text(encoding="utf-8")
        # O parâmetro citado na ajuda precisa ser o mesmo que o helper lê.
        self.assertIn("vhack_skip_vulkan=1", texto[texto.find("vhack_vulkan_check_skipped() {"):])


class OsDoisGatesConcordamTests(unittest.TestCase):
    """O shell e o Python precisam chegar à MESMA decisão para o mesmo boot.

    Este é o teste que faltava, e o defeito que ele cobre foi medido numa ISO
    real: o `profile.d` entendia `vhack_skip_vulkan=1` e decidia pular, mas em
    seguida chamava `vhackintosh check-vulkan`, que é `core/gpu.py` — e ali o
    parâmetro era desconhecido, porque o Python só olhava `os.environ`. O gate
    então bloqueava DEPOIS do pulo e a tela de bloqueio aparecia mesmo bootando
    pela entrada "Testar em VM".

    Os testes anteriores passavam porque exercitavam cada lado sozinho. Um
    bypass que só metade do caminho entende não é bypass: por isso a asserção
    aqui é a IGUALDADE das duas decisões, não o valor de uma delas.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dois-gates-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cmdline = self.tmp / "cmdline"
        self.funcs = _so_as_funcoes(self.tmp / "funcs.sh", cmdline=self.cmdline)

    def _shell(self, cmdline: str, *, env_valor: str | None = None) -> bool:
        self.cmdline.write_text(cmdline, encoding="utf-8")
        env = dict(os.environ)
        env.pop(VULKAN_SKIP_ENV, None)
        if env_valor is not None:
            env[VULKAN_SKIP_ENV] = env_valor
        r = subprocess.run(
            ["bash", "-c",
             f'. "{self.funcs}"; vhack_vulkan_check_skipped && echo SIM || echo NAO'],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip() == "SIM"

    def _python(self, cmdline: str, *, env_valor: str | None = None) -> bool:
        """Roda `core/gpu.py` de verdade, com o cmdline passado por parâmetro.

        Não há como mentir sobre `/proc/cmdline` sem root, então o caminho do
        arquivo é injetado em `_cmdline_do_kernel` — que é exatamente a função
        que o código de produção usa para obter o conteúdo. O que o teste NÃO
        faz é reescrever a decisão: ela vem de `vulkan_check_skipped`.
        """
        self.cmdline.write_text(cmdline, encoding="utf-8")
        env = dict(os.environ)
        env.pop(VULKAN_SKIP_ENV, None)
        if env_valor is not None:
            env[VULKAN_SKIP_ENV] = env_valor
        script = (
            "import sys; sys.path.insert(0, sys.argv[1])\n"
            "from core import gpu\n"
            "gpu._cmdline_do_kernel = lambda: open(sys.argv[2]).read()\n"
            "print('SIM' if gpu.vulkan_check_skipped() else 'NAO')\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", script, str(RAIZ), str(self.cmdline)],
            capture_output=True, text=True, timeout=120, env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip() == "SIM"

    def _ambos(self, cmdline: str, *, env_valor: str | None = None) -> tuple[bool, bool]:
        return (self._shell(cmdline, env_valor=env_valor),
                self._python(cmdline, env_valor=env_valor))

    def test_the_python_gate_reads_the_kernel_parameter(self):
        """A contraprova direta do defeito: sozinho, o Python já pula."""
        self.assertTrue(self._python("quiet splash vhack_skip_vulkan=1"),
                        "core/gpu.py ignora vhack_skip_vulkan=1: o gate bloqueia "
                        "depois do pulo e a ISO fica inutilizável na VM")

    def test_both_gates_agree_on_every_boot(self):
        """A decisão dos dois lados tem de bater em todos os cmdlines."""
        casos = [
            "quiet splash vhack_skip_vulkan=1",
            "vhack_skip_vulkan=1",
            "quiet splash",
            "",
            "vhack_build_test=1",
            "vhack_skip_vulkan=0",
            "vhack_skip_vulkan=10",
            "vhack_skip_vulkan=1abc",
            "vhack_skip_vulkan=",
            "meu_vhack_skip_vulkan=1",
            "quiet vhack_skip_vulkan=1 splash",
        ]
        for cmdline in casos:
            with self.subTest(cmdline=cmdline):
                shell, python = self._ambos(cmdline)
                self.assertEqual(
                    shell, python,
                    f"os dois gates divergem com cmdline={cmdline!r}: "
                    f"shell={'pula' if shell else 'bloqueia'}, "
                    f"python={'pula' if python else 'bloqueia'}",
                )

    def test_the_environment_still_works_on_both_sides(self):
        """A variável de ambiente continua valendo nos dois (não regrediu)."""
        for valor in ["1", "true", "sim", "on", "yes"]:
            with self.subTest(valor=valor):
                shell, python = self._ambos("quiet splash", env_valor=valor)
                self.assertTrue(shell, f"shell ignorou {valor!r}")
                self.assertTrue(python, f"python ignorou {valor!r}")

    def test_neither_side_skips_without_being_asked(self):
        """Boot normal: os dois bloqueiam. O padrão não foi afrouxado."""
        shell, python = self._ambos("archisobasedir=arch quiet splash")
        self.assertFalse(shell)
        self.assertFalse(python)

    def test_the_real_python_rejects_partial_values(self):
        """O valor tem de ser `1` literal, igual à tabela do shell."""
        for cmdline in ["vhack_skip_vulkan=10", "vhack_skip_vulkan=1abc",
                        "vhack_skip_vulkan=true", "vhack_skip_vulkan="]:
            with self.subTest(cmdline=cmdline):
                self.assertFalse(self._python(cmdline),
                                 f"{cmdline!r} não pode destravar o boot")

    def test_a_cmdline_file_that_does_not_exist_is_not_an_error(self):
        """Fora do Linux (ou sem /proc) o gate não pode explodir.

        Aqui NÃO se substitui `_cmdline_do_kernel` por uma função que levanta:
        isso testaria o meu dublê, não o código. O que se faz é apontar o
        `open` do módulo para um caminho inexistente e deixar o `try/except`
        real do `core/gpu.py` fazer o trabalho. Foi assim que a versão antiga
        (que só protegia um `open` cru, sem o `except` da chamada) escapou da
        mutação: o teste anterior trocava a função inteira.
        """
        script = (
            "import sys, builtins; sys.path.insert(0, sys.argv[1])\n"
            "from core import gpu\n"
            "real = builtins.open\n"
            "builtins.open = lambda p, *a, **k: real('/caminho/que/nao/existe', *a, **k)\n"
            "print('SIM' if gpu.vulkan_check_skipped() else 'NAO')\n"
        )
        env = dict(os.environ)
        env.pop(VULKAN_SKIP_ENV, None)
        r = subprocess.run([sys.executable, "-c", script, str(RAIZ)],
                           capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(r.returncode, 0,
                         f"exceção vazou de vulkan_check_skipped:\n{r.stderr}")
        self.assertEqual(r.stdout.strip(), "NAO")

    def test_the_reader_survives_a_hostile_cmdline(self):
        """`_cmdline_do_kernel` nunca levanta, seja qual for a falha do open.

        Cada modo de falha que um sistema real apresenta (sem permissão,
        diretório no lugar de arquivo, I/O) tem de virar string vazia, porque
        o resultado de "não consegui ler" é "não pulou" — e não uma exceção
        que derruba a checagem de hardware inteira.
        """
        modos = {
            "permissao": "PermissionError",
            "diretorio": "IsADirectoryError",
            "io": "OSError",
            "generico": "RuntimeError",
        }
        for rotulo, excecao in modos.items():
            with self.subTest(modo=rotulo):
                script = (
                    "import sys, builtins; sys.path.insert(0, sys.argv[1])\n"
                    "from core import gpu\n"
                    f"def boom(p, *a, **k):\n    raise {excecao}('x')\n"
                    "builtins.open = boom\n"
                    "valor = gpu._cmdline_do_kernel()\n"
                    "print('SIM' if gpu.vulkan_check_skipped() else 'NAO')\n"
                    "print(repr(valor))\n"
                )
                env = dict(os.environ)
                env.pop(VULKAN_SKIP_ENV, None)
                r = subprocess.run([sys.executable, "-c", script, str(RAIZ)],
                                   capture_output=True, text=True, timeout=120, env=env)
                self.assertEqual(r.returncode, 0,
                                 f"{rotulo}: exceção vazou:\n{r.stderr}")
                linhas = r.stdout.splitlines()
                self.assertEqual(linhas[0], "NAO", f"{rotulo}: pulou sem poder ler")
                self.assertEqual(linhas[1], "''", f"{rotulo}: devolveu {linhas[1]}")

    def test_a_non_string_cmdline_does_not_skip(self):
        """Se a leitura devolver algo que não é texto, o padrão é bloquear.

        Sem este guarda, um objeto inesperado chegaria ao `re.search` e
        levantaria `TypeError` no boot — trocando "não pulou" por "o gate
        quebrou".
        """
        for valor in ["None", "b' vhack_skip_vulkan=1 '", "0", "[]", "object()"]:
            with self.subTest(valor=valor):
                script = (
                    "import sys; sys.path.insert(0, sys.argv[1])\n"
                    "from core import gpu\n"
                    f"gpu._cmdline_do_kernel = lambda: {valor}\n"
                    "print('SIM' if gpu.vulkan_check_skipped() else 'NAO')\n"
                )
                env = dict(os.environ)
                env.pop(VULKAN_SKIP_ENV, None)
                r = subprocess.run([sys.executable, "-c", script, str(RAIZ)],
                                   capture_output=True, text=True, timeout=120, env=env)
                self.assertEqual(r.returncode, 0,
                                 f"{valor}: exceção vazou:\n{r.stderr}")
                self.assertEqual(r.stdout.strip(), "NAO",
                                 f"{valor}: não pode destravar o boot")

        r = subprocess.run([sys.executable, "-c", script, str(RAIZ)],
                           capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(r.returncode, 0,
                         f"exceção vazou de vulkan_check_skipped:\n{r.stderr}")
        self.assertEqual(r.stdout.strip(), "NAO")

    def test_the_skip_message_names_the_parameter_that_was_used(self):
        """A mensagem de pulo não pode afirmar uma via que não foi usada."""
        ok, msg = self._mensagem_com_cmdline("quiet vhack_skip_vulkan=1")
        self.assertTrue(ok, msg)
        self.assertIn("vhack_skip_vulkan", msg,
                      "a mensagem de pulo não cita o parâmetro de kernel")

    def _mensagem_com_cmdline(self, cmdline: str) -> tuple[bool, str]:
        self.cmdline.write_text(cmdline, encoding="utf-8")
        arquivo = self.tmp / "saida.txt"
        script = (
            "import sys; sys.path.insert(0, sys.argv[1])\n"
            "from core import gpu\n"
            "gpu._cmdline_do_kernel = lambda: open(sys.argv[2]).read()\n"
            "ok, msg = gpu.GPUChecker.check_vulkan_host_readiness()\n"
            "open(sys.argv[3], 'w').write(f'{int(ok)}\\n{msg}\\n')\n"
        )
        env = dict(os.environ)
        env.pop(VULKAN_SKIP_ENV, None)
        r = subprocess.run(
            [sys.executable, "-c", script, str(RAIZ), str(self.cmdline), str(arquivo)],
            capture_output=True, text=True, timeout=120, env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        linhas = arquivo.read_text(encoding="utf-8").splitlines()
        return linhas[0] == "1", linhas[1]


class ACopiaDaISOEstaEmDiaTests(unittest.TestCase):
    """A cópia de `core/` dentro do archiso não pode divergir do repositório.

    `build-iso.sh` faz `rsync` de `core/` do repositório para
    `archiso/airootfs/opt/vhackintosh/`, então a cópia é regenerada no build —
    mas enquanto o build não roda, é a cópia que está no disco que engana quem
    for ler o arquivo. Aconteceu nesta própria correção: `core/gpu.py` foi
    consertado e a cópia do archiso continuou com a versão velha, que é
    exatamente o arquivo que alguém abriria para conferir "o que a ISO faz".

    O teste não exige que a cópia seja commitada igual (ela é artefato de
    build); exige que, SE existir no disco, ela não contradiga o original no
    ponto que decide o destino do boot.
    """

    COPIA = (
        RAIZ / "appliance" / "archiso" / "airootfs" / "opt" / "vhackintosh"
        / "core" / "gpu.py"
    )

    def test_if_present_the_copy_reads_the_kernel_parameter(self):
        if not self.COPIA.exists():
            self.skipTest("a cópia só existe depois de um build")
        copia = self.COPIA.read_text(encoding="utf-8")
        self.assertIn(
            VULKAN_SKIP_CMDLINE, copia,
            "a cópia de core/gpu.py dentro do archiso não conhece o parâmetro "
            "de kernel: uma ISO construída a partir dela bloquearia a VM",
        )
        self.assertIn("/proc/cmdline", copia)

    def test_if_present_the_two_copies_agree(self):
        if not self.COPIA.exists():
            self.skipTest("a cópia só existe depois de um build")
        original = (RAIZ / "core" / "gpu.py").read_text(encoding="utf-8")
        self.assertEqual(
            original, self.COPIA.read_text(encoding="utf-8"),
            "core/gpu.py e a cópia no archiso divergiram: rode "
            "`install -m 0644 core/gpu.py "
            "appliance/archiso/airootfs/opt/vhackintosh/core/gpu.py`",
        )


if __name__ == "__main__":
    unittest.main()