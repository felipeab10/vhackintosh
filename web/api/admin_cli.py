"""Local-only first-administrator setup for the appliance."""

from __future__ import annotations

import argparse
import getpass
import os
import pwd
import sys
from collections.abc import Callable
from pathlib import Path

from .app import SQLiteAuthStore


def create_first_admin(
    database: str | os.PathLike[str],
    username: str,
    password: str,
    confirmation: str,
) -> None:
    """Create the single administrator after local console confirmation."""
    normalized = username.strip()
    if not normalized or len(normalized) > 64 or any(not char.isprintable() for char in normalized):
        raise ValueError("o usuário precisa ter entre 1 e 64 caracteres imprimíveis")
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("a senha precisa ter entre 12 e 1024 caracteres")
    if password != confirmation:
        raise ValueError("as senhas não coincidem")
    store = SQLiteAuthStore(Path(database))
    if store.has_admin():
        raise FileExistsError("a conta administrativa já foi configurada")
    store.create_admin(normalized, password)


def setup_interactive(
    database: str | os.PathLike[str],
    *,
    read_username: Callable[[str], str] = input,
    read_secret: Callable[[str], str] = getpass.getpass,
) -> None:
    username = read_username("Usuário administrador: ")
    password = read_secret("Senha (mínimo 12 caracteres): ")
    confirmation = read_secret("Confirme a senha: ")
    create_first_admin(database, username, password, confirmation)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup",))
    parser.add_argument("--database", default=os.environ.get("VHACKINTOSH_WEB_AUTH_DB", "/var/lib/vhackintosh/web/auth.sqlite"))
    args = parser.parse_args(argv)
    try:
        service_uid = pwd.getpwnam("vhackintosh-web").pw_uid
    except KeyError:
        service_uid = -1
    if os.geteuid() not in {0, service_uid}:
        print("Configuração inicial disponível somente para o operador local autorizado.", file=sys.stderr)
        return 1
    if not sys.stdin.isatty() or os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        print("Execute a configuração em um terminal local do appliance; sessões remotas são recusadas.", file=sys.stderr)
        return 1
    try:
        setup_interactive(args.database)
    except (FileExistsError, ValueError) as exc:
        print(f"Não foi possível configurar o administrador: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Não foi possível configurar o administrador: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("Conta administrativa criada. Entre no painel com as credenciais configuradas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
