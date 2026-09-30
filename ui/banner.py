"""
vHackintosh — ASCII Art, Branding & Theme Colors
"""

from rich.text import Text
from rich.panel import Panel
from rich.align import Align

APP_NAME = "vHackintosh"
APP_SUBTITLE = "Dedicated Linux Appliance & macOS VM Manager"
VERSION = "1.0.0"

ASCII_LOGO = r"""
        __  __            _    _       _                  _     
 __   _|  \/  | __ _  ___| |  | | __ _| |_ ___  ___ _   _| |_   
 \ \ / / |\/| |/ _` |/ __| |/\| |/ _` | __/ _ \/ __| | | | '_ \  
  \ V /| |  | | (_| | (__\  /\  / (_| | || (_) \__ \ |_| | | | | 
   \_/ |_|  |_|\__,_|\___|\/  \/ \__,_|\__\___/|___/\__,_|_| |_| 
                                             Apple Silicon & KVM 
"""


def render_banner() -> Panel:
    logo_text = Text(ASCII_LOGO, style="bold cyan")
    sub_text = Text(f"{APP_SUBTITLE} • v{VERSION}", style="bold white on blue")
    return Panel(
        Align.center(logo_text + Text("\n") + sub_text),
        border_style="cyan",
        padding=(0, 1),
    )
