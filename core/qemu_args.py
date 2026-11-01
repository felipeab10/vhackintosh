"""Restricted, token-preserving QEMU arguments accepted by the web editor.

These options are intentionally a small safe subset. The appliance owns all
device, storage, network, identity, process, control-channel and isolation
arguments; exposing the rest requires runtime capability discovery.
"""

from __future__ import annotations

from collections.abc import Sequence


_ALLOWED_PAIRS = {
    "-rtc": {"base=utc"},
    "-msg": {"timestamp=on"},
}


def validate_qemu_extra_args(tokens: Sequence[str]) -> list[str]:
    """Validate and copy a strictly allowlisted sequence of QEMU tokens."""
    if not isinstance(tokens, (list, tuple)) or len(tokens) > 64:
        raise ValueError("Argumentos QEMU devem conter no máximo 64 tokens.")
    if any(not isinstance(token, str) or not token or len(token) > 512 or "\x00" in token for token in tokens):
        raise ValueError("Argumentos QEMU devem ser tokens não vazios de até 512 caracteres.")

    index = 0
    seen: set[str] = set()
    while index < len(tokens):
        option = tokens[index]
        allowed_values = _ALLOWED_PAIRS.get(option)
        if allowed_values is None:
            raise ValueError(
                f"A opção QEMU {option!r} não está liberada pelo appliance. "
                "Opções de dispositivo, disco, rede, identidade, controle e isolamento são gerenciadas pelo sistema."
            )
        if option in seen:
            raise ValueError(f"A opção QEMU {option} pode aparecer somente uma vez.")
        if index + 1 >= len(tokens) or tokens[index + 1] not in allowed_values:
            expected = " ou ".join(sorted(allowed_values))
            raise ValueError(f"A opção QEMU {option} requer um valor permitido: {expected}.")
        seen.add(option)
        index += 2
    return list(tokens)
