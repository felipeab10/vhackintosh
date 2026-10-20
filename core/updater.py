"""Git source updater for the vHackintosh appliance."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from rich.console import Console

console = Console()
REIMS_URL = "https://github.com/felipeab10/reims-vgpu.git"
VHACK_URL = "https://github.com/felipeab10/vhackintosh.git"
DEFAULT_REIMS_DIR = Path("/opt/reims-vgpu")
DEFAULT_VHACK_DIR = Path("/opt/vhackintosh")
WAYSESSION_SOURCE = (
    DEFAULT_VHACK_DIR / "appliance" / "archiso" / "airootfs" / "usr" / "local" / "bin"
    / "vhackintosh-waysession"
)
WAYSESSION_DEST = Path("/usr/local/bin/vhackintosh-waysession")


@dataclass
class RepositoryUpdate:
    name: str
    path: Path
    before: str
    after: str
    changed: bool


class UpdateError(RuntimeError):
    def __init__(self, repo: str, message: str):
        super().__init__(message)
        self.repo = repo


class SystemUpdater:
    """Fast-forward source repositories and rebuild the local engine."""

    def __init__(self, *, repo_paths: Optional[dict[str, Path]] = None,
                 clone_fn: Optional[Callable[[str, Path], None]] = None):
        self.paths = repo_paths or {"vHackintosh": DEFAULT_VHACK_DIR, "reims-vgpu": DEFAULT_REIMS_DIR}
        self.clone_fn = clone_fn or self._clone

    @staticmethod
    def _git(path: Path, *args: str, check: bool = True) -> str:
        env = os.environ.copy()
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
            env.pop(key, None)
        result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, env=env)
        if check and result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git command failed")
        return result.stdout.strip()

    @staticmethod
    def _clone(url: str, path: Path) -> None:
        env = os.environ.copy()
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
            env.pop(key, None)
        result = subprocess.run(
            ["git", "clone", "--recurse-submodules", "--filter=blob:none", url, str(path)],
            capture_output=True, text=True, env=env,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "git clone failed")
        if not path.is_dir() or not (path / ".git").exists():
            raise RuntimeError("git clone exited successfully without creating a Git checkout")

    @staticmethod
    def _expected_url(name: str) -> str:
        if name == "reims-vgpu":
            return REIMS_URL
        if name == "vHackintosh":
            return VHACK_URL
        raise UpdateError(name, "unknown repository")

    @staticmethod
    def _matches_remote(remote: str, expected: str) -> bool:
        def normalize(value: str) -> str:
            value = value.strip().rstrip("/").removesuffix(".git")
            value = value.replace("git@github.com:", "github.com/")
            return value.removeprefix("https://").removeprefix("http://")
        return normalize(remote) == normalize(expected)

    @classmethod
    def _check_checkout(cls, name: str, path: Path, expected: str) -> str:
        if not path.is_dir() or not (path / ".git").exists():
            raise UpdateError(name, f"no Git checkout exists at {path}")
        if cls._git(path, "rev-parse", "--show-toplevel") != str(path.resolve()):
            raise UpdateError(name, "checkout root does not match configured path")
        remote = cls._git(path, "remote", "get-url", "origin", check=False)
        if not remote or not cls._matches_remote(remote, expected):
            raise UpdateError(name, f"unexpected origin URL: {remote or '(missing)'}")
        branch = cls._git(path, "branch", "--show-current")
        if not branch:
            raise UpdateError(name, "detached HEAD; check out the integration branch")
        if cls._git(path, "status", "--porcelain", "--untracked-files=all"):
            raise UpdateError(name, "working tree has modified or untracked files")
        upstream = cls._git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
        if not upstream:
            raise UpdateError(name, f"branch {branch!r} has no configured upstream")
        if not upstream.startswith("origin/") or upstream.rsplit("/", 1)[-1] != branch:
            raise UpdateError(name, f"branch {branch!r} tracks unexpected upstream {upstream!r}; expected origin/{branch}")
        return branch

    @staticmethod
    def _has_build_sources(path: Path) -> bool:
        legacy = path / "scripts/qemu-build/qemu-build.sh"
        return ((path / "Cargo.toml").is_file() and (path / "crates").is_dir()
                and (path / "vendor/qemu/meson.build").is_file()
                and ((path / "scripts/install-ctl.sh").is_file() or legacy.is_file()))

    @classmethod
    def _preflight(cls, name: str, path: Path, expected: str) -> tuple[RepositoryUpdate, str]:
        try:
            cls._check_checkout(name, path, expected)
            before = cls._git(path, "rev-parse", "HEAD")
            cls._git(path, "fetch", "--prune", "origin")
            upstream = cls._git(path, "rev-parse", "@{upstream}")
            ahead, behind = (int(x) for x in cls._git(path, "rev-list", "--left-right", "--count", f"HEAD...{upstream}").split())
            if ahead and behind:
                raise UpdateError(name, f"branches diverged ({ahead} local, {behind} remote commits); manual resolution required")
            if ahead:
                raise UpdateError(name, f"local branch is ahead by {ahead} commit(s); preserve/push them first")
            if behind and cls._git(path, "merge-base", "HEAD", upstream) != before:
                raise UpdateError(name, "branches diverged; no merge or reset performed")
            return RepositoryUpdate(name, path, before, upstream, behind > 0), upstream
        except UpdateError:
            raise
        except Exception as exc:
            raise UpdateError(name, str(exc)) from exc

    @classmethod
    def _apply(cls, update: RepositoryUpdate, target: str) -> RepositoryUpdate:
        try:
            cls._git(update.path, "merge", "--ff-only", target)
            update.after = cls._git(update.path, "rev-parse", "HEAD")
            return update
        except Exception as exc:
            raise UpdateError(update.name, str(exc)) from exc

    @staticmethod
    def _install_waysession(source: Path = WAYSESSION_SOURCE,
                            destination: Path = WAYSESSION_DEST) -> None:
        """Promote the kiosk launcher from the updated checkout atomically."""
        if not source.is_file():
            raise UpdateError("vHackintosh", f"vhackintosh-waysession source missing: {source}")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", dir=destination.parent
            )
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as target, source.open("rb") as original:
                    shutil.copyfileobj(original, target)
                    target.flush()
                    os.fsync(target.fileno())
                temp_path.chmod(0o755)
                os.replace(temp_path, destination)
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise
        except Exception as exc:
            raise UpdateError("vHackintosh", f"could not install vhackintosh-waysession: {exc}") from exc

    @classmethod
    def _clone_staged(cls, path: Path, clone_fn: Callable[[str, Path], None]) -> Path:
        if not path.is_dir() or not any(path.iterdir()):
            raise UpdateError("reims-vgpu", f"source tree missing at {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Keep staging on the same filesystem as the destination for atomic rename.
        staging = Path(tempfile.mkdtemp(prefix=f".{path.name}.clone-", dir=path.parent)) / "checkout"
        try:
            clone_fn(REIMS_URL, staging)
            cls._check_checkout("reims-vgpu", staging, REIMS_URL)
            # The published checkout's build must be self-contained. Verify that
            # Git populated the pinned QEMU submodule rather than accepting an
            # empty gitlink, which produces a false-success promotion.
            qemu = staging / "vendor/qemu"
            if not (qemu / "meson.build").is_file():
                raise UpdateError("reims-vgpu", "clone has no populated vendor/qemu submodule")
            if not cls._has_build_sources(staging):
                raise UpdateError("reims-vgpu", "clone missing required build sources")
            return staging
        except Exception:
            shutil.rmtree(staging.parent, ignore_errors=True)
            raise

    @classmethod
    def update_repositories(cls, *, repo_paths: Optional[dict[str, Path]] = None,
                            clone_fn: Optional[Callable[[str, Path], None]] = None) -> list[RepositoryUpdate]:
        if os.geteuid() != 0 and repo_paths is None:
            raise UpdateError("permissions", "updating appliance checkouts under /opt requires root")
        updater = cls(repo_paths=repo_paths, clone_fn=clone_fn)
        if set(updater.paths) != {"vHackintosh", "reims-vgpu"}:
            raise UpdateError("configuration", "expected exactly the vHackintosh and reims-vgpu checkouts")
        engine_path = updater.paths["reims-vgpu"]
        staged: Optional[Path] = None
        original_missing = not (engine_path / ".git").exists()
        try:
            # Packaged source trees are the expected first-run case. Validate them
            # before cloning so an arbitrary directory cannot be replaced silently.
            if original_missing:
                if not cls._has_build_sources(engine_path):
                    raise UpdateError("reims-vgpu", f"invalid packaged source tree at {engine_path}")
                staged = cls._clone_staged(engine_path, updater.clone_fn)

            targets: dict[str, Path] = {
                "vHackintosh": updater.paths["vHackintosh"],
                "reims-vgpu": staged if staged is not None else engine_path,
            }
            plans = [cls._preflight(name, targets[name], cls._expected_url(name))
                     for name in ("vHackintosh", "reims-vgpu")]
            results = [item for item, _ in plans]
            try:
                completed: list[RepositoryUpdate] = []
                for result, (_planned, target) in zip(results, plans):
                    try:
                        cls._apply(result, target)
                        completed.append(result)
                    except UpdateError as exc:
                        advanced = "; ".join(
                            f"{item.name} {item.before[:12]} -> {item.after[:12]}"
                            for item in completed
                        ) or "none"
                        raise UpdateError(
                            result.name,
                            f"{exc}; earlier repositories advanced: {advanced}; retry is safe",
                        ) from exc
                if staged is not None:
                    backup = engine_path.with_name(engine_path.name + ".source-backup")
                    if backup.exists():
                        raise UpdateError("reims-vgpu", f"backup path already exists: {backup}")
                    # Keep the original tree until a verified clone is atomically promoted.
                    os.replace(engine_path, backup)
                    try:
                        os.replace(staged, engine_path)
                        shutil.rmtree(backup)
                        shutil.rmtree(staged.parent, ignore_errors=True)
                    except Exception:
                        if engine_path.exists():
                            shutil.rmtree(engine_path, ignore_errors=True)
                        os.replace(backup, engine_path)
                        raise
                return results
            except Exception:
                # If a repository already advanced, keep that valid state for idempotent retry.
                raise
        except Exception as exc:
            if staged is not None:
                shutil.rmtree(staged.parent, ignore_errors=True)
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError("update", str(exc)) from exc

    @classmethod
    def run_vhackintosh_update(cls) -> bool:
        """Update only the appliance's vHackintosh checkout.

        The reims-vgpu has its own release updater in the TUI. Keeping this
        action scoped to vHackintosh avoids fetching/building the GPU engine
        when the user only wants to refresh the appliance source.
        """
        from core import i18n

        console.print(f"[bold white]{i18n.t('tui.update.title')}[/bold white]")
        if os.geteuid() != 0:
            console.print(i18n.t("tui.update.not_root"))
            return False
        try:
            path = DEFAULT_VHACK_DIR
            update, target = cls._preflight("vHackintosh", path, VHACK_URL)
            result = cls._apply(update, target)
        except Exception as exc:
            repo = getattr(exc, "repo", "update")
            console.print(i18n.t("tui.update.error", repo=repo, error=str(exc)))
            console.print(i18n.t("tui.update.retry"))
            return False
        status = i18n.t("tui.update.repo_updated") if result.changed else i18n.t("tui.update.repo_unchanged")
        console.print(i18n.t("tui.update.repo_status", repo=result.name, status=status,
                             before=result.before[:12], after=result.after[:12]))
        # The source updater can advance vHackintosh while this process still has
        # the old Python modules loaded. Always flag that runtime state explicitly.
        console.print(i18n.t("tui.update.restart"))
        try:
            source = path / "appliance/archiso/airootfs/usr/local/bin/vhackintosh-waysession"
            cls._install_waysession(source=source)
        except UpdateError as exc:
            console.print(i18n.t("tui.update.error", repo=exc.repo, error=str(exc)))
            console.print(i18n.t("tui.update.failed"))
            console.print(i18n.t("tui.update.retry"))
            return False
        console.print(i18n.t("tui.update.waysession"))
        console.print(i18n.t("tui.update.success"))
        return True
