# ==============================================================================
# vHackintosh Appliance - Autostart TUI on tty1 & Pre-flight Compatibility
# ==============================================================================

# Garante que /usr/local/bin e npm global estejam no PATH
export PATH="/usr/local/bin:${PATH}"

# ------------------------------------------------------------------------------
# Utilitários de Detecção (Live USB vs Sistema Instalado em Disco)
# ------------------------------------------------------------------------------

# Testa conectividade real com a Internet (Apple SUS / DNS público).
#
# Duas sondagens, cada uma cobrindo uma falha diferente, e nada além disso —
# repetir testes equivalentes só multiplica o tempo de espera no pior caso:
#   1. HTTPS no apple.com: exercita DNS + rota + TLS (o caminho que o macOS usa);
#   2. ICMP para 8.8.8.8: endereço cru, sem DNS. Cobre o caso de DNS quebrado mas
#      rota funcional. É a última tentativa porque nem toda rede virtualizada
#      encaminha ICMP de forma confiável, e um falso "sem Internet" aqui é pior
#      do que não detectar esse cenário raro.
vhack_has_internet() {
    curl -s -m 2 -I https://www.apple.com >/dev/null 2>&1 && return 0
    ping -c1 -W1 8.8.8.8 >/dev/null 2>&1 && return 0
    return 1
}

# Aguarda a rede "assentar" e então testa a conectividade (orçamento: 4 segundos).
#
# Sem esta espera a checagem rodava antes de o DHCP concluir a negociação e
# reportava "SEM INTERNET" numa máquina já instalada e já ligada por cabo — o que
# fazia aparecer o menu de configuração de rede onde ele não era necessário.
# O `nm-online` aguarda o NetworkManager terminar de ativar as conexões e retorna
# imediatamente se já houver conexão ativa: o caso feliz (cabo/Wi-Fi já conectado)
# custa ~0s, e o orçamento só é gasto quando realmente não há rede.
vhack_wait_internet() {
    local timeout="${1:-4}"
    if command -v nm-online >/dev/null 2>&1; then
        nm-online -q -t "$timeout" >/dev/null 2>&1 || true
    else
        sleep "$timeout"
    fi
    vhack_has_internet
}

# Descobre se estamos rodando do pendrive/ISO (live) ou do disco (installed).
# A presença de `vhackintosh-install` NÃO é usada como sinal: o instalador é
# copiado para o sistema definitivo por design (atalho permanente [I] no TUI).
vhack_system_mode() {
    if command -v vhackintosh-mode >/dev/null 2>&1; then
        vhackintosh-mode
        return
    fi

    # Fallback mínimo caso o helper não esteja presente no PATH.
    if [ -f /etc/vhackintosh/installed ]; then
        echo "installed"
        return
    fi

    local fs src
    fs="$(findmnt -n -o FSTYPE / 2>/dev/null || awk '$2 == "/" { print $3; exit }' /proc/mounts 2>/dev/null || true)"
    src="$(findmnt -n -o SOURCE / 2>/dev/null || awk '$2 == "/" { print $1; exit }' /proc/mounts 2>/dev/null || true)"

    case "$fs" in
        overlay|squashfs|erofs) echo "live"; return ;;
    esac
    case "$src" in
        /dev/loop*) echo "live"; return ;;
        /dev/*)     echo "installed"; return ;;
    esac
    if [ -d /run/archiso ]; then
        echo "live"
        return
    fi
    echo "installed"
}

# Limpa qualquer buffer residual do teclado antes de abrir um menu.
vhack_flush_input() {
    while read -r -t 0.1 -n 10000 discard; do :; done 2>/dev/null || true
}

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

    # 2. Detecção do modo de execução (Live USB vs sistema instalado em disco).
    VHACK_MODE="$(vhack_system_mode)"

    # 3. Aguarda a rede assentar ANTES de decidir se o menu de conexão é
    # necessário. O `nm-online` retorna na hora quando já há conexão ativa, então
    # o caso feliz (cabo já conectado) não sofre atraso perceptível; o caso sem
    # rede é limitado pelo timeout abaixo.
    echo -e "\033[1;36mVerificando conexão de rede...\033[0m"
    vhack_wait_internet 4 || true

    if [ "$VHACK_MODE" = "live" ]; then
        # ==========================================================================
        # MODO LIVE USB — Menu de instalação e demonstração
        # ==========================================================================
        vhack_flush_input

        while true; do
            clear
            echo -e "\033[1;36m======================================================================\033[0m"
            echo -e "\033[1;37m         Bem-vindo ao vHackintosh OS Appliance (Live USB)             \033[0m"
            echo -e "\033[1;36m======================================================================\033[0m\n"

            # Status de Internet em tempo real
            if vhack_has_internet; then
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

            # Encerra o menu em EOF (tty desconectado) em vez de girar em loop.
            if ! read -r -p "Escolha [1, 2, 3 ou 0]: " boot_choice; then
                break
            fi
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
    else
        # ==========================================================================
        # MODO INSTALADO NO DISCO — Sem opção de instalação
        # ==========================================================================
        # Com Internet: segue direto para o gerenciador (zero atrito).
        # Sem Internet: oferece a configuração de Wi-Fi antes de prosseguir.
        # ==========================================================================
        if ! vhack_has_internet; then
            vhack_flush_input

            while true; do
                clear
                echo -e "\033[1;36m======================================================================\033[0m"
                echo -e "\033[1;37m            vHackintosh OS — Sistema Instalado em Disco               \033[0m"
                echo -e "\033[1;36m======================================================================\033[0m\n"

                echo -e "  \033[1;33m● Status de Conexão: SEM INTERNET\033[0m"
                echo -e "    Os downloads do macOS (Apple Recovery) e dos AI Harnesses"
                echo -e "    ficarão indisponíveis até que uma rede seja configurada.\n"

                echo -e "Escolha uma opção:\n"
                echo -e "  \033[1;34m[1]\033[0m \033[1;37mConfigurar Conexão Wi-Fi / Rede (nmtui)\033[0m\n"
                echo -e "  \033[1;32m[2]\033[0m \033[1;37mContinuar sem Internet (usar VMs já instaladas)\033[0m\n"
                echo -e "  \033[1;31m[0]\033[0m \033[1;37mReiniciar o Computador\033[0m\n"

                # Encerra o menu em EOF (tty desconectado) em vez de girar em loop.
                if ! read -r -p "Escolha [1, 2 ou 0]: " net_choice; then
                    break
                fi
                case "$net_choice" in
                    1)
                        if command -v nmtui >/dev/null 2>&1; then
                            nmtui
                        else
                            echo -e "\033[1;31m'nmtui' não encontrado. Use 'nmcli' manualmente.\033[0m"
                            sleep 2
                        fi
                        # Se conectou durante a configuração, prossegue direto.
                        # A espera cobre a negociação de DHCP do Wi-Fi recém-configurado.
                        if vhack_wait_internet 5; then
                            break
                        fi
                        ;;
                    2)
                        break
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
    fi

    clear

    # Executa o gerenciador Kiosk do vHackintosh
    if command -v vhackintosh >/dev/null 2>&1; then
        vhackintosh
    fi
fi
