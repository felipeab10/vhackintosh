#!/usr/bin/env bash
# ==============================================================================
# vHackintosh Appliance - Automated ISO Generator
# Constrói a imagem ISO bootável (Live Kiosk) do vHackintosh OS
# com suporte a reims-vgpu (Vulkan) e Harnesses de IA (Claude, Codex, OpenCode).
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ARCHISO_PROFILE_SOURCE="${SCRIPT_DIR}/archiso"
BUILD_ID="${BUILD_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
BUILD_WORK="${BUILD_WORK:-/var/tmp/vhackintosh-archiso-${BUILD_ID}}"
PROFILE_WORK="${PROFILE_WORK:-/var/tmp/vhackintosh-profile-${BUILD_ID}}"
ARCHISO_PROFILE="${PROFILE_WORK}"
OUTPUT_DIR="${OUTPUT_DIR:-${SCRIPT_DIR}/out-${BUILD_ID}}"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}===================================================================${NC}"
echo -e "${CYAN}          vHackintosh Appliance - ISO Image Builder                ${NC}"
echo -e "${CYAN}===================================================================${NC}\n"

# 1. Parsing de Argumentos e Opções
REIMS_SRC="/home/felipeab10/reims-vgpu"
CUSTOM_VERSION=""
KEEP_WORK=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--version)
            CUSTOM_VERSION="$2"
            shift 2
            ;;
        -s|--source)
            REIMS_SRC="$2"
            shift 2
            ;;
        --keep-work)
            KEEP_WORK=1
            shift
            ;;
        -h|--help)
            echo "Uso: sudo $0 [opções]"
            echo ""
            echo "Opções:"
            echo "  -v, --version <VER>   Versão da ISO (ex: 1.1.0) e nome da imagem."
            echo "  -s, --source <DIR>    Diretório das fontes do reims-vgpu (padrão: /home/felipeab10/reims-vgpu)."
            echo "                        Empacota fontes/dependências; os binários são compilados no appliance."
            echo "  --keep-work           Preserva os diretórios temporários em /var/tmp após o build (útil para debug)."
            echo "  -h, --help            Exibe esta mensagem de ajuda"
            exit 0
            ;;
        *)
            echo -e "${RED}Opção inválida: $1${NC}"
            echo "Execute '$0 --help' para instruções."
            exit 1
            ;;
    esac
done

# 2. Checagem de privilégios de superusuário
if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}Erro: A construção de imagens de sistema de arquivos squashfs (archiso)${NC}"
    echo -e "${RED}requer privilégios de root para montar chroot e definir permissões.${NC}"
    echo -e "\nPor favor, execute novamente com sudo:"
    echo -e "  ${YELLOW}sudo ${BASH_SOURCE[0]} [opções]${NC}\n"
    exit 1
fi

# 3. Checagem do utilitário mkarchiso antes de criar temporários.
if ! command -v mkarchiso &>/dev/null; then
    echo -e "${YELLOW}Aviso: 'mkarchiso' não está instalado no sistema host.${NC}"
    echo -e "Para instalar as ferramentas oficiais do Archiso, execute:"
    echo -e "  ${GREEN}pacman -S --needed archiso${NC}\n"
    exit 1
fi

# 4. Criar cópia isolada do profile para não alterar arquivos versionados.
if [ -e "${PROFILE_WORK}" ] || [ -L "${PROFILE_WORK}" ]; then
    echo -e "${RED}Erro: PROFILE_WORK já existe; escolha um caminho novo: ${PROFILE_WORK}${NC}"
    exit 1
fi
if [ -e "${OUTPUT_DIR}" ] || [ -L "${OUTPUT_DIR}" ]; then
    echo -e "${RED}Erro: OUTPUT_DIR já existe; escolha um caminho novo: ${OUTPUT_DIR}${NC}"
    exit 1
fi
mkdir -p -- "${PROFILE_WORK}"
cp -a "${ARCHISO_PROFILE_SOURCE}/." "${PROFILE_WORK}/"

# A versão da ISO vem de um único lugar: -v, senão da tag -t, senão do que já
# está no profiledef.sh. Ela vale para o NOME do arquivo (mkarchiso monta
# <iso_name>-<iso_version>-<arch>.iso), para o metadata interno da imagem e
# fica gravada no profile de trabalho, sem alterar o perfil versionado.
if [ -n "${CUSTOM_VERSION}" ]; then
    ISO_VERSION="${CUSTOM_VERSION}"
else
    ISO_VERSION="$(sed -n 's/^iso_version="\(.*\)"/\1/p' "${ARCHISO_PROFILE_SOURCE}/profiledef.sh")"
fi
sed -i "s/^iso_version=\".*\"/iso_version=\"${ISO_VERSION}\"/" "${ARCHISO_PROFILE}/profiledef.sh"
echo -e "${BLUE}▶ Versão da ISO: ${ISO_VERSION} → vhackintosh-os-${ISO_VERSION}-x86_64.iso${NC}"
echo -e "${BLUE}  (grava iso_version=\"${ISO_VERSION}\" somente no perfil isolado)${NC}"

# 3. Preparação dos diretórios de saída e airootfs
echo -e "${BLUE}▶ Limpando e preparando estrutura da Appliance...${NC}"
rm -rf "${ARCHISO_PROFILE}/airootfs/opt"
mkdir -p "${OUTPUT_DIR}"
mkdir -p "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh"
mkdir -p "${ARCHISO_PROFILE}/airootfs/opt/reims-vgpu"

# 4. Sincronização do código-fonte do vHackintosh para o rootfs
echo -e "${BLUE}▶ Sincronizando módulos essenciais do vHackintosh em /opt/vhackintosh...${NC}"
rsync -av \
    --exclude='appliance' \
    --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='out' \
    "${PROJECT_ROOT}/bin" \
    "${PROJECT_ROOT}/cli" \
    "${PROJECT_ROOT}/core" \
    "${PROJECT_ROOT}/ui" \
    "${PROJECT_ROOT}/themes" \
    "${PROJECT_ROOT}/templates" \
    "${PROJECT_ROOT}/resources" \
    "${PROJECT_ROOT}/README.md" \
    "${PROJECT_ROOT}/.git" \
    "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/"

# O macserial fica fora do rsync genérico para preservar explicitamente
# executável e licença no payload da appliance.
mkdir -p "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/resources/macserial"
install -m 0755 "${PROJECT_ROOT}/resources/macserial/macserial" "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/resources/macserial/macserial"
install -m 0644 "${PROJECT_ROOT}/resources/macserial/LICENSE.txt" "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/resources/macserial/LICENSE.txt"
install -m 0644 "${PROJECT_ROOT}/resources/macserial/README.md" "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/resources/macserial/README.md"

# 5. Empacotamento exclusivo das fontes do reims-vgpu. Não transportar
# binários de um host de build: o QEMU, a ROM e o reims-ctl são compilados no
# próprio Linux appliance na primeira atualização.
REIMS_DEST="${ARCHISO_PROFILE}/airootfs/opt/reims-vgpu"
mkdir -p "${REIMS_DEST}"

if [ ! -d "${REIMS_SRC}" ] || [ ! -f "${REIMS_SRC}/Cargo.toml" ] || \
   [ ! -d "${REIMS_SRC}/crates" ] || [ ! -f "${REIMS_SRC}/vendor/qemu/meson.build" ] || \
   [ ! -f "${REIMS_SRC}/scripts/install-ctl.sh" ]; then
    echo -e "${RED}Erro: ${REIMS_SRC} não contém as fontes completas necessárias para compilar no appliance.${NC}"
    exit 1
fi

if [ -d "${REIMS_SRC}" ]; then
    if [ -e "${REIMS_SRC}/.git" ] && [ -n "$(git -C "${REIMS_SRC}" status --porcelain)" ]; then
        if [ "${ALLOW_DIRTY_REIMS:-0}" != "1" ]; then
            echo -e "${RED}Erro: fontes reims-vgpu modificadas; use cópia limpa ou defina ALLOW_DIRTY_REIMS=1 explicitamente.${NC}"
            exit 1
        fi
        echo -e "${YELLOW}AVISO: empacotando checkout reims-vgpu dirty por solicitação explícita.${NC}"
    fi
    if [ -d "${REIMS_SRC}/vendor/qemu" ]; then
        QEMU_SUBMODULE_STATUS="$(git -C "${REIMS_SRC}" submodule status -- vendor/qemu 2>/dev/null || true)"
        if [[ "${QEMU_SUBMODULE_STATUS}" != ' '* ]]; then
            echo -e "${RED}Erro: submódulo QEMU ausente, não inicializado, divergente ou com merge conflict.${NC}"
            exit 1
        fi
    fi
    REIMS_GIT_HEAD="$(git -C "${REIMS_SRC}" rev-parse HEAD 2>/dev/null || echo unknown)"
    REIMS_GIT_BRANCH="$(git -C "${REIMS_SRC}" branch --show-current 2>/dev/null || echo unknown)"
    QEMU_GIT_PIN="$(git -C "${REIMS_SRC}/vendor/qemu" rev-parse HEAD 2>/dev/null || echo unknown)"
    printf '%s\n' "source=${REIMS_SRC}" "branch=${REIMS_GIT_BRANCH}" "commit=${REIMS_GIT_HEAD}" "qemu=${QEMU_GIT_PIN}" "dirty_allowed=${ALLOW_DIRTY_REIMS:-0}" > "${REIMS_DEST}/SOURCE-PROVENANCE.txt"
    echo -e "${BLUE}▶ Empacotando apenas fontes do reims-vgpu para build no appliance...${NC}"
    for src_file in Cargo.toml Cargo.lock; do
        [ -f "${REIMS_SRC}/${src_file}" ] && cp -a "${REIMS_SRC}/${src_file}" "${REIMS_DEST}/"
    done
    if [ -d "${REIMS_SRC}/crates" ]; then
        rsync -a --exclude='target' --exclude='out' --exclude='*.pyc' \
            --exclude='*.a' --exclude='*.so' --exclude='*.dylib' \
            "${REIMS_SRC}/crates/" "${REIMS_DEST}/crates/"
    fi
    if [ -d "${REIMS_SRC}/firmware" ]; then
        rsync -a --exclude='out' --exclude='target' \
            "${REIMS_SRC}/firmware/" "${REIMS_DEST}/firmware/"
    fi
    if [ -d "${REIMS_SRC}/vendor/qemu" ]; then
        echo -e "${BLUE}▶ Empacotando fontes do QEMU (sem build e sem git metadata)...${NC}"
        mkdir -p "${REIMS_DEST}/vendor/qemu"
        rsync -a --exclude='build' --exclude='.git' --exclude='*.a' \
            --exclude='*.so' --exclude='*.dylib' --exclude='*.dll' --exclude='*.exe' \
            "${REIMS_SRC}/vendor/qemu/" "${REIMS_DEST}/vendor/qemu/"
    fi
    if [ -d "${REIMS_SRC}/scripts" ]; then
        rsync -a --exclude='*.pyc' "${REIMS_SRC}/scripts/" "${REIMS_DEST}/scripts/"
    fi
    if [ -d "${REIMS_SRC}/vm" ]; then
        mkdir -p "${REIMS_DEST}/vm"
        [ -f "${REIMS_SRC}/vm/boot-x86.sh" ] && cp -a "${REIMS_SRC}/vm/boot-x86.sh" "${REIMS_DEST}/vm/"
    fi
    if [ -d "${REIMS_SRC}/vendor/cargo" ]; then
        echo -e "${BLUE}▶ Empacotando dependências Rust vendorizadas...${NC}"
        rsync -a "${REIMS_SRC}/vendor/cargo/" "${REIMS_DEST}/vendor/cargo/"
        echo -e "${YELLOW}Aviso: este checkout não configura crates.io offline; build do Cargo poderá exigir rede.${NC}"
    else
        echo -e "${YELLOW}Aviso: vendor/cargo ausente; o appliance precisará de rede para resolver dependências.${NC}"
    fi
    if [ -d "${REIMS_SRC}/.cargo" ]; then
        mkdir -p "${REIMS_DEST}/.cargo"
        rsync -a "${REIMS_SRC}/.cargo/" "${REIMS_DEST}/.cargo/"
    fi
    if [ -f "${REIMS_SRC}/.cargo/config.toml" ] && \
       grep -q 'vendored-sources' "${REIMS_SRC}/.cargo/config.toml"; then
        mkdir -p "${REIMS_DEST}/.cargo"
        cp -a "${REIMS_SRC}/.cargo/config.toml" "${REIMS_DEST}/.cargo/config.toml"
    fi
else
    echo -e "${YELLOW}Aviso: fontes do reims-vgpu não encontradas em ${REIMS_SRC}; ISO será criada sem elas.${NC}"
fi

# 6. Permissões de execução dos scripts da appliance
chmod +x "${ARCHISO_PROFILE}/airootfs/usr/local/bin/"* || true
chmod +x "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/bin/"* || true

# 7. Execução do mkarchiso
echo -e "\n${CYAN}▶ Iniciando montagem do sistema e compressão da ISO (ZSTD-19)...${NC}"
if [ -e "${BUILD_WORK}" ]; then
    echo -e "${RED}Erro: BUILD_WORK já existe; escolha um caminho novo em vez de apagar dados: ${BUILD_WORK}${NC}"
    exit 1
fi
mkdir -p "${BUILD_WORK}"

mkarchiso -v -w "${BUILD_WORK}" -o "${OUTPUT_DIR}" "${ARCHISO_PROFILE}"

# 8. Validar artefato final e produzir checksum/manifesto apenas após sucesso.
ISO_FILE="${OUTPUT_DIR}/vhackintosh-os-${ISO_VERSION}-x86_64.iso"
if [ ! -f "${ISO_FILE}" ]; then
    echo -e "${RED}Erro: mkarchiso não produziu o arquivo esperado ${ISO_FILE}.${NC}"
    exit 1
fi
if [ -f "${ISO_FILE}" ]; then
    python3 "${PROJECT_ROOT}/appliance/validate_iso.py" "${ISO_FILE}" "${REIMS_SRC}"
    echo -e "\n${GREEN}✔ Imagem ISO gerada e validada com sucesso!${NC}"
    echo -e "Arquivo: [bold cyan]${ISO_FILE}[/bold cyan]"
    echo -e "Tamanho: $(du -h "${ISO_FILE}" | cut -f1)"
    
    echo -e "${BLUE}▶ Gerando checksum SHA256...${NC}"
    CHECKSUM_TMP="${ISO_FILE}.sha256.tmp"
    sha256sum "${ISO_FILE}" > "${CHECKSUM_TMP}"
    mv -f -- "${CHECKSUM_TMP}" "${ISO_FILE}.sha256"
    cat "${ISO_FILE}.sha256"

    # 9. Limpeza dos diretórios de trabalho temporários (/var/tmp)
    if [ "${KEEP_WORK}" -eq 0 ]; then
        echo -e "\n${BLUE}▶ Limpando diretórios de trabalho temporários em /var/tmp...${NC}"
        rm -rf -- "${BUILD_WORK}" "${PROFILE_WORK}"
        echo -e "${GREEN}✔ Diretórios de trabalho removidos com sucesso.${NC}"
    else
        echo -e "\n${YELLOW}Aviso: diretórios de trabalho preservados (--keep-work):${NC}"
        echo -e "  Build:   ${BUILD_WORK}"
        echo -e "  Profile: ${PROFILE_WORK}"
    fi

    echo -e "\n${GREEN}Para gravar em um Pendrive USB (substitua /dev/sdX pelo seu dispositivo):${NC}"
    echo -e "  sudo dd if=${ISO_FILE} of=/dev/sdX bs=4M status=progress oflag=sync"
fi
