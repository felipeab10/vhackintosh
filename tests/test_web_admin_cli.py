from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from web.api.admin_cli import create_first_admin, setup_interactive
from web.api.app import SQLiteAuthStore


class WebAdminCliTests(unittest.TestCase):
    def test_local_setup_creates_one_admin_with_confirmed_password(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "auth.sqlite"
            answers = iter((" felipe ", "a strong local password", "a strong local password"))
            setup_interactive(
                database,
                read_username=lambda _prompt: next(answers),
                read_secret=lambda _prompt: next(answers),
            )

            store = SQLiteAuthStore(database)
            self.assertTrue(store.has_admin())
            self.assertTrue(store.authenticate("felipe", "a strong local password"))
            self.assertFalse(store.authenticate("felipe", "wrong password"))
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(FileExistsError, "já foi configurada"):
                create_first_admin(database, "another", "another strong password", "another strong password")

    def test_rejects_mismatched_or_short_password_before_creating_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "auth.sqlite"
            with self.assertRaisesRegex(ValueError, "não coincidem"):
                create_first_admin(database, "admin", "a sufficiently long password", "different password")
            with self.assertRaisesRegex(ValueError, "12 e 1024"):
                create_first_admin(database, "admin", "short", "short")
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
