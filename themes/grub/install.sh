#!/usr/bin/env bash
# Instala o tema GRUB do vHackintosh no sistema host

set -euo pipefail

THEME_DIR="/boot/grub/themes/vhackintosh"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

echo "Instalando tema vHackintosh no GRUB..."
${SUDO} mkdir -p "$THEME_DIR"
${SUDO} cp -r "$SOURCE_DIR"/* "$THEME_DIR"/

# Configura no /etc/default/grub se existir
if [ -f /etc/default/grub ]; then
    ${SUDO} sed -i '/GRUB_THEME=/d' /etc/default/grub
    echo "GRUB_THEME=\"$THEME_DIR/theme.txt\"" | ${SUDO} tee -a /etc/default/grub >/dev/null
    
    if command -v grub-mkconfig >/dev/null 2>&1; then
        ${SUDO} grub-mkconfig -o /boot/grub/grub.cfg
    fi
fi

echo "Tema do GRUB vHackintosh instalado com sucesso!"
