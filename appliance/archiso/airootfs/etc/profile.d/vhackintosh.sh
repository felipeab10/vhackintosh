# ==============================================================================
# vHackintosh Appliance - Autostart TUI on tty1 & Pre-flight Compatibility
# ==============================================================================

# Garante que /usr/local/bin e npm global estejam no PATH
export PATH="/usr/local/bin:${PATH}"

if [ "$(tty)" = "/dev/tty1" ]; then
    # Inicia subsistema de áudio PipeWire para a sessão de usuário
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user start pipewire pipewire-pulse wireplumber 2>/dev/null || true
    fi

    clear

    # 1. Checagem Pré-Boot de Virtualização na BIOS (Intel VT-x / AMD SVM)
    if [ ! -e "/dev/kvm" ]; then
        echo -e "\033[1;31m======================================================================\033[0m"
        echo -e "\033[1;31m  ALERTA CRÍTICO: VIRTUALIZAÇÃO POR HARDWARE DESATIVADA NA BIOS!       \033[0m"
        echo -e "\033[1;31m======================================================================\033[0m\n"
        if grep -q "vmx" /proc/cpuinfo; then
            echo -e "O seu processador \033[1;37mIntel\033[0m suporta virtualização (VT-x),"
            echo -e "mas ela está \033[1;31mDESLIGADA\033[0m nas configurações da BIOS da placa-mãe."
            echo -e "\n\033[1;33mComo resolver:\033[0m"
            echo -e "1. Reinicie o computador e pressione Del ou F2 para acessar a BIOS."
            echo -e "2. Vá em 'Advanced' ou 'CPU Configuration' e ative 'Intel Virtualization Technology'."
        elif grep -q "svm" /proc/cpuinfo; then
            echo -e "O seu processador \033[1;37mAMD\033[0m suporta virtualização (AMD-V),"
            echo -e "mas ela está \033[1;31mDESLIGADA\033[0m nas configurações da BIOS da placa-mãe."
            echo -e "\n\033[1;33mComo resolver:\033[0m"
            echo -e "1. Reinicie o computador e pressione Del ou F2 para acessar a BIOS."
            echo -e "2. Vá em 'Advanced' ou 'CPU Configuration' e ative 'SVM Mode'."
        else
            echo -e "Seu processador não possui suporte à virtualização por hardware (VT-x ou AMD-V)."
        fi
        echo -e "\n\033[1;33mPressione Enter para continuar mesmo assim ou 'r' para reiniciar...\033[0m"
        read -r key
        if [ "$key" = "r" ]; then
            reboot
        fi
    fi

    # 2. Checagem de Conexão com a Internet (TCP HTTP/HTTPS para compatibilidade com QEMU e Proxies)
    if ! curl -s -m 2 -I https://www.apple.com >/dev/null 2>&1 && ! curl -s -m 2 -I https://1.1.1.1 >/dev/null 2>&1; then
        echo -e "\033[1;33m======================================================================\033[0m"
        echo -e "\033[1;33m  AVISO: NENHUMA CONEXÃO COM A INTERNET DETECTADA                     \033[0m"
        echo -e "\033[1;33m======================================================================\033[0m"
        echo -e "A internet é necessária para baixar as imagens oficiais da Apple e Harnesses."
        echo -e "Deseja configurar o Wi-Fi agora via 'nmtui'? [S/n] (Auto-continua em 6s)"
        read -r -t 6 ans || ans="n"
        if [[ "$ans" =~ ^[Ss]$ ]]; then
            if command -v nmtui >/dev/null 2>&1; then
                nmtui
            fi
        fi
    fi

    clear

    # Executa o gerenciador Kiosk do vHackintosh
    if command -v vhackintosh >/dev/null 2>&1; then
        vhackintosh
    fi
fi
