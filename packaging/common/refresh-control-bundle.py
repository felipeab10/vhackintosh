#!/usr/bin/env python3
"""Finalize the shared versioned Control bundle after native deps are staged."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from core.web_bundle import write_bundle_metadata


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: refresh-control-bundle.py PACKAGE_ROOT")
    root = Path(sys.argv[1])
    web_root = root / "opt/vhackintosh/web"
    current = web_root / "current"
    if not current.is_symlink():
        raise SystemExit("Control package staging directory has no current symlink")
    stage = current.resolve(strict=True)
    if stage.parent != (web_root / "bundles").resolve(strict=True) or stage.name != ".control-staging":
        raise SystemExit("Control package staging symlink is unsafe or unexpected")
    metadata = write_bundle_metadata(stage, runtime_release="control-independent")
    destination = stage.parent / metadata.bundle_id
    if destination.exists():
        raise SystemExit("Control bundle ID already exists in the package staging tree")
    os.replace(stage, destination)
    link = web_root / f".current.{os.getpid()}"
    link.symlink_to(f"bundles/{metadata.bundle_id}")
    os.replace(link, current)
    version_dir = root / "usr/lib/vhackintosh"
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "control-bundle-id").write_text(metadata.bundle_id + "\n", encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
