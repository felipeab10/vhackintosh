"""Fixtures compartilhadas dos testes do vHackintosh.

`tests/test_updater.py` monta repositórios Git de verdade em disco e recebe um
diretório-base por parâmetro (`base: Path`); sem esta fixture o pytest reprova
nove testes no setup com "fixture 'base' not found".
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """Diretório temporário e isolado onde os testes criam remotes/worktrees."""
    return tmp_path
