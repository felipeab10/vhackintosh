#!/usr/bin/env bash
# ==============================================================================
# vHackintosh Appliance - ISO Boot Verifier
# Testa a inicialização da ISO em uma VM QEMU local com firmware UEFI.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${SCRIPT_DIR}/out"

ISO_FILE="${1:-$(ls -t "${OUTPUT_DIR}"/*.iso 2>/dev/null | head -n 1 || true)}"

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
NC='\033[0m'

echo -e "${CYAN}===================================================================${NC}"
echo -e "${CYAN}        vHackintosh Appliance - QEMU ISO Boot Verifier             ${NC}"
echo -e "${CYAN}===================================================================${NC}\n"
echo -e "  • Imagem ISO:     ${GREEN}${ISO_FILE}${NC} ($(du -h "${ISO_FILE}" | cut -f1))"
echo -e "  • Firmware UEFI:  ${GREEN}${OVMF_BIOS:-'Padrão QEMU'}${NC}"
echo -e "  • Memória RAM:    ${YELLOW}4 GB${NC}"
echo -e "  • vCPUs:          ${YELLOW}4 Cores${NC}\n"
echo -e "${CYAN}Iniciando QEMU com aceleração KVM e interface gráfica...${NC}\n"

TEST_SSD="/tmp/vhackintosh-test-ssd.qcow2"
if [ ! -f "${TEST_SSD}" ]; then
    echo -e "${BLUE}▶ Criando disco virtual de teste SSD NVMe/VirtIO (64 GB sparse)...${NC}"
    qemu-img create -f qcow2 "${TEST_SSD}" 64G >/dev/null
fi

# Argumentos base do QEMU com suporte a Virtualização Nested e SSD de Teste
QEMU_ARGS=(
    -enable-kvm
    -cpu host,kvm=on
    -m 4G
    -smp 4
    -drive file="${TEST_SSD}",if=virtio,format=qcow2,id=ssd0
    -cdrom "${ISO_FILE}"
    -boot d
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
