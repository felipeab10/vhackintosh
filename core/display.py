"""Validation for the structured display settings supported by Reims."""

from __future__ import annotations

import math
import re
import struct


def _f32(value: float) -> float:
    return struct.unpack("f", struct.pack("f", value))[0]


def hidpi_supported(width: int) -> bool:
    """Mirror reims-ctl's apple-gfx density checks for HiDPI modes."""
    if width < 1920:
        return False
    ppi = math.floor(width * 25.46 / 295.0 + 0.5)
    mm_float = _f32(_f32(width) / _f32(ppi / 25.46))
    millimeters = int(mm_float + 0.5)
    centimeters = (millimeters + 9) // 10
    hidpi = width * 35 >= 2021 * centimeters
    if hidpi:
        return width >= 1920 and centimeters >= 29
    return False


def validate_display_options(resolution: str, hidpi: bool, refresh_hz: int | None) -> str:
    """Validate and normalize the display fields accepted by the Reims TOML."""
    if not isinstance(resolution, str):
        raise ValueError("Resolução de tela inválida.")
    normalized = resolution.strip().lower().replace("×", "x")
    width = None
    if normalized != "host":
        match = re.fullmatch(r"([0-9]{1,5})x([0-9]{1,5})", normalized)
        if not match:
            raise ValueError("Resolução deve ser host ou largura x altura entre 1 e 65535 pixels.")
        width, height = (int(part) for part in match.groups())
        if not 1 <= width <= 65535 or not 1 <= height <= 65535:
            raise ValueError("Largura e altura devem ficar entre 1 e 65535 pixels.")
    if hidpi and (width is None or not hidpi_supported(width)):
        raise ValueError("HiDPI não é suportado para esta resolução pelo runtime Reims.")
    if refresh_hz is not None and (
        isinstance(refresh_hz, bool) or not isinstance(refresh_hz, int) or not 1 <= refresh_hz <= 1000
    ):
        raise ValueError("Taxa de atualização deve ficar entre 1 e 1000 Hz.")
    return normalized
