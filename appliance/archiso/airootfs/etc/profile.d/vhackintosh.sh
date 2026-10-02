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

    clear

    # Se estiver rodando do Pendrive/Live ISO ou se o utilitário de instalação estiver disponível
    IS_LIVE_MEDIA=0
    if command -v vhackintosh-install >/dev/null 2>&1 || [ -d "/run/archiso" ] || [ -d "/run/archiso/bootmnt" ] || grep -q "archiso" /proc/cmdline 2>/dev/null; then
        IS_LIVE_MEDIA=1
    fi

    if [ "$IS_LIVE_MEDIA" -eq 1 ]; then
        # Limpa qualquer buffer residual do teclado
        while read -r -t 0.1 -n 10000 discard; do :; done 2>/dev/null || true

        while true; do
            clear
            echo -e "\033[1;36m======================================================================\033[0m"
            echo -e "\033[1;37m         Bem-vindo ao vHackintosh OS Appliance (Live USB)             \033[0m"
            echo -e "\033[1;36m======================================================================\033[0m\n"

            # Status de Internet em tempo real
            if curl -s -m 2 -I https://www.apple.com >/dev/null 2>&1 || curl -s -m 2 -I https://1.1.1.1 >/dev/null 2>&1; then
                echo -e "  \033[1;32m● Status de Conexão: CONECTADO À INTERNET\033[0m\n"
            else
                echo -e "  \033[1;33m● Status de Conexão: SEM INTERNET (conecte via opção [3] abaixo)\033[0m\n"
            fi

            echo -e "Escolha o modo de operação:\n"
            echo -e "  \033[1;32m[1]\033[0m \033[1;37m★ INSTALAR vHackintosh OS no SSD / Disco deste Computador (Recomendado)\033[0m"
            echo -e "      Apaga o disco selecionado e instala o sistema operacional definitivo.\n"
            echo -e "  \033[1;33m[2]\033[0m \033[1;37mExecutar em Modo Live / Demonstração (Memória RAM)\033[0m"
            echo -e "      Testa o vHackintosh sem alterar os discos físicos do computador.\n"
            echo -e "  \033[1;34m[3]\033[0m \033[1;37mConfigurar Conexão Wi-Fi / Rede (nmtui)\033[0m\n"
            echo -e "  \033[1;31m[0]\033[0m \033[1;37mReiniciar o Computador\033[0m\n"

            read -r -p "Escolha [1, 2, 3 ou 0]: " boot_choice
            case "$boot_choice" in
                1)
                    if command -v vhackintosh-install >/dev/null 2>&1; then
                        vhackintosh-install
                        echo -e "\n\033[1;33mInstalador finalizado. Pressione Enter para voltar ao menu...\033[0m"
                        read -r
                    fi
                    ;;
                2)
                    break
                    ;;
                3)
                    if command -v nmtui >/dev/null 2>&1; then
                        nmtui
                    fi
                    ;;
                0)
                    reboot
                    ;;
                *)
                    echo -e "\033[1;31mOpção inválida.\033[0m"
                    sleep 1
                    ;;
            esac
        done
    fi

    clear

    # Executa o gerenciador Kiosk do vHackintosh
    if command -v vhackintosh >/dev/null 2>&1; then
        vhackintosh
    fi
fi
