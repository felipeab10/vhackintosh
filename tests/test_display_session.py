from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from core.display_session import discover_graphical_environment


class DisplaySessionTests(unittest.TestCase):
    def test_discovers_only_validated_variables_from_active_wayland_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc_root = root / "proc"
            process = proc_root / "42"
            process.mkdir(parents=True)
            runtime_root = root / "run/user"
            runtime = runtime_root / str(os.getuid())
            runtime.mkdir(parents=True, mode=0o700)
            wayland = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            wayland.bind(str(runtime / "wayland-1"))
            bus = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            bus.bind(str(runtime / "bus"))
            (process / "environ").write_bytes(
                b"WAYLAND_DISPLAY=wayland-1\0"
                + f"XDG_RUNTIME_DIR={runtime}\0".encode()
                + b"XDG_SESSION_TYPE=wayland\0"
                + f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus\0".encode()
                + b"SECRET_TOKEN=must-not-be-copied\0"
            )

            def fake_run(argv, **_kwargs):
                if argv[1] == "list-sessions":
                    return subprocess.CompletedProcess(argv, 0, "7 1000 felipe seat0 tty2\n", "")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    f"Active=yes\nRemote=no\nType=wayland\nClass=user\nUser={os.getuid()}\n",
                    "",
                )

            env = discover_graphical_environment(
                proc_root=proc_root,
                runtime_root=runtime_root,
                run=fake_run,
            )

            self.assertEqual(env["XDG_RUNTIME_DIR"], str(runtime))
            self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-1")
            self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], f"unix:path={runtime}/bus")
            self.assertNotIn("SECRET_TOKEN", env)
            bus.close()
            wayland.close()

    def test_does_not_forward_x11_display_without_its_authorization_cookie(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc_root = root / "proc"
            process = proc_root / "42"
            process.mkdir(parents=True)
            runtime_root = root / "run/user"
            runtime = runtime_root / str(os.getuid())
            runtime.mkdir(parents=True, mode=0o700)
            wayland = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            wayland.bind(str(runtime / "wayland-1"))
            (process / "environ").write_bytes(
                b"WAYLAND_DISPLAY=wayland-1\0"
                + f"XDG_RUNTIME_DIR={runtime}\0".encode()
                + b"XDG_SESSION_TYPE=wayland\0DISPLAY=:0\0"
            )

            def fake_run(argv, **_kwargs):
                if argv[1] == "list-sessions":
                    return subprocess.CompletedProcess(argv, 0, "7 1000 felipe seat0 tty2\n", "")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    f"Active=yes\nRemote=no\nType=wayland\nClass=user\nUser={os.getuid()}\n",
                    "",
                )

            with patch("core.display_session.Path.is_socket", return_value=True):
                env = discover_graphical_environment(
                    proc_root=proc_root,
                    runtime_root=runtime_root,
                    run=fake_run,
                )

            self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-1")
            self.assertNotIn("DISPLAY", env)
            self.assertNotIn("XAUTHORITY", env)
            wayland.close()

    def test_ignores_sessions_without_an_active_local_graphical_login(self):
        def fake_run(argv, **_kwargs):
            if argv[1] == "list-sessions":
                return subprocess.CompletedProcess(argv, 0, "7 1000 felipe seat0 tty2\n", "")
            return subprocess.CompletedProcess(
                argv,
                0,
                "Active=no\nRemote=no\nType=wayland\nClass=user\nUser=1000\n",
                "",
            )

        with tempfile.TemporaryDirectory() as temporary:
            env = discover_graphical_environment(
                proc_root=Path(temporary) / "proc",
                runtime_root=Path(temporary) / "run/user",
                run=fake_run,
            )
        self.assertEqual(env, {})


if __name__ == "__main__":
    unittest.main()
