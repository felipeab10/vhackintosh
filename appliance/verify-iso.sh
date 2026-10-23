#!/usr/bin/env bash
# ==============================================================================
# vHackintosh Appliance - ISO Boot Verifier
# Testa a inicialização da ISO em uma VM QEMU local com firmware UEFI.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${SCRIPT_DIR}/out"

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

# --- Argumentos ---------------------------------------------------------------
# Contrato: o primeiro argumento que for um ARQUIVO existente é a ISO a testar;
# qualquer outra coisa é repassada ao QEMU. Também reconhecemos as flags abaixo.
#
# Isto corrige um bug antigo: $1 era usado ao mesmo tempo como caminho da ISO e
# como argumento posicional do QEMU, então a ISO entrava DUAS vezes (uma como
# -cdrom, outra como disco posicional). Pior, `verify-iso.sh --window` fazia
# `--window` virar ISO_FILE e abortava com "Nenhuma ISO encontrada".
FULLSCREEN_FLAG="-full-screen"
EXTRA_ARGS=()
ISO_FILE=""
HEADLESS=0
VMWARE_SVGA_TEST=0

usage() {
    echo "uso: $(basename "$0") [ISO] [opções] [args extras do QEMU...]"
    echo
    echo "  ISO             imagem a testar (padrão: a mais recente em appliance/out)"
    echo "  --window, -w    inicia em janela em vez de tela cheia"
    echo "  --headless      sem display: '-display none' + serial para arquivo de log"
    echo "  --no-window     apelido de --headless"
    echo "  --vmware-svga   inicia com vídeo VMware SVGA, sem validar Reims Vulkan"
    echo "  args extras     repassados literalmente ao qemu-system-x86_64"
    echo
    echo "Ambiente: VHACK_QEMU, VHACK_TEST_SSD, VHACK_SERIAL_DIR e VHACK_OVMF"
}

while [ "$#" -gt 0 ]; do
    arg="$1"
    case "$arg" in
        --window|-w|--windowed)
            FULLSCREEN_FLAG=""
            ;;
        --headless|--no-window)
            HEADLESS=1
            ;;
        --vmware-svga)
            VMWARE_SVGA_TEST=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            if [ -z "${ISO_FILE}" ] && [ -f "${arg}" ]; then
                ISO_FILE="${arg}"
            else
                EXTRA_ARGS+=("${arg}")
            fi
            ;;
    esac
    shift
done

if [ -z "${ISO_FILE}" ]; then
    ISO_FILE="$(ls -t "${OUTPUT_DIR}"/*.iso "${SCRIPT_DIR}"/out-*/*.iso 2>/dev/null | head -n 1 || true)"
fi

if [ -z "${ISO_FILE}" ] || [ ! -f "${ISO_FILE}" ]; then
    echo -e "${RED}Erro: Nenhuma ISO encontrada em ${OUTPUT_DIR}. Execute build-iso.sh primeiro.${NC}" >&2
    exit 1
fi

# ==============================================================================
# 1. Pré-requisitos — falhar com mensagem acionável, não com erro cru do shell.
# ==============================================================================

QEMU_BIN="${VHACK_QEMU:-qemu-system-x86_64}"
if ! command -v "${QEMU_BIN}" >/dev/null 2>&1; then
    echo -e "${RED}Erro: '${QEMU_BIN}' não encontrado no PATH.${NC}" >&2
    echo -e "${YELLOW}Como resolver:${NC}" >&2
    echo -e "  Arch:   ${GREEN}sudo pacman -S --needed qemu-system-x86 qemu-img${NC}" >&2
    echo -e "  Debian: ${GREEN}sudo apt install qemu-system-x86 qemu-utils${NC}" >&2
    echo -e "  Fedora: ${GREEN}sudo dnf install qemu-kvm qemu-img${NC}" >&2
    echo -e "  Ou aponte para um binário específico: ${GREEN}VHACK_QEMU=/caminho/qemu-system-x86_64 $0${NC}" >&2
    exit 1
fi

if ! command -v qemu-img >/dev/null 2>&1; then
    echo -e "${RED}Erro: 'qemu-img' não encontrado no PATH (necessário para criar o disco de teste).${NC}" >&2
    echo -e "${YELLOW}Instale o pacote qemu-img/qemu-utils junto com o qemu-system-x86.${NC}" >&2
    exit 1
fi

# --- KVM ---------------------------------------------------------------------
# Sem /dev/kvm NÃO seguimos com `-enable-kvm`: o QEMU abortaria com
# "Could not access KVM kernel module" e o usuário veria só um erro cru.
# A escolha aqui é FALLBACK EXPLÍCITO para TCG, com aviso alto, porque:
#   • a tarefa é testar se a ISO boota e se o build passa — isso funciona em TCG;
#   • abortar deixaria o usuário sem nenhuma saída em máquinas sem virt aninhada.
# O custo (build possivelmente 10–30x mais lento) é avisado em texto inequívoco.
# ARRAY de propósito: "-accel tcg" precisa virar DOIS argumentos.
# Como string única o QEMU recebia o literal "-accel tcg" e abortava com
#   qemu-system-x86_64: -accel tcg: invalid option
# Isso quebrava TODO o fallback TCG (máquina sem /dev/kvm), que é
# justamente o caso deste harness. Medido em QEMU 11.1.2.
KVM_FLAG=("-enable-kvm")
# O modelo de CPU precisa acompanhar o acelerador: `host,kvm=on` só existe sob
# KVM, e em TCG o QEMU aborta com "CPU model 'host' requires KVM or HVF".
QEMU_CPU_MODEL="host,kvm=on"
ACCEL_DESC="KVM (aceleração por hardware)"
TCG_FALLBACK=0
if [ -w /dev/kvm ]; then
    KVM_FLAG=("-enable-kvm")
elif [ -e /dev/kvm ]; then
    echo -e "${YELLOW}Aviso: /dev/kvm existe mas não é gravável pelo usuário atual ($(id -un)).${NC}" >&2
    echo -e "${YELLOW}       Rodando em TCG. Para usar KVM: ${GREEN}sudo usermod -aG kvm $(id -un)${NC}${YELLOW} e relogar.${NC}" >&2
    KVM_FLAG=(-accel tcg)
    QEMU_CPU_MODEL="max"
    ACCEL_DESC="TCG (sem aceleração — LENTO)"
    TCG_FALLBACK=1
else
    KVM_FLAG=(-accel tcg)
    QEMU_CPU_MODEL="max"
    ACCEL_DESC="TCG (sem aceleração — LENTO)"
    TCG_FALLBACK=1
fi

if [ "${TCG_FALLBACK}" -eq 1 ]; then
    echo -e "${RED}===================================================================${NC}" >&2
    echo -e "${RED}  AVISO ALTO: /dev/kvm INDISPONÍVEL — caindo para TCG (emulação pura)${NC}" >&2
    echo -e "${RED}===================================================================${NC}" >&2
    echo -e "  O guest vai rodar ORDENS DE GRANDEZA mais devagar. Um build do" >&2
    echo -e "  reims-vgpu que leva minutos em KVM pode levar ${YELLOW}horas${NC} em TCG." >&2
    echo -e "" >&2
    echo -e "  ${YELLOW}Para habilitar KVM:${NC}" >&2
    if grep -qE '(^| )(vmx|svm)( |$)' /proc/cpuinfo 2>/dev/null; then
        echo -e "  1. CPU com VT-x/AMD-V: ${GREEN}sim${NC}" >&2
    else
        echo -e "  1. CPU com VT-x/AMD-V: ${RED}não detectado${NC}" >&2
    fi
    echo -e "  2. Carregue o módulo:   ${GREEN}sudo modprobe kvm_intel${NC} (ou kvm_amd)" >&2
    echo -e "  3. Se estiver DENTRO de uma VM (caso típico deste host), o anfitrião" >&2
    echo -e "     precisa expor virt aninhada:" >&2
    echo -e "       ${GREEN}echo 1 | sudo tee /sys/module/kvm_intel/parameters/nested${NC}" >&2
    echo -e "       e a VM externa precisa ter ${GREEN}nested=1${NC} / 'Virtualization' ligado." >&2
    echo -e "  4. Permissão:           ${GREEN}sudo usermod -aG kvm $(id -un)${NC} e relogar" >&2
    echo -e "  5. Confirme:            ${GREEN}ls -l /dev/kvm${NC}" >&2
    echo -e "" >&2
    echo -e "  Seguindo em TCG mesmo assim. ${YELLOW}Ctrl+C agora para abortar.${NC}" >&2
    echo -e "${RED}===================================================================${NC}" >&2
fi

# --- OVMF --------------------------------------------------------------------
OVMF_BIOS="${VHACK_OVMF:-}"
if [ -z "${OVMF_BIOS}" ]; then
    for cand in \
        "/usr/share/edk2-ovmf/x64/OVMF.4m.fd" \
        "/usr/share/edk2-ovmf/x64/OVMF_CODE.4m.fd" \
        "/usr/share/OVMF/OVMF_CODE_4M.fd" \
        "/usr/share/OVMF/OVMF_CODE.fd" \
        "/usr/share/qemu/OVMF_CODE.4m.fd" \
        "/home/felipeab10/reims-vgpu/vm/ovmf/OVMF_CODE_4M.fd"
    do
        if [ -f "${cand}" ]; then
            OVMF_BIOS="${cand}"
            break
        fi
    done
fi

if [ -z "${OVMF_BIOS}" ]; then
    echo -e "${RED}Erro: nenhum firmware UEFI (OVMF) encontrado.${NC}" >&2
    echo -e "A appliance é UEFI-only (o grub.cfg declara bootmodes=('uefi.grub')):" >&2
    echo -e "bootar sem OVMF falharia ou cairia no SeaBIOS, testando um caminho falso." >&2
    echo -e "${YELLOW}Como resolver:${NC}" >&2
    echo -e "  Arch:   ${GREEN}sudo pacman -S --needed edk2-ovmf${NC}" >&2
    echo -e "  Debian: ${GREEN}sudo apt install ovmf${NC}" >&2
    echo -e "  Fedora: ${GREEN}sudo dnf install edk2-ovmf${NC}" >&2
    echo -e "  Ou aponte manualmente: ${GREEN}VHACK_OVMF=/caminho/OVMF_CODE_4M.fd $0${NC}" >&2
    echo -e "" >&2
    echo -e "  Locais procurados: /usr/share/edk2-ovmf/x64/, /usr/share/OVMF/, /usr/share/qemu/" >&2
    exit 1
fi
if [ ! -r "${OVMF_BIOS}" ]; then
    echo -e "${RED}Erro: firmware OVMF encontrado mas ilegível: ${OVMF_BIOS}${NC}" >&2
    exit 1
fi

# ==============================================================================
# 2. Cabeçalho informativo
# ==============================================================================

echo -e "${CYAN}===================================================================${NC}"
echo -e "${CYAN}        vHackintosh Appliance - QEMU ISO Boot Verifier             ${NC}"
echo -e "${CYAN}===================================================================${NC}\n"
echo -e "  • Imagem ISO:     ${GREEN}${ISO_FILE}${NC} ($(du -h "${ISO_FILE}" | cut -f1))"
echo -e "  • Firmware UEFI:  ${GREEN}${OVMF_BIOS}${NC}"
echo -e "  • Aceleração:     ${GREEN}${ACCEL_DESC}${NC}"
echo -e "  • Vídeo:          ${GREEN}VGA std — mesmo dispositivo do motor nativo do appliance${NC}"

# ==============================================================================
# 3. Disco de teste (reaproveitado entre execuções)
# ==============================================================================

TEST_SSD="${VHACK_TEST_SSD:-/var/tmp/vhackintosh-test-ssd.qcow2}"
if [ ! -e "${TEST_SSD}" ]; then
    echo -e "${BLUE}▶ Criando disco virtual de teste SSD NVMe/VirtIO (64 GB sparse em /var/tmp)...${NC}"
    qemu-img create -f qcow2 "${TEST_SSD}" 64G >/dev/null
elif [ ! -f "${TEST_SSD}" ]; then
    # Existe mas não é arquivo regular (diretório, fifo, socket...). Recriar algo
    # assim seria destrutivo e provavelmente errado — melhor abortar e avisar.
    echo -e "${RED}Erro: ${TEST_SSD} existe mas não é um arquivo regular.${NC}" >&2
    echo -e "${YELLOW}Remova/mova manualmente ou use VHACK_TEST_SSD=<outro caminho>.${NC}" >&2
    exit 1
else
    echo -e "${BLUE}▶ Reutilizando disco de teste existente: ${TEST_SSD}${NC}"
fi

# ==============================================================================
# 4. Montagem da linha de comando do QEMU
# ==============================================================================
# INVARIANTE: a ISO aparece exatamente UMA vez (em -cdrom). ISO_FILE nunca entra
# em EXTRA_ARGS, e EXTRA_ARGS nunca é usado como argumento posicional.

QEMU_ARGS=(
    "${KVM_FLAG[@]}"
    -machine q35
    # `-cpu host,kvm=on` só é válido SOB KVM. Em TCG o QEMU aborta com
    #   qemu-system-x86_64: CPU model 'host' requires KVM or HVF
    # ou seja, o fallback TCG (máquina sem /dev/kvm) morria na partida — o mesmo
    # tipo de falha silenciosa do `-accel tcg`. Em TCG usamos `max`, que expõe o
    # conjunto de recursos que a emulação implementa.
    -cpu "${QEMU_CPU_MODEL}"
    -m 16G
    -smp 8
    -drive file="${TEST_SSD}",if=virtio,format=qcow2,id=ssd0
    -cdrom "${ISO_FILE}"
    # Mantém OVMF/GRUB no caminho normal. O menu de boot do firmware fica
    # habilitado para que Esc permita escolher entre ISO e disco.
    -boot order=d,menu=on
    # -vga std espelha o motor nativo do appliance (ui/tui.py usa `-vga std`).
    # Testar com outro dispositivo de vídeo verificaria um caminho que não é o
    # que roda de verdade.
    -vga std
)

SERIAL_LOG=""
if [ "${HEADLESS}" -eq 1 ]; then
    # VHACK_SERIAL_LOG permite redirecionar o serial quando /var/tmp não é
    # gravável (sandbox, container com rootfs read-only, CI). Sem esta saída o
    # script falhava com "No such file or directory" do QEMU e o veredito virava
    # TIMEOUT indistinguível de um build que não terminou.
    SERIAL_DIR="${VHACK_SERIAL_DIR:-/var/tmp}"
    SERIAL_LOG="${SERIAL_DIR}/vhackintosh-verify-serial.$(date +%Y%m%dT%H%M%S).log"
    if ! ( : > "${SERIAL_LOG}" ) 2>/dev/null; then
        echo -e "${RED}Erro: não consigo escrever o log serial em ${SERIAL_LOG}.${NC}" >&2
        echo -e "${RED}       Use VHACK_SERIAL_DIR=<dir-gravável> ou rode com --display.${NC}" >&2
        exit 2
    fi
    : > "${SERIAL_LOG}"
    QEMU_ARGS+=(-display none -serial "file:${SERIAL_LOG}")
else
    QEMU_ARGS+=(-display gtk,show-menubar=off,zoom-to-fit=on,grab-on-hover=on,show-cursor=on)
    if [ -n "${FULLSCREEN_FLAG}" ]; then
        QEMU_ARGS+=("${FULLSCREEN_FLAG}")
    fi
fi

if [ -n "${OVMF_BIOS}" ]; then
    QEMU_ARGS+=(-bios "${OVMF_BIOS}")
fi

if [ "${VMWARE_SVGA_TEST}" -eq 1 ]; then
    # O sistema convidado lê estes marcadores SMBIOS para permitir o boot
    # de teste sem exigir Vulkan da GPU física do host.
    QEMU_ARGS+=(-smbios type=11,value=vhack_skip_vulkan=true,value=vhack_graphics=vmware-svga)
    echo -e "${BLUE}▶ Modo VMware SVGA ativado via SMBIOS (boot normal por ISO/OVMF).${NC}"
    echo -e "${YELLOW}  Este modo ignora a validação de Vulkan para teste em VM.${NC}"
fi

if [ -n "${SERIAL_LOG}" ]; then
    echo -e "  • Console serial: ${GREEN}${SERIAL_LOG}${NC}"
    echo -e "  ${YELLOW}Acompanhe em outro terminal com:${NC} ${GREEN}tail -f ${SERIAL_LOG}${NC}"
fi
if [ "${HEADLESS}" -eq 1 ]; then
    echo -e "  • Modo:           ${GREEN}HEADLESS (sem display) — a VM pode ficar rodando sozinha${NC}"
else
    echo -e "  • Atalhos úteis:  ${YELLOW}Ctrl + Alt + F (Alterna Tela Cheia) | Ctrl + Alt + G (Libera Mouse)${NC}"
fi
echo

if [ "${HEADLESS}" -eq 0 ]; then
    echo -e "${CYAN}Iniciando QEMU Appliance em modo Kiosk Fullscreen...${NC}\n"
    echo -e "${YELLOW}Dica: dentro do appliance, valide a config da sessão com:${NC}"
    echo -e "  ${GREEN}sway -C -c /etc/vhackintosh/sway.conf${NC}\n"
fi

echo -e "${BLUE}▶ Comando:${NC} ${QEMU_BIN} ${QEMU_ARGS[*]} ${EXTRA_ARGS[*]:-}"
echo

# ==============================================================================
# 6. Execução
# ==============================================================================

exec "${QEMU_BIN}" "${QEMU_ARGS[@]}" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
