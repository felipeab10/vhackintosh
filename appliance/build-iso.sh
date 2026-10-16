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
OUTPUT_DIR="${OUTPUT_DIR:-${SCRIPT_DIR}/out}"

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
if [ -d "${OUTPUT_DIR}" ] && [ -z "$(ls -A "${OUTPUT_DIR}" 2>/dev/null)" ]; then
    rmdir "${OUTPUT_DIR}" 2>/dev/null || true
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

# Empacotamento do binário reims-util. É OBRIGATÓRIO: o reims-util não é
# compilado junto com o reims-vgpu (o `reims-ctl __record-install` o instala de
# https://github.com/steelbrain/reims-util com `cargo install --git`, e isso
# exige rede que o primeiro boot pode não ter). Sem ele na imagem,
# `core/reims_util.py:is_available()` devolve False e o OpenCore volta a ser
# provisionado por template + guestfish. Ver appliance/find_reims_util.sh.
if ! REIMS_UTIL_HOST_BIN="$(bash "${SCRIPT_DIR}/find_reims_util.sh")"; then
    echo -e "${RED}✖ reims-util não encontrado neste host de build.${NC}"
    echo -e "  A ISO precisa levá-lo: no primeiro boot o appliance compila o reims-vgpu"
    echo -e "  offline, mas o reims-util só existe se a imagem o trouxer consigo."
    echo -e "  Compile uma vez com ${CYAN}${REIMS_SRC}/scripts/install-ctl.sh${NC} (ele instala o"
    echo -e "  reims-util ao lado do reims-ctl) ou aponte ${CYAN}REIMS_UTIL_HOST_BIN=/caminho/para/reims-util${NC}."
    exit 1
fi

echo -e "${BLUE}▶ Empacotando utilitário nativo reims-util (${REIMS_UTIL_HOST_BIN})...${NC}"
mkdir -p "${ARCHISO_PROFILE}/airootfs/usr/local/bin"
install -m 0755 "${REIMS_UTIL_HOST_BIN}" "${ARCHISO_PROFILE}/airootfs/usr/local/bin/reims-util"

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

REIMS_REF="${REIMS_REF:-master}"

if [ -d "${REIMS_SRC}" ]; then
    if [ -d "${REIMS_SRC}/.git" ]; then
        # Extração local limpa a partir de REIMS_REF (default: master)
        if git -C "${REIMS_SRC}" rev-parse --verify "${REIMS_REF}" >/dev/null 2>&1; then
            TARGET_REF="${REIMS_REF}"
        else
            TARGET_REF="HEAD"
        fi
        REIMS_GIT_HEAD="$(git -C "${REIMS_SRC}" rev-parse "${TARGET_REF}" 2>/dev/null || echo unknown)"
        REIMS_GIT_BRANCH="$(git -C "${REIMS_SRC}" rev-parse --abbrev-ref "${TARGET_REF}" 2>/dev/null || echo "${TARGET_REF}")"
        QEMU_GIT_PIN="$(git -C "${REIMS_SRC}" rev-parse "${TARGET_REF}:vendor/qemu" 2>/dev/null || git -C "${REIMS_SRC}/vendor/qemu" rev-parse HEAD 2>/dev/null || echo unknown)"

        echo -e "${BLUE}▶ Extraindo fontes limpas do reims-vgpu a partir de '${TARGET_REF}' (${REIMS_GIT_HEAD:0:8})...${NC}"
        git -C "${REIMS_SRC}" archive "${TARGET_REF}" | tar -x -C "${REIMS_DEST}"

        if [ -d "${REIMS_SRC}/vendor/qemu" ]; then
            echo -e "${BLUE}▶ Extraindo fontes limpas do QEMU a partir do commit do submódulo (${QEMU_GIT_PIN:0:8})...${NC}"
            mkdir -p "${REIMS_DEST}/vendor/qemu"
            if git -C "${REIMS_SRC}/vendor/qemu" rev-parse --verify "${QEMU_GIT_PIN}" >/dev/null 2>&1; then
                git -C "${REIMS_SRC}/vendor/qemu" archive "${QEMU_GIT_PIN}" | tar -x -C "${REIMS_DEST}/vendor/qemu"
            else
                rsync -a --exclude='build' --exclude='.git' --exclude='*.a' \
                    --exclude='*.so' --exclude='*.dylib' --exclude='*.dll' --exclude='*.exe' \
                    "${REIMS_SRC}/vendor/qemu/" "${REIMS_DEST}/vendor/qemu/"
            fi
        fi
    else
        REIMS_GIT_HEAD="unknown"
        REIMS_GIT_BRANCH="unknown"
        QEMU_GIT_PIN="unknown"
        rsync -a --exclude='target' --exclude='build' --exclude='.git' "${REIMS_SRC}/" "${REIMS_DEST}/"
    fi

    printf '%s\n' "source=${REIMS_SRC}" "branch=${REIMS_GIT_BRANCH}" "commit=${REIMS_GIT_HEAD}" "qemu=${QEMU_GIT_PIN}" "dirty_allowed=0" > "${REIMS_DEST}/SOURCE-PROVENANCE.txt"

    if [ -d "${REIMS_SRC}/vendor/cargo" ]; then
        echo -e "${BLUE}▶ Empacotando dependências Rust vendorizadas...${NC}"
        rsync -a "${REIMS_SRC}/vendor/cargo/" "${REIMS_DEST}/vendor/cargo/"
        echo -e "${YELLOW}Aviso: este checkout não configura crates.io offline; build do Cargo poderá exigir rede.${NC}"
    else
        echo -e "${YELLOW}Aviso: vendor/cargo ausente; o appliance precisará de rede para resolver dependências.${NC}"
    fi

    chmod +x "${REIMS_DEST}/scripts/"* 2>/dev/null || true
else
    echo -e "${YELLOW}Aviso: fontes do reims-vgpu não encontradas em ${REIMS_SRC}; ISO será criada sem elas.${NC}"
fi

# 6. Permissões de execução dos scripts da appliance
chmod +x "${ARCHISO_PROFILE}/airootfs/usr/local/bin/"* || true
chmod +x "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/bin/"* || true
chmod +x "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/resources/macserial/macserial" || true
chmod +x "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/themes/grub/install.sh" || true

# 6.5. O appliance é uma imagem live: o pacman não tem bases de sincronização
# (/var/lib/pacman/sync) e pode não haver rede no primeiro boot, então o build do
# reims-vgpu só funciona se a ISO já levar todas as dependências que o
# `scripts/install-ctl.sh` exige por nome. Aqui isso é contrato, não esperança:
# o perfil tem de declarar cada nome de `pacman_pkgs`. (A checagem dos arquivos
# dentro do squashfs fica no validate_iso.py, depois do mkarchiso.)
echo -e "\n${BLUE}▶ Conferindo dependências do build do reims-vgpu no perfil...${NC}"
if ! python3 "${PROJECT_ROOT}/appliance/check_build_deps.py" \
    --reims-src "${REIMS_SRC}" \
    --profile "${ARCHISO_PROFILE}"; then
    echo -e "${RED}Erro: o perfil da ISO não cobre as dependências exigidas pelo reims-vgpu.${NC}"
    echo -e "${RED}Declare os pacotes listados acima em appliance/archiso/packages.x86_64.${NC}"
    exit 1
fi

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
