"""
vHackintosh — Troca de idioma do appliance (task 2.5)

`vhackintosh-lang --choose` abre a tela interativa usada antes do menu de
instalação do ISO Live; `vhackintosh-lang pt|en` — valida a entrada, persiste em
`~/.config/vhackintosh/settings.json` e vale na próxima abertura da TUI e em
reboots (o settings sobrevive ao live). Sem argumentos, mostra o idioma atual
e os suportados. Entrada inválida: recusa, mantém o atual e lista os aceitos
(especificação de `specs/language-selection`).
"""

from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.i18n import (
    LANGUAGES,
    get_language,
    has_language_choice,
    set_language,
)


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]

    # The live ISO presents the choice before its installation menu. Keeping
    # the picker here lets the shell boot flow and the TUI share the same
    # persisted setting without starting the full VM manager.
    if args == ["--choose"]:
        if has_language_choice():
            return 0
        print("Escolha o idioma do appliance — Choose the appliance language")
        options = list(LANGUAGES)
        for index, code in enumerate(options, 1):
            print(f"  {index} - {LANGUAGES[code]}")
        while True:
            try:
                choice = input("Idioma / Language [1/2] (1): ").strip() or "1"
            except (EOFError, KeyboardInterrupt):
                # Preserve the appliance's Portuguese default if input is
                # unavailable, so unattended boots remain usable.
                choice = "1"
            if choice in {str(i) for i in range(1, len(options) + 1)}:
                code = options[int(choice) - 1]
                set_language(code)
                print(f"Idioma / Language: {LANGUAGES[code]}")
                return 0
            print("Opção inválida / Invalid choice. Select 1 or 2.")

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
