"""Focused checks for build artifact paths and active-build rollback."""
from pathlib import Path
from unittest.mock import patch

from core import reims_build


def test_iso_packaging_excludes_prebuilt_artifacts():
    script = Path(__file__).resolve().parents[1] / "appliance/build-iso.sh"
    text = script.read_text()
    assert "RELEASE_TAG" not in text
    assert "releases/download" not in text
    assert "vendor/qemu/build/qemu-system-x86_64" not in text
    assert 'cp -a /opt/reims-vgpu/*' not in text
    assert "--exclude='build'" in text
    assert text.index('mkdir -p "${REIMS_DEST}/vendor/qemu"') < text.index('"${REIMS_SRC}/vendor/qemu/" "${REIMS_DEST}/vendor/qemu/"')
    assert "--exclude='out'" in text
    assert "Cargo.toml" in text and "scripts/install-ctl.sh" in text
    assert "não contém as fontes completas" in text


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


def test_install_script_publishes_artifacts_transactionally():
    script = Path(__file__).resolve().parents[1] / "appliance/archiso/airootfs/opt/reims-vgpu/scripts/install-ctl.sh"
    text = script.read_text()
    cargo = text.index('cargo install --locked --force')
    publish = text.index('mv "$STAGED_BIN" "$FINAL_BIN"')
    activate = text.index('ln -sfn "$(basename "$QEMU_DIR")"')
    record = text.index('"$FINAL_BIN" __record-install')
    cleanup = text.index('Cleanup only after the active controller')
    assert cargo < publish < activate < record < cleanup
    assert 'new QEMU activation failed validation' in text
    assert 'could not record the installation' in text
