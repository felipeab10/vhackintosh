#!/usr/bin/env bash
# ==============================================================================
# vHackintosh Appliance - Automated ISO Generator
# Constrói a imagem ISO bootável (Live Kiosk) do vHackintosh OS
# com suporte a reims-vgpu (Vulkan) e Harnesses de IA (Claude, Codex, OpenCode).
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ARCHISO_PROFILE="${SCRIPT_DIR}/archiso"
BUILD_WORK="/tmp/vhackintosh-archiso-work"
OUTPUT_DIR="${SCRIPT_DIR}/out"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}===================================================================${NC}"
echo -e "${CYAN}          vHackintosh Appliance - ISO Image Builder                ${NC}"
echo -e "${CYAN}===================================================================${NC}\n"

# 1. Checagem de privilégios de superusuário
if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}Erro: A construção de imagens de sistema de arquivos squashfs (archiso)${NC}"
    echo -e "${RED}requer privilégios de root para montar chroot e definir permissões.${NC}"
    echo -e "\nPor favor, execute novamente com sudo:"
    echo -e "  ${YELLOW}sudo ${BASH_SOURCE[0]} [opções]${NC}\n"
    exit 1
fi

# 2. Checagem do utilitário mkarchiso
if ! command -v mkarchiso &>/dev/null; then
    echo -e "${YELLOW}Aviso: 'mkarchiso' não está instalado no sistema host.${NC}"
    echo -e "Para instalar as ferramentas oficiais do Archiso, execute:"
    echo -e "  ${GREEN}pacman -S --needed archiso${NC}\n"
    exit 1
fi

# 3. Preparação dos diretórios de saída e airootfs
echo -e "${BLUE}▶ Preparando estrutura de arquivos da Appliance...${NC}"
mkdir -p "${OUTPUT_DIR}"
mkdir -p "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh"
mkdir -p "${ARCHISO_PROFILE}/airootfs/opt/reims-vgpu"

# 4. Sincronização do código-fonte do vHackintosh para o rootfs
echo -e "${BLUE}▶ Sincronizando código-fonte do vHackintosh em /opt/vhackintosh...${NC}"
rsync -av --delete \
    --exclude='.git' \
    --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='out' \
    --exclude='appliance/out' \
    "${PROJECT_ROOT}/" "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/"

# 5. Sincronização dos binários e ROMs do reims-vgpu se presentes no host
REIMS_SRC="/home/felipeab10/reims-vgpu"
if [ -d "${REIMS_SRC}" ]; then
    echo -e "${BLUE}▶ Empacotando binários compilados do reims-vgpu e ROMs UEFI...${NC}"
    mkdir -p "${ARCHISO_PROFILE}/airootfs/opt/reims-vgpu"
    rsync -av \
        --include='*/' \
        --include='crates/reims-vgpu-efi/out/**' \
        --include='vendor/qemu/build/qemu-system-x86_64' \
        --include='vendor/qemu/build/pc-bios/**' \
        --include='vendor/qemu/pc-bios/**' \
        --include='vm/ovmf/**' \
        --include='vm/boot-x86.sh' \
        --include='scripts/**' \
        --exclude='*' \
        "${REIMS_SRC}/" "${ARCHISO_PROFILE}/airootfs/opt/reims-vgpu/" || true
fi

# 6. Permissões de execução dos scripts da appliance
chmod +x "${ARCHISO_PROFILE}/airootfs/usr/local/bin/"* || true

# 7. Execução do mkarchiso
echo -e "\n${CYAN}▶ Iniciando montagem do sistema e compressão da ISO (ZSTD-19)...${NC}"
rm -rf "${BUILD_WORK}"
mkdir -p "${BUILD_WORK}"

mkarchiso -v -w "${BUILD_WORK}" -o "${OUTPUT_DIR}" "${ARCHISO_PROFILE}"

# 8. Cálculo de Checksum
ISO_FILE=$(ls -t "${OUTPUT_DIR}"/*.iso 2>/dev/null | head -n 1 || true)
if [ -n "${ISO_FILE}" ] && [ -f "${ISO_FILE}" ]; then
    echo -e "\n${GREEN}✔ Imagem ISO gerada com sucesso!${NC}"
    echo -e "Arquivo: [bold cyan]${ISO_FILE}[/bold cyan]"
    echo -e "Tamanho: $(du -h "${ISO_FILE}" | cut -f1)"
    
    echo -e "${BLUE}▶ Gerando checksum SHA256...${NC}"
    sha256sum "${ISO_FILE}" > "${ISO_FILE}.sha256"
    cat "${ISO_FILE}.sha256"

    echo -e "\n${GREEN}Para gravar em um Pendrive USB (substitua /dev/sdX pelo seu dispositivo):${NC}"
    echo -e "  sudo dd if=${ISO_FILE} of=/dev/sdX bs=4M status=progress oflag=sync"
else
    echo -e "${YELLOW}Aviso: Processo finalizado, verifique o diretório ${OUTPUT_DIR}.${NC}"
fi
