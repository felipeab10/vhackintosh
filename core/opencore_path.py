"""Single source of truth for a VM's OpenCore EFI image path.

`vm.toml` declares the file under `[q35] opencore`, and the reims-ctl runtime
boots exactly that name. The repository used to hardcode `OpenCore.qcow2` in
several places while the runtime declared `opencore.qcow2`; on a case-sensitive
filesystem that produced two distinct images and a VM that booted without the
kexts written to the other one (the 18/11 `taeho1` incident).

Rules implemented here:
  - the declared basename wins, always;
  - an existing file that differs only by case is used as-is (the legacy
    layout), because it is the image the runtime already boots;
  - if both spellings exist and are distinct files, fail closed naming what
    `vm.toml` declares — never pick one and never write a third file.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

# What `core/reims_ctl.write_vm_toml` writes when the TOML has no `[q35] opencore`
# yet, and the layout every VM created before the rename used.
DEFAULT_OPENCORE_NAME = "OpenCore.qcow2"

# `core/config.VMManagerStore.update_vm` refuses an SMBIOS identity change on an
# existing VM until this exact file exists under the VM directory's
# `smbios-backup-before-regeneration/` — the pre-change image the apply job
# writes in `core/web_agent.py` before injecting. Both sides must keep using
# this constant or the guard silently stops protecting the VMs.
SMBIOS_BACKUP_IMAGE_NAME = "OpenCore.qcow2"


class OpencoreImageError(RuntimeError):
    """The EFI image name is ambiguous or the vm.toml cannot be trusted."""


def declared_opencore_name(vm_dir: str | Path) -> str:
    """Return the `[q35] opencore` basename declared by ``vm_dir/vm.toml``.

    Falls back to :data:`DEFAULT_OPENCORE_NAME` when the TOML or the key is
    absent (a VM being provisioned has no vm.toml yet). A value that is not a
    plain file name is refused: the image must live beside the TOML.
    """
    directory = Path(vm_dir)
    config_path = directory / "vm.toml"
    if not config_path.is_file():
        return DEFAULT_OPENCORE_NAME
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise OpencoreImageError(f"não consegui ler {config_path}: {exc}") from exc
    q35 = data.get("q35")
    name = q35.get("opencore") if isinstance(q35, dict) else None
    if name is None:
        return DEFAULT_OPENCORE_NAME
    if not isinstance(name, str) or not name.strip():
        raise OpencoreImageError(
            f"{config_path}: [q35] opencore precisa ser um nome de arquivo"
        )
    name = name.strip()
    candidate = Path(name)
    if candidate.is_absolute() or len(candidate.parts) != 1 or name in {".", ".."}:
        raise OpencoreImageError(
            f"{config_path}: [q35] opencore precisa ser um nome simples "
            f"(sem diretório), obtido {name!r}"
        )
    return name


def opencore_image_path(vm_dir: str | Path) -> Path:
    """Resolve the EFI image of ``vm_dir`` to the file the runtime boots.

    The returned path may not exist yet (creation); callers that need an
    existing image check it themselves. Raises :class:`OpencoreImageError` when
    two files differ only by case and are not the same underlying inode, naming
    the declared one.
    """
    directory = Path(vm_dir)
    declared = declared_opencore_name(directory)
    matches: list[Path] = []
    if directory.is_dir():
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            if entry.name.casefold() != declared.casefold():
                continue
            try:
                if not entry.exists(follow_symlinks=True):
                    continue
            except OSError:
                continue
            try:
                present = entry.is_file() or entry.is_symlink()
            except OSError:
                present = False
            if present:
                matches.append(entry)
    if len(matches) > 1:
        # Two candidates with matching case: check whether they resolve to the
        # same underlying inode (e.g. hard links on a case-insensitive FS view).
        real_paths = {os.path.realpath(match) for match in matches}
        if len(real_paths) > 1:
            raise OpencoreImageError(
                f"{directory} tem {', '.join(sorted(m.name for m in matches))} e o vm.toml declara "
                f"{declared!r}: remova o arquivo com a caixa errada antes de continuar "
                "(dois arquivos distintos no FS case-sensitive fariam o boot usar o "
                "que não foi editado)."
            )
        # Same file under two names (case variants of the same inode): prefer
        # the declared name or the canonical default over an arbitrary variant.
        names: dict[str, Path] = {m.name.casefold(): m for m in matches}
        chosen = names.get(declared.casefold())
        if chosen is None:
            chosen = names.get(DEFAULT_OPENCORE_NAME.casefold(), matches[0])
        return directory / chosen.name
    if matches:
        # Exactly one existing match: use it as-is even if its case differs from
        # the declaration, because it matches the runtime's actual legacy layout.
        return directory / matches[0].name
    # Nothing exists yet: create at the declared path.
    return directory / declared
