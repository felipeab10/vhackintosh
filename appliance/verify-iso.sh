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

OVMF_CODE="/usr/share/edk2-ovmf/x64/OVMF_CODE.fd"
if [ ! -f "${OVMF_CODE}" ]; then
    OVMF_CODE="/home/felipeab10/reims-vgpu/vm/ovmf/OVMF_CODE_4M.fd"
fi

echo "▶ Iniciando teste da ISO no QEMU com UEFI..."
echo "  • Arquivo ISO: ${ISO_FILE}"
echo "  • Firmware UEFI: ${OVMF_CODE}"

exec qemu-system-x86_64 \
    -enable-kvm \
    -m 4G \
    -smp 4 \
    -drive if=pflash,format=raw,readonly=on,file="${OVMF_CODE}" \
    -cdrom "${ISO_FILE}" \
    -boot d \
    -vga std \
    -device virtio-net-pci,netdev=net0 \
    -netdev user,id=net0
