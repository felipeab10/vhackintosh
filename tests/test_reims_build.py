"""Focused checks for build artifact paths and active-build rollback."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import reims_build


def test_iso_packaging_supports_prebuilt_release():
    """O caminho padrão agora é a release pré-compilada do GitHub.

    Antes este teste exigia o contrário ("nunca transportar binários"), porque o
    appliance compilava o fork do QEMU a cada boot. Compilar levava horas e o
    usuário trocava o SSD do notebook para testar; a decisão foi reverter para o
    binário pré-compilado, que é x86-64 genérico e carrega o Vulkan por dlopen.
    O que continua proibido é *compilar* algo aqui, não empacotar o binário.
    """
    script = Path(__file__).resolve().parents[1] / "appliance/build-iso.sh"
    text = script.read_text()

    # A release é o caminho padrão...
    assert "-r|--release|--tag" in text
    assert "releases/latest/download" in text
    assert "releases/download" in text
    assert "reims-vgpu-linux-x86_64.tar.gz" in text
    assert "REIMS_PREBUILT=1" in text
    # ...e o modo fontes continua existindo como alternativa.
    assert "--from-source" in text
    assert "Cargo.toml" in text and "scripts/install-ctl.sh" in text
    assert "não contém as fontes completas" in text
    # A opção de atualizar as fontes a partir da release.
    assert "-u|--update-sources" in text

    # Não transportar binários de um build *local* do QEMU pelos caminhos de
    # sempre: o que vai para a ISO é o tarball da release, extraído, não um
    # `build/` copiado do checkout do host.
    assert 'cp -a /opt/reims-vgpu/*' not in text
    assert "--exclude='build'" in text
    assert "--exclude='out'" in text
    assert text.index('mkdir -p "${REIMS_DEST}/vendor/qemu"') < text.index('"${REIMS_SRC}/vendor/qemu/" "${REIMS_DEST}/vendor/qemu/"')


def test_root_paths_match_appliance_runtime():
    with patch.object(reims_build.os, "geteuid", return_value=0), patch.dict(
        reims_build.os.environ, {"HOME": "/home/build-user", "XDG_DATA_HOME": "/tmp/user-data"}
    ):
        env = reims_build.appliance_build_environment()
        assert env["HOME"] == "/root"
        assert env["CARGO_HOME"] == "/root/.cargo"
        assert env["XDG_DATA_HOME"] == "/root/.local/share"
        assert reims_build.qemu_binary() == Path(
            "/root/.local/share/reims-ctl/qemu/current/bin/qemu-system-x86_64"
        )


def test_user_paths_are_not_rewritten():
    with patch.object(reims_build.os, "geteuid", return_value=1000), patch.dict(
        reims_build.os.environ, {"HOME": "/home/tester", "XDG_DATA_HOME": "/data/tester"}
    ):
        env = reims_build.appliance_build_environment()
        assert env["HOME"] == "/home/tester"
        assert env["XDG_DATA_HOME"] == "/data/tester"
        assert reims_build.qemu_binary() == Path(
            "/data/tester/reims-ctl/qemu/current/bin/qemu-system-x86_64"
        )


def test_failed_stage_does_not_write_success_stamp(tmp_path: Path):
    source = tmp_path / "engine"
    (source / "scripts").mkdir(parents=True)
    (source / "scripts/install-ctl.sh").write_text("#!/bin/sh\n")
    (source / "crates").mkdir()
    (source / "vendor/qemu").mkdir(parents=True)
    (source / "vendor/qemu/meson.build").write_text("project('qemu')\n")
    (source / "Cargo.toml").write_text("[workspace]\n")
    stamp = source / reims_build.BUILD_STAMP_NAME
    stamp.write_text("previous-success\n")
    with patch.object(reims_build, "ensure_executable_bits"), patch.object(
        reims_build, "_run_stage", return_value=False
    ), patch.object(reims_build, "LOGS_DIR", tmp_path / "logs"):
        assert not reims_build.build(source)
    assert stamp.read_text() == "previous-success\n"


def authoritative_install_ctl() -> Path:
    """O `install-ctl.sh` que a ISO empacota de verdade.

    NÃO é a cópia em `appliance/archiso/airootfs/opt/` — esse diretório é
    gitignored e o `build-iso.sh` o apaga e reescreve a partir da árvore do fork
    (`rm -rf "${ARCHISO_PROFILE}/airootfs/opt"` + `git archive`), então ele é
    lixo de build, não fonte da verdade.
    """
    candidates = [os.environ.get("REIMS_SRC", ""), "/opt/reims-vgpu", str(Path.home() / "reims-vgpu")]
    for candidate in candidates:
        script = Path(candidate) / "scripts" / "install-ctl.sh" if candidate else None
        if script is not None and script.is_file():
            return script
    raise unittest.SkipTest("árvore do reims-vgpu ausente neste host")


def test_install_script_publishes_the_qemu_build_atomically():
    """O build novo só vira `current` depois de existir por inteiro no stage.

    Um QEMU publicado pela metade derruba o appliance inteiro (é ele que abre a
    janela), e o appliance recompila no primeiro boot — sem esta ordem, uma
    interrupção deixa `current` apontando para um diretório incompleto.
    """
    text = authoritative_install_ctl().read_text()
    staged = text.index('DESTDIR="$STAGE" ninja -C "$BUILD" install')
    binary = text.index('"$STAGE$PREFIX/bin/$QEMU_BINARY"', staged)
    rom = text.index("ReimsVgpu.rom", binary)
    publish = text.index('mv "$STAGE$PREFIX" "$DATA/qemu/$ID"', rom)
    activate = text.index('ln -sfn "$ID" "$DATA/qemu/current"', publish)
    in_use = text.index("in_use", activate)
    record = text.index("__record-install", in_use)
    assert staged < binary < rom < publish < activate < in_use < record
    # A limpeza dos builds antigos respeita uma VM em execução: apagar o QEMU de
    # uma VM viva a mata no meio.
    cleanup = text[in_use:record]
    assert 'grep -qF "$old"' in cleanup and "rm -rf" in cleanup

class ReimsBuildStampTests(unittest.TestCase):
    def test_stamp_lifecycle_and_invalidation(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            repo = td_path / "reims-vgpu"
            repo.mkdir()
            (repo / "crates").mkdir()
            (repo / "crates" / "lib.rs").write_text("// test source")
            (repo / "vendor" / "qemu").mkdir(parents=True)
            (repo / "vendor" / "qemu" / "meson.build").write_text("project('qemu')\n")
            (repo / "scripts").mkdir()
            (repo / "scripts" / "install-ctl.sh").write_text("#!/bin/bash\nexit 0\n")
            (repo / "Cargo.toml").write_text("[workspace]\n")

            # Fake QEMU binary path
            fake_bin = td_path / "qemu-system-x86_64"
            fake_bin.write_bytes(b"\x7fELF")

            with patch.object(reims_build, "qemu_binary", return_value=fake_bin), patch.object(
                reims_build, "CONFIG_DIR", td_path / "config"
            ):
                # 1. Sem stamp -> precisa compilar
                st = reims_build.evaluate(explicit=repo)
                self.assertTrue(st.needs_build)
                self.assertIn("sem registro", st.reason)

                # 2. Grava stamp -> não precisa compilar
                fp = reims_build.source_fingerprint(repo)
                reims_build.write_stamp(repo, fp)
                st2 = reims_build.evaluate(explicit=repo)
                self.assertFalse(st2.needs_build)
                self.assertIn("binário atualizado", st2.reason)

                # 3. Altera fonte -> precisa compilar novamente
                (repo / "crates" / "lib.rs").write_text("// updated source code")
                st3 = reims_build.evaluate(explicit=repo)
                self.assertTrue(st3.needs_build)
                self.assertIn("o fonte do reims-vgpu mudou", st3.reason)

                # 4. Grava novo stamp -> volta a ficar pronto
                reims_build.write_stamp(repo, reims_build.source_fingerprint(repo))
                st4 = reims_build.evaluate(explicit=repo)
                self.assertFalse(st4.needs_build)

    def test_stamp_fallback_to_config_dir(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            repo = td_path / "reims-vgpu"
            repo.mkdir()
            (repo / "scripts").mkdir()
            (repo / "scripts" / "install-ctl.sh").write_text("#!/bin/bash\nexit 0\n")
            (repo / "Cargo.toml").write_text("[workspace]\n")
            fake_bin = td_path / "qemu-system-x86_64"
            fake_bin.write_bytes(b"\x7fELF")

            config_dir = td_path / "config"
            with patch.object(reims_build, "qemu_binary", return_value=fake_bin), \
                 patch.object(reims_build, "CONFIG_DIR", config_dir):
                fp = reims_build.source_fingerprint(repo)
                reims_build.write_stamp(repo, fp)

                # Remove o stamp do repo (simulando sistema somente-leitura ou limpo)
                (repo / reims_build.BUILD_STAMP_NAME).unlink()

                # Deve ler do fallback em config_dir
                read = reims_build.read_stamp(repo)
                self.assertEqual(read, fp)


if __name__ == "__main__":
    unittest.main()

