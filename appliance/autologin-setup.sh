#!/usr/bin/env bash
# Configura o Linux Host para Auto-login no tty1 e inicialização do vHackintosh

set -euo pipefail

USER_NAME="${SUDO_USER:-$USER}"
SERVICE_DIR="/etc/systemd/system/getty@tty1.service.d"

echo "Configurando Auto-login para o usuário '$USER_NAME' no tty1..."

sudo mkdir -p "$SERVICE_DIR"
cat <<EOF | sudo tee "$SERVICE_DIR/autologin.conf"
[Service]
ExecStart=
ExecStart=-/sbin/agetty -o '-p -f -- \\\\u' --noclear --autologin $USER_NAME %I \$TERM
EOF

sudo systemctl daemon-reload

# Adiciona inicialização automática no .bash_profile / .zprofile se for tty1
PROFILE_FILE="/home/$USER_NAME/.bash_profile"
if [ ! -f "$PROFILE_FILE" ]; then
    PROFILE_FILE="/home/$USER_NAME/.profile"
fi

if ! grep -q "vhackintosh" "$PROFILE_FILE" 2>/dev/null; then
    cat <<'EOF' >> "$PROFILE_FILE"

# Inicia o vHackintosh Kiosk automaticamente apenas no tty1
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec /usr/local/bin/vhackintosh
fi
EOF
fi

# Cria symlink em /usr/local/bin
sudo ln -sf /home/$USER_NAME/vhackintosh/bin/vhackintosh /usr/local/bin/vhackintosh

echo "Auto-login e Kiosk configurados com sucesso!"
