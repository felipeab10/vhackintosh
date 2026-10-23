# ==============================================================================
# vHackintosh Appliance - Autostart TUI on local VTs & Pre-flight Compatibility
# ==============================================================================

# Garante que os utilitários locais estejam no PATH
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

# O usuário pediu explicitamente para não bloquear no gate de Vulkan?
#
# Duas formas, porque as duas são necessárias em momentos diferentes:
#
#   1. `VHACK_SKIP_VULKAN_CHECK=1` no ambiente — espelha
#      `core/gpu.py:vulkan_check_skipped()` (mesma variável, mesma lista
#      1/true/yes/on/sim, mesmo strip). É o ÚNICO bypass de Vulkan do projeto;
#      existir em dois lugares só é aceitável porque ambos leem a MESMA
#      variável e a mesma tabela. O que não pode é um caminho ler e o outro
#      não, que foi o defeito: o boot normal desligava a máquina mesmo com o
#      usuário tendo pedido o pulo.
#
#   2. `vhack_skip_vulkan=true` no cmdline do kernel ou como SMBIOS OEM string
#      — porque dentro da ISO o
#      usuário NÃO tem como definir uma variável de ambiente antes do gate
#      rodar: o gate roda no login, e a variável só existiria se ele já
#      estivesse logado. Sem esta segunda via o escape seria decorativo na
#      prática. É o mesmo mecanismo já usado e testado pelo
#      `vhack_build_test=1` (ver o hook logo abaixo), e o parâmetro se
#      acrescenta com `e` no menu do GRUB, sem reescrever a ISO.
#
# O parâmetro de cmdline só LIGA o pulo; não existe forma de forçar o
# bloqueio a partir dele, para não abrir uma segunda política.
vhack_vulkan_check_skipped() {
    local valor
    valor="$(printf '%s' "${VHACK_SKIP_VULKAN_CHECK:-}" | tr '[:upper:]' '[:lower:]')"
    # Tira espaços das pontas para " 1 " valer tanto quanto "1".
    valor="${valor#"${valor%%[![:space:]]*}"}"
    valor="${valor%"${valor##*[![:space:]]}"}"
    case "$valor" in
        1|true|yes|on|sim) return 0 ;;
    esac

    # Valores verdadeiros iguais ao ambiente, em token completo. Valores
    # parciais como `vhack_skip_vulkan=1abc` não passam.
    if grep -qiE '(^| )vhack_skip_vulkan=(1|true|yes|on|sim)( |$)' /proc/cmdline 2>/dev/null; then
        return 0
    fi

    # verify-iso.sh --vmware-svga envia o marcador como SMBIOS Type 11 para
    # manter o boot normal por OVMF/GRUB (e permitir Esc no menu de boot).
    local dmi_raw
    for dmi_raw in /sys/firmware/dmi/entries/11-*/raw; do
        [ -f "$dmi_raw" ] || continue
        if grep -aqF 'vhack_skip_vulkan=true' "$dmi_raw" 2>/dev/null; then
            return 0
        fi
    done

    return 1
}

if [ "$(tty)" = "/dev/tty1" ]; then
    # Inicia subsistema de áudio PipeWire para a sessão de usuário
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user start pipewire pipewire-pulse wireplumber 2>/dev/null || true
    fi
    # Fallback caso systemd --user não esteja ativo (ex: live root no archiso)
    if command -v pipewire >/dev/null 2>&1 && ! pgrep -x pipewire >/dev/null 2>&1; then
        pipewire >/dev/null 2>&1 &
        if command -v pipewire-pulse >/dev/null 2>&1; then
            pipewire-pulse >/dev/null 2>&1 &
        fi
        if command -v wireplumber >/dev/null 2>&1; then
            wireplumber >/dev/null 2>&1 &
        fi
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

    # 4. Checagem Rigorosa de Hardware do Host (Vulkan 1.2+ para Reims vGPU)
    #
    # O escape `VHACK_SKIP_VULKAN_CHECK=1` é o mesmo de `core/gpu.py` e permite
    # pular temporariamente a barreira em uma sessão de diagnóstico.
    echo -e "\033[1;36mVerificando aceleração de vídeo do host (Vulkan 1.2+)...\033[0m"
    VULKAN_OUTPUT=""
    VULKAN_OK=1

    if vhack_vulkan_check_skipped; then
        VULKAN_OUTPUT="Checagem de Vulkan PULADA a pedido explícito (ambiente, cmdline ou marcador SMBIOS de teste)."
        echo -e "\033[1;33m${VULKAN_OUTPUT}\033[0m"
    elif command -v vhackintosh >/dev/null 2>&1; then
        if ! VULKAN_OUTPUT="$(vhackintosh check-vulkan 2>&1)"; then
            VULKAN_OK=0
        fi
    elif [ -f "/opt/vhackintosh/bin/vhackintosh" ]; then
        if ! VULKAN_OUTPUT="$(/opt/vhackintosh/bin/vhackintosh check-vulkan 2>&1)"; then
            VULKAN_OK=0
        fi
    fi

    if [ "$VULKAN_OK" -ne 1 ]; then
        clear
        echo -e "\033[1;31m======================================================================\033[0m"
        echo -e "\033[1;31m  BLOQUEIO CRÍTICO: GPU / HARDWARE INCOMPATÍVEL COM vHACKINTOSH OS    \033[0m"
        echo -e "\033[1;31m======================================================================\033[0m\n"
        echo -e "\033[1;37mEste computador não possui os requisitos de hardware necessários.\033[0m\n"
        echo -e "\033[1;33mDiagnóstico:\033[0m"
        echo -e "  ${VULKAN_OUTPUT}\n"
        echo -e "\033[1;37mMotivo do Bloqueio:\033[0m"
        echo -e "  • O vHackintosh OS depende do motor Reims vGPU para acelerar o macOS."
        echo -e "  • O Reims vGPU exige obrigatoriamente GPU física com Vulkan 1.2 ou superior."
        echo -e "  • GPUs emuladas/virtuais (QEMU, VMware, VirtualBox, llvmpipe) e GPUs antigas"
        echo -e "    sem Vulkan 1.2+ não conseguem executar o macOS com aceleração.\n"
        echo -e "\033[1;33mRequisitos Mínimos de GPU:\033[0m"
        echo -e "  • Intel: HD Graphics Gen9 (Skylake 6ª Geração) ou superior"
        echo -e "  • NVIDIA: GeForce GTX série 900 (Maxwell), GTX série 10/16, RTX ou superior"
        echo -e "  • AMD: Radeon GCN 4ª Geração (RX 400/500), Vega, RDNA (RX 5000+) ou superior\n"
        echo -e "\033[1;31mNão é permitido instalar o vHackintosh OS neste computador.\033[0m\n"

        # A instrução de saída fica AQUI, na própria tela que bloqueia: quem
        # cai neste ponto está numa tela que desliga a máquina e não tem como
        # adivinhar que existe um parâmetro de kernel que destrava. Sem esta
        # dica o escape existe mas é indescobrível — foi o que aconteceu no
        # teste numa VM, em que a única saída era trocar o SSD do notebook.
        echo -e "\033[1;33mSó para testar a ISO numa VM emulada (não valida GPU nenhuma):\033[0m"
        echo -e "  \033[1;37m1.\033[0m Reinicie e, no menu do GRUB, pressione \033[1;36me\033[0m na entrada do vHackintosh."
        echo -e "  \033[1;37m2.\033[0m No fim da linha que começa com \033[1;36mlinux\033[0m, acrescente: \033[1;32mvhack_skip_vulkan=true\033[0m (ou legado: vhack_skip_vulkan=1)"
        echo -e "  \033[1;37m3.\033[0m \033[1;36mCtrl+X\033[0m (ou \033[1;36mF10\033[0m) para bootar. Para desfazer, é só não salvar."
        echo -e "  \033[2mAlternativa, se você tem acesso ao shell: export VHACK_SKIP_VULKAN_CHECK=1\033[0m\n"

        while true; do
            echo -ne "\033[1;33mPressione [Enter] para desligar o computador (ou 'r' para reiniciar): \033[0m"
            read -r shutdown_key
            if [ "$shutdown_key" = "r" ] || [ "$shutdown_key" = "R" ]; then
                echo -e "\n\033[1;36mReiniciando o computador...\033[0m"
                reboot
            else
                echo -e "\n\033[1;31mDesligando o computador...\033[0m"
                if command -v systemctl >/dev/null 2>&1; then
                    systemctl poweroff -i || poweroff
                else
                    poweroff
                fi
            fi
            sleep 2
        done
    fi

    if [ "$VHACK_MODE" = "live" ]; then
        # ==========================================================================
        # MODO LIVE USB — Menu de instalação e demonstração
        # ==========================================================================
        vhack_flush_input

        # A instalação em disco é escolhida neste menu antes de o gerenciador
        # TUI iniciar. Portanto, ofereça a escolha de idioma ainda no fluxo do
        # instalador Live; o settings.json fica em /root/.config e é levado
        # junto para o sistema instalado pela cópia do rootfs.
        if [ -x /opt/vhackintosh/bin/vhackintosh-lang ]; then
            /opt/vhackintosh/bin/vhackintosh-lang --choose
        fi

        while true; do
            clear
            echo -e "\033[1;36m======================================================================\033[0m"
            echo -e "\033[1;37m         Bem-vindo ao vHackintosh OS Appliance (Live USB)             \033[0m"
            echo -e "\033[1;36m======================================================================\033[0m\n"

            # Status de Internet em tempo real
            if vhack_has_internet; then
                echo -e "  \033[1;32m● Conexão: CONECTADO À INTERNET\033[0m"
            else
                echo -e "  \033[1;33m● Conexão: SEM INTERNET (conecte via opção [3] abaixo)\033[0m"
            fi

            # Status de Compatibilidade de Hardware (KVM e Reims vGPU)
            if command -v vhackintosh >/dev/null 2>&1 && vhackintosh check -q 2>/dev/null; then
                echo -e "  \033[1;32m● Hardware: COMPATÍVEL (KVM e Reims vGPU prontos)\033[0m\n"
            else
                echo -e "  \033[1;31m● Hardware: INCOMPATÍVEL (KVM na BIOS ou GPU sem Vulkan 1.2+)\033[0m\n"
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
                echo -e "    Os downloads do macOS (Apple Recovery) ficarão indisponíveis"
                echo -e "    até que uma rede seja configurada.\n"

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
        export VHACKINTOSH_AUTOSTARTED=1
        vhackintosh
    fi
fi

# Em outras sessões de console local, abre o mesmo gerenciador sem repetir o
# preflight de boot da tty1. Assim Ctrl+Alt+F2 permite gerenciar a VM pelo
# terminal Linux enquanto o Sway/QEMU seguem ativos na tty1.
VHACK_CURRENT_TTY="$(tty 2>/dev/null || true)"
case "$VHACK_CURRENT_TTY" in
    /dev/tty[2-9]|/dev/tty[1-5][0-9]|/dev/tty6[0-3])
        clear
        if command -v vhackintosh >/dev/null 2>&1; then
            export VHACKINTOSH_AUTOSTARTED=1
            vhackintosh
        fi
        ;;
esac
