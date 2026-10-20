"""vHackintosh terminal identity and optional startup animation."""

from __future__ import annotations

import os
import sys
import time

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

APP_NAME = "vHackintosh"
APP_SUBTITLE = "Dedicated Linux Appliance & macOS VM Manager"
VERSION = "1.0.0"

# Kept for callers that imported the original banner art directly.
ASCII_LOGO = r"""
        __  __            _    _       _                  _
 __   _|  \/  | __ _  ___| |  | | __ _| |_ ___  ___ _   _| |_
 \ \ / / |\/| |/ _` |/ __| |/\| |/ _` | __/ _ \/ __| | | | '_ \
  \ V /| |  | | (_| | (__\  /\  / (_| | || (_) \__ \ |_| | | | |
   \_/ |_|  |_|\__,_|\___|\/  \/ \__,_|\__\___/|___/\__,_|_| |_|
                                             Apple Silicon & KVM
"""

_ACCENT_START = (0, 229, 255)
_ACCENT_END = (72, 110, 255)
_SCAN_WIDTH = 44

_FONT_5X7 = {
    "V": ("10001", "10001", "10001", "10001", "01010", "01010", "00100"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "N": ("10001", "11001", "10101", "10101", "10011", "10001", "10001"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
}

_FONT_3X5 = {
    "V": ("101", "101", "101", "101", "010"),
    "H": ("101", "101", "111", "101", "101"),
    "A": ("010", "101", "111", "101", "101"),
    "C": ("011", "100", "100", "100", "011"),
    "K": ("101", "101", "110", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "N": ("101", "111", "111", "111", "101"),
    "T": ("111", "010", "010", "010", "010"),
    "O": ("010", "101", "101", "101", "010"),
    "S": ("011", "100", "010", "001", "110"),
}


def _pixel_wordmark(width: int | None) -> Text:
    """Render a large digital wordmark, compacting only on narrow terminals."""
    label = APP_NAME.upper()
    compact = width is not None and width < 72
    font = _FONT_3X5 if compact else _FONT_5X7
    rows = len(next(iter(font.values())))
    gap = "" if compact else " "
    result = Text()

    for row in range(rows):
        for index, char in enumerate(label):
            ratio = index / max(len(label) - 1, 1)
            color = tuple(
                round(start + (end - start) * ratio)
                for start, end in zip(_ACCENT_START, _ACCENT_END)
            )
            style = f"bold rgb({color[0]},{color[1]},{color[2]})"
            result.append(
                font[char][row].replace("1", "█").replace("0", " "),
                style=style,
            )
            if index < len(label) - 1:
                result.append(gap)
        if row < rows - 1:
            result.append("\n")
    return result


def _scan_line(position: int | None) -> Text:
    line = Text("━" * _SCAN_WIDTH, style="#123044")
    if position is not None:
        left = max(0, min(position, _SCAN_WIDTH - 1))
        right = min(left + 5, _SCAN_WIDTH)
        line.stylize("bold #00e5ff", left, right)
    else:
        line.stylize("#168ca5", 0, _SCAN_WIDTH)
    return line


def render_banner(width: int | None = None, *, scan_position: int | None = None) -> Panel:
    """Build the static header; width is optional for existing callers."""
    subtitle = Text(f"{APP_SUBTITLE}  •  v{VERSION}", style="#d6e7f5")
    capabilities = Text("KVM  /  VULKAN  /  OPENCORE", style="bold #49d9e8")
    content = Group(
        Align.center(_pixel_wordmark(width)),
        Align.center(subtitle),
        Align.center(capabilities),
        Align.center(_scan_line(scan_position)),
    )
    panel_width = min(width or 96, 104) if width else 96
    return Panel(
        content,
        title="[bold #00e5ff] VM CONTROL  /  KVM [/bold #00e5ff]",
        border_style="#168ca5",
        box=box.ROUNDED,
        padding=(0, 2),
        width=panel_width,
    )


def centered_banner(console: Console) -> Align:
    """Keep the compact HUD centered on both narrow and ultrawide consoles."""
    return Align.center(render_banner(console.width))


def _animation_enabled(console: Console) -> bool:
    """Avoid cursor/motion effects in pipes, CI, monochrome, or dumb terminals."""
    return (
        console.is_terminal
        and sys.stdout.isatty()
        and console.color_system is not None
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "") != "dumb"
        and not os.environ.get("CI")
        and not os.environ.get("VHACKINTOSH_NO_ANIMATION")
    )


def animate_banner(console: Console) -> None:
    """Play a brief, one-shot scan, with a static fallback for plain output."""
    if not _animation_enabled(console):
        console.print(centered_banner(console))
        return

    try:
        with Live(
            Align.center(render_banner(console.width, scan_position=0)),
            console=console,
            refresh_per_second=24,
            transient=False,
            screen=False,
        ) as live:
            for position in (0, 10, 20, 30, 39):
                live.update(Align.center(render_banner(console.width, scan_position=position)))
                time.sleep(0.045)
            live.update(centered_banner(console))
    except (OSError, RuntimeError):
        # A terminal may disappear between detection and Live startup. The
        # interface should still be usable and render a plain static header.
        console.print(centered_banner(console))
