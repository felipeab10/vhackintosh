from __future__ import annotations

import base64
import struct
import tempfile
import unittest
from pathlib import Path

from appliance.configure_debug_ssh import configure_debug_ssh
from appliance.validate_iso import validate_debug_ssh_payload


class DebugSshPackagingTests(unittest.TestCase):
    def _public_key(self, directory: Path) -> Path:
        wire = struct.pack(">I", len("ssh-ed25519")) + b"ssh-ed25519" + bytes(range(32))
        path = directory / "debug.pub"
        path.write_text(
            "ssh-ed25519 " + base64.b64encode(wire).decode("ascii") + " local-test-key\n",
            encoding="ascii",
        )
        return path

    def test_adds_key_only_ssh_and_persistent_diagnostics(self):
        with tempfile.TemporaryDirectory(prefix="vhack-debug-ssh-") as tmp:
            rootfs = Path(tmp) / "rootfs"
            rootfs.mkdir()
            key = self._public_key(Path(tmp))

            configure_debug_ssh(rootfs, key)

            ssh_dir = rootfs / "root/.ssh"
            auth_keys = ssh_dir / "authorized_keys"
            unit = rootfs / "usr/lib/systemd/system/sshd.service"
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text("[Service]\nExecStart=/usr/bin/sshd -D\n", encoding="ascii")
            (unit.parent / "sshdgenkeys.service").write_text("[Service]\nExecStart=/usr/bin/ssh-keygen -A\n", encoding="ascii")
            self.assertEqual(ssh_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(auth_keys.stat().st_mode & 0o777, 0o600)
            self.assertTrue(auth_keys.read_text().startswith("restrict ssh-ed25519 "))
            self.assertIn("PermitRootLogin prohibit-password", (rootfs / "etc/ssh/sshd_config.d/00-vhackintosh-debug.conf").read_text())
            self.assertIn("AuthenticationMethods publickey", (rootfs / "etc/ssh/sshd_config.d/00-vhackintosh-debug.conf").read_text())
            self.assertEqual(
                (rootfs / "etc/systemd/system/multi-user.target.wants/sshd.service").readlink(),
                Path("/usr/lib/systemd/system/sshd.service"),
            )
            self.assertTrue((rootfs / "etc/systemd/journald.conf.d/90-vhackintosh-debug.conf").is_file())
            self.assertTrue((rootfs / "etc/systemd/system/vhackintosh-display.service.d/10-vhackintosh-diagnostics.conf").is_file())
            self.assertTrue(validate_debug_ssh_payload(rootfs))

    def test_rejects_non_ed25519_key_and_multiple_key_lines(self):
        with tempfile.TemporaryDirectory(prefix="vhack-debug-ssh-invalid-") as tmp:
            directory = Path(tmp)
            invalid = directory / "invalid.pub"
            invalid.write_text("ssh-rsa YWJj test\n", encoding="ascii")
            rootfs = directory / "rootfs"
            rootfs.mkdir()
            with self.assertRaisesRegex(ValueError, "ssh-ed25519"):
                configure_debug_ssh(rootfs, invalid)

            invalid.write_text("ssh-ed25519 YWJj test\nssh-ed25519 YWJj other\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "exactly one key"):
                configure_debug_ssh(rootfs, invalid)

    def test_builder_exposes_explicit_debug_ssh_mode(self):
        root = Path(__file__).resolve().parents[1]
        build = (root / "appliance/build-iso.sh").read_text(encoding="utf-8")
        self.assertIn("--debug-ssh", build)
        self.assertIn("configure_debug_ssh.py", build)
        self.assertIn("vhackintosh-appliance-debug.pub", build)
        self.assertIn('["/root/.ssh"]="0:0:700"', build)
        installer = (root / "appliance/archiso/airootfs/usr/local/bin/vhackintosh-install").read_text(encoding="utf-8")
        self.assertIn('"/mnt/etc/ssh/sshd_config.d/00-vhackintosh-debug.conf"', installer)
        self.assertIn('"${SYSTEMD_WANTS_DIR}/sshd.service"', installer)


if __name__ == "__main__":
    unittest.main()
