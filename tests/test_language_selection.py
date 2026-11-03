from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from core import i18n


ROOT = Path(__file__).resolve().parents[1]
LANG_CLI = ROOT / "cli/lang.py"


class LanguageSelectionTests(unittest.TestCase):
    def test_english_is_the_default(self):
        self.assertEqual(i18n.DEFAULT_LANGUAGE, "en")
        self.assertEqual(i18n.LANGUAGES["pt"], "Português (Brasil)")
        self.assertEqual(i18n.LANGUAGES["en"], "English (US)")

    def test_language_cli_persists_the_selected_english_choice(self):
        with tempfile.TemporaryDirectory() as config_dir:
            env = os.environ.copy()
            env["VHACKINTOSH_CONFIG_DIR"] = config_dir
            result = subprocess.run(
                [sys.executable, str(LANG_CLI), "--choose"],
                input="2\n",
                text=True,
                capture_output=True,
                env=env,
                check=True,
            )
            settings = json.loads((Path(config_dir) / "settings.json").read_text())

        self.assertEqual(settings["language"], "en")
        self.assertIn("1 - Português (Brasil)", result.stdout)
        self.assertIn("2 - English (US)", result.stdout)

    def test_language_cli_defaults_to_english_when_input_is_empty(self):
        with tempfile.TemporaryDirectory() as config_dir:
            env = os.environ.copy()
            env["VHACKINTOSH_CONFIG_DIR"] = config_dir
            subprocess.run(
                [sys.executable, str(LANG_CLI), "--choose"],
                input="\n",
                text=True,
                capture_output=True,
                env=env,
                check=True,
            )
            result = subprocess.run(
                [sys.executable, str(LANG_CLI), "--get-code"],
                text=True,
                capture_output=True,
                env=env,
                check=True,
            )

        self.assertEqual(result.stdout.strip(), "en")

    def test_boot_menus_render_english_using_the_persisted_language(self):
        shell = (
            ROOT
            / "appliance/archiso/airootfs/etc/profile.d/vhackintosh.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('"$helper" --get-code', shell)
        lang_cli = (ROOT / "cli/lang.py").read_text(encoding="utf-8")
        tui = (ROOT / "ui/tui.py").read_text(encoding="utf-8")
        self.assertIn('choice = input("Idioma / Language [1/2] (2): ")', lang_cli)
        self.assertIn('default="2"', tui)
        self.assertIn("Welcome to vHackintosh OS Appliance (Live USB)", shell)
        self.assertIn("Choose an operation:", shell)
        self.assertIn("Choose [1, 2, 3 or 0]: ", shell)


if __name__ == "__main__":
    unittest.main()
