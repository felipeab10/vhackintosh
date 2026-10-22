#!/usr/bin/env bash
# Configura o Linux Host para auto-login nos consoles locais e inicialização do vHackintosh

set -euo pipefail

USER_NAME="${SUDO_USER:-$USER}"
SERVICE_DIR="/etc/systemd/system/getty@.service.d"

echo "Configurando auto-login para o usuário '$USER_NAME' nos consoles locais..."

sudo mkdir -p "$SERVICE_DIR"
sudo rm -f /etc/systemd/system/getty@tty1.service.d/autologin.conf
cat <<EOF | sudo tee "$SERVICE_DIR/autologin.conf"
[Service]
ExecStart=
ExecStart=-/sbin/agetty -o '-p -f -- \\\\u' --noclear --autologin $USER_NAME %I \$TERM
EOF

sudo systemctl daemon-reload

# Adiciona inicialização automática no perfil dos terminais virtuais locais.
PROFILE_FILE="/home/$USER_NAME/.bash_profile"
if [ ! -f "$PROFILE_FILE" ]; then
    PROFILE_FILE="/home/$USER_NAME/.profile"
fi

if ! grep -q "vhackintosh" "$PROFILE_FILE" 2>/dev/null; then
    cat <<'EOF' >> "$PROFILE_FILE"

# Inicia o gerenciador vHackintosh em cada terminal virtual local (tty1-tty63).
if [ -z "${DISPLAY:-}" ] && [ -z "${VHACKINTOSH_AUTOSTARTED:-}" ]; then
    case "$(tty)" in
        /dev/tty[1-9]|/dev/tty[1-5][0-9]|/dev/tty6[0-3])
            export VHACKINTOSH_AUTOSTARTED=1
            /usr/local/bin/vhackintosh
            ;;
    esac
fi
EOF
fi

# Cria symlink em /usr/local/bin
sudo ln -sf /home/$USER_NAME/vhackintosh/bin/vhackintosh /usr/local/bin/vhackintosh

echo "Auto-login nos consoles locais e inicialização do gerenciador configurados."
