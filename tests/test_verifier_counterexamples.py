"""Contraexemplos do verificador independente (adversarial).

Documenta, como teste de REGRESSÃO, o furo encontrado em
`_git_dep_cache_present`: o predicado casava pelo NOME do diretório, então um
`git/db/metal2vulkan-*` **vazio** (rsync interrompido, cópia parcial, ou um
diretório criado por `mkdir -p` antes do conteúdo chegar) fazia a política
automática prometer `CARGO_NET_OFFLINE=true` — e o Cargo morria exatamente com o
erro original do usuário.

Reproduzido com cargo 1.98.1 real, ANTES da correção:
  git_present = True | registry_present = True
  => policy decides CARGO_NET_OFFLINE = true
  cargo metadata (offline) -> rc=101
  error: failed to get `metal2vulkan` as a dependency ...
    can't checkout from 'https://github.com/steelbrain/metal2vulkan':
    you are in the offline mode (--offline)

Depois da correção o predicado exige a forma de um repositório bare
(`objects/` + `refs/`), e estes testes passaram a afirmar o comportamento
CORRETO — eles falham se o furo voltar.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import reims_build, reims_release  # noqa: E402

REPO = "metal2vulkan-ec622ce80a554d9d"


class EmptyGitDbFalsePositiveTests(unittest.TestCase):
    """Um `db` sem conteúdo não pode ser aceito como cache válido."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vhack-verify-"))

    def _home(self, *, db_files: dict[str, str] | None = None) -> Path:
        home = self.root / "cargo"
        db = home / "git" / "db" / REPO
        db.mkdir(parents=True)
        for name, content in (db_files or {}).items():
            (db / name).write_text(content)
        (home / "registry" / "index").mkdir(parents=True)
        (home / "registry" / "cache").mkdir(parents=True)
        return home

    def test_empty_db_dir_is_rejected(self):
        """CORRIGIDO: dir vazio não satisfaz mais o predicado nem liga offline."""
        home = self._home()
        self.assertFalse(reims_build._git_dep_cache_present(home))
        self.assertTrue(reims_build._registry_cache_present(home))
        env = {"CARGO_HOME": str(home)}
        decision = reims_build._apply_cargo_offline_policy(env, self.root)
        # Com o cache git inválido, a política é honesta: usa rede.
        self.assertEqual(decision["CARGO_NET_OFFLINE"], "false")

    def test_db_with_only_HEAD_is_rejected(self):
        home = self._home(db_files={"HEAD": "ref: refs/heads/main\n"})
        self.assertFalse(reims_build._git_dep_cache_present(home))

    def test_db_with_unrelated_junk_is_rejected(self):
        home = self._home(db_files={"junk": "x"})
        self.assertFalse(reims_build._git_dep_cache_present(home))

    def test_db_with_bare_repo_layout_is_accepted(self):
        """A forma mínima de um repo bare (`objects/` + `refs/`) conta."""
        home = self.root / "ok"
        repo = home / "git" / "db" / REPO
        (repo / "objects").mkdir(parents=True)
        (repo / "refs").mkdir(parents=True)
        self.assertTrue(reims_build._git_dep_cache_present(home))

    def test_file_named_like_repo_is_rejected(self):
        """O `if d.is_dir()` evita o caso de arquivo solto (este funciona)."""
        home = self.root / "c2"
        d = home / "git" / "db"
        d.mkdir(parents=True)
        (d / REPO).write_text("not a dir")
        self.assertFalse(reims_build._git_dep_cache_present(home))

    def test_required_deps_cover_both_git_crates(self):
        """metal2vulkan-runtime e metal2vulkan compartilham URL => 1 db basta."""
        self.assertEqual(reims_build._REQUIRED_GIT_DEPS, ("metal2vulkan",))


class HealConvergesOnObjectsAsFileTests(unittest.TestCase):
    """DEFECT-4 (CORRIGIDO): a cura converge quando `objects` é um ARQUIVO.

    Antes da correção, `_seed_cache_tree` só removia o destino quando ele era um
    DIRETÓRIO vazio. Se o resquício fosse um `objects` de 0 byte (arquivo), o
    `copytree` estourava `Errno 17 File exists` no meio do merge: copiava
    `refs/`, `HEAD`, `config`... e deixava `objects` como arquivo. O predicado
    continuava False, então a política era honesta (`CARGO_NET_OFFLINE=false`,
    usa rede) — NÃO recriava o rc=101 do usuário — mas a cura nunca convergia:
    repetir o seed não consertava, porque o destino virava um diretório
    não-vazio e a limpeza de "vazio" nunca mais disparava.

    Correção: `_clear_blocking_files` remove recursivamente arquivos que
    bloqueiam a cópia de um diretório do payload, antes do `copytree`.

    Verificado com cargo 1.98.1 real após a correção: seed => True, pred=True,
    `cargo metadata --offline` => rc=0.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vhack-heal-"))
        self.payload = self.root / "payload-git"
        repo = self.payload / "git" / "db" / REPO
        (repo / "objects").mkdir(parents=True)
        (repo / "refs").mkdir(parents=True)
        (repo / "HEAD").write_text("ref: refs/heads/main\n")
        self.home = self.root / "cargo"
        poisoned = self.home / "git" / "db" / REPO
        poisoned.mkdir(parents=True)
        # O resquício exato: um `objects` que é ARQUIVO de 0 byte.
        (poisoned / "objects").write_text("")
        self.env = {"CARGO_HOME": str(self.home)}

    def _seed(self) -> bool:
        with patch.object(reims_build, "GIT_CACHE_PAYLOAD", self.payload), patch.object(
            reims_build, "REGISTRY_CACHE_PAYLOAD", self.root / "no-registry"
        ):
            return reims_build.seed_cargo_git_cache(self.env)

    def test_predicate_rejects_objects_as_file(self):
        """O predicado rejeita (não promete offline mentindo)."""
        self.assertFalse(reims_build._git_dep_cache_present(self.home))

    def test_policy_refuses_offline_so_it_degrades_to_network(self):
        """A propriedade que mais importa: degrada para rede, não para rc=101."""
        decision = reims_build._apply_cargo_offline_policy(dict(self.env), self.root)
        self.assertEqual(decision["CARGO_NET_OFFLINE"], "false")

    def test_heal_converges_on_objects_as_file(self):
        """CORRIGIDO: o seed substitui o arquivo pelo diretório e converge."""
        self.assertTrue(self._seed())
        self.assertTrue(reims_build._git_dep_cache_present(self.home))
        self.assertTrue((self.home / "git" / "db" / REPO / "objects").is_dir())
        # Convergiu: repetir é estável e não regride.
        self.assertTrue(self._seed())
        self.assertTrue(reims_build._git_dep_cache_present(self.home))

    def test_nonempty_dir_shaped_poison_is_repaired(self):
        """`objects/` com lixo dentro: o merge por cima também cura."""
        home = self.root / "c2"
        repo = home / "git" / "db" / REPO
        (repo / "objects").mkdir(parents=True)
        (repo / "objects" / "junk").write_text("x")
        with patch.object(reims_build, "GIT_CACHE_PAYLOAD", self.payload), patch.object(
            reims_build, "REGISTRY_CACHE_PAYLOAD", self.root / "no-registry"
        ):
            reims_build.seed_cargo_git_cache({"CARGO_HOME": str(home)})
        # O merge trouxe `objects/` e `refs/` reais; o predicado passa a valer.
        self.assertTrue(reims_build._git_dep_cache_present(home))
        self.assertTrue((repo / "refs").is_dir())

    def test_legit_bare_repo_is_not_disturbed(self):
        """Um cache GENUÍNO continua intocado pelo reparo (não-clobber)."""
        home = self.root / "real"
        repo = home / "git" / "db" / REPO
        (repo / "objects").mkdir(parents=True)
        (repo / "refs").mkdir(parents=True)
        marker = repo / "objects" / "HOSTILE_USER_OBJECT"
        marker.write_text("meu dado")
        with patch.object(reims_build, "GIT_CACHE_PAYLOAD", self.payload), patch.object(
            reims_build, "REGISTRY_CACHE_PAYLOAD", self.root / "no-registry"
        ):
            reims_build.seed_cargo_git_cache({"CARGO_HOME": str(home)})
        self.assertTrue(reims_build._git_dep_cache_present(home))
        self.assertTrue(marker.is_file(), "dado genuíno do usuário foi apagado")


class ReverseDirectionPoisonTests(unittest.TestCase):
    """DEFECT-5 (CORRIGIDO): payload tem ARQUIVO onde o destino tem DIRETÓRIO.

    `_clear_blocking_files` só removia quando o payload era **diretório** e o
    alvo não. Na direção reversa (payload `HEAD` é arquivo, destino `HEAD` é
    diretório) nada era removido e o `copytree(dirs_exist_ok=True)` **não
    estourava**: ele simplesmente pulava o arquivo bloqueado. O merge terminava
    "com sucesso" com o repositório incompleto, `_git_dep_cache_present` voltava
    True (o predicado checa FORMA, não conteúdo), a política prometia
    `CARGO_NET_OFFLINE=true`, e o cargo real voltava ao erro original rc=101.

    Correção: a direção reversa também remove o diretório bloqueador, para o
    merge reproduzir fielmente o payload.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vhack-rev-"))
        self.payload = self.root / "payload"
        repo = self.payload / "git" / "db" / REPO
        (repo / "objects").mkdir(parents=True)
        (repo / "refs").mkdir(parents=True)
        (repo / "HEAD").write_text("ref: refs/heads/main\n")  # ARQUIVO no payload
        self.home = self.root / "cargo"
        poisoned = self.home / "git" / "db" / REPO
        (poisoned / "objects").mkdir(parents=True)
        (poisoned / "refs").mkdir(parents=True)
        (poisoned / "HEAD").mkdir()  # DIRETÓRIO no destino: bloqueia o merge
        self.env = {"CARGO_HOME": str(self.home)}

    def _seed(self) -> bool:
        with patch.object(reims_build, "GIT_CACHE_PAYLOAD", self.payload), patch.object(
            reims_build, "REGISTRY_CACHE_PAYLOAD", self.root / "no-registry"
        ):
            return reims_build.seed_cargo_git_cache(self.env, log=lambda _m: None)

    def test_blocking_dir_is_repaired(self):
        """CORRIGIDO: o diretório bloqueador é removido e vira o arquivo."""
        self.assertTrue(self._seed())
        repo = self.home / "git" / "db" / REPO
        self.assertTrue((repo / "HEAD").is_file(), "bloqueio deveria ter sido removido")

    def test_merge_produces_real_repo_content(self):
        """O `copytree` deixa de pular em silêncio: o conteúdo real chega."""
        self._seed()
        repo = self.home / "git" / "db" / REPO
        self.assertEqual((repo / "HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertTrue((repo / "objects").is_dir())
        self.assertTrue((repo / "refs").is_dir())

    def test_predicate_is_not_fooled_into_promising_offline(self):
        """A política só promete offline sobre um cache git REALMENTE restaurado.

        O cenário realista é registry completo + `git/db` com a direção reversa
        envenenada. Antes, a política prometia `true` com o repositório vazio e
        o cargo morria rc=101. Agora o merge restaurou o conteúdo de verdade.
        """
        self._seed()
        self.assertTrue(reims_build._git_dep_cache_present(self.home))
        # `_registry_cache_present` exige index+cache DENTRO do cargo_home.
        (self.home / "registry" / "index").mkdir(parents=True, exist_ok=True)
        (self.home / "registry" / "cache").mkdir(parents=True, exist_ok=True)
        decision = reims_build._apply_cargo_offline_policy(dict(self.env), self.root)
        # `true` agora é uma promessa HONESTA: o repo foi mesmo restaurado.
        self.assertEqual(decision["CARGO_NET_OFFLINE"], "true")
        repo = self.home / "git" / "db" / REPO
        self.assertTrue((repo / "HEAD").is_file())

    def test_symlink_poison_is_removed_and_does_not_escape(self):
        """Contra-prova: a direção symlink ESTÁ correta (não segue o link)."""
        root = Path(tempfile.mkdtemp(prefix="vhack-sym-"))
        outside = root / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("FORA")
        home = root / "cargo"
        repo = home / "git" / "db" / REPO
        repo.mkdir(parents=True)
        (repo / "objects").symlink_to(outside, target_is_directory=True)
        (repo / "refs").mkdir()
        with patch.object(reims_build, "GIT_CACHE_PAYLOAD", self.payload), patch.object(
            reims_build, "REGISTRY_CACHE_PAYLOAD", root / "no"
        ):
            reims_build.seed_cargo_git_cache(
                {"CARGO_HOME": str(home)}, log=lambda _m: None
            )
        self.assertFalse((repo / "objects").is_symlink())
        self.assertTrue((repo / "objects").is_dir())
        self.assertTrue((outside / "sentinel").is_file(), "não pode deletar fora")


class ReverseDirectionDeletesUserDataTests(unittest.TestCase):
    """DEFECT-6 (ABERTO, risco de não-clobber): a direção reversa apaga dados.

    A correção do DEFECT-5 remove o diretório bloqueador com `shutil.rmtree`.
    Isso é o certo para um resquício de rsync, MAS `_seed_cache_tree` chama
    `_clear_blocking_files` em **todo** `copytree`, inclusive quando o cache já
    é um repositório bare VÁLIDO (`_git_dep_cache_present` já retornava True).
    Logo: um diretório com conteúdo genuíno do usuário, colocado onde o payload
    tem um ARQUIVO, é apagado inteiro — sem backup e sem confirmação.

    Nomes de ARQUIVO no topo de um payload bare real (verificado no host):
    `FETCH_HEAD`, `HEAD`, `config`, `description`. Qualquer diretório nesses
    nomes dentro de um `git/db/<repo>` legítimo é destruído no próximo seed.

    Verificado: 50 arquivos em `HEAD/` => diretório removido e dados perdidos;
    `config/` com trabalho do usuário dentro de um repo com pred=True => perdido.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vhack-clob-"))
        self.payload = self.root / "payload"
        repo = self.payload / "git" / "db" / REPO
        (repo / "objects").mkdir(parents=True)
        (repo / "refs").mkdir(parents=True)
        (repo / "HEAD").write_text("payload-head\n")
        (repo / "config").write_text("payload-config\n")
        self.env = {"CARGO_HOME": str(self.root / "cargo")}

    def _seed(self) -> bool:
        with patch.object(reims_build, "GIT_CACHE_PAYLOAD", self.payload), patch.object(
            reims_build, "REGISTRY_CACHE_PAYLOAD", self.root / "no"
        ):
            return reims_build.seed_cargo_git_cache(self.env, log=lambda _m: None)

    def test_valid_repo_predicate_is_true_before_seed(self):
        """O ponto central: a limpeza roda mesmo com o cache JÁ válido."""
        repo = Path(self.env["CARGO_HOME"]) / "git" / "db" / REPO
        (repo / "objects").mkdir(parents=True)
        (repo / "refs").mkdir(parents=True)
        (repo / "HEAD").write_text("user-head\n")
        (repo / "config").mkdir()
        (repo / "config" / "user_work.txt").write_text("TRABALHO")
        self.assertTrue(reims_build._git_dep_cache_present(Path(self.env["CARGO_HOME"])))

    def test_user_dir_at_a_payload_file_name_is_deleted(self):
        """DEFEITO ABERTO: dados do usuário apagados dentro de um cache válido."""
        repo = Path(self.env["CARGO_HOME"]) / "git" / "db" / REPO
        (repo / "objects").mkdir(parents=True)
        (repo / "refs").mkdir(parents=True)
        (repo / "HEAD").write_text("user-head\n")
        (repo / "config").mkdir()
        for i in range(50):
            (repo / "config" / f"user{i}.txt").write_text(f"DADO{i}")
        self.assertTrue(self._seed())
        # A correção de DEFECT-5 apaga o diretório inteiro, com os 50 arquivos.
        self.assertFalse((repo / "config" / "user0.txt").exists())
        self.assertTrue((repo / "config").is_file())

    def test_no_symlink_escape_when_target_is_a_link(self):
        """Contra-prova: symlink NÃO é seguido (dados externos preservados)."""
        root = Path(tempfile.mkdtemp(prefix="vhack-esc-"))
        outside = root / "OUTSIDE"
        outside.mkdir()
        (outside / "precious.txt").write_text("P")
        home = root / "cargo"
        repo = home / "git" / "db" / REPO
        (repo / "objects").mkdir(parents=True)
        (repo / "refs").mkdir(parents=True)
        (repo / "HEAD").symlink_to(outside, target_is_directory=True)
        with patch.object(reims_build, "GIT_CACHE_PAYLOAD", self.payload), patch.object(
            reims_build, "REGISTRY_CACHE_PAYLOAD", root / "no"
        ):
            reims_build.seed_cargo_git_cache(
                {"CARGO_HOME": str(home)}, log=lambda _m: None
            )
        self.assertTrue((outside / "precious.txt").is_file(), "não pode deletar fora")


class PayloadSymlinkIsTraversedTests(unittest.TestCase):
    """DEFECT-7 (ABERTO): symlink no PAYLOAD é seguido e vira árvore real.

    `_clear_blocking_files` decide recursar com `entry.is_dir()`, que **segue**
    symlinks. Para um symlink-para-diretório no payload, `entry.is_dir()` é True
    e o alvo é um diretório (recém-criado), então a recursão entra pelo link,
    volta para dentro do payload e **materializa uma árvore de diretórios reais**
    em `dest`. Depois o `copytree(..., symlinks=True)` encontra um diretório real
    e para de reproduzir o symlink.

    Medido (payload com `sub/loop -> sub`):
      stdlib `copytree` sozinho .................... 2 entradas, loop = symlink
      `_clear_blocking_files` sozinho .............. 0 entradas criadas
      `_seed_cache_tree` (os dois juntos) .......... 42 entradas, loop = DIR
      com a guarda `and not entry.is_symlink()` .... 2 entradas, loop = symlink

    Cresce com a profundidade (medido até 60 níveis no destino) e não é um
    `RecursionError`: é cópia silenciosa de uma árvore fantasma.

    Impacto no payload real: `~/.cargo/git/db` e `registry/` deste host têm ZERO
    symlinks; os 13 symlinks de `~/.cargo` estão em `bin/` (rustup shims), que
    não é empacotado. Logo é um risco latente, não um defeito ativo hoje.
    """

    def _loop_payload(self, root: Path) -> Path:
        src = root / "payload"
        (src / "sub").mkdir(parents=True)
        (src / "sub" / "loop").symlink_to(src / "sub", target_is_directory=True)
        return src

    def test_payload_symlink_should_stay_a_symlink(self):
        """DEFEITO-7 CORRIGIDO: o symlink é preservado, não materializado.

        A asserção foi invertida quando a correção entrou. Segue sendo teste de
        regressão: se a guarda `and not entry.is_symlink()` sumir de
        `_merge_tree`, isto volta a falhar.
        """
        root = Path(tempfile.mkdtemp(prefix="vhack-sloop-"))
        src = self._loop_payload(root)
        dest = root / "dest"
        dest.mkdir()
        reims_build._seed_cache_tree(src, dest, lambda _m: None)
        # Comportamento correto: continua symlink, como no copytree puro.
        self.assertTrue(
            (dest / "sub" / "loop").is_symlink(),
            "DEFECT-7 reaberto: o symlink virou diretório real",
        )
        # E resolve para o MESMO ponto no destino (o diretório que o contém),
        # sem apontar de volta para o payload — que na ISO não existe.
        alvo = (dest / "sub" / "loop").resolve()
        self.assertEqual(alvo, (dest / "sub").resolve())
        self.assertNotIn(str(src.resolve()), str((dest / "sub" / "loop").readlink()))

    def test_absolute_symlink_out_of_its_directory_is_rewritten(self):
        """Regressão: link absoluto que sai do diretório corrente.

        `_merge_tree` passava o diretório corrente como raiz do payload, então um
        link apontando para um irmão ou avô não era reconhecido como interno e
        era copiado cru — apontando para o payload temporário, que não existe na
        ISO.
        """
        root = Path(tempfile.mkdtemp(prefix="vhack-slink-"))
        src = root / "payload"
        (src / "a" / "b").mkdir(parents=True)
        (src / "c").mkdir()
        (src / "a" / "b" / "x").symlink_to(src / "c", target_is_directory=True)
        dest = root / "dest"
        dest.mkdir()
        reims_build._seed_cache_tree(src, dest, lambda _m: None)

        link = dest / "a" / "b" / "x"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (dest / "c").resolve())
        # O link gravado tem de ser relativo e não citar o payload temporário:
        # um caminho absoluto para `payload/` funcionaria aqui e quebraria na ISO.
        raw = os.readlink(link)
        self.assertFalse(os.path.isabs(raw), f"link absoluto: {raw}")
        self.assertNotIn(str(src), raw)

    def test_stdlib_copytree_alone_is_correct(self):
        """Contra-prova: o stdlib faz certo; o defeito é da recursão própria."""
        root = Path(tempfile.mkdtemp(prefix="vhack-sl2-"))
        src = self._loop_payload(root)
        dest = root / "dest"
        dest.mkdir()
        shutil.copytree(src, dest, dirs_exist_ok=True, symlinks=True)
        self.assertTrue((dest / "sub" / "loop").is_symlink())

    def test_recursion_is_bounded_not_infinite(self):
        """DEFEITO-7 CORRIGIDO: a árvore não incha seguindo o symlink.

        Antes da correção o mesmo payload produzia 42 entradas (o symlink era
        seguido como diretório); agora produz 2, fiel ao payload.
        """
        root = Path(tempfile.mkdtemp(prefix="vhack-sl3-"))
        src = self._loop_payload(root)
        dest = root / "dest"
        dest.mkdir()
        reims_build._seed_cache_tree(src, dest, lambda _m: None)
        entries = sum(len(d) + len(f) for _r, d, f in os.walk(dest))
        # Fiel ao payload (`sub/` + o symlink): 2. O inchaço era 42.
        self.assertEqual(entries, 2, f"árvore fantasma: {entries} entradas")


class OfflinePromiseIsNonVacuousTests(unittest.TestCase):
    """Contra-prova (pedida pelo lead): a política não promete offline "de graça".

    Sem o cache git o resultado TEM de virar `false`. Sem este teste, o assert
    `== "true"` do cenário curado poderia passar por acidente (variável herdada,
    predicado sempre-verdadeiro), escondendo uma promessa falsa.
    """

    def test_missing_git_payload_forces_network(self):
        root = Path(tempfile.mkdtemp(prefix="vhack-nv-"))
        home = root / "cargo"
        (home / "registry" / "index").mkdir(parents=True)
        (home / "registry" / "cache").mkdir(parents=True)
        with patch.object(reims_build, "GIT_CACHE_PAYLOAD", root / "nonexistent"):
            decision = reims_build._apply_cargo_offline_policy(
                {"CARGO_HOME": str(home)}, root
            )
        self.assertEqual(decision["CARGO_NET_OFFLINE"], "false")

    def test_empty_git_db_forces_network_too(self):
        """Mesmo com registry completo, um `git/db` vazio não promete offline."""
        root = Path(tempfile.mkdtemp(prefix="vhack-nv2-"))
        home = root / "cargo"
        (home / "registry" / "index").mkdir(parents=True)
        (home / "registry" / "cache").mkdir(parents=True)
        (home / "git" / "db" / REPO).mkdir(parents=True)
        decision = reims_build._apply_cargo_offline_policy(
            {"CARGO_HOME": str(home)}, root
        )
        self.assertEqual(decision["CARGO_NET_OFFLINE"], "false")
        self.assertFalse(reims_build._git_dep_cache_present(home))


class _RomInstallBase(unittest.TestCase):
    """Base: monta uma árvore de release pré-compilada mínima e isolada."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        raiz = Path(self.tmp.name)
        self.reims = raiz / "release"
        self.destino = raiz / "dados-do-usuario"
        build = self.reims / "vendor" / "qemu" / "build"
        (build / "bin").mkdir(parents=True)
        binario = build / "bin" / "qemu-system-x86_64"
        binario.write_bytes(b"#!/bin/sh\n")
        binario.chmod(0o755)
        (self.reims / "vendor" / "qemu" / "pc-bios").mkdir(parents=True)
        (self.reims / "vendor" / "qemu" / "pc-bios" / "bios-256k.bin").write_bytes(b"b")

    def _instalar(self) -> Path:
        with patch.object(reims_build, "qemu_data_dir", return_value=self.destino):
            ok = reims_build.install_prebuilt(self.reims)
        self.assertTrue(ok, "install_prebuilt devolveu False")
        return self.destino

    def _rom_onde_o_qemu_procura(self) -> Path:
        """O caminho derivado SÓ da posição do binário, como o QEMU faz."""
        binarios = sorted(self.destino.rglob("qemu-system-x86_64"))
        self.assertTrue(binarios, "nenhum binário instalado")
        return binarios[0].parent.parent / "share" / "qemu" / "ReimsVgpu.rom"


class RomReachesTheQemuDataDirTests(_RomInstallBase):
    """Contraexemplo ao furo do MUT L3: o loop pode mandar a ROM para o vazio.

    Achado do verificador, medido com a suíte inteira verde (64 passed) sob a
    mutação:

        (reims_dir/"vendor"/"qemu"/"build"/"share"/"qemu",
         target_dir/"share"/"reims-vgpu-qemu"/"share"/"qemu")

    A ROM continuava sendo COPIADA — só que para `<prefixo>/share/reims-vgpu-
    qemu/share/qemu/`, um nível a mais do que o QEMU procura. Sintoma: tela
    preta, sem erro. É o mesmo defeito de layout do `build-iso.sh` original,
    reintroduzido pelo OUTRO caminho de cópia (o loop, não o `rom_src`).

    Os testes do produto afirmam a posição da ROM nos caminhos explícitos
    (`prebuilt_rom`/`rom_src`); nenhum deles fixa o destino do loop quando o
    loop é o ÚNICO caminho que traz a ROM.
    """

    def test_rom_do_loop_chega_no_share_qemu_do_prefixo(self) -> None:
        # A ROM existe SÓ dentro do diretório varrido pelo loop, e o caminho
        # explícito é neutralizado — então quem tem de entregá-la é o loop.
        rom_loop = self.reims / "vendor" / "qemu" / "build" / "share" / "qemu"
        rom_loop.mkdir(parents=True)
        (rom_loop / "ReimsVgpu.rom").write_bytes(b"ROM-PELO-LOOP")

        with patch.object(reims_build, "prebuilt_rom", return_value=None):
            self._instalar()

        alvo = self._rom_onde_o_qemu_procura()
        self.assertTrue(
            alvo.is_file(),
            "a ROM nunca chegou a <prefixo>/share/qemu/: o loop a copiou para "
            f"outro lugar. Arquivos encontrados: "
            f"{[str(p.relative_to(self.destino)) for p in self.destino.rglob('ReimsVgpu.rom')]}",
        )
        self.assertEqual(alvo.read_bytes(), b"ROM-PELO-LOOP")

    def test_a_rom_do_loop_nao_e_duplicada_em_subdiretorio_aninhado(self) -> None:
        """Trava o layout: nada de `<prefixo>/share/reims-vgpu-qemu/...`."""
        rom_loop = self.reims / "vendor" / "qemu" / "build" / "share" / "qemu"
        rom_loop.mkdir(parents=True)
        (rom_loop / "ReimsVgpu.rom").write_bytes(b"ROM-PELO-LOOP")

        with patch.object(reims_build, "prebuilt_rom", return_value=None):
            self._instalar()

        aninhada = [
            str(p.relative_to(self.destino))
            for p in self.destino.rglob("ReimsVgpu.rom")
            if "reims-vgpu-qemu" in p.parts
        ]
        self.assertEqual(
            aninhada,
            [],
            f"a ROM foi parar num prefixo aninhado; o QEMU não olha ali: {aninhada}",
        )


class AmbiguousRomCandidatesTests(_RomInstallBase):
    """Item 2(c): com vários `ReimsVgpu.rom`, quem decide é o `sorted()`.

    `prebuilt_rom` testa 3 caminhos explícitos antes do `rglob`; a ambiguidade
    só aparece quando NENHUM deles casa e existem 2+ arquivos com o mesmo nome.
    Aí a ordem lexicográfica decide — e é uma escolha silenciosa. Estes testes
    fixam QUAL é a escolha, para que uma mudança de comportamento apareça.
    """

    def _arvore_sem_caminhos_explicitos(self, com_vendor: bool) -> None:
        """Remove os 3 caminhos explícitos, deixando só candidatos ambíguos."""
        for rel in (
            Path("vendor/qemu/build/share/qemu"),
            Path("vendor/qemu/pc-bios"),
            Path("vendor/qemu/build"),
        ):
            alvo = self.reims / rel / "ReimsVgpu.rom"
            if alvo.is_file():
                alvo.unlink()
        if com_vendor:
            (self.reims / "vendor" / "aaa").mkdir(parents=True, exist_ok=True)
            (self.reims / "vendor" / "aaa" / "ReimsVgpu.rom").write_bytes(b"vendor-aaa")
        (self.reims / "outro" / "nested").mkdir(parents=True, exist_ok=True)
        (self.reims / "outro" / "nested" / "ReimsVgpu.rom").write_bytes(b"outro-nested")

    def test_multiplos_candidatos_escolhem_o_lexicograficamente_menor(self) -> None:
        # 'outro/...' < 'vendor/...' lexicograficamente.
        self._arvore_sem_caminhos_explicitos(com_vendor=True)
        escolhida = reims_build.prebuilt_rom(self.reims)
        self.assertIsNotNone(escolhida)
        rel = str(escolhida.relative_to(self.reims))
        self.assertEqual(
            rel,
            "outro/nested/ReimsVgpu.rom",
            "a escolha entre candidatos ambíguos mudou; era a lexicograficamente "
            f"menor. Escolheu: {rel}",
        )

    def test_caminho_explicito_vence_qualquer_candidato_ambiguo(self) -> None:
        """A garantia que importa: o explícito tem precedência sobre o `rglob`.

        Sem isto, um `ReimsVgpu.rom` qualquer numa subárvore arbitrária poderia
        sequestrar a escolha mesmo existindo a ROM no lugar canônico.
        """
        self._arvore_sem_caminhos_explicitos(com_vendor=False)
        canonico = self.reims / "vendor" / "qemu" / "pc-bios" / "ReimsVgpu.rom"
        canonico.parent.mkdir(parents=True, exist_ok=True)
        canonico.write_bytes(b"CANONICA")

        escolhida = reims_build.prebuilt_rom(self.reims)
        self.assertIsNotNone(escolhida)
        self.assertEqual(
            escolhida.read_bytes(),
            b"CANONICA",
            f"o rglob sequestrou a escolha: {escolhida}",
        )


class LegacyGopRomNeverBecomesTheInstalledRomTests(_RomInstallBase):
    """Item 3: `legacy_gop_rom` não pode acabar instalada como `ReimsVgpu.rom`.

    As duas ROMs são firmwares diferentes (a antiga tem 26112 B e sha256
    02cf046f…; a atual, 5632 B e 2165aa4c…) e o QEMU aceita as duas como
    `romfile` SEM reclamar. Renomear a antiga produziria VM com o firmware
    errado e nenhum erro — a tela preta silenciosa.
    """

    def test_instalacao_com_apenas_a_rom_legada_nao_cria_reims_vgpu_rom(self) -> None:
        antiga = self.reims / "crates" / "reims-vgpu-efi" / "out"
        antiga.mkdir(parents=True)
        (antiga / "reims-vgpu-gop.rom").write_bytes(b"GOP-ANTIGA-26112")

        # A árvore não tem a ROM atual em lugar nenhum.
        self.assertIsNone(reims_build.prebuilt_rom(self.reims))

        self._instalar()

        self.assertFalse(
            self._rom_onde_o_qemu_procura().is_file(),
            "a ROM GOP antiga foi instalada renomeada como ReimsVgpu.rom — o "
            "QEMU carregaria o firmware errado sem erro nenhum",
        )
        # E o binário, que É válido, continua sendo instalado.
        self.assertTrue(sorted(self.destino.rglob("qemu-system-x86_64")))

    def test_a_rom_legada_nunca_e_devolvida_por_prebuilt_rom(self) -> None:
        antiga = self.reims / "crates" / "reims-vgpu-efi" / "out"
        antiga.mkdir(parents=True)
        (antiga / "reims-vgpu-gop.rom").write_bytes(b"GOP-ANTIGA")
        self.assertIsNone(reims_build.prebuilt_rom(self.reims))
        self.assertIsNotNone(reims_build.legacy_gop_rom(self.reims))
        self.assertNotEqual(
            reims_build.legacy_gop_rom(self.reims),
            reims_build.prebuilt_rom(self.reims),
        )

    def test_a_rom_legada_nao_sequestra_a_escolha_quando_a_atual_existe(self) -> None:
        """As duas presentes: a atual ganha, a antiga é só diagnóstico."""
        antiga = self.reims / "crates" / "reims-vgpu-efi" / "out"
        antiga.mkdir(parents=True)
        (antiga / "reims-vgpu-gop.rom").write_bytes(b"GOP-ANTIGA")
        atual_dir = self.reims / "vendor" / "qemu" / "pc-bios"
        atual_dir.mkdir(parents=True, exist_ok=True)
        (atual_dir / "ReimsVgpu.rom").write_bytes(b"ATUAL-5632")

        escolhida = reims_build.prebuilt_rom(self.reims)
        self.assertIsNotNone(escolhida)
        self.assertEqual(escolhida.read_bytes(), b"ATUAL-5632")

        self._instalar()
        self.assertEqual(self._rom_onde_o_qemu_procura().read_bytes(), b"ATUAL-5632")


class BinaryRomNameHeuristicTests(unittest.TestCase):
    """Adversarial: `_binary_wants_the_new_rom_name` decide pelo CONTEÚDO.

    O SINAL MUDOU (round 12). Antes era o nome da ROM (`ReimsVgpu.rom`), um
    sinal apenas NEGATIVO: "sem a string" e "geração antiga" eram
    indistinguíveis, e o buraco era real — binário novo com a string do
    `romfile` removida + ROM antiga era ACEITO em silêncio. Agora o sinal é o
    NOME DO DEVICE compilado, que responde nos DOIS sentidos.

    Decisão medida (verificador, binários reais):

      v1.0.1 real (11.0.50)  apple-gfx-pci=False  reims-vgpu-pci=True   -> antigo
      atual      (11.1.50)   apple-gfx-pci=True   reims-vgpu-pci=False  -> novo
      atual c/ string do romfile sabotada  apple-gfx-pci=True           -> novo

    A terceira linha é a razão da troca, medida com `strip` GNU de verdade:
    `strip` NÃO remove nenhuma das duas strings (vivem em `.rodata` alocado;
    o `strip` tira símbolo e depuração, não conteúdo). O que faltava era um
    sinal positivo, não sobrevivência a um apagamento.

    Modo de falha de cada lado:
      falso POSITIVO (`"novo"` num binário antigo) -> recusa release boa
      falso NEGATIVO (`"antigo"` num binário novo) -> TELA PRETA silenciosa
    `None` — nenhum dos dois nomes, ou os DOIS (ambíguo) — recusa nos dois
    ramos do `_verify_tree`, então o lado desconhecido erra para o conservador.
    Um binário real nunca é ambíguo (medido: o antigo tem 0 `apple-gfx-pci`, o
    novo tem 0 `reims-vgpu-pci`); a regra existe para não escolher por ordem.
    """

    def _binario(self, conteudo: bytes) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "qemu-system-x86_64"
        p.write_bytes(conteudo)
        return p

    def test_device_novo_implica_geracao_nova(self) -> None:
        p = self._binario(b"\x7fELF" + b"\x00apple-gfx-pci\x00display-modes\x00")
        self.assertTrue(reims_release._binary_wants_the_new_rom_name(p))

    def test_device_antigo_implica_geracao_antiga(self) -> None:
        # O binário antigo real: `reims-vgpu-pci` presente, `apple-gfx-pci` não.
        p = self._binario(
            b"\x7fELF\x00reims-vgpu-pci\x00ReimsVgpuDirty\x00ReimsVgpuHostOps\x00"
        )
        self.assertFalse(
            reims_release._binary_wants_the_new_rom_name(p),
            "o device antigo (`reims-vgpu-pci`) não carrega `ReimsVgpu.rom`; "
            "tratá-lo como novo recusaria a release boa",
        )

    def test_binario_ilegivel_recusa_nos_dois_ramos(self) -> None:
        """Sem poder ler, o `_verify_tree` tem de recusar — nos DOIS sentidos.

        A função devolve `False` (não é "novo"), mas a garantia de recusar não
        mora no valor booleano: mora no `None` do `_device_of_this_binary`, que
        faz o `_verify_tree` abortar tanto com ROM nova quanto com ROM antiga.
        Por isso o teste assere os dois níveis — o valor e o efeito.
        """
        inexistente = Path(tempfile.mkdtemp()) / "nao-existe"
        self.assertFalse(inexistente.exists())
        self.assertIsNone(
            reims_release._device_of_this_binary(inexistente),
            "binário ilegível não tem geração identificável (`None`)",
        )
        self.assertFalse(
            reims_release._binary_wants_the_new_rom_name(inexistente),
            "contra-intuitivo mas correto: a recusa vem do `None`, não deste "
            "booleano — o lado conservador é o `_verify_tree`",
        )

    def test_ambiguidade_recusa_em_vez_de_escolher_por_ordem(self) -> None:
        """Os dois nomes no mesmo binário -> `None` -> recusa.

        Escolher por ordem de `if` daria "novo" por acidente de implementação
        para um binário que pode ser antigo — falso positivo que aceita
        firmware errado. Sem sinal inequívoco, o `None` faz o `_verify_tree`
        recusar nos dois ramos. Este teste fixa a escolha; se alguém voltar ao
        `if/elif`, ele cai.
        """
        p = self._binario(b"\x7fELF\x00apple-gfx-pci\x00reims-vgpu-pci\x00")
        self.assertIsNone(
            reims_release._device_of_this_binary(p),
            "ambíguo não pode escolher um lado por ordem de `if`",
        )
        self.assertFalse(
            reims_release._binary_wants_the_new_rom_name(p),
            "sem saber a geração, a resposta é recusar",
        )

    def test_ambiguidade_com_o_antigo_escrito_primeiro(self) -> None:
        """A ordem dos bytes não muda o veredito — o que importa é o conjunto.

        Sem este caso, uma implementação que devolvesse `"antigo"` só porque
        `reims-vgpu-pci` vem antes passaria no teste anterior. Aqui os dois
        nomes aparecem nas duas ordens e o resultado tem de ser o mesmo: `None`.
        """
        for dados in (
            b"\x7fELF\x00reims-vgpu-pci\x00apple-gfx-pci\x00",
            b"\x7fELF\x00apple-gfx-pci\x00reims-vgpu-pci\x00",
        ):
            with self.subTest(ordem=dados[6:20]):
                self.assertIsNone(
                    reims_release._device_of_this_binary(self._binario(dados))
                )

    def test_silencio_total_recusa_em_vez_de_adivinhar(self) -> None:
        """Nenhum dos dois nomes -> `None` -> o `_verify_tree` recusa.

        É o limite documentado: um binário empacotado de forma exótica cai
        aqui. A escolha é recusar (falso alarme) em vez de adivinhar um lado
        (risco de tela preta). O teste cobre os dois níveis, o `None` da
        detecção e a recusa que ele produz.
        """
        p = self._binario(b"\x7fELF\x00display-modes\x00efi-framebuffer-bar\x00")
        self.assertIsNone(reims_release._device_of_this_binary(p))
        self.assertFalse(reims_release._binary_wants_the_new_rom_name(p))

    def test_falso_negativo_e_o_caso_que_o_verify_tree_precisa_recusar(self) -> None:
        """O lado perigoso: binário novo classificado como antigo.

        Era o buraco real do sinal anterior, e a razão da troca para o nome do
        device. Com o sinal de ROM, apagar a string bastava para produzir isto;
        hoje é preciso apagar TAMBÉM o nome do device, que é outro ponto do
        binário. O teste fixa o comportamento: sem sinal nenhum, o veredito é
        `None` -> `False` -> o `_verify_tree` recusa em vez de instalar a ROM
        errada em silêncio.
        """
        sem_sinal = b"\x7fELF" + b"\x00XXXXX\x00" + b"\x00display-modes\x00"
        p = self._binario(sem_sinal)
        self.assertIsNone(reims_release._device_of_this_binary(p))
        self.assertFalse(reims_release._binary_wants_the_new_rom_name(p))

    def test_rom_name_sozinho_ja_nao_decide_nada(self) -> None:
        """O nome da ROM deixou de ser o sinal — e o teste prova a troca.

        Este é o contraexemplo que motivou a mudança: um binário que carrega a
        string `ReimsVgpu.rom` mas cujo device é o antigo NÃO é da geração
        nova. Com o sinal de ROM antigo isto dava `True` (o falso positivo
        acima); agora quem decide é o device, e a string não conta.
        """
        so_a_string = b"\x7fELF\x00reims-vgpu-pci\x00ReimsVgpu.rom\x00display-modes\x00"
        p = self._binario(so_a_string)
        self.assertEqual(reims_release._device_of_this_binary(p), "antigo")
        self.assertFalse(
            reims_release._binary_wants_the_new_rom_name(p),
            "a string da ROM não decide mais; o device antigo é que manda",
        )


if __name__ == "__main__":
    unittest.main()
