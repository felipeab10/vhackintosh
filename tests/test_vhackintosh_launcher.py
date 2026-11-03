from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin/vhackintosh"


@unittest.skipIf(os.geteuid() == 0, "launcher privilege test requires an unprivileged user")
class VhackintoshLauncherTests(unittest.TestCase):
    def test_agent_backed_tui_reuses_control_agent_with_sudo(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake_bin = base / "bin"
            fake_bin.mkdir()
            args_file = base / "sudo-args"
            fake_sudo = fake_bin / "sudo"
            fake_sudo.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$VH_TEST_SUDO_ARGS\"\n",
                encoding="utf-8",
            )
            fake_sudo.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "VHACKINTOSH_AGENT_REQUIRED": "1",
                "VH_TEST_SUDO_ARGS": str(args_file),
            }

            result = subprocess.run(
                ["bash", str(LAUNCHER), "list"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(args_file.read_text(encoding="utf-8").splitlines(), [
                "--",
                "/usr/bin/env",
                f"PYTHONPATH={ROOT}",
                "/usr/bin/python3",
                str(ROOT / "cli/main.py"),
                "list",
            ])

    def test_help_does_not_escalate(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            args_file = Path(directory) / "sudo-args"
            fake_sudo = fake_bin / "sudo"
            fake_sudo.write_text(
                "#!/usr/bin/env bash\n"
                "printf called > \"$VH_TEST_SUDO_ARGS\"\n",
                encoding="utf-8",
            )
            fake_sudo.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "VHACKINTOSH_AGENT_REQUIRED": "1",
                "VH_TEST_SUDO_ARGS": str(args_file),
            }

            result = subprocess.run(
                ["bash", str(LAUNCHER), "--help"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Uso: vhackintosh", result.stdout)
            self.assertFalse(args_file.exists())


if __name__ == "__main__":
    unittest.main()
