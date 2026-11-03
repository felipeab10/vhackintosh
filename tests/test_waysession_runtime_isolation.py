"""Regression coverage for independent Sway launch state per VM session."""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import reims_ctl

ROOT = Path(__file__).resolve().parents[1]
WAYSESSION = (
    ROOT / "appliance/archiso/airootfs/usr/local/bin/vhackintosh-waysession"
)


class WaySessionRuntimeIsolationTests(unittest.TestCase):
    def test_each_vm_gets_its_own_sway_config_and_client_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            # The launcher only checks the PipeWire socket; a bound socket
            # keeps the test independent from any audio daemons on the host.
            pipewire = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            pipewire.bind(str(runtime / "pipewire-0"))

            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_sway = fake_bin / "sway"
            fake_sway.write_text(
                "#!/bin/sh\n"
                "conf=\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = -c ]; then conf=$2; shift 2; else shift; fi\n"
                "done\n"
                "app=$(awk '$1 == \"exec\" { print $2; exit }' \"$conf\")\n"
                "WAYLAND_DISPLAY=wayland-test \"$app\"\n",
                encoding="utf-8",
            )
            fake_sway.chmod(0o755)
            fake_swaymsg = fake_bin / "swaymsg"
            fake_swaymsg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_swaymsg.chmod(0o755)
            base_conf = root / "sway.conf"
            base_conf.write_text("xwayland disable\nexec /run/vhackintosh/sway-app.sh\n", encoding="utf-8")
            sessions = [root / "vm-a", root / "vm-b"]
            for session in sessions:
                session.mkdir(mode=0o700)
                env = os.environ.copy()
                env.update({
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "XDG_RUNTIME_DIR": str(runtime),
                    "RUNTIME_DIRECTORY": str(session),
                    "VHACK_SWAY_CONF": str(base_conf),
                })
                completed = subprocess.run(
                    [str(WAYSESSION), "/usr/bin/true"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

            app_a = sessions[0] / "sway-app.sh"
            app_b = sessions[1] / "sway-app.sh"
            conf_a = (sessions[0] / "sway.conf").read_text(encoding="utf-8")
            conf_b = (sessions[1] / "sway.conf").read_text(encoding="utf-8")
            self.assertTrue(app_a.is_file())
            self.assertTrue(app_b.is_file())
            self.assertIn(str(app_a), conf_a)
            self.assertIn(str(app_b), conf_b)
            self.assertNotEqual(app_a, app_b)
            self.assertNotIn("/run/vhackintosh/sway-app.sh", conf_a)
            self.assertNotIn("/run/vhackintosh/sway-app.sh", conf_b)
            pipewire.close()

    def test_appliance_starts_and_reuses_the_shared_display_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            (runtime / "wayland-display").write_text("wayland-1\n", encoding="utf-8")
            socket_path = runtime / "wayland-1"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(socket_path))
            self.addCleanup(server.close)
            env = {}
            with (
                mock.patch.object(reims_ctl, "SHARED_DISPLAY_UNIT") as unit,
                mock.patch.object(reims_ctl, "SHARED_DISPLAY_RUNTIME", runtime),
                mock.patch.object(
                    reims_ctl.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as run,
            ):
                unit.is_file.return_value = True
                argv, missing = reims_ctl.session_argv(["reims-ctl", "start", "vm-a"], env)

        self.assertEqual(argv, ["reims-ctl", "start", "vm-a"])
        self.assertFalse(missing)
        self.assertEqual(env["XDG_RUNTIME_DIR"], str(runtime))
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-1")
        run.assert_called_once_with(
            ["/usr/bin/systemctl", "start", "vhackintosh-display.service"],
            capture_output=True,
            text=True,
            timeout=135,
            check=False,
        )

    def test_appliance_refuses_to_start_a_vm_when_shared_display_fails(self):
        with (
            mock.patch.object(reims_ctl, "SHARED_DISPLAY_UNIT") as unit,
            mock.patch.object(
                reims_ctl.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="DRM unavailable"),
            ),
        ):
            unit.is_file.return_value = True
            with self.assertRaisesRegex(RuntimeError, "DRM unavailable"):
                reims_ctl.session_argv(["reims-ctl", "start", "vm-a"], {})


if __name__ == "__main__":
    unittest.main()
