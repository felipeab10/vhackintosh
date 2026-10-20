"""A placa que o wizard promete tem que ser a placa que o reims-ctl usa.

Regressão de 19/10 (medida no appliance): o operador escolheu a Intel no
wizard e a VM abriu em tela preta; trocou para NVIDIA, viu "already runs (pid
2793)" e não tinha como parar. Três defeitos independentes, todos da mesma
família — dois componentes discordando sobre o mesmo fato:

1. `write_vm_toml` aceitava `vm.selected_gpu` e nunca o lia. A escolha do
   wizard era descartada junto com o `[gpu]` do TOML. O `reims-ctl`, sem
   `[gpu]`, aplica o próprio default: "the first discrete GPU, then the first
   integrated one" — num notebook com RTX 3050 (discrete) e Iris Xe
   (integrated), isso é a NVIDIA, independente do que o operador clicou.

2. O wizard pré-selecionava a GPU por FABRICANTE (`vendor_name in ("NVIDIA",
   "AMD")`). Ser discreta não é prova de funcionar: medido neste host, o
   Vulkan enumera as duas placas, então nenhuma das duas é "a aprovada" e não
   há base para preferir uma. A regra agora é medir, não supor.

3. `_pid_alive` dizia que um processo ZUMBI estava vivo (`kill(pid, 0)`
   funciona em zumbi), e o laço de espera não tinha teto. Uma VM que morria
   sem ser recolhida prendia a TUI para sempre, parecendo "VM iniciada".

O que estes testes guardam: a escolha explícita do operador chega ao TOML, o
default não é decidido por fabricante, e esperar por um pid tem fim.
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TUI = ROOT / "ui" / "tui.py"


def _vm(slot=None, nome=None):
    from core.config import VMConfig

    return VMConfig(
        id="teste",
        name="Teste",
        macos_version="tahoe",
        vcpus=4,
        ram_gb=8,
        disk_size_gb=64,
        disk_path="/tmp/nao-usado.qcow2",
        selected_gpu=slot,
        selected_gpu_name=nome,
    )


# ---------------------------------------------------------------- Bug 1: TOML


class AEscolhaDoWizardChegaAoTomlTests(unittest.TestCase):
    """`vm.selected_gpu` precisa virar `[gpu] driver_file` no vm.toml."""

    def test_a_gpu_escolhida_vira_secao_gpu(self):
        """Sem `[gpu]` o reims-ctl escolhe sozinho — era o bug."""
        from unittest import mock
        from core import reims_ctl

        with tempfile.TemporaryDirectory() as tmp:
            # This unit test verifies the writer, not the host's Vulkan stack.
            # The real PCI->ICD probe has separate coverage and may be
            # unavailable in CI, containers, or an emulated test host.
            with mock.patch.object(reims_ctl, "vm_dir", return_value=Path(tmp)), \
                 mock.patch.object(reims_ctl, "_icd_para_slot", return_value="/usr/share/vulkan/icd.d/intel_icd.json"):
                destino = reims_ctl.write_vm_toml(_vm("0000:00:02.0", "Intel Iris Xe"))
            texto = destino.read_text(encoding="utf-8")
        self.assertIn("[gpu]", texto, "a escolha do wizard sumiu do vm.toml")
        self.assertIn("driver_file", texto)

    def test_a_secao_gpu_traz_o_icd_medido_nao_o_nome_do_fabricante(self):
        """O campo é o manifest ICD que o reims-ctl aceita, não um rótulo."""
        from unittest import mock
        from core import reims_ctl

        if not reims_ctl._gpus_do_reims_ctl():
            self.skipTest("reims-ctl indisponível neste host para mapear o slot")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(reims_ctl, "vm_dir", return_value=Path(tmp)):
                destino = reims_ctl.write_vm_toml(_vm("0000:00:02.0"))
            texto = destino.read_text(encoding="utf-8")
        self.assertIn(".json", texto, "driver_file deve ser um manifest ICD")
        self.assertNotIn("NVIDIA", texto, "a Intel escolhida não pode virar NVIDIA")

    def test_slot_invalido_nao_inventa_gpu(self):
        """Slot que não existe não pode gerar um `[gpu]` falso."""
        from core import reims_ctl

        for lixo in ("", "abc", "zzz", "0000:99:00.0", None):
            with self.subTest(slot=lixo):
                self.assertIsNone(
                    reims_ctl._icd_para_slot(lixo),
                    f"slot {lixo!r} não deveria mapear para nenhum ICD",
                )

    def test_falha_no_mapeamento_recusa_escrita_sem_preservar_gpu_antiga(self):
        """Intel selecionada sem ICD não pode iniciar com NVIDIA antiga/default."""
        from core import reims_ctl

        with tempfile.TemporaryDirectory() as tmp:
            vm_dir = Path(tmp)
            vm_toml = vm_dir / "vm.toml"
            original = 'name = "teste"\n\n[gpu]\ndriver_file = "/vulkan/nvidia_icd.json"\n'
            vm_toml.write_text(original, encoding="utf-8")
            with mock.patch.object(reims_ctl, "vm_dir", return_value=vm_dir), \
                 mock.patch.object(reims_ctl, "_icd_para_slot", return_value=None):
                with self.assertRaises(reims_ctl.GPUSelectionError):
                    reims_ctl.write_vm_toml(_vm("0000:00:02.0", "Intel Iris Xe"))
            self.assertEqual(vm_toml.read_text(encoding="utf-8"), original)

    def test_a_gpu_e_mapeada_pela_fonte_autoritativa(self):
        """O mapeamento slot->ICD consulta o probe, não adivinha por nome.

        Um host com dois manifests Intel (`intel_icd.json` do Alder Lake e
        `intel_hasvk_icd.json`, que é Haswell) casa por nome de arquivo e
        escolhe o errado — medido. Quem enumera é o reims-ctl.
        """
        from core import reims_ctl

        fonte = inspect.getsource(reims_ctl._icd_para_slot)
        self.assertIn("_gpus_do_reims_ctl", fonte)
        lista = reims_ctl._gpus_do_reims_ctl()
        if not lista:
            self.skipTest("reims-ctl indisponível neste host")
        for gpu in lista:
            self.assertIn("vendor_device", gpu)
            self.assertIn("icd", gpu)
            self.assertIn("name", gpu)
            self.assertRegex(gpu["vendor_device"], r"^[0-9a-f]{4}:[0-9a-f]{4}$")


class ODefaultNaoVemDoFabricanteTests(unittest.TestCase):
    """O wizard não pode pré-selecionar placa por ser NVIDIA/AMD."""

    def test_o_wizard_nao_pre_seleciona_por_vendor_name(self):
        """Comportamento, não texto: o default não pode vir do fabricante.

        Um teste que só procura a string antiga deixa passar qualquer variação
        (foi o caso do mutante `_g.vendor_name in [...]`). Aqui extraímos a
        expressão que calcula `default_choice` e conferimos de que ela depende.
        """
        tui = TUI.read_text(encoding="utf-8")
        trecho = tui.split("if len(gpus) > 1:", 1)[1].split("gpu_choices", 1)[0]
        linhas = [
            l.strip()
            for l in trecho.splitlines()
            if l.strip().startswith("default_choice")
        ]
        self.assertTrue(
            linhas,
            "não achei a atribuição de default_choice para inspecionar",
        )
        expr = " ".join(linhas)
        self.assertIn("servem", expr, "o default não usa o resultado da checagem Vulkan")
        self.assertNotIn("vendor_name", expr, "o default ainda olha o fabricante")

    def test_o_default_usa_a_checagem_de_vulkan(self):
        tui = TUI.read_text(encoding="utf-8")
        trecho = tui.split("if len(gpus) > 1:", 1)[1].split("gpu_choices", 1)[0]
        self.assertIn("vulkan_dirige", trecho, "o default não mede o Vulkan")

    def test_o_criterio_do_default_e_o_vulkan(self):
        """`vulkan_dirige` é quem decide, e ele não olha o fabricante."""
        from core import gpu as gpu_mod

        fonte = inspect.getsource(gpu_mod.vulkan_dirige)
        self.assertNotIn("vendor_name", fonte, "vulkan_dirige decidindo por fabricante")
        self.assertIn("gpus_com_vulkan", fonte)

    def test_vulkan_dirige_consulta_o_probe(self):
        from core import gpu as gpu_mod

        fonte = inspect.getsource(gpu_mod.vulkan_dirige)
        self.assertIn("gpus_com_vulkan", fonte)
        fonte_probe = inspect.getsource(gpu_mod.gpus_com_vulkan)
        self.assertIn("_gpus_do_reims_ctl", fonte_probe)

    def test_uma_so_gpu_aprovada_vira_o_default(self):
        """Se exatamente uma passa, ela é pré-selecionada."""
        from core import gpu as gpu_mod

        class _G:
            vendor_id = "8086"
            device_id = "46a6"

        with mock.patch.object(gpu_mod, "gpus_com_vulkan", return_value={"8086:46a6": "Iris Xe"}):
            self.assertTrue(gpu_mod.vulkan_dirige(_G()))

    def test_gpu_ausente_da_lista_nao_e_aprovada(self):
        """Se o Vulkan não a enumera, não pode ser sugerida como default."""
        from core import gpu as gpu_mod

        class _G:
            vendor_id = "10de"
            device_id = "25e2"

        with mock.patch.object(gpu_mod, "gpus_com_vulkan", return_value={"8086:46a6": "Iris Xe"}):
            self.assertFalse(gpu_mod.vulkan_dirige(_G()))


# --------------------------------------------------------- Bug 4: pid zumbi


class EsperarPorPidTemFimTests(unittest.TestCase):
    """Um zumbi não é uma VM rodando, e a espera não pode ser infinita."""

    def test_zumbi_conta_como_morto(self):
        from core.reims_ctl import _pid_alive

        pid = os.fork()
        if pid == 0:  # pragma: no cover — caminho do filho
            os._exit(0)
        estado = "?"
        try:
            for _ in range(100):
                estado = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][:1]
                if estado == "Z":
                    break
                time.sleep(0.05)
            self.assertEqual(estado, "Z", "o filho não virou zumbi; teste inconclusivo")
            self.assertFalse(
                _pid_alive(pid),
                "zumbi tratado como vivo: o laço de espera gira para sempre",
            )
        finally:
            os.waitpid(pid, 0)

    def test_processo_vivo_conta_como_vivo(self):
        from core.reims_ctl import _pid_alive

        self.assertTrue(_pid_alive(os.getpid()))

    def test_pid_inexistente_conta_como_morto(self):
        from core.reims_ctl import _pid_alive

        self.assertFalse(_pid_alive(4194303))

    def test_o_laco_de_espera_tem_teto(self):
        """Sem teto, uma VM que não morre prende a TUI para sempre.

        Não basta procurar o nome da constante: o mutante
        `limite = float('inf')` mantinha a citação e escapava.

        Também não serve medir o relógio de verdade: quando o teto some, o
        laço gira para sempre e o teste TRAVA em vez de falhar (medido: 14 min
        de pytest pendurado). Aqui o relógio é falso e avança a cada consulta,
        então um laço sem teto estoura um orçamento de iterações e o teste
        falha rápido.
        """
        from unittest import mock
        from core import reims_ctl

        self.assertTrue(
            hasattr(reims_ctl, "_ESPERA_MAXIMA_S"),
            "o laço de espera do ReimsCtlSession.wait voltou a ser ilimitado",
        )
        self.assertGreater(reims_ctl._ESPERA_MAXIMA_S, 0)

        # "already runs (pid N)" -> o wrapper adota o pid; ele nunca morre.
        fake = mock.Mock(returncode=1, stdout="", stderr="vm already runs (pid 424242)")
        sessao = reims_ctl.ReimsCtlSession(["reims-ctl", "start", "x"])

        # Relógio falso: avança 100 s por consulta. Com o teto real (600 s),
        # a 7a consulta já passa do limite e o laço sai.
        relogio = {"t": 0.0}

        def _agora():
            relogio["t"] += 100.0
            if relogio["t"] > 5_000:
                raise AssertionError(
                    "o laço de espera não respeitou o teto: girou sem parar"
                )
            return relogio["t"]

        with mock.patch.object(reims_ctl.subprocess, "run", return_value=fake), \
             mock.patch.object(reims_ctl, "_pid_alive", return_value=True), \
             mock.patch.object(reims_ctl.time, "monotonic", side_effect=_agora), \
             mock.patch.object(reims_ctl.time, "sleep", return_value=None):
            sessao.wait()  # se não houver teto, estoura aqui

    def test_waysession_e_esperada_ate_o_fim_mesmo_sem_pid_no_stdout(self):
        """A saída do comando é assíncrona; a sessão kiosk segura o ciclo de vida."""
        from core import reims_ctl

        with mock.patch.object(
            reims_ctl.subprocess,
            "run",
            return_value=mock.Mock(returncode=0, stdout="", stderr=""),
        ) as run:
            sessao = reims_ctl.ReimsCtlSession(
                ["/usr/local/bin/vhackintosh-waysession", "/usr/bin/reims-ctl", "start", "teste"]
            )
            self.assertEqual(sessao.wait(), 0)

        run.assert_called_once()

    def test_waysession_mantem_o_sway_ate_o_pid_do_reims_terminar(self):
        script = (
            ROOT / "appliance" / "archiso" / "airootfs" / "usr" / "local" /
            "bin" / "vhackintosh-waysession"
        ).read_text(encoding="utf-8")
        self.assertIn('"$VHACK_REIMS_CTL" list --json', script)
        self.assertIn('while [ -r "/proc/$__vhack_vm_pid/stat" ]', script)
        self.assertLess(
            script.index('while [ -r "/proc/$__vhack_vm_pid/stat" ]'),
            script.index('swaymsg exit >/dev/null 2>&1 || true'),
        )

    def test_reboot_qmp_relanca_durante_instalacao(self):
        from ui.tui import run_rail_session

        monitor_resets = iter([1, 0])
        launches = []

        class Monitor:
            def __init__(self):
                self.reset_count = next(monitor_resets)
                self.last_reason = "guest-reset" if self.reset_count else None

            def start(self):
                pass

            def stop(self):
                pass

        def launch():
            launches.append(1)
            return mock.Mock(wait=lambda: 0)

        total, reason = run_rail_session(
            launch,
            Monitor,
            is_installing=True,
            settle_sec=0,
        )

        self.assertEqual(len(launches), 2)
        self.assertEqual(total, 1)
        self.assertEqual(reason, "guest-reset")


# ------------------------------------------------------- Bug 3: parar a VM


class DaParaPararAVmTests(unittest.TestCase):
    """O appliance tem que conseguir encerrar uma VM."""

    def test_existe_uma_funcao_de_parar(self):
        from core import reims_ctl

        self.assertTrue(callable(getattr(reims_ctl, "parar_vm", None)))

    def test_parada_normal_pede_desligamento_acpi_pelo_qmp(self):
        from core import reims_ctl

        fonte = inspect.getsource(reims_ctl.parar_vm)
        self.assertIn('qmp.command("system_powerdown")', fonte)
        self.assertIn('"stop"', fonte)
        self.assertIn("vm.id", fonte)

    def test_parada_normal_nao_forca_quando_mac_os_nao_desliga(self):
        from core import passthrough, reims_ctl

        qmp = mock.MagicMock()
        qmp.__enter__.return_value = qmp
        with mock.patch.object(reims_ctl, "reims_ctl_bin", return_value="/fake/reims-ctl"), \
             mock.patch.object(reims_ctl, "vms_rodando", return_value={"teste": 123}), \
             mock.patch.object(reims_ctl, "_pid_alive", return_value=True), \
             mock.patch.object(reims_ctl, "_DESLIGAMENTO_ACPI_ESPERA_S", 0), \
             mock.patch.object(passthrough, "QmpClient", return_value=qmp), \
             mock.patch.object(reims_ctl.subprocess, "run") as run:
            ok, msg = reims_ctl.parar_vm(_vm())

        self.assertFalse(ok)
        self.assertIn("não desligou", msg)
        run.assert_not_called()

    def test_parada_forcada_chama_stop_force_imediatamente(self):
        from core import reims_ctl

        proc = mock.Mock(returncode=0, stdout="forçado", stderr="")
        with mock.patch.object(reims_ctl, "reims_ctl_bin", return_value="/fake/reims-ctl"), \
             mock.patch.object(reims_ctl.subprocess, "run", return_value=proc) as run:
            ok, msg = reims_ctl.parar_vm(_vm(), force=True)

        self.assertTrue(ok)
        self.assertEqual(msg, "forçado")
        run.assert_called_once_with(
            ["/fake/reims-ctl", "stop", "teste", "--force"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_parar_sem_reims_ctl_falha_alto(self):
        """Sem binário tem que devolver erro claro, não fingir sucesso."""
        from unittest import mock
        from core import reims_ctl

        with mock.patch.object(reims_ctl, "reims_ctl_bin", return_value=None):
            ok, msg = reims_ctl.parar_vm(_vm("0000:00:02.0"))
        self.assertFalse(ok, "parar sem reims-ctl não pode ser reportado como sucesso")
        self.assertTrue(msg, "a falha precisa de uma mensagem")

    def test_vms_rodando_le_o_json_nao_a_saida_humana(self):
        """A saída humana traz o nome na 1a coluna e a palavra 'stopped'.

        O mock devolve JSON: se o código não pedir `--json`, ele lê a saída
        humana (`macos-tahoe stopped ...`) e não acha pid nenhum — o teste
        mede o argv, não só o resultado.
        """
        from unittest import mock
        from core import reims_ctl

        saida = (
            '[\n  {"dir": "/x", "name": "macos-tahoe", "pid": 2793, '
            '"state": "running"},\n  {"dir": "/y", "name": "outra", '
            '"pid": null, "state": "stopped"}\n]'
        )
        chamadas = []

        def _run(argv, **kw):
            chamadas.append(list(argv))
            return mock.Mock(stdout=saida)

        with mock.patch.object(reims_ctl, "reims_ctl_bin", return_value="/usr/bin/reims-ctl"), \
             mock.patch.object(reims_ctl.subprocess, "run", side_effect=_run):
            rodando = reims_ctl.vms_rodando()

        self.assertEqual(rodando, {"macos-tahoe": 2793})
        self.assertTrue(chamadas, "vms_rodando não executou nada")
        self.assertIn(
            "--json", chamadas[0],
            f"a saída humana não traz o pid; faltou --json em {chamadas[0]}",
        )

    def test_vms_rodando_com_a_saida_humana_nao_inventa_pid(self):
        """Contraprova: alimentado com a saída humana real, não acha VM."""
        from unittest import mock
        from core import reims_ctl

        humana = (
            "NAME                 STATE      CPUS  MEMORY DISPLAYS  DIR\n"
            "macos-tahoe          stopped      12     12G 1x 1920x1080 "
            "~/.local/share/reims-ctl/vms/macos-tahoe\n"
        )
        with mock.patch.object(reims_ctl, "reims_ctl_bin", return_value="/usr/bin/reims-ctl"), \
             mock.patch.object(reims_ctl.subprocess, "run",
                               return_value=mock.Mock(stdout=humana)):
            self.assertEqual(
                reims_ctl.vms_rodando(), {},
                "a saída humana foi interpretada como pid",
            )

    def test_vms_rodando_aguenta_saida_estranha(self):
        from unittest import mock
        from core import reims_ctl

        for saida in ("", "nao e json", "[]", "{}", '{"vms": []}'):
            with self.subTest(saida=saida):
                with mock.patch.object(reims_ctl, "reims_ctl_bin", return_value="/bin/x"), \
                     mock.patch.object(reims_ctl.subprocess, "run",
                                       return_value=mock.Mock(stdout=saida)):
                    self.assertEqual(reims_ctl.vms_rodando(), {})

    def test_a_tui_oferece_a_opcao_de_parar(self):
        tui = TUI.read_text(encoding="utf-8")
        self.assertIn("_parar_vm_interativo", tui)
        self.assertIn("tui.vm.actions.stop", tui)

    def test_o_estado_da_vm_aparece_na_tela(self):
        """Antes a TUI não dizia se a VM estava rodando."""
        tui = TUI.read_text(encoding="utf-8")
        self.assertIn("vms_rodando", tui)
        self.assertIn("tui.vm.details.run_state", tui)

    def test_a_tecla_p_esta_entre_as_acoes_permitidas(self):
        tui = TUI.read_text(encoding="utf-8")
        # O despacho da ação e a lista de escolhas ficam na tela de detalhes.
        self.assertIn('elif action in ("p", "P"):', tui)
        self.assertRegex(tui, r'allowed_actions = \[[^\]]*"p"[^\]]*\]')

    def test_parar_de_tentativa_educada_antes_da_forcada(self):
        """A UI mantém a parada normal e a forçada em ações separadas."""
        from core import reims_ctl
        from ui.tui import VMTUI

        fonte = inspect.getsource(reims_ctl.parar_vm)
        self.assertIn("--force", fonte)
        self.assertIn("force", fonte)
        tela = inspect.getsource(VMTUI._parar_vm_interativo)
        self.assertNotIn("parar_vm(vm, force=True)", tela)
        self.assertIn("parar_vm(vm, force=force)", tela)

    def test_stop_normal_nao_aplica_forca_automaticamente(self):
        from ui.tui import VMTUI

        fonte = inspect.getsource(VMTUI._parar_vm_interativo)
        self.assertIn("parar_vm(vm, force=force)", fonte)
        self.assertNotIn("parar_vm(vm, force=True)", fonte)
        self.assertIn("stop_graceful_failed_hint", fonte)


class DaParaRetomarATelaDaVmTests(unittest.TestCase):
    def test_view_usa_o_subcomando_interativo_sem_iniciar_outra_vm(self):
        from core import reims_ctl

        with mock.patch.object(reims_ctl, "reims_ctl_bin", return_value="/fake/reims-ctl"), \
             mock.patch.object(reims_ctl.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            ok, erro = reims_ctl.visualizar_vm(_vm())

        self.assertTrue(ok)
        self.assertEqual(erro, "")
        run.assert_called_once_with(["/fake/reims-ctl", "view", "teste"], check=False)

    def test_menu_oferece_view_quando_ha_pid_e_redireciona_a_opcao_principal(self):
        fonte = TUI.read_text(encoding="utf-8")
        self.assertIn('allowed_actions.extend(["f", "F", "v", "V"])', fonte)
        self.assertIn('"tui.vm.actions.start_running" if pid_desta else "tui.vm.actions.start"', fonte)
        self.assertIn('self._visualizar_vm_interativo(vm)\n                    continue', fonte)


# ----------------------------------------------------------- i18n coerente


class AsChavesNovasExistemNosDoisIdiomasTests(unittest.TestCase):
    def _conjuntos(self):
        arvore = ast.parse((ROOT / "core" / "i18n.py").read_text(encoding="utf-8"))
        conjuntos = []
        for no in ast.walk(arvore):
            if isinstance(no, ast.Dict):
                chaves = [k.value for k in no.keys if isinstance(k, ast.Constant)]
                if len(chaves) > 50:
                    conjuntos.append(set(chaves))
        return conjuntos

    def test_paridade_de_chaves(self):
        conjuntos = self._conjuntos()
        self.assertGreaterEqual(len(conjuntos), 2, "esperava pt e en")
        pt, en = conjuntos[0], conjuntos[1]
        self.assertEqual(
            pt, en, f"só em pt: {sorted(pt - en)}; só em en: {sorted(en - pt)}"
        )

    def test_as_chaves_usadas_pela_tui_existem(self):
        """Chave citada e ausente só quebra na hora de usar, dentro da VM."""
        from core import i18n

        tui = TUI.read_text(encoding="utf-8")
        usadas = set(re.findall(r'i18n\.t\(\s*"([^"]+)"', tui))
        usadas |= set(re.findall(r'i18n\.t\(\s*\n\s*"([^"]+)"', tui))
        todas = set(i18n.PT) | set().union(*(set(c) for c in i18n.CATALOGS.values()))
        faltando = sorted(k for k in usadas if k not in todas)
        self.assertEqual(faltando, [], f"chaves usadas e inexistentes: {faltando}")

    def test_as_chaves_novas_do_estado_e_do_stop_existem(self):
        from core import i18n

        for chave in (
            "tui.vm.actions.stop",
            "tui.vm.actions.state_running",
            "tui.vm.actions.state_stopped",
            "tui.vm.actions.stop_none",
            "tui.vm.actions.stop_done",
            "tui.vm.actions.stop_fail",
            "tui.vm.actions.stop_confirm",
            "tui.vm.actions.stop_force",
            "tui.vm.details.run_state",
            "tui.gpu.only_one_vulkan",
            "tui.gpu.many_vulkan",
            "tui.gpu.no_vulkan_known",
        ):
            with self.subTest(chave=chave):
                self.assertNotEqual(i18n.t(chave), chave, f"{chave} não traduzida")


class OIcdTemUmaFonteSoTests(unittest.TestCase):
    """`VK_DRIVER_FILES` é escrito num lugar só: o `reims-ctl`.

    Regressão de 19/10, medida no host: `_apply_gpu_environment` escolhia o
    ICD pelo *nome do fabricante* e procurava `intel_icd.x86_64.json` /
    `intel_hasvk_icd.x86_64.json` / `radeon_icd.x86_64.json`. Os três nomes
    são de outra distribuição e não existem — medido neste host, o diretório
    tem só `intel_hasvk_icd.json`, `intel_icd.json` e `nvidia_icd.json`.
    Resultado: escolhendo Intel ou AMD, o processo subia sem ICD nenhum.

    Pior que o nome errado era haver *duas* fontes para o mesmo fato: o
    `reims-ctl` também grava `VK_DRIVER_FILES`/`VK_ICD_FILENAMES` no script
    do QEMU, a partir da seção `[gpu]` do vm.toml — que é a escolha medida no
    Vulkan. Quando as duas concordavam ninguém notava; quando discordavam,
    quem decidia era o `reims-ctl`, e a variável daqui só poluía.

    O que estes testes guardam: o launcher não escreve ICD por conta própria,
    não decide por fabricante, e não inventa nome de arquivo que não existe.
    """

    TUI = ROOT / "ui" / "tui.py"

    def _env_para(self, gpu_name: str, slot: str) -> dict:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from ui.tui import VMTUI

        vm = mock.Mock()
        vm.selected_gpu_name = gpu_name
        vm.selected_gpu = slot
        env: dict = {}
        VMTUI._apply_gpu_environment(None, vm, env)
        return env

    def test_o_launcher_nao_escreve_vk_driver_files(self):
        for nome, slot in (
            ("NVIDIA GeForce RTX 3050 Laptop GPU", "0000:01:00.0"),
            ("Intel(R) Iris(R) Xe Graphics (ADL GT2)", "0000:00:02.0"),
        ):
            with self.subTest(gpu=nome):
                env = self._env_para(nome, slot)
                self.assertNotIn(
                    "VK_DRIVER_FILES", env,
                    "quem define o ICD é o reims-ctl, pela seção [gpu] do "
                    "vm.toml; escrever aqui cria uma segunda fonte",
                )
                self.assertNotIn("VK_ICD_FILENAMES", env)

    def test_o_launcher_nao_escreve_variaveis_que_ninguem_le(self):
        # `REIMS_VGPU_PCI_SLOT` não é lida no reims-vgpu nem aqui; e
        # `DRI_PRIME`/`__GLX_*`/`__NV_PRIME_*` são de GLX/X11 e Mesa, não do
        # caminho Vulkan que o reims-vgpu usa.
        env = self._env_para("NVIDIA GeForce RTX 3050 Laptop GPU", "0000:01:00.0")
        for morta in (
            "REIMS_VGPU_PCI_SLOT",
            "DRI_PRIME",
            "__NV_PRIME_RENDER_OFFLOAD",
            "__GLX_VENDOR_LIBRARY_NAME",
            "__VK_LAYER_NV_optimus",
        ):
            with self.subTest(variavel=morta):
                self.assertNotIn(morta, env)

    def test_nao_ha_nome_de_icd_inventado_no_codigo_do_launcher(self):
        # Os nomes com sufixo de arquitetura não existem neste host nem no
        # appliance. A docstring de `_apply_gpu_environment` pode citá-los
        # (é o registro do defeito); o que não pode é o código usá-los.
        tui = self.TUI.read_text(encoding="utf-8")
        corpo = tui.split("def _apply_gpu_environment", 1)[1].split("\n    def ", 1)[0]
        codigo = "\n".join(
            linha for linha in corpo.splitlines()
            if not linha.strip().startswith("#")
        )
        codigo = "".join(codigo.split('"""')[0::2])
        for inventado in (
            "intel_icd.x86_64.json",
            "intel_hasvk_icd.x86_64.json",
            "radeon_icd.x86_64.json",
        ):
            with self.subTest(nome=inventado):
                self.assertNotIn(
                    inventado, codigo,
                    f"{inventado} não existe: o manifesto é "
                    f"{inventado.replace('.x86_64', '')}",
                )

    def test_o_launcher_nao_nomeia_manifesto_nenhum(self):
        # Mais forte que a lista acima: o launcher não tem por que conhecer
        # caminho de ICD. Quem resolve isso é o reims-ctl, pela seção [gpu].
        tui = self.TUI.read_text(encoding="utf-8")
        corpo = tui.split("def _apply_gpu_environment", 1)[1].split("\n    def ", 1)[0]
        codigo = "".join(
            "\n".join(
                linha for linha in corpo.splitlines()
                if not linha.strip().startswith("#")
            ).split('"""')[0::2]
        )
        self.assertNotIn("/usr/share/vulkan/icd.d", codigo)

    def test_nao_decide_icd_por_nome_de_fabricante(self):
        tui = self.TUI.read_text(encoding="utf-8")
        trecho = tui.split("def _apply_gpu_environment", 1)
        self.assertEqual(len(trecho), 2, "não achei _apply_gpu_environment")
        corpo = trecho[1].split("\n    def ", 1)[0]
        # A docstring pode (e deve) citar o defeito antigo; o que não pode é
        # haver código comparando fabricante. Tira docstrings e comentários.
        sem_doc = "".join(corpo.split('"""')[0::2])
        codigo = "\n".join(
            linha for linha in sem_doc.splitlines()
            if not linha.strip().startswith("#")
        )
        for fabricante in ("NVIDIA", "INTEL", "AMD"):
            with self.subTest(fabricante=fabricante):
                self.assertNotIn(
                    f'"{fabricante}" in', codigo,
                    f"o ICD não é escolhido por nome de fabricante "
                    f"({fabricante}); a placa é medida, não suposta",
                )

    def test_os_manifests_do_host_nao_tem_sufixo_de_arquitetura(self):
        # O que o host realmente tem, medido: três manifestos, sem sufixo de
        # arquitetura. Este teste falha se a premissa acima virar mentira.
        icd_dir = Path("/usr/share/vulkan/icd.d")
        if not icd_dir.is_dir():
            self.skipTest("host sem /usr/share/vulkan/icd.d")
        reais = {p.name for p in icd_dir.glob("*.json")}
        self.assertIn("nvidia_icd.json", reais)
        self.assertIn("intel_icd.json", reais)
        self.assertEqual(
            [n for n in reais if ".x86_64.json" in n], [],
            f"apareceu manifesto com sufixo de arquitetura: {sorted(reais)}",
        )


if __name__ == "__main__":
    unittest.main()
