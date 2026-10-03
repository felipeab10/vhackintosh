#!/usr/bin/env bash
# ==============================================================================
# vHackintosh Appliance - ISO Boot Verifier
# Testa a inicialização da ISO em uma VM QEMU local com firmware UEFI.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${SCRIPT_DIR}/out"

# --- Argumentos ---------------------------------------------------------------
# Contrato: o primeiro argumento que for um ARQUIVO existente é a ISO a testar;
# qualquer outra coisa é repassada ao QEMU. Também reconhecemos --window.
#
# Isto corrige um bug antigo: $1 era usado ao mesmo tempo como caminho da ISO e
# como argumento posicional do QEMU, então a ISO entrava DUAS vezes (uma como
# -cdrom, outra como disco posicional). Pior, `verify-iso.sh --window` fazia
# `--window` virar ISO_FILE e abortava com "Nenhuma ISO encontrada".
FULLSCREEN_FLAG="-full-screen"
EXTRA_ARGS=()
ISO_FILE=""

for arg in "$@"; do
    case "$arg" in
        --window|-w|--windowed)
            FULLSCREEN_FLAG=""
            ;;
        -h|--help)
            echo "uso: $(basename "$0") [ISO] [--window] [args extras do QEMU...]"
            echo
            echo "  ISO             imagem a testar (padrão: a mais recente em appliance/out)"
            echo "  --window, -w    inicia em janela em vez de tela cheia"
            echo "  args extras     repassados literalmente ao qemu-system-x86_64"
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
done

if [ -z "${ISO_FILE}" ]; then
    ISO_FILE="$(ls -t "${OUTPUT_DIR}"/*.iso 2>/dev/null | head -n 1 || true)"
fi

if [ -z "${ISO_FILE}" ] || [ ! -f "${ISO_FILE}" ]; then
    echo "Erro: Nenhuma ISO encontrada em ${OUTPUT_DIR}. Execute build-iso.sh primeiro."
    exit 1
fi

# Seleção do firmware UEFI OVMF
OVMF_BIOS=""
if [ -f "/usr/share/edk2-ovmf/x64/OVMF.4m.fd" ]; then
    OVMF_BIOS="/usr/share/edk2-ovmf/x64/OVMF.4m.fd"
elif [ -f "/usr/share/edk2-ovmf/x64/OVMF_CODE.4m.fd" ]; then
    OVMF_BIOS="/usr/share/edk2-ovmf/x64/OVMF_CODE.4m.fd"
elif [ -f "/home/felipeab10/reims-vgpu/vm/ovmf/OVMF_CODE_4M.fd" ]; then
    OVMF_BIOS="/home/felipeab10/reims-vgpu/vm/ovmf/OVMF_CODE_4M.fd"
fi

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${CYAN}===================================================================${NC}"
echo -e "${CYAN}        vHackintosh Appliance - QEMU ISO Boot Verifier             ${NC}"
echo -e "${CYAN}===================================================================${NC}\n"
echo -e "  • Imagem ISO:     ${GREEN}${ISO_FILE}${NC} ($(du -h "${ISO_FILE}" | cut -f1))"
echo -e "  • Firmware UEFI:  ${GREEN}${OVMF_BIOS:-'Padrão QEMU'}${NC}"
echo -e "  • Vídeo:          ${GREEN}VGA std — mesmo dispositivo do motor nativo do appliance${NC}"
echo -e "  • Sessão:         ${GREEN}Wayland (sway kiosk) dentro da ISO — sem X11${NC}"
echo -e "  • Atalhos úteis:  ${YELLOW}Ctrl + Alt + F (Alterna Tela Cheia) | Ctrl + Alt + G (Libera Mouse)${NC}\n"
echo -e "${CYAN}Iniciando QEMU Appliance em modo Kiosk Fullscreen...${NC}\n"
echo -e "${YELLOW}Dica: dentro do appliance, valide a config da sessão com:${NC}"
echo -e "  ${GREEN}sway -C -c /etc/vhackintosh/sway.conf${NC}\n"

TEST_SSD="/var/tmp/vhackintosh-test-ssd.qcow2"
if [ ! -f "${TEST_SSD}" ]; then
    echo -e "${BLUE}▶ Criando disco virtual de teste SSD NVMe/VirtIO (64 GB sparse em /var/tmp)...${NC}"
    qemu-img create -f qcow2 "${TEST_SSD}" 64G >/dev/null
fi

# Argumentos base do QEMU com suporte a Q35 moderno, Virtualização Nested e SSD de Teste
QEMU_ARGS=(
    -enable-kvm
    -machine q35
    -cpu host,kvm=on
    -m 16G
    -smp 8
    -drive file="${TEST_SSD}",if=virtio,format=qcow2,id=ssd0
    -cdrom "${ISO_FILE}"
    -boot d
    # -vga std espelha o motor nativo do appliance (ui/tui.py usa `-vga std`).
    # Testar com outro dispositivo de vídeo verificaria um caminho que não é o
    # que roda de verdade.
    -vga std
    -display gtk,show-menubar=off,zoom-to-fit=on,grab-on-hover=on,show-cursor=on
)

if [ -n "${FULLSCREEN_FLAG}" ]; then
    QEMU_ARGS+=("${FULLSCREEN_FLAG}")
fi

if [ -n "${OVMF_BIOS}" ]; then
    QEMU_ARGS+=(-bios "${OVMF_BIOS}")
fi

exec qemu-system-x86_64 "${QEMU_ARGS[@]}" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
