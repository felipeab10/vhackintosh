#!/usr/bin/env bash
# ==============================================================================
# vHackintosh OS — Boot Direto pelo SSD Virtual (QEMU)
# Testa a inicialização do sistema vHackintosh já instalado no HD/SSD.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_SSD="${1:-/tmp/vhackintosh-test-ssd.qcow2}"

if [ ! -f "${TEST_SSD}" ]; then
    echo -e "\033[0;31mErro: Disco virtual '${TEST_SSD}' não encontrado!\033[0m"
    echo -e "Você precisa executar a instalação primeiro via: \033[1;33mbash appliance/verify-iso.sh\033[0m"
    exit 1
fi

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
NC='\033[0m'

echo -e "${CYAN}===================================================================${NC}"
echo -e "${CYAN}        vHackintosh OS — Boot Direto do SSD (Bare-Metal Emulado)   ${NC}"
echo -e "${CYAN}===================================================================${NC}\n"
echo -e "  • Disco de Boot:  ${GREEN}${TEST_SSD}${NC} ($(du -h "${TEST_SSD}" | cut -f1))"
echo -e "  • Firmware UEFI:  ${GREEN}${OVMF_BIOS:-'Padrão QEMU'}${NC}"
echo -e "  • Memória RAM:    ${YELLOW}4 GB${NC}"
echo -e "  • vCPUs:          ${YELLOW}4 Cores${NC}\n"
echo -e "${CYAN}Iniciando QEMU diretamente pelo SSD (sem mídia de instalação)...${NC}\n"

QEMU_ARGS=(
    -enable-kvm
    -machine q35
    -cpu host,kvm=on
    -m 4G
    -smp 4
    -drive file="${TEST_SSD}",if=virtio,format=qcow2,id=ssd0
    -boot c
    -vga std
    -device virtio-net-pci,netdev=net0
    -netdev user,id=net0
    -device usb-ehci,id=ehci
    -device usb-tablet
)

if [ -n "${OVMF_BIOS}" ]; then
    QEMU_ARGS+=(-bios "${OVMF_BIOS}")
fi

exec qemu-system-x86_64 "${QEMU_ARGS[@]}" "$@"
