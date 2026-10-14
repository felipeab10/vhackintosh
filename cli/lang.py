"""
vHackintosh — Troca de idioma do appliance (task 2.5)

`vhackintosh-lang pt|en` — valida a entrada, persiste em
`~/.config/vhackintosh/settings.json` e vale na próxima abertura da TUI e em
reboots (o settings sobrevive ao live). Sem argumentos, mostra o idioma atual
e os suportados. Entrada inválida: recusa, mantém o atual e lista os aceitos
(especificação de `specs/language-selection`).
"""

from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.i18n import LANGUAGES, get_language, set_language


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]

    if not args:
        current = get_language()
        print(f"Idioma atual / Current language: {current} ({LANGUAGES[current]})")
        print("Uso / Usage: vhackintosh-lang " + "|".join(LANGUAGES))
        print("Suportados / Supported: " + ", ".join(
            f"{code} = {label}" for code, label in LANGUAGES.items()
        ))
        return 0

    if len(args) > 1:
        print(f"Erro: um único idioma por vez (recebido: {', '.join(args)})")
        return 2

    choice = args[0]
    if choice not in LANGUAGES:
        current = get_language()
        print(f"Idioma não suportado: {choice!r} — o idioma atual ({current}) foi mantido.")
        print("Suportados: " + ", ".join(
            f"{code} = {label}" for code, label in LANGUAGES.items()
        ))
        return 2

    set_language(choice)
    print(f"Idioma do appliance agora: {choice} ({LANGUAGES[choice]}).")
    print("Language set. It applies to the next TUI start and survives reboots.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
