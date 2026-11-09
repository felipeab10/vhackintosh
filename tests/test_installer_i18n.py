from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "appliance/archiso/airootfs/usr/local/lib/vhackintosh-install-i18n.sh"
INSTALLER = ROOT / "appliance/archiso/airootfs/usr/local/bin/vhackintosh-install"
PROFILE = ROOT / "appliance/archiso/airootfs/etc/profile.d/vhackintosh.sh"
TUI = ROOT / "ui/tui.py"


def _messages(language: str, *keys: str) -> list[str]:
    script = '. "$1"; VHACK_INSTALL_LANGUAGE="$2"; shift 2; for key in "$@"; do vhack_install_msg "$key"; printf "\\n"; done'
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(CATALOG), language, *keys],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def test_installer_partitioning_screen_uses_english_catalog() -> None:
    actual = _messages(
        "en",
        "partition_title",
        "automatic_mode",
        "automatic_desc1",
        "manual_mode",
        "partition_prompt",
    )

    assert actual == [
        "Choose a Partitioning Method",
        "Automatic (recommended — erase the entire disk)",
        "• Automatically creates an EFI partition (1 GB, FAT32) and a root partition (Ext4)",
        "Manual / Advanced (visual cfdisk manager)",
        "Choose an option [1 or 2]: ",
    ]
    assert _messages("en", "selected_disk") == ["Selected disk: %s"]


def test_installer_catalog_keeps_portuguese_default() -> None:
    assert _messages("pt", "partition_title", "partition_prompt") == [
        "Escolha o Método de Particionamento",
        "Escolha a opção [1 ou 2]: ",
    ]


def test_selected_language_is_forwarded_to_installer_entry_points() -> None:
    profile = PROFILE.read_text(encoding="utf-8")
    tui = TUI.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")

    assert 'VHACK_INSTALL_LANGUAGE="$VHACK_LANGUAGE" vhackintosh-install' in profile
    assert 'install_env["VHACK_INSTALL_LANGUAGE"] = i18n.get_language()' in tui
    assert 'f"VHACK_INSTALL_LANGUAGE={install_env[\'VHACK_INSTALL_LANGUAGE\']}"' in tui
    assert 'INSTALL_I18N="/usr/local/lib/vhackintosh-install-i18n.sh"' in installer
