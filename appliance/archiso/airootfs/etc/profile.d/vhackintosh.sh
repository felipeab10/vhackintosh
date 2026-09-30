# ==============================================================================
# vHackintosh Appliance - Autostart TUI on tty1
# ==============================================================================

# Garante que /usr/local/bin e npm global estejam no PATH
export PATH="/usr/local/bin:${PATH}"

if [ "$(tty)" = "/dev/tty1" ]; then
    # Inicia subsistema de áudio PipeWire para a sessão de usuário
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user start pipewire pipewire-pulse wireplumber 2>/dev/null || true
    fi

    # Limpa a tela e executa o gerenciador Kiosk do vHackintosh
    clear
    if command -v vhackintosh >/dev/null 2>&1; then
        vhackintosh
    fi
fi
