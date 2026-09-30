#!/usr/bin/env bash
# Instala o tema GRUB do vHackintosh no sistema host

set -euo pipefail

THEME_DIR="/boot/grub/themes/vhackintosh"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Instalando tema vHackintosh no GRUB..."
sudo mkdir -p "$THEME_DIR"
sudo cp -r "$SOURCE_DIR"/* "$THEME_DIR"/

# Configura no /etc/default/grub se existir
if [ -f /etc/default/grub ]; then
    sudo sed -i '/GRUB_THEME=/d' /etc/default/grub
    echo "GRUB_THEME=\"$THEME_DIR/theme.txt\"" | sudo tee -a /etc/default/grub
    
    if command -v grub-mkconfig >/dev/null 2>&1; then
        sudo grub-mkconfig -o /boot/grub/grub.cfg
    fi
fi

echo "Tema do GRUB vHackintosh instalado com sucesso!"
