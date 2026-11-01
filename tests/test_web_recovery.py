from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web.api.app import ActivationCode, SQLiteAuthStore


HELPER = Path(__file__).resolve().parents[1] / "appliance/archiso/airootfs/usr/local/bin/vhackintosh-web-recover"
SPEC = importlib.util.spec_from_loader("vhackintosh_web_recover", SourceFileLoader("vhackintosh_web_recover", str(HELPER)))
assert SPEC is not None and SPEC.loader is not None
RECOVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECOVERY)


class WebRecoveryTests(unittest.TestCase):
    def test_bootstrap_activation_expires_after_ten_minutes_and_is_one_use(self):
        import time

        now = time.time()
        with patch("web.api.app.time.time", return_value=now):
            code = ActivationCode("single-use-code")
            expired = ActivationCode("expired-code")
        with patch("web.api.app.time.time", return_value=now + 599):
            self.assertTrue(code.consume("single-use-code"))
        with patch("web.api.app.time.time", return_value=now + 601):
            self.assertFalse(code.consume("expired-code"))
        with patch("web.api.app.time.time", return_value=now + 601):
            self.assertFalse(code.consume("single-use-code"))

    def test_resets_argon2_password_and_revokes_existing_sessions(self):
        with tempfile.TemporaryDirectory(prefix="vhack-web-recovery-") as tmp:
            database = Path(tmp) / "auth.sqlite"
            auth = SQLiteAuthStore(database)
            auth.create_admin("admin", "old-password-long-enough")
            token, _csrf, _expires = auth.create_session("admin")
            self.assertIsNotNone(auth.get_session(token))

            RECOVERY.reset_password(database, "new-password-long-enough", "new-password-long-enough")

            self.assertFalse(auth.authenticate("admin", "old-password-long-enough"))
            self.assertTrue(auth.authenticate("admin", "new-password-long-enough"))
            self.assertIsNone(auth.get_session(token))

    def test_refuses_mismatch_missing_admin_and_symlink_database(self):
        with tempfile.TemporaryDirectory(prefix="vhack-web-recovery-") as tmp:
            root = Path(tmp)
            empty = root / "empty.sqlite"
            empty_auth = SQLiteAuthStore(empty)
            with self.assertRaisesRegex(ValueError, "não coincidem"):
                RECOVERY.reset_password(empty, "new-password-long-enough", "different-password-long")
            with self.assertRaisesRegex(LookupError, "nenhum administrador"):
                RECOVERY.reset_password(empty, "new-password-long-enough", "new-password-long-enough")

            real = root / "real.sqlite"
            SQLiteAuthStore(real).create_admin("admin", "old-password-long-enough")
            link = root / "link.sqlite"
            link.symlink_to(real)
            with self.assertRaises(PermissionError):
                RECOVERY.reset_password(link, "new-password-long-enough", "new-password-long-enough")

    def test_command_refuses_non_root_and_remote_sessions_before_reading_database(self):
        with patch.object(RECOVERY.os, "geteuid", return_value=1000):
            self.assertEqual(RECOVERY.main(["--database", "/missing/auth.sqlite"]), 1)
        with patch.object(RECOVERY.os, "geteuid", return_value=0), \
             patch.dict(RECOVERY.os.environ, {"SSH_CONNECTION": "192.0.2.1 50000 192.0.2.2 22"}), \
             patch.object(RECOVERY.sys.stdin, "isatty", return_value=True):
            self.assertEqual(RECOVERY.main(["--database", "/missing/auth.sqlite"]), 1)


if __name__ == "__main__":
    unittest.main()
