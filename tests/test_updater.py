"""Integration checks against temporary local bare Git repositories."""
import subprocess
import tempfile
from pathlib import Path

from core.updater import REIMS_URL, VHACK_URL, SystemUpdater, UpdateError


class LocalTestUpdater(SystemUpdater):
    @staticmethod
    def _matches_remote(remote: str, expected: str) -> bool:
        return (remote.endswith(".git") and Path(remote).is_absolute()) or SystemUpdater._matches_remote(remote, expected)


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(base: Path, name: str, *, sources: bool = False) -> tuple[Path, Path, Path]:
    base.mkdir(parents=True, exist_ok=True)
    remote, work, checkout = base / f"{name}.git", base / f"{name}-work", base / name
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "master", str(work)], check=True, capture_output=True)
    git(work, "config", "user.email", "test@example.invalid")
    git(work, "config", "user.name", "Test")
    (work / "source.txt").write_text("initial\n")
    if sources:
        (work / "Cargo.toml").write_text("[workspace]\n")
        (work / "crates").mkdir()
        (work / "vendor/qemu").mkdir(parents=True)
        (work / "vendor/qemu/meson.build").write_text("project('qemu')\n")
        (work / "scripts").mkdir()
        (work / "scripts/install-ctl.sh").write_text("#!/bin/sh\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "initial")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "master")
    subprocess.run(["git", "clone", str(remote), str(checkout)], check=True, capture_output=True)
    git(checkout, "config", "user.email", "test@example.invalid")
    git(checkout, "config", "user.name", "Test")
    return checkout, work, remote


def make_pair(base: Path, *, engine_sources=False):
    app, app_work, app_remote = make_repo(base, "app")
    engine, engine_work, engine_remote = make_repo(base, "engine", sources=engine_sources)
    git(engine, "remote", "set-url", "origin", REIMS_URL)
    git(engine_work, "remote", "set-url", "origin", REIMS_URL)
    git(engine, "config", "remote.origin.url", str(engine_remote))
    git(engine_work, "config", "remote.origin.url", str(engine_remote))
    return {"vHackintosh": app, "reims-vgpu": engine}, (app, app_work, app_remote), (engine, engine_work, engine_remote)


def test_full_update_builds_exact_default_checkout():
    from unittest.mock import patch
    from core.updater import RepositoryUpdate
    app, engine = Path("/opt/vhackintosh"), Path("/opt/reims-vgpu")
    results = [RepositoryUpdate("vHackintosh", app, "a", "b", False),
               RepositoryUpdate("reims-vgpu", engine, "c", "d", False)]
    with patch("core.updater.os.geteuid", return_value=0), \
         patch.object(SystemUpdater, "update_repositories", return_value=results), \
         patch("core.reims_build.build", return_value=True) as build, \
         patch.object(SystemUpdater, "_has_build_sources", return_value=True), \
         patch.object(SystemUpdater, "_install_waysession") as install_waysession, \
         patch("core.i18n.t", side_effect=lambda key, **kwargs: key), \
         patch("core.updater.console.print"):
        assert SystemUpdater.run_full_update()
        assert build.call_args.args[0] == Path("/opt/reims-vgpu")
        install_waysession.assert_called_once_with()


def test_waysession_install_replaces_destination_and_sets_executable_mode(base: Path):
    import os

    source = base / "checkout/vhackintosh-waysession"
    destination = base / "usr/local/bin/vhackintosh-waysession"
    source.parent.mkdir(parents=True)
    source.write_text("#!/bin/sh\necho updated\n")
    destination.parent.mkdir(parents=True)
    destination.write_text("old launcher\n")

    SystemUpdater._install_waysession(source, destination)

    assert destination.read_text() == source.read_text()
    assert os.stat(destination).st_mode & 0o777 == 0o755
    assert list(destination.parent.iterdir()) == [destination]


def test_remote_validation():
    assert SystemUpdater._matches_remote(REIMS_URL, REIMS_URL)
    assert SystemUpdater._matches_remote("git@github.com:felipeab10/reims-vgpu.git", REIMS_URL)
    assert not SystemUpdater._matches_remote("https://github.com/attacker/reims-vgpu.git", REIMS_URL)


def test_appliance_update_requires_root():
    from unittest.mock import patch
    with patch("core.updater.os.geteuid", return_value=1000):
        try:
            SystemUpdater.update_repositories()
        except UpdateError as exc:
            assert exc.repo == "permissions" and "requires root" in str(exc)
        else:
            raise AssertionError("non-root appliance update should be rejected")


def test_fast_forward_and_idempotence(base: Path):
    paths, (app, app_work, _), (engine, engine_work, _) = make_pair(base)
    for work in (app_work, engine_work):
        (work / "source.txt").write_text("remote update\n")
        git(work, "commit", "-am", "remote update")
        git(work, "push")
    result = LocalTestUpdater.update_repositories(repo_paths=paths)
    assert len(result) == 2 and all(r.changed and r.before != r.after for r in result)
    assert git(app, "rev-parse", "HEAD") == git(app, "rev-parse", "origin/master")
    assert git(engine, "rev-parse", "HEAD") == git(engine, "rev-parse", "origin/master")
    result = LocalTestUpdater.update_repositories(repo_paths=paths)
    assert all(not r.changed for r in result)


def test_dirty_checkout_rejected(base: Path):
    repo, _, _ = make_repo(base, "dirty")
    (repo / "source.txt").write_text("local edit\n")
    try:
        LocalTestUpdater._check_checkout("vHackintosh", repo, VHACK_URL)
    except UpdateError as exc:
        assert "modified or untracked" in str(exc)
    else:
        raise AssertionError("dirty checkout should be rejected")
    assert (repo / "source.txt").read_text() == "local edit\n"


def test_ahead_branch_rejected(base: Path):
    paths, (app, _, _), _ = make_pair(base)
    (app / "source.txt").write_text("local commit\n")
    git(app, "commit", "-am", "local only")
    try:
        LocalTestUpdater.update_repositories(repo_paths=paths)
    except UpdateError as exc:
        assert "ahead" in str(exc)
    else:
        raise AssertionError("ahead branch should be rejected")


def test_divergence_rejected(base: Path):
    paths, (app, app_work, _), _ = make_pair(base)
    (app / "source.txt").write_text("local\n")
    git(app, "commit", "-am", "local only")
    (app_work / "source.txt").write_text("remote\n")
    git(app_work, "commit", "-am", "remote only")
    git(app_work, "push")
    try:
        LocalTestUpdater.update_repositories(repo_paths=paths)
    except UpdateError as exc:
        assert "diverged" in str(exc)
    else:
        raise AssertionError("diverged branches should be rejected")


def test_partial_apply_is_reported(base: Path):
    paths, (app, app_work, _), (engine, engine_work, _) = make_pair(base)
    for work in (app_work, engine_work):
        (work / "source.txt").write_text("remote update\n")
        git(work, "commit", "-am", "remote update")
        git(work, "push")
    original_apply = LocalTestUpdater._apply

    def fail_engine(update, path):
        if update.name == "reims-vgpu":
            raise UpdateError(update.name, "simulated transient failure")
        return original_apply(update, path)

    LocalTestUpdater._apply = classmethod(lambda cls, update, path: fail_engine(update, path))
    try:
        try:
            LocalTestUpdater.update_repositories(repo_paths=paths)
        except UpdateError as exc:
            assert "vHackintosh" in str(exc) and "retry is safe" in str(exc)
        else:
            raise AssertionError("partial update should be reported")
    finally:
        LocalTestUpdater._apply = original_apply
    assert git(app, "rev-parse", "HEAD") != git(app, "rev-parse", "HEAD~1")
    assert git(engine, "rev-parse", "HEAD") != git(engine, "rev-parse", "origin/master")


def test_first_clone_and_preserve_packaged_sources(base: Path):
    paths, _, (engine, _work, engine_remote) = make_pair(base)
    import shutil
    shutil.rmtree(engine / ".git")
    (engine / "Cargo.toml").write_text("packaged tree\n")
    (engine / "crates").mkdir()
    (engine / "vendor/qemu").mkdir(parents=True)
    (engine / "vendor/qemu/meson.build").write_text("project('image')\n")
    (engine / "scripts").mkdir()
    (engine / "scripts/install-ctl.sh").write_text("#!/bin/sh\n")
    def clone(_url: str, dest: Path):
        subprocess.run(["git", "clone", str(engine_remote), str(dest)], check=True, capture_output=True)
        git(dest, "config", "user.email", "test@example.invalid")
        git(dest, "config", "user.name", "Test")
        (dest / "Cargo.toml").write_text("[workspace]\n")
        (dest / "crates").mkdir()
        (dest / "vendor/qemu").mkdir(parents=True)
        (dest / "vendor/qemu/meson.build").write_text("project('qemu')\n")
        (dest / "scripts").mkdir()
        (dest / "scripts/install-ctl.sh").write_text("#!/bin/sh\n")
        git(dest, "add", ".")
        git(dest, "commit", "-m", "build source tree")
        git(dest, "push", "origin", "master")
        git(dest, "fetch", "origin")
        git(dest, "reset", "--hard", "origin/master")
    # This valid local clone exercises the real staging and atomic promotion path.
    results = LocalTestUpdater.update_repositories(repo_paths=paths, clone_fn=clone)
    assert len(results) == 2
    assert (engine / ".git").exists()
    assert (engine / "Cargo.toml").read_text() == "[workspace]\n"
    assert (engine / "vendor/qemu/meson.build").is_file()
    assert not engine.with_name(engine.name + ".source-backup").exists()


def test_clone_network_failure_preserves_packaged_sources(base: Path):
    paths, _, (engine, _, _) = make_pair(base)
    import shutil
    shutil.rmtree(engine / ".git")
    (engine / "Cargo.toml").write_text("keep\n")
    (engine / "crates").mkdir()
    (engine / "vendor/qemu").mkdir(parents=True)
    (engine / "vendor/qemu/meson.build").write_text("project('image')\n")
    (engine / "scripts").mkdir()
    (engine / "scripts/install-ctl.sh").write_text("#!/bin/sh\n")
    def fail(_url: str, _dest: Path):
        raise RuntimeError("network down")
    try:
        LocalTestUpdater.update_repositories(repo_paths=paths, clone_fn=fail)
    except UpdateError as exc:
        assert "network down" in str(exc)
    else:
        raise AssertionError("clone failure should propagate")
    assert (engine / "Cargo.toml").read_text() == "keep\n"
    assert not (engine / ".git").exists()


def test_existing_backup_blocks_promotion_and_preserves_sources(base: Path):
    paths, _, (engine, _work, _remote) = make_pair(base)
    import shutil
    shutil.rmtree(engine / ".git")
    (engine / "Cargo.toml").write_text("packaged source\n")
    (engine / "crates").mkdir()
    (engine / "vendor/qemu").mkdir(parents=True)
    (engine / "vendor/qemu/meson.build").write_text("project('image')\n")
    (engine / "scripts").mkdir()
    (engine / "scripts/install-ctl.sh").write_text("#!/bin/sh\n")
    backup = engine.with_name(engine.name + ".source-backup")
    backup.mkdir()
    (backup / "recovery-marker").write_text("keep backup\n")

    def clone(_url: str, dest: Path):
        subprocess.run(["git", "clone", str(_remote), str(dest)], check=True, capture_output=True)
        git(dest, "config", "user.email", "test@example.invalid")
        git(dest, "config", "user.name", "Test")
        (dest / "Cargo.toml").write_text("[workspace]\n")
        (dest / "crates").mkdir()
        (dest / "vendor/qemu").mkdir(parents=True)
        (dest / "vendor/qemu/meson.build").write_text("project('image')\n")
        (dest / "scripts").mkdir()
        (dest / "scripts/install-ctl.sh").write_text("#!/bin/sh\n")
        git(dest, "add", ".")
        git(dest, "commit", "-m", "complete source clone")
        git(dest, "push", "origin", "master")
        git(dest, "fetch", "origin")
        git(dest, "reset", "--hard", "origin/master")

    try:
        LocalTestUpdater.update_repositories(repo_paths=paths, clone_fn=clone)
    except UpdateError as exc:
        assert "backup path already exists" in str(exc)
    else:
        raise AssertionError("existing recovery backup must block promotion")
    assert (engine / "Cargo.toml").read_text() == "packaged source\n"
    assert (backup / "recovery-marker").read_text() == "keep backup\n"


def test_unpopulated_qemu_submodule_clone_rejected(base: Path):
    paths, _, (engine, _, _remote) = make_pair(base)
    import shutil
    shutil.rmtree(engine / ".git")
    (engine / "Cargo.toml").write_text("packaged tree\n")
    (engine / "crates").mkdir()
    (engine / "vendor/qemu").mkdir(parents=True)
    (engine / "vendor/qemu/meson.build").write_text("project('image')\n")
    (engine / "scripts").mkdir()
    (engine / "scripts/install-ctl.sh").write_text("#!/bin/sh\n")
    def clone(_url: str, dest: Path):
        subprocess.run(["git", "clone", str(_remote), str(dest)], check=True, capture_output=True)
        git(dest, "config", "user.email", "test@example.invalid")
        git(dest, "config", "user.name", "Test")
        (dest / "Cargo.toml").write_text("[workspace]\n")
        (dest / "crates").mkdir()
        (dest / "vendor/qemu").mkdir(parents=True)
        (dest / "scripts").mkdir()
        (dest / "scripts/install-ctl.sh").write_text("#!/bin/sh\n")
        git(dest, "add", ".")
        git(dest, "commit", "-m", "checkout missing submodule")
    try:
        LocalTestUpdater.update_repositories(repo_paths=paths, clone_fn=clone)
    except UpdateError as exc:
        assert "populated vendor/qemu submodule" in str(exc)
    else:
        raise AssertionError("clone with unpopulated QEMU must be rejected")
    assert not (engine / ".git").exists()
    assert (engine / "Cargo.toml").read_text() == "packaged tree\n"


def run():
    test_remote_validation()
    test_appliance_update_requires_root()
    test_full_update_builds_exact_default_checkout()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_fast_forward_and_idempotence(root / "ff")
        test_dirty_checkout_rejected(root / "dirty")
        test_ahead_branch_rejected(root / "ahead")
        test_divergence_rejected(root / "diverged")
        test_partial_apply_is_reported(root / "partial")
        test_first_clone_and_preserve_packaged_sources(root / "clone-validate")
        test_clone_network_failure_preserves_packaged_sources(root / "clone-fail")
        test_unpopulated_qemu_submodule_clone_rejected(root / "clone-no-submodule")
        test_existing_backup_blocks_promotion_and_preserves_sources(root / "clone-backup")
    print("updater integration checks passed")


if __name__ == "__main__":
    run()
