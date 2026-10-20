"""Regressões da política de build offline do reims-vgpu.

Contexto que originou estes testes: o appliance levava apenas as fontes do
reims-vgpu e, ao compilar no primeiro boot, o Cargo morria com

    error: failed to get `metal2vulkan` as a dependency
    Caused by: can't checkout from 'https://github.com/steelbrain/metal2vulkan':
                you are in the offline mode (--offline)

Duas causas independentes, cobertas aqui separadamente:

1. `CARGO_NET_OFFLINE=true` era ligado só porque existia `vendor/cargo`, mas
   `cargo vendor` cobre **apenas** o crates.io — `metal2vulkan` é dependência
   **git** e não está em vendor/cargo. A heurística antiga *garantia* a falha.
2. A ISO não levava cache nenhum do Cargo, então o primeiro boot sempre
   dependia de rede.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import reims_build

GIT_REPO = "metal2vulkan-ec622ce80a554d9d"


def _cargo_home(root: Path, *, git_db=False, registry=False, checkouts=False) -> Path:
    home = root / ".cargo"
    home.mkdir(parents=True, exist_ok=True)
    if git_db:
        # Layout de repositório bare de verdade: `objects/` + `refs/`.
        repo = home / "git" / "db" / GIT_REPO
        (repo / "objects").mkdir(parents=True)
        (repo / "refs").mkdir(parents=True)
    if checkouts:
        (home / "git" / "checkouts" / GIT_REPO).mkdir(parents=True, exist_ok=True)
    if registry:
        (home / "registry" / "index").mkdir(parents=True, exist_ok=True)
        (home / "registry" / "cache").mkdir(parents=True, exist_ok=True)
    return home


class CargoOfflinePolicyTests(unittest.TestCase):
    """`_apply_cargo_offline_policy` — a decisão precisa ser honesta."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cwd = self.root / "src"
        (self.cwd / "vendor" / "cargo").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _env(self, home: Path) -> dict:
        return {"HOME": str(self.root), "CARGO_HOME": str(home)}

    def test_no_cache_at_all_uses_network(self):
        """Sem cache git, forçar offline é o bug original — tem de usar rede."""
        home = _cargo_home(self.root, registry=True)
        env = reims_build._apply_cargo_offline_policy(self._env(home), self.cwd)
        self.assertEqual(env["CARGO_NET_OFFLINE"], "false")

    def test_vendor_alone_does_not_enable_offline(self):
        """`vendor/cargo` só cobre crates.io; sozinho não pode ligar offline."""
        home = _cargo_home(self.root)
        env = reims_build._apply_cargo_offline_policy(self._env(home), self.cwd)
        self.assertEqual(env["CARGO_NET_OFFLINE"], "false")

    def test_git_cache_without_registry_uses_network(self):
        """Meio cache não basta: faltaria a crate `bitflags` na resolução."""
        home = _cargo_home(self.root, git_db=True)
        env = reims_build._apply_cargo_offline_policy(self._env(home), self.cwd)
        self.assertEqual(env["CARGO_NET_OFFLINE"], "false")

    def test_registry_without_git_cache_uses_network(self):
        home = _cargo_home(self.root, registry=True)
        env = reims_build._apply_cargo_offline_policy(self._env(home), self.cwd)
        self.assertEqual(env["CARGO_NET_OFFLINE"], "false")

    def test_full_cache_enables_offline(self):
        """git + registry presentes => offline é uma promessa cumprível."""
        home = _cargo_home(self.root, git_db=True, registry=True)
        env = reims_build._apply_cargo_offline_policy(self._env(home), self.cwd)
        self.assertEqual(env["CARGO_NET_OFFLINE"], "true")

    def test_registry_cache_is_required_even_with_checkouts(self):
        """Ter o checkout extraído não dispensa o registry."""
        home = _cargo_home(self.root, git_db=True, checkouts=True)
        env = reims_build._apply_cargo_offline_policy(self._env(home), self.cwd)
        self.assertEqual(env["CARGO_NET_OFFLINE"], "false")

    def test_caller_choice_is_never_overwritten(self):
        """Uma decisão já tomada pelo chamador manda."""
        home = _cargo_home(self.root, git_db=True, registry=True)
        env = self._env(home)
        env["CARGO_NET_OFFLINE"] = "false"
        out = reims_build._apply_cargo_offline_policy(env, self.cwd)
        self.assertEqual(out["CARGO_NET_OFFLINE"], "false")

    def test_env_override_forces_offline(self):
        home = _cargo_home(self.root)
        with patch.dict(os.environ, {"REIMS_CARGO_OFFLINE": "1"}):
            env = reims_build._apply_cargo_offline_policy(self._env(home), self.cwd)
        self.assertEqual(env["CARGO_NET_OFFLINE"], "true")

    def test_env_override_forces_network(self):
        home = _cargo_home(self.root, git_db=True, registry=True)
        with patch.dict(os.environ, {"REIMS_CARGO_OFFLINE": "0"}):
            env = reims_build._apply_cargo_offline_policy(self._env(home), self.cwd)
        self.assertEqual(env["CARGO_NET_OFFLINE"], "false")


class GitDepCacheDetectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_bare_repo_is_enough(self):
        """O `git/db` basta: o Cargo extrai o checkout sozinho, offline."""
        home = _cargo_home(self.root, git_db=True)
        self.assertTrue(reims_build._git_dep_cache_present(home))

    def test_empty_cargo_home_is_absent(self):
        home = _cargo_home(self.root)
        self.assertFalse(reims_build._git_dep_cache_present(home))

    def test_unrelated_repo_is_not_counted(self):
        home = _cargo_home(self.root)
        (home / "git" / "db" / "serde-abcdef0123456789").mkdir(parents=True)
        self.assertFalse(reims_build._git_dep_cache_present(home))

    def test_empty_repo_dir_is_not_a_hit(self):
        """DEFECT: um `metal2vulkan-*` VAZIO não pode contar como cache.

        `build-iso.sh` faz `mkdir -p` e depois `rsync` para o destino; um rsync
        interrompido deixa a pasta vazia. Aceitá-la por nome fazia a política
        decidir `offline=true` e o cargo voltava ao erro original
        (`can't checkout ... offline mode`).
        """
        home = _cargo_home(self.root)
        (home / "git" / "db" / GIT_REPO).mkdir(parents=True)
        self.assertFalse(reims_build._git_dep_cache_present(home))

    def test_junk_repo_dir_is_not_a_hit(self):
        home = _cargo_home(self.root)
        repo = home / "git" / "db" / GIT_REPO
        repo.mkdir(parents=True)
        (repo / "lixo.txt").write_text("nao e um repo")
        self.assertFalse(reims_build._git_dep_cache_present(home))

    def test_real_bare_repo_layout_is_a_hit(self):
        home = _cargo_home(self.root)
        repo = home / "git" / "db" / GIT_REPO
        (repo / "objects").mkdir(parents=True)
        (repo / "refs").mkdir(parents=True)
        self.assertTrue(reims_build._git_dep_cache_present(home))

    def test_poisoned_cache_does_not_enable_offline(self):
        """O ponto do defeito: pasta vazia + registry => NÃO pode ser offline."""
        home = _cargo_home(self.root, registry=True)
        (home / "git" / "db" / GIT_REPO).mkdir(parents=True)
        self.assertFalse(reims_build._git_dep_cache_present(home))
        env = reims_build._apply_cargo_offline_policy(
            {"HOME": str(self.root), "CARGO_HOME": str(home)}, self.root
        )
        self.assertEqual(env["CARGO_NET_OFFLINE"], "false")

    def test_registry_detection_requires_index_and_cache(self):
        only_index = _cargo_home(self.root / "a")
        (only_index / "registry" / "index").mkdir(parents=True, exist_ok=True)
        self.assertFalse(reims_build._registry_cache_present(only_index))

        both = _cargo_home(self.root / "b", registry=True)
        self.assertTrue(reims_build._registry_cache_present(both))


class SeedCargoGitCacheTests(unittest.TestCase):
    """`seed_cargo_git_cache` — o payload da ISO tem de chegar ao CARGO_HOME."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.payload = self.root / "payload"
        repo = self.payload / "git" / "db" / GIT_REPO
        (repo / "objects").mkdir(parents=True)
        (repo / "refs").mkdir(parents=True)
        (repo / "HEAD").write_text("ref: refs/heads/main\n")
        self.reg_payload = self.root / "regpayload"
        (self.reg_payload / "registry" / "index").mkdir(parents=True)
        (self.reg_payload / "registry" / "cache").mkdir(parents=True)
        (self.reg_payload / "registry" / "cache" / "bitflags-2.13.2.crate").write_text("x")
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {"HOME": str(self.home), "CARGO_HOME": str(self.home / ".cargo")}

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        with patch.object(reims_build, "GIT_CACHE_PAYLOAD", self.payload), patch.object(
            reims_build, "REGISTRY_CACHE_PAYLOAD", self.reg_payload
        ):
            return reims_build.seed_cargo_git_cache(self.env)

    def test_seeds_both_caches(self):
        self.assertTrue(self._seed())
        home = self.home / ".cargo"
        self.assertTrue(reims_build._git_dep_cache_present(home))
        self.assertTrue(reims_build._registry_cache_present(home))

    def test_enables_offline_after_seeding(self):
        """O objetivo final: depois do seed a política passa a permitir offline."""
        self.assertEqual(
            reims_build._apply_cargo_offline_policy(dict(self.env), self.root)[
                "CARGO_NET_OFFLINE"
            ],
            "false",
        )
        self._seed()
        self.assertEqual(
            reims_build._apply_cargo_offline_policy(dict(self.env), self.root)[
                "CARGO_NET_OFFLINE"
            ],
            "true",
        )

    def test_is_idempotent(self):
        self.assertTrue(self._seed())
        self.assertTrue(self._seed())

    def test_missing_payload_is_not_an_error(self):
        with patch.object(reims_build, "GIT_CACHE_PAYLOAD", self.root / "nope"), patch.object(
            reims_build, "REGISTRY_CACHE_PAYLOAD", self.root / "nope2"
        ):
            self.assertFalse(reims_build.seed_cargo_git_cache(self.env))

    def test_existing_cache_is_not_clobbered(self):
        home = _cargo_home(self.home, git_db=True, registry=True)
        marker = home / "git" / "db" / GIT_REPO / "KEEP"
        marker.write_text("intacto")
        self.assertTrue(self._seed())
        self.assertEqual(marker.read_text(), "intacto")

    def test_heals_a_poisoned_empty_cache(self):
        """Um resquício vazio de rsync interrompido tem de ser substituído."""
        home = self.home / ".cargo"
        (home / "git" / "db" / GIT_REPO).mkdir(parents=True)
        self.assertFalse(reims_build._git_dep_cache_present(home))
        self.assertTrue(self._seed())
        self.assertTrue(reims_build._git_dep_cache_present(home))
        self.assertTrue((home / "git" / "db" / GIT_REPO / "HEAD").is_file())

    def test_heals_a_partial_non_empty_cache(self):
        """Resquício NÃO-vazio (só `HEAD`, sem `objects/refs`) também se cura.

        O merge por cima é o que torna isso possível; se o seed pulasse qualquer
        destino existente, este cache envenenado persistiria para sempre.
        """
        home = self.home / ".cargo"
        repo = home / "git" / "db" / GIT_REPO
        repo.mkdir(parents=True)
        (repo / "HEAD").write_text("ref: refs/heads/main\n")
        self.assertFalse(reims_build._git_dep_cache_present(home))
        self.assertTrue(self._seed())
        self.assertTrue(reims_build._git_dep_cache_present(home))
        self.assertTrue((repo / "objects").is_dir())
        self.assertTrue((repo / "refs").is_dir())

    def test_heals_objects_present_as_a_file(self):
        """DEFECT: `objects` como ARQUIVO de 0 byte tornava o cache incurável.

        `copytree(dirs_exist_ok=True)` morre com EEXIST ao achar um arquivo onde
        o payload tem diretório, no meio do merge. O diretório-pai ficava
        não-vazio, então a limpeza de "vazio" nunca mais disparava: o cache
        envenenado persistia para sempre (3 execuções seguidas não curavam).
        """
        home = self.home / ".cargo"
        repo = home / "git" / "db" / GIT_REPO
        repo.mkdir(parents=True)
        (repo / "objects").write_text("")
        (repo / "HEAD").write_text("ref: refs/heads/main\n")
        self.assertFalse(reims_build._git_dep_cache_present(home))
        self.assertTrue(self._seed())
        self.assertTrue(reims_build._git_dep_cache_present(home))
        self.assertTrue((repo / "objects").is_dir())
        # Convergiu: rodar de novo é estável.
        self.assertTrue(self._seed())

    def test_heals_nested_poison_that_looks_like_a_bare_repo(self):
        """DEFECT: poço mais fundo — `objects/` e `refs/` são dirs, mas
        `objects/pack` é ARQUIVO.

        Este é o pior caso, porque o predicado de forma PASSA (`objects/` +
        `refs/` existem), então `seed_cargo_git_cache` não visitava o cache e a
        política automática mentia `CARGO_NET_OFFLINE=true` com um repositório
        corrompido. O guard do seed não pode depender do predicado: precisa
        sempre mesclar.
        """
        home = self.home / ".cargo"
        repo = home / "git" / "db" / GIT_REPO
        (repo / "objects").mkdir(parents=True)
        (repo / "refs").mkdir(parents=True)
        (repo / "objects" / "pack").write_text("")
        # O payload precisa ter `objects/pack` como diretório para a mesclagem
        # ter o que entregar nesse ponto (o setUp cria `objects/` vazio).
        (self.payload / "git" / "db" / GIT_REPO / "objects" / "pack").mkdir(
            parents=True, exist_ok=True
        )
        # O predicado de forma é enganado — é justamente por isso que o seed
        # não pode confiar nele.
        self.assertTrue(reims_build._git_dep_cache_present(home))
        self.assertTrue(self._seed())
        self.assertTrue((repo / "objects" / "pack").is_dir())
        self.assertTrue(self._seed())

    def test_does_not_clobber_genuine_user_content(self):
        """O reparo remove só o que bloquearia a cópia, nunca dado do usuário."""
        home = self.home / ".cargo"
        repo = home / "git" / "db" / GIT_REPO
        (repo / "objects").mkdir(parents=True)
        (repo / "refs").mkdir(parents=True)
        mine_obj = repo / "objects" / "MEU_OBJETO"
        mine_obj.write_text("dado do usuário")
        mine_ref = repo / "refs" / "minha-ref"
        mine_ref.write_text("ref do usuário")
        self.assertTrue(self._seed())
        self.assertTrue(mine_obj.is_file(), "objeto genuíno foi apagado")
        self.assertTrue(mine_ref.is_file(), "ref genuína foi apagada")


class BuildIsoPackagesTheCachesTests(unittest.TestCase):
    """A ISO precisa empacotar os dois caches, senão nada disso roda."""

    def setUp(self):
        self.script = (
            Path(__file__).resolve().parents[1] / "appliance/build-iso.sh"
        ).read_text()

    def test_packages_git_db_but_not_the_huge_checkouts(self):
        self.assertIn("reims-vgpu-cargo-git", self.script)
        self.assertIn("git/db/metal2vulkan", self.script)
        # `checkouts/` tem ~9,3 GB de árvores de trabalho por revisão; copiar
        # isso inviabiliza a ISO e é desnecessário (o Cargo extrai do `db`).
        self.assertNotIn("checkouts/${_name}", self.script)

    def test_packages_registry_cache(self):
        self.assertIn("reims-vgpu-cargo-registry", self.script)
        self.assertIn('registry/${_sub}', self.script)

    def test_payload_paths_match_runtime_expectation(self):
        """O destino no airootfs vira a raiz da ISO; o runtime tem de achar."""
        for payload in (
            reims_build.GIT_CACHE_PAYLOAD,
            reims_build.REGISTRY_CACHE_PAYLOAD,
        ):
            self.assertIn(f"/airootfs{payload}", self.script)


if __name__ == "__main__":
    unittest.main()
