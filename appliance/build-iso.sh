#!/usr/bin/env bash
# ==============================================================================
# vHackintosh Appliance - Automated ISO Generator
# Constrói a imagem ISO bootável (Live Kiosk) do vHackintosh OS
# com suporte ao reims-vgpu (Vulkan) e instaladores de macOS.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# O build pode remover/recriar appliance/out quando ela está vazia. Garanta
# que o processo não esteja executando a partir desse diretório removível.
cd "${PROJECT_ROOT}"
ARCHISO_PROFILE_SOURCE="${SCRIPT_DIR}/archiso"
BUILD_ID="${BUILD_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
BUILD_WORK="${BUILD_WORK:-/var/tmp/vhackintosh-archiso-${BUILD_ID}}"
PROFILE_WORK="${PROFILE_WORK:-/var/tmp/vhackintosh-profile-${BUILD_ID}}"
ARCHISO_PROFILE="${PROFILE_WORK}"
OUTPUT_DIR="${OUTPUT_DIR:-${SCRIPT_DIR}/out}"
PROFILE_WORK_CREATED=0
BUILD_WORK_CREATED=0

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

# Conferências do pacote pré-compilado (motor obrigatório). Fica num
# arquivo à parte para ser exercitada sem root/rede, ao contrário do build.
# Depois das cores: a função usa GREEN/YELLOW/NC.
# shellcheck source=lib/bundle_checks.sh
. "${SCRIPT_DIR}/lib/bundle_checks.sh"

echo -e "${CYAN}===================================================================${NC}"
echo -e "${CYAN}          vHackintosh Appliance - ISO Image Builder                ${NC}"
echo -e "${CYAN}===================================================================${NC}\n"

# 1. Parsing de Argumentos e Opções
CUSTOM_VERSION=""
KEEP_WORK=0
REIMS_RELEASE="${REIMS_RELEASE:-latest}"
REIMS_REPO="${REIMS_REPO:-felipeab10/reims-vgpu}"
RELEASE_ASSET_NAME="reims-vgpu-linux-x86_64.tar.gz"
REIMS_UTIL_UPDATE=0
REIMS_UTIL_REF="${REIMS_UTIL_REF:-master}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--version)
            CUSTOM_VERSION="$2"
            shift 2
            ;;
        -r|--release|--tag)
            REIMS_RELEASE="$2"
            shift 2
            ;;
        --update-reims-util)
            REIMS_UTIL_UPDATE=1
            shift
            ;;
        --reims-util-ref)
            REIMS_UTIL_REF="$2"
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
            echo "  -r, --release <TAG>   Tag da release do GitHub com o reims-vgpu pré-compilado"
            echo "                        (padrão: latest). Ex: --release v1.0.1"
            echo "      --update-reims-util  Compila e atualiza o reims-util oficial, sem construir a ISO."
            echo "      --reims-util-ref <REF> Branch/tag oficial do reims-util (padrão: master)."
            echo "  --keep-work           Preserva os diretórios temporários em /var/tmp após o build (útil para debug)."
            echo "  -h, --help            Exibe esta mensagem de ajuda"
            exit 0
            ;;
        *)
            if [[ "$1" =~ ^v?[0-9]+\.[0-9]+.*$ ]] || [ "$1" = "latest" ]; then
                REIMS_RELEASE="$1"
                shift
            else
                echo -e "${RED}Opção inválida: $1${NC}"
                echo "Execute '$0 --help' para instruções."
                exit 1
            fi
            ;;
    esac
done

# Atualização do reims-util roda no host e é independente da ISO.
if [ "${REIMS_UTIL_UPDATE}" -eq 1 ]; then
    REIMS_UTIL_REF="${REIMS_UTIL_REF}" bash "${SCRIPT_DIR}/update-reims-util.sh"
    exit $?
fi

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

# Limpa somente diretórios criados pelo builder. O sufixo das pastas padrão é
# o PID da execução; pastas de outra execução ainda ativa são preservadas.
cleanup_workdirs() {
    local exit_code=$?
    trap - EXIT

    if [ "${KEEP_WORK}" -eq 1 ]; then
        echo -e "\n${YELLOW}Aviso: diretórios de trabalho preservados (--keep-work):${NC}"
        echo -e "  Build:   ${BUILD_WORK}"
        echo -e "  Profile: ${PROFILE_WORK}"
        exit "${exit_code}"
    fi

    echo -e "\n${BLUE}▶ Limpando diretórios temporários do appliance...${NC}"
    for workdir in "${BUILD_WORK}" "${PROFILE_WORK}"; do
        case "${workdir}" in
            /var/tmp/vhackintosh-archiso-*|/var/tmp/vhackintosh-profile-*)
                if { [ "${workdir}" = "${BUILD_WORK}" ] && [ "${BUILD_WORK_CREATED}" -eq 1 ]; } || \
                   { [ "${workdir}" = "${PROFILE_WORK}" ] && [ "${PROFILE_WORK_CREATED}" -eq 1 ]; }; then
                    if [ -d "${workdir}" ] && [ ! -L "${workdir}" ]; then
                        rm -rf -- "${workdir}"
                    fi
                fi
                ;;
        esac
    done

    # Também recolhe resíduos de execuções anteriores que terminaram à força.
    # O PID no nome evita remover worktrees temporários de um build concorrente.
    local workdir name owner_pid
    for workdir in /var/tmp/vhackintosh-archiso-* /var/tmp/vhackintosh-profile-*; do
        [ -d "${workdir}" ] && [ ! -L "${workdir}" ] || continue
        [[ "${workdir}" == "${BUILD_WORK}" || "${workdir}" == "${PROFILE_WORK}" ]] && continue
        name="${workdir##*/}"
        if [[ "${name}" =~ ^vhackintosh-(archiso|profile)-.+-([0-9]+)$ ]]; then
            owner_pid="${BASH_REMATCH[2]}"
            if ! kill -0 "${owner_pid}" 2>/dev/null; then
                rm -rf -- "${workdir}"
            fi
        fi
    done
    echo -e "${GREEN}✔ Diretórios temporários removidos.${NC}"
    exit "${exit_code}"
}
trap cleanup_workdirs EXIT

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
PROFILE_WORK_CREATED=1
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

# OVMF_CODE.fd sempre mantém o mesmo tamanho. Force a cópia para que uma
# imagem recompilada com o mesmo mtime/tamanho do artefato anterior não fique
# de fora por causa do quick-check padrão do rsync.
install -m 0644 "${PROJECT_ROOT}/templates/OVMF_CODE_4M.fd" \
    "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/templates/OVMF_CODE_4M.fd"

# O macserial fica fora do rsync genérico para preservar explicitamente
# executável e licença no payload da appliance.
mkdir -p "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/resources/macserial"
install -m 0755 "${PROJECT_ROOT}/resources/macserial/macserial" "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/resources/macserial/macserial"
install -m 0644 "${PROJECT_ROOT}/resources/macserial/LICENSE.txt" "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/resources/macserial/LICENSE.txt"
install -m 0644 "${PROJECT_ROOT}/resources/macserial/README.md" "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/resources/macserial/README.md"

mkdir -p "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/appliance"
install -m 0644 "${SCRIPT_DIR}/build_opencore.py" \
    "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/appliance/build_opencore.py"

# Empacotamos o executável e usamos a mesma versão para gerar a EFI da ISO.
# O firmware OVMF permanece o do vHackintosh e mantém a logo embutida.
if ! REIMS_UTIL_HOST_BIN="$(bash "${SCRIPT_DIR}/find_reims_util.sh")"; then
    echo -e "${RED}✖ reims-util não encontrado neste host de build.${NC}"
    echo -e "  A ISO precisa levar este binário para gerar a EFI do OpenCore."
    echo -e "  Instale-o no host ou aponte ${CYAN}REIMS_UTIL_HOST_BIN=/caminho/para/reims-util${NC}."
    exit 1
fi

echo -e "${BLUE}▶ Empacotando utilitário nativo reims-util (${REIMS_UTIL_HOST_BIN})...${NC}"
mkdir -p "${ARCHISO_PROFILE}/airootfs/usr/local/bin"
install -m 0755 "${REIMS_UTIL_HOST_BIN}" "${ARCHISO_PROFILE}/airootfs/usr/local/bin/reims-util"
REIMS_UTIL_VERSION="$("${REIMS_UTIL_HOST_BIN}" --version | head -n1)"
REIMS_UTIL_COMMIT="unknown"
if [ -f "${REIMS_UTIL_HOST_BIN}.commit" ]; then
    REIMS_UTIL_COMMIT="$(head -n1 "${REIMS_UTIL_HOST_BIN}.commit")"
fi
cat > "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/REIMS-UTIL-PROVENANCE.txt" <<EOF
repository=https://github.com/steelbrain/reims-util
version=${REIMS_UTIL_VERSION}
commit=${REIMS_UTIL_COMMIT}
EOF

# A imagem vai pronta para uso offline: OpenCore, Lilu e os kexts de aceleração
# ficam no qcow2. --no-ovmf mantém o OVMF vHackintosh, com a logo do projeto.
echo -e "${BLUE}▶ Gerando OpenCore e kexts de aceleração com reims-util...${NC}"
python3 "${SCRIPT_DIR}/build_opencore.py" \
    --reims-util "${REIMS_UTIL_HOST_BIN}" \
    --output "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/templates/OpenCore.qcow2" \
    --config-output "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/templates/config.plist"

# 5. Obtenção obrigatória do reims-vgpu pré-compilado da release do fork.
REIMS_DEST="${ARCHISO_PROFILE}/airootfs/opt/reims-vgpu"
mkdir -p "${REIMS_DEST}"
if [ "${REIMS_RELEASE}" = "latest" ]; then
    RELEASE_URL="https://github.com/${REIMS_REPO}/releases/latest/download/${RELEASE_ASSET_NAME}"
else
    RELEASE_URL="https://github.com/${REIMS_REPO}/releases/download/${REIMS_RELEASE}/${RELEASE_ASSET_NAME}"
fi

echo -e "${BLUE}▶ Baixando o reims-vgpu pré-compilado (${REIMS_RELEASE}) de ${REIMS_REPO}...${NC}"
echo -e "  URL: ${CYAN}${RELEASE_URL}${NC}"
REIMS_TMP_TAR="$(mktemp "${TMPDIR:-/tmp}/reims-vgpu-release-XXXXXX.tar.gz")"
if command -v curl >/dev/null 2>&1; then
    _DL=(curl -fL --progress-bar -o "${REIMS_TMP_TAR}" "${RELEASE_URL}")
elif command -v wget >/dev/null 2>&1; then
    _DL=(wget -q --show-progress -O "${REIMS_TMP_TAR}" "${RELEASE_URL}")
else
    echo -e "${RED}Erro: nem curl nem wget estão disponíveis para baixar a release.${NC}"
    rm -f "${REIMS_TMP_TAR}"
    exit 1
fi
if ! "${_DL[@]}"; then
    rm -f "${REIMS_TMP_TAR}"
    echo -e "${RED}Erro: não foi possível baixar a release '${REIMS_RELEASE}'.${NC}"
    echo -e "Confira o asset ${RELEASE_ASSET_NAME} em ${CYAN}https://github.com/${REIMS_REPO}/releases${NC}"
    exit 1
fi
# O appliance usa os templates OVMF x86_64 próprios em /opt/vhackintosh; os
# firmwares x86_64, AArch64 e RISC-V do bundle são redundantes nesta ISO.
if ! tar -xzf "${REIMS_TMP_TAR}" -C "${REIMS_DEST}" --exclude='*/vm/ovmf/*'; then
    rm -f "${REIMS_TMP_TAR}"
    echo -e "${RED}Erro: o tarball da release ${REIMS_RELEASE} está corrompido.${NC}"
    exit 1
fi
rm -f "${REIMS_TMP_TAR}"
printf '%s\n' "${REIMS_RELEASE}" > "${REIMS_DEST}/.release"
echo -e "${GREEN}✓ reims-vgpu pré-compilado extraído (${REIMS_RELEASE}).${NC}"
if ! checa_motor_do_pacote "${REIMS_DEST}" "${REIMS_RELEASE}"; then
    echo -e "${RED}Erro: a ISO não pode ser gerada sem o reims-ctl da release.${NC}"
    exit 1
fi

# Confirma o QEMU e a ROM dentro do bundle que `core.reims_build` instala no
# primeiro boot. A cópia em /usr/local/lib era redundante: o launcher usa
# ~/.local/share/reims-ctl/qemu/current, preenchido por install_prebuilt().
REIMS_QEMU_BIN=""
for candidato in \
    "${REIMS_DEST}/vendor/qemu/build/bin/qemu-system-x86_64" \
    "${REIMS_DEST}/vendor/qemu/build/qemu-system-x86_64"
do
    if [ -f "${candidato}" ]; then REIMS_QEMU_BIN="${candidato}"; break; fi
done
if [ -z "${REIMS_QEMU_BIN}" ]; then
    echo -e "${RED}Erro: a release ${REIMS_RELEASE} não trouxe qemu-system-x86_64.${NC}"
    exit 1
fi

REIMS_ROM="$(find "${REIMS_DEST}" -name 'ReimsVgpu.rom' -type f 2>/dev/null | head -n1 || true)"
if [ -z "${REIMS_ROM}" ]; then
    echo -e "${RED}Erro: a release ${REIMS_RELEASE} não trouxe ReimsVgpu.rom.${NC}"
    exit 1
fi
if [ ! -f "${REIMS_DEST}/vendor/qemu/build/share/qemu/bios-256k.bin" ] \
    && [ ! -f "${REIMS_DEST}/vendor/qemu/pc-bios/bios-256k.bin" ]; then
    echo -e "${RED}Erro: a release ${REIMS_RELEASE} não trouxe bios-256k.bin no diretório de ROMs do QEMU.${NC}"
    echo -e "${RED}  Sem este firmware o QEMU pode não iniciar; publique o asset completo no fork.${NC}"
    exit 1
fi
printf '%s\n' \
    "mode=prebuilt" \
    "release=${REIMS_RELEASE}" \
    "repo=${REIMS_REPO}" \
    "asset=${RELEASE_ASSET_NAME}" > "${REIMS_DEST}/SOURCE-PROVENANCE.txt"

echo -e "${BLUE}▶ Modo release: o appliance instala somente o reims-vgpu pré-compilado.${NC}"

# 6. Permissões de execução dos scripts da appliance
chmod +x "${ARCHISO_PROFILE}/airootfs/usr/local/bin/"* || true
chmod +x "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/bin/"* || true
chmod +x "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/resources/macserial/macserial" || true
chmod +x "${ARCHISO_PROFILE}/airootfs/opt/vhackintosh/themes/grub/install.sh" || true

# 6.5. Bibliotecas de RUNTIME do QEMU da release.
# O binário da release é um ELF x86-64 dinâmico: exige libgtk-3, libepoxy,
# libpixman, libslirp, libpng, libjpeg, libasound, libpulse, libzstd, libz,
# libncursesw e libbz2. Na ISO isso TEM de estar presente, senão o appliance
# sobe sem QEMU. A checagem real (dentro do squashfs) está em
# appliance/validate_iso.py; aqui conferimos o contrato do perfil.
_REIMS_QEMU_LIBS=(gtk3 libepoxy pixman libslirp libpng libjpeg-turbo alsa-lib \
                  libpulse zstd ncurses bzip2)
_missing_libs=()
for _lib in "${_REIMS_QEMU_LIBS[@]}"; do
    if ! grep -qE "^[[:space:]]*${_lib}([[:space:]]|$)" \
        "${ARCHISO_PROFILE}/packages.x86_64"; then
        _missing_libs+=("${_lib}")
    fi
done
if [ "${#_missing_libs[@]}" -gt 0 ]; then
    echo -e "${RED}Erro: o perfil não declara bibliotecas de runtime do QEMU:${NC}"
    echo -e "${RED}  ${_missing_libs[*]}${NC}"
    exit 1
else
    echo -e "${GREEN}✓ Bibliotecas de runtime do QEMU da release declaradas.${NC}"
fi

# 7. Execução do mkarchiso
echo -e "\n${CYAN}▶ Iniciando montagem do sistema e compressão da ISO (ZSTD-19)...${NC}"
if [ -e "${BUILD_WORK}" ]; then
    echo -e "${RED}Erro: BUILD_WORK já existe; escolha um caminho novo em vez de apagar dados: ${BUILD_WORK}${NC}"
    exit 1
fi
mkdir -p "${BUILD_WORK}"
BUILD_WORK_CREATED=1

mkarchiso -v -w "${BUILD_WORK}" -o "${OUTPUT_DIR}" "${ARCHISO_PROFILE}"

# 8. Validar artefato final e produzir checksum/manifesto apenas após sucesso.
ISO_FILE="${OUTPUT_DIR}/vhackintosh-os-${ISO_VERSION}-x86_64.iso"
if [ ! -f "${ISO_FILE}" ]; then
    echo -e "${RED}Erro: mkarchiso não produziu o arquivo esperado ${ISO_FILE}.${NC}"
    exit 1
fi
if [ -f "${ISO_FILE}" ]; then
    python3 "${PROJECT_ROOT}/appliance/validate_iso.py" "${ISO_FILE}"
    echo -e "\n${GREEN}✔ Imagem ISO gerada e validada com sucesso!${NC}"
    echo -e "Arquivo: [bold cyan]${ISO_FILE}[/bold cyan]"
    echo -e "Tamanho: $(du -h "${ISO_FILE}" | cut -f1)"
    
    echo -e "${BLUE}▶ Gerando checksum SHA256...${NC}"
    CHECKSUM_TMP="${ISO_FILE}.sha256.tmp"
    sha256sum "${ISO_FILE}" > "${CHECKSUM_TMP}"
    mv -f -- "${CHECKSUM_TMP}" "${ISO_FILE}.sha256"
    cat "${ISO_FILE}.sha256"

    echo -e "\n${GREEN}Para gravar em um Pendrive USB (substitua /dev/sdX pelo seu dispositivo):${NC}"
    echo -e "  sudo dd if=${ISO_FILE} of=/dev/sdX bs=4M status=progress oflag=sync"
fi
