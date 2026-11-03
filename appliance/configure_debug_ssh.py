#!/usr/bin/env python3
"""Add temporary, key-only root SSH access to a debug appliance image."""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import re
from pathlib import Path


_KEY_TYPE = "ssh-ed25519"


def _validate_public_key(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        raise ValueError("debug SSH public-key file must contain exactly one key")
    fields = lines[0].split()
    if len(fields) < 2 or fields[0] != _KEY_TYPE:
        raise ValueError("debug SSH key must be a single ssh-ed25519 public key")
    try:
        decoded = base64.b64decode(fields[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("debug SSH public key has invalid base64 data") from exc
    if not decoded or len(fields[1]) > 16384 or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", fields[1]):
        raise ValueError("debug SSH public key is malformed")
    # The authorized_keys parser validates the full SSH wire format at runtime;
    # require the encoded key's embedded type to match its textual type here.
    if len(decoded) < 4:
        raise ValueError("debug SSH public key is truncated")
    type_size = int.from_bytes(decoded[:4], "big")
    if type_size != len(_KEY_TYPE) or decoded[4 : 4 + type_size].decode("ascii", "ignore") != _KEY_TYPE:
        raise ValueError("debug SSH public key type does not match its encoding")
    comment = " ".join(fields[2:]) if len(fields) > 2 else "vhackintosh-temporary-debug"
    if any(ord(char) < 32 for char in comment):
        raise ValueError("debug SSH key comment contains control characters")
    return f"restrict {fields[0]} {fields[1]} {comment}"


def configure_debug_ssh(rootfs: Path, public_key_file: Path) -> None:
    """Configure passwordless-key-only SSH and diagnostics in an image root."""
    rootfs = rootfs.resolve()
    authorized_key = _validate_public_key(public_key_file.read_text(encoding="ascii"))

    ssh_dir = rootfs / "root/.ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    (ssh_dir / "authorized_keys").write_text(authorized_key + "\n", encoding="ascii")
    os.chmod(ssh_dir, 0o700)
    os.chmod(ssh_dir / "authorized_keys", 0o600)

    ssh_config_dir = rootfs / "etc/ssh/sshd_config.d"
    ssh_config_dir.mkdir(parents=True, exist_ok=True)
    ssh_config = ssh_config_dir / "00-vhackintosh-debug.conf"
    ssh_config.write_text(
        "# Temporary appliance debugging access; remove after diagnosis.\n"
        "PermitRootLogin prohibit-password\n"
        "PasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\n"
        "PubkeyAuthentication yes\n"
        "AuthenticationMethods publickey\n"
        "LogLevel VERBOSE\n",
        encoding="ascii",
    )
    os.chmod(ssh_config, 0o600)

    wants = rootfs / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True, exist_ok=True)
    sshd_link = wants / "sshd.service"
    sshd_link.unlink(missing_ok=True)
    sshd_link.symlink_to("/usr/lib/systemd/system/sshd.service")

    # Preserve logs across a power cycle so failed boots can still be inspected.
    journal_config_dir = rootfs / "etc/systemd/journald.conf.d"
    journal_config_dir.mkdir(parents=True, exist_ok=True)
    journal_config = journal_config_dir / "90-vhackintosh-debug.conf"
    journal_config.write_text(
        "[Journal]\nStorage=persistent\nSystemMaxUse=200M\nRuntimeMaxUse=50M\n",
        encoding="ascii",
    )
    os.chmod(journal_config, 0o644)
    (rootfs / "var/log/journal").mkdir(parents=True, exist_ok=True)

    # Keep verbose compositor logs limited to this explicitly requested debug ISO.
    display_dropin = rootfs / "etc/systemd/system/vhackintosh-display.service.d"
    display_dropin.mkdir(parents=True, exist_ok=True)
    (display_dropin / "10-vhackintosh-diagnostics.conf").write_text(
        "[Service]\nEnvironment=VHACKINTOSH_DIAGNOSTICS=1\n", encoding="ascii"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rootfs", type=Path)
    parser.add_argument("public_key", type=Path)
    args = parser.parse_args()
    configure_debug_ssh(args.rootfs, args.public_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
