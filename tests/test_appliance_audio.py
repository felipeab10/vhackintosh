"""A VM só sobe se o backend de áudio que o QEMU escolhe existir de fato na ISO.

Regressão de 19/10 (ISO 1.1.1): a VM não iniciava no appliance. O erro não era
mais de biblioteca — o binário carregava e montava a linha de comando inteira —
mas o QEMU morria ao abrir o áudio:

    qemu-system-x86_64: Failed to connect to PipeWire instance: Host is down

O `reims-ctl` pergunta ao QEMU quais backends ele foi COMPILADO para suportar
(`-audiodev help`) e escolhe o primeiro da lista de preferência. Essa pergunta
é respondida no host de BUILD, onde o pipewire roda; a ISO é uma sessão de
console sem `systemd --user`, então nenhum serviço `--user` do pipewire
iniciava. O QEMU escolhia `pipewire` e morria.

É o mesmo erro de raciocínio das duas regressões anteriores deste appliance:
decidir por disponibilidade global em vez de por estado relevante. A guarda
antiga (`pgrep -x pipewire`) perguntava "existe um processo pipewire no
sistema?", e num host com pipewire de sessão — exatamente onde o QEMU é
compilado — isso é sempre verdade, mesmo que o daemon sirva outro runtime dir.

A regra que estes testes guardam: o script de sessão sobe o pipewire ANTES do
compositor, e a guarda de "já está rodando" olha o socket do runtime dir certo,
não a existência de um processo qualquer.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WAYSESSION = (
    ROOT / "appliance" / "archiso" / "airootfs" / "usr" / "local" / "bin"
    / "vhackintosh-waysession"
)


def _script() -> str:
    return WAYSESSION.read_text(encoding="utf-8")


def _corpo_da_funcao() -> str:
    """O texto de `vhack_sobe_pipewire`, da assinatura até o `}` na coluna 0.

    Não basta procurar o primeiro `\\n}`: a função tem blocos `if` internos
    cujo fechamento também cai numa linha só com `}` (indentado). Truncar ali
    deixa um `{` aberto e o bash recusa o arquivo com "fim inesperado do
    arquivo" — foi o que aconteceu na primeira versão deste helper.
    """
    texto = _script()
    m = re.search(r"^vhack_sobe_pipewire\(\)\s*\{$", texto, re.M)
    assert m, "a função vhack_sobe_pipewire sumiu do script de sessão"
    linhas = texto[m.start():].splitlines()
    for i, linha in enumerate(linhas[1:], start=2):
        if linha == "}":
            return "\n".join(linhas[:i])
    raise AssertionError("a função vhack_sobe_pipewire não fecha com } na coluna 0")


class OAudioNaoPodeImpedirAVmTests(unittest.TestCase):
    def test_o_script_de_sessao_sobe_o_pipewire(self):
        """Sem isto a ISO volta a morrer com 'Host is down'."""
        assert "vhack_sobe_pipewire" in _script(), (
            "o script de sessão não sobe mais o pipewire"
        )
        assert re.search(r"^vhack_sobe_pipewire\s*$", _script(), re.M), (
            "vhack_sobe_pipewire existe mas nunca é chamada"
        )

    def test_o_pipewire_sobe_antes_do_sway(self):
        """Ordem importa: o QEMU é filho do sway e precisa do socket pronto."""
        texto = _script()
        chamada = texto.index("\nvhack_sobe_pipewire\n")
        sway = texto.index("sway -c")
        assert chamada < sway, (
            "o pipewire é iniciado depois do sway; a VM pode abrir antes do "
            "socket existir e o QEMU morre com 'Host is down'"
        )

    def test_falha_de_audio_nunca_derruba_a_vm(self):
        """Áudio é acessório: sem ele a VM tem de subir mesmo assim."""
        corpo = _corpo_da_funcao()
        for daemon in ("pipewire", "wireplumber", "pipewire-pulse"):
            linha = re.search(rf"^\s*setsid {daemon} .*$", corpo, re.M)
            assert linha, f"{daemon} não é iniciado com setsid (morre com o shell)"
            assert "</dev/null" in linha.group(0), (
                f"{daemon} segura a entrada do script"
            )
            assert ">/dev/null 2>&1 &" in linha.group(0), (
                f"{daemon} não é desanexado nem silenciado"
            )

    def test_avisa_quando_o_pipewire_nao_sobe(self):
        """Silêncio inexplicável vira meia hora de depuração."""
        corpo = _corpo_da_funcao()
        assert re.search(r'\[ ! -S "\$XDG_RUNTIME_DIR/pipewire-0" \]', corpo), (
            "a função não confere se o socket nasceu"
        )
        assert "sem audio" in corpo, (
            "a função não avisa no console quando o pipewire não sobe"
        )

    def test_espera_o_socket_antes_de_subir_o_wireplumber(self):
        """O wireplumber desiste na hora se conectar antes do socket existir.

        Sem esta espera o áudio falha de forma intermitente e dependente de
        timing — o pior tipo de falha para depurar dentro de uma ISO.
        """
        corpo = _corpo_da_funcao()
        espera = re.search(
            r'while \[ "\$_i" -lt \d+ \]; do\s*\n'
            r'\s*\[ -S "\$XDG_RUNTIME_DIR/pipewire-0" \] && break',
            corpo,
        )
        assert espera, (
            "a função não espera o socket $XDG_RUNTIME_DIR/pipewire-0 aparecer"
        )
        assert espera.start() < corpo.index("setsid wireplumber"), (
            "o wireplumber sobe ANTES da espera pelo socket; ele vai desistir "
            "e os nós de áudio nunca são criados"
        )


class AGuardaUsaOEstadoRelevanteTests(unittest.TestCase):
    """A pergunta certa: existe pipewire servindo o MEU runtime dir?"""

    def test_a_guarda_nao_e_pgrep_global(self):
        """`pgrep -x pipewire` erra num host que já tem pipewire de sessão.

        Olha só o código executável: o comentário acima da guarda cita `pgrep`
        de propósito, para explicar por que ela foi trocada. Casar com o texto
        cru confundiria a documentação com a execução.
        """
        codigo = "\n".join(
            linha for linha in _corpo_da_funcao().splitlines()
            if not linha.lstrip().startswith("#")
        )
        assert "pgrep" not in codigo, (
            "a guarda do pipewire voltou a usar pgrep: num host com pipewire "
            "de sessão (onde o QEMU é compilado) ela acha um processo de "
            "OUTRO runtime dir e desiste, deixando a ISO sem áudio"
        )

    def test_a_guarda_olha_o_socket_do_runtime_dir(self):
        corpo = _corpo_da_funcao()
        assert re.search(
            r'if \[ -S "\$XDG_RUNTIME_DIR/pipewire-0" \]; then\s*\n\s*return 0',
            corpo,
        ), (
            "a guarda não usa o socket $XDG_RUNTIME_DIR/pipewire-0, que é o "
            "mesmo ponto que o QEMU usa para se conectar"
        )

    def test_nao_usa_a_flag_daemon_que_o_pipewire_nao_tem(self):
        """`--daemon` é do pulseaudio; o pipewire responde 'opção não reconhecida'."""
        codigo = "\n".join(
            linha for linha in _corpo_da_funcao().splitlines()
            if not linha.lstrip().startswith("#")
        )
        assert "pipewire --daemon" not in codigo, (
            "o pipewire não tem flag --daemon; ela é do pipewire-pulse. Passá-la "
            "rende 'opção não reconhecida' e nenhum daemon sobe"
        )


@pytest.mark.skipif(
    shutil.which("pipewire") is None,
    reason="pipewire não instalado neste host; o teste precisaria subir um daemon",
)
class OPipewireSobeDeVerdadeTests(unittest.TestCase):
    """Mede a função rodando, não só o texto dela."""

    def test_a_funcao_cria_o_socket_num_runtime_dir_novo(self):
        # `tempfile` e não a fixture `tmp_path`: as classes deste arquivo são
        # `unittest.TestCase` para o pytest coletá-las (o repo não configura
        # `python_classes`, então só `Test*` seria coletado), e TestCase não
        # recebe fixtures do pytest.
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            runtime.mkdir()
            runtime.chmod(0o700)

            script = Path(tmp) / "fn.sh"
            script.write_text(_corpo_da_funcao(), encoding="utf-8")

            proc = subprocess.run(
                [
                    "bash", "-c",
                    f'set -u; XDG_RUNTIME_DIR={runtime}; export XDG_RUNTIME_DIR; '
                    f'source {script}; vhack_sobe_pipewire; echo "rc=$?"',
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            socket = runtime / "pipewire-0"
            self.assertTrue(
                socket.is_socket(),
                "a função não criou $XDG_RUNTIME_DIR/pipewire-0; sem esse socket "
                "o QEMU (audiodev pipewire) morre com 'Host is down'. "
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
            )

    def test_o_binario_empacotado_tem_backend_none(self):
        """`none` é a degradação graciosa se o pipewire não subir na ISO."""
        qemu = (
            Path.home() / ".cache" / "reims-ctl"
            / "qemu-build-linux-x86_64" / "qemu-system-x86_64"
        )
        if not qemu.is_file():
            pytest.skip("QEMU empacotado não construído neste host")

        out = subprocess.run(
            [str(qemu), "-audiodev", "help"],
            capture_output=True, text=True, timeout=60,
        ).stdout
        backends = {
            linha.strip()
            for linha in out.splitlines()
            if linha.strip() and "audio drivers" not in linha
        }
        assert "none" in backends, "sem backend 'none' não há degradação graciosa"
