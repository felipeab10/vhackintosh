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

# Conferências do pacote pré-compilado (aviso do motor ausente). Fica num
# arquivo à parte para ser exercitada sem root/rede, ao contrário do build.
# Depois das cores: a função usa GREEN/YELLOW/NC.
# shellcheck source=lib/bundle_checks.sh
. "${SCRIPT_DIR}/lib/bundle_checks.sh"

echo -e "${CYAN}===================================================================${NC}"
echo -e "${CYAN}          vHackintosh Appliance - ISO Image Builder                ${NC}"
echo -e "${CYAN}===================================================================${NC}\n"

# 1. Parsing de Argumentos e Opções
REIMS_SRC="/home/felipeab10/reims-vgpu"
CUSTOM_VERSION=""
KEEP_WORK=0
# Modo pré-compilado: a ISO nasce com o reims-vgpu já compilado, vindo de uma
# release do GitHub. É o caminho recomendado: um binário x86-64 genérico serve
# para todos os hosts, em vez de cada máquina compilar o fork do QEMU por horas.
REIMS_RELEASE="${REIMS_RELEASE:-latest}"
REIMS_PREBUILT=1
REIMS_REPO="${REIMS_REPO:-felipeab10/reims-vgpu}"
# Nome do asset publicado em cada release do repositório.
RELEASE_ASSET_NAME="reims-vgpu-linux-x86_64.tar.gz"
# `--update-sources`: baixa a release e a extrai por cima de REIMS_SRC, para quem
# mantém um checkout das fontes em dia sem `git pull` no meio do build.
REIMS_UPDATE_SRC=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--version)
            CUSTOM_VERSION="$2"
            shift 2
            ;;
        -s|--source)
            REIMS_SRC="$2"
            # Passar as fontes é pedir o modo fontes: no modo pré-compilado elas
            # não são usadas, e ignorar a flag em silêncio faria o usuário achar
            # que compilou quando na verdade baixou um binário.
            REIMS_PREBUILT=0
            shift 2
            ;;
        -r|--release|--tag)
            REIMS_RELEASE="$2"
            shift 2
            ;;
        -u|--update-sources)
            # Atualiza as fontes do reims-vgpu a partir da release do GitHub e sai.
            REIMS_UPDATE_SRC=1
            shift
            ;;
        --from-source|--build-source)
            # Força o modo antigo (fontes empacotadas + compilação no appliance).
            REIMS_PREBUILT=0
            shift
            ;;
        --prebuilt)
            REIMS_PREBUILT=1
            shift
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
            echo "  --from-source         Empacota as fontes e compila no appliance (mais lento,"
            echo "                        exige rede ou cache do Cargo no primeiro boot)."
            echo "  -s, --source <DIR>    Diretório das fontes do reims-vgpu (padrão: /home/felipeab10/reims-vgpu)."
            echo "                        Implica --from-source; use --prebuilt depois para voltar."
            echo "  -u, --update-sources  Baixa a release do reims-vgpu e atualiza REIMS_SRC, sem"
            echo "                        construir a ISO. Combine com --release <TAG> para escolher."
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

# 1.5. `--update-sources`: só atualiza as fontes/artefato do reims-vgpu e sai.
# Não exige root nem mkarchiso: é download e extração.
if [ "${REIMS_UPDATE_SRC}" -eq 1 ]; then
    if [ "${REIMS_RELEASE}" = "latest" ]; then
        _UPD_URL="https://github.com/${REIMS_REPO}/releases/latest/download/${RELEASE_ASSET_NAME}"
    else
        _UPD_URL="https://github.com/${REIMS_REPO}/releases/download/${REIMS_RELEASE}/${RELEASE_ASSET_NAME}"
    fi
    if [ ! -d "${REIMS_SRC}" ]; then
        echo -e "${YELLOW}${REIMS_SRC} não existe; criando para receber a release.${NC}"
        mkdir -p "${REIMS_SRC}"
    fi
    echo -e "${BLUE}▶ Atualizando o reims-vgpu (${REIMS_RELEASE}) em ${REIMS_SRC}...${NC}"
    echo -e "  URL: ${CYAN}${_UPD_URL}${NC}"
    _UPD_TAR="$(mktemp "${TMPDIR:-/tmp}/reims-update-XXXXXX.tar.gz")"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --progress-bar -o "${_UPD_TAR}" "${_UPD_URL}"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --show-progress -O "${_UPD_TAR}" "${_UPD_URL}"
    else
        echo -e "${RED}Erro: nem curl nem wget estão disponíveis.${NC}"
        rm -f "${_UPD_TAR}"
        exit 1
    fi || {
        echo -e "${RED}Erro: download da release '${REIMS_RELEASE}' falhou.${NC}"
        echo -e "Confira as tags em ${CYAN}https://github.com/${REIMS_REPO}/releases${NC}"
        rm -f "${_UPD_TAR}"
        exit 1
    }
    # Preserva as fontes que já existem (o tarball só traz o artefato compilado).
    #
    # `-xzf`, e NÃO `-xkzf`: no `tar`, `k` é `--keep-old-files`, que PROÍBE
    # sobrescrever e faz a extração morrer com "Arquivo existe" (status 2) na
    # primeira colisão — isto é, no uso NORMAL desta opção, que é atualizar
    # fontes que já estão lá. O `k` estava ali por engano, arrastado do `k` de
    # `-xkzf` em outros pontos do script. A árvore ficava pela metade e o
    # script não percebia: seguia adiante e gravava o `.release`, devolvendo
    # sucesso. O `-z` é que descompacta; o `k` só atrapalhava.
    # A falha agora é checada: se a extração não completar, o `.release` NÃO é
    # escrito, para não marcar como atualizada uma árvore incompleta.
    if ! tar -xzf "${_UPD_TAR}" -C "${REIMS_SRC}"; then
        echo -e "${RED}Erro: extração da release ${REIMS_RELEASE} em ${REIMS_SRC} falhou.${NC}"
        echo -e "${RED}  A árvore pode ter ficado incompleta; NÃO foi marcada como atualizada.${NC}"
        rm -f "${_UPD_TAR}"
        exit 1
    fi
    rm -f "${_UPD_TAR}"
    printf '%s\n' "${REIMS_RELEASE}" > "${REIMS_SRC}/.release"
    echo -e "${GREEN}✓ reims-vgpu atualizado para ${REIMS_RELEASE} em ${REIMS_SRC}.${NC}"
    exit 0
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

# Empacotamento do binário reims-util, usado pelas operações nativas de OpenCore
# do reims-ctl. Ele não escolhe a EFI inicial do wizard: novas VMs copiam o
# template versionado derivado do Ultimate-macOS-KVM e injetam nele o SMBIOS.
# O reims-util não é compilado junto com o reims-vgpu (o `reims-ctl
# __record-install` o instala de https://github.com/steelbrain/reims-util com
# `cargo install --git`), então a ISO precisa levá-lo para funcionar offline.
# Ver appliance/find_reims_util.sh.
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

# 5. Obtenção do reims-vgpu.
#
# Padrão: baixar a release pré-compilada do GitHub. O binário é um ELF x86-64
# genérico (sem `-march=native`, sem caminhos do host de build) e carrega o
# Vulkan em runtime via dlopen de `libvulkan.so.1` — então o MESMO binário
# funciona em qualquer computador x64 cujo host tenha Vulkan 1.2+, sem compilar
# nada. Compilar o fork do QEMU em cada máquina custava horas.
#
# Alternativa (`--from-source`): empacotar as fontes e deixar o appliance compilar
# no primeiro boot — usada quando a release não existe ou se quer código local.
REIMS_DEST="${ARCHISO_PROFILE}/airootfs/opt/reims-vgpu"
mkdir -p "${REIMS_DEST}"

REIMS_PREBUILT_OK=0
# `mktemp` evita colisão entre builds concorrentes (set -u exige definir antes).
REIMS_TMP_TAR="$(mktemp "${TMPDIR:-/tmp}/reims-vgpu-release-XXXXXX.tar.gz")"

if [ "${REIMS_PREBUILT}" -eq 1 ]; then
    if [ "${REIMS_RELEASE}" = "latest" ]; then
        RELEASE_URL="https://github.com/${REIMS_REPO}/releases/latest/download/${RELEASE_ASSET_NAME}"
    else
        RELEASE_URL="https://github.com/${REIMS_REPO}/releases/download/${REIMS_RELEASE}/${RELEASE_ASSET_NAME}"
    fi

    echo -e "${BLUE}▶ Baixando o reims-vgpu pré-compilado (${REIMS_RELEASE}) de ${REIMS_REPO}...${NC}"
    echo -e "  URL: ${CYAN}${RELEASE_URL}${NC}"

    if command -v curl >/dev/null 2>&1; then
        _DL=(curl -fL --progress-bar -o "${REIMS_TMP_TAR}" "${RELEASE_URL}")
    elif command -v wget >/dev/null 2>&1; then
        _DL=(wget -q --show-progress -O "${REIMS_TMP_TAR}" "${RELEASE_URL}")
    else
        echo -e "${RED}Erro: nem curl nem wget estão disponíveis para baixar a release.${NC}"
        exit 1
    fi

    if "${_DL[@]}"; then
        # O tarball traz a raiz `./`; extraímos dentro de REIMS_DEST.
        if tar -xzf "${REIMS_TMP_TAR}" -C "${REIMS_DEST}"; then
            REIMS_PREBUILT_OK=1
            printf '%s\n' "${REIMS_RELEASE}" > "${REIMS_DEST}/.release"
            rm -f "${REIMS_TMP_TAR}"
            echo -e "${GREEN}✓ reims-vgpu pré-compilado extraído (${REIMS_RELEASE}).${NC}"
            # O motor `reims-ctl` é quem cria e sobe a VM; o pacote QEMU só dá a
            # GPU virtual. Releases empacotadas antes desta correção não trazem o
            # motor, e o sintoma é o appliance instalar a compilação e mesmo
            # assim não iniciar VM nenhuma. Aviso no build, onde ainda dá para
            # agir. A conferência mora em lib/bundle_checks.sh para ser testável
            # sem root/rede; o status é ignorado de propósito — a ISO continua
            # sendo montada, porque ela serve justamente para diagnosticar isso.
            checa_motor_do_pacote "${REIMS_DEST}" "${REIMS_RELEASE}" || true
        else
            echo -e "${RED}Erro: o tarball da release ${REIMS_RELEASE} está corrompido.${NC}"
            rm -f "${REIMS_TMP_TAR}"
            exit 1
        fi
    else
        rm -f "${REIMS_TMP_TAR}"
        echo -e "${YELLOW}Aviso: não foi possível baixar a release '${REIMS_RELEASE}'.${NC}"
        echo -e "${YELLOW}  Verifique a tag em https://github.com/${REIMS_REPO}/releases${NC}"
        echo -e "${YELLOW}  Caindo para o modo --from-source (compilação no appliance).${NC}"
        REIMS_PREBUILT=0
    fi
fi

if [ "${REIMS_PREBUILT_OK}" -eq 1 ]; then
    # Em modo pré-compilado NÃO existe árvore de fontes: `opt/reims-vgpu` fica só
    # com o que veio no tarball (vendor/qemu e vm). Duas entradas de
    # `file_permissions` apontam para caminhos que só o modo fontes cria, e o
    # mkarchiso reclamava a cada build:
    #   WARNING: Cannot change permissions of '.../opt/reims-vgpu/scripts/'. The file or directory does not exist.
    #   WARNING: Cannot change permissions of '.../opt/reims-vgpu/crates/'. The file or directory does not exist.
    # Aviso não derruba a build, mas mente: sugere payload faltando quando o modo
    # pré-compilado simplesmente não usa esses diretórios. Removemos SÓ no perfil
    # isolado — o profiledef.sh versionado continua com as quatro entradas, que o
    # modo `--from-source` precisa.
    #
    # `vendor/qemu/` e `vm/` NÃO saem daqui: os dois existem de verdade no
    # tarball, então as entradas deles valem e são necessárias.
    sed -i -e '\#^[[:space:]]*\["/opt/reims-vgpu/scripts/"\]#d' \
           -e '\#^[[:space:]]*\["/opt/reims-vgpu/crates/"\]#d' \
        "${ARCHISO_PROFILE}/profiledef.sh"

    # A release publica o QEMU no layout do prefixo instalado:
    #   vendor/qemu/build/bin/qemu-system-x86_64
    #   vendor/qemu/build/share/qemu/ReimsVgpu.rom
    # O caminho plano é aceito para não invalidar releases já publicadas.
    REIMS_QEMU_BIN=""
    for candidato in \
        "${REIMS_DEST}/vendor/qemu/build/bin/qemu-system-x86_64" \
        "${REIMS_DEST}/vendor/qemu/build/qemu-system-x86_64"
    do
        if [ -f "${candidato}" ]; then REIMS_QEMU_BIN="${candidato}"; break; fi
    done
    if [ -z "${REIMS_QEMU_BIN}" ]; then
        echo -e "${RED}Erro: a release ${REIMS_RELEASE} não trouxe o binário qemu-system-x86_64.${NC}"
        exit 1
    fi

    # Sem o binário do QEMU a ISO não serve para nada; sem a ROM do vGPU ela
    # sobe sem GPU virtual, e o sintoma (tela preta) não aponta para a causa.
    #
    # SÓ a `ReimsVgpu.rom` serve aqui. A `reims-vgpu-gop.rom` é de OUTRA geração
    # (26112 B contra 5632 B), e instalá-la com o nome novo publicaria uma ISO
    # com o firmware errado e nenhum erro na tela — o QEMU aceita as duas como
    # `romfile` sem reclamar. É o mesmo motivo pelo qual `prebuilt_rom` (Python)
    # a ignora de propósito: os dois caminhos têm de dar o MESMO veredito, e
    # antes desta correção davam opostos (o `find` a encontrava como fallback e
    # o `install` a renomeava). Quem precisa da ROM antiga é a release antiga, e
    # lá quem a passa é o `boot-x86.sh` dela, com `romfile=` explícito — o nome
    # neste prefixo não é consultado.
    REIMS_ROM="$(find "${REIMS_DEST}" -name 'ReimsVgpu.rom' -type f 2>/dev/null | head -n1 || true)"
    if [ -z "${REIMS_ROM}" ]; then
        REIMS_GOP_ANTIGA="$(find "${REIMS_DEST}" -name 'reims-vgpu-gop.rom' -type f 2>/dev/null | head -n1 || true)"
        echo -e "${YELLOW}Aviso: a release ${REIMS_RELEASE} veio sem a ROM do vGPU.${NC}"
        echo -e "${YELLOW}  A VM vai subir sem GPU virtual (tela preta/tinyCore sem aceleração).${NC}"
        if [ -n "${REIMS_GOP_ANTIGA}" ]; then
            echo -e "${YELLOW}  Ela traz só a 'reims-vgpu-gop.rom', que é da geração antiga e NÃO será instalada com o nome novo: isso poria firmware errado na ISO sem erro visível.${NC}"
        fi
    fi

    # O QEMU resolve `romfile` relativo ao próprio executável: ele procura em
    # `<dir do binário>/../share/qemu/`. Por isso o binário e as ROMs vão para
    # um prefixo próprio, com bin/ e share/qemu/ IRMÃOS — foi verificado com o
    # binário real (ROM em share/qemu/ ao lado de bin/ carrega; a mesma ROM um
    # nível abaixo dá "could not load PC BIOS"). NÃO mover o binário para
    # usr/local/bin/ sozinho: ali o prefixo viraria usr/local/ e as ROMs não
    # seriam achadas.
    REIMS_QEMU_PREFIX="${ARCHISO_PROFILE}/airootfs/usr/local/lib/reims-vgpu-qemu"
    mkdir -p "${REIMS_QEMU_PREFIX}/bin" "${REIMS_QEMU_PREFIX}/share/qemu"
    install -m 0755 "${REIMS_QEMU_BIN}" \
        "${REIMS_QEMU_PREFIX}/bin/qemu-system-x86_64" 2>/dev/null || true
    # Um atalho no PATH, já que o prefixo é profundo.
    #
    # Isto era um `ln -sf` para o caminho ABSOLUTO de dentro do guest
    # (`/usr/local/lib/reims-vgpu-qemu/bin/qemu-system-x86_64`). No HOST esse
    # destino não existe, então o symlink nascia QUEBRADO — e o mkarchiso, ao
    # reaplicar as permissões de `file_permissions`, resolve o link e aborta:
    #
    #   chmod: não é possível operar no link simbólico quebrado '.../reims-vgpu-qemu'
    #   ERROR: Failed to set permissions on '.../reims-vgpu-qemu'. Outside of valid path.
    #
    # Foi assim que a primeira build pré-compilada morreu (18/10) — a ISO não era
    # gerada. Um arquivo comum não tem esse problema, porque não há link para
    # resolver. O `exec` faz o /proc/self/exe virar o binário REAL, que é o
    # caminho de onde o QEMU deriva `<dir>/../share/qemu/` — o mesmo diretório de
    # onde ele lê `bios-256k.bin` e `ReimsVgpu.rom`. Medido nesta máquina com o
    # binário real: com `share/qemu/` ao lado de `bin/` o QEMU inicia; escondendo
    # o diretório ele aborta com "could not load PC BIOS 'bios-256k.bin'".
    # O symlink dava o mesmo resultado — o defeito era só o mkarchiso.
    REIMS_QEMU_SHORTCUT="${ARCHISO_PROFILE}/airootfs/usr/local/bin/reims-vgpu-qemu"
    # O diretório existe no perfil versionado (tem outros scripts), mas criar é
    # idempotente e evita que o atalho desapareça em silêncio se algum dia não
    # existir. O `ln -sf` antigo tolerava isso com `|| true` — e foi justamente
    # esse `|| true` que escondeu a falha até a ISO não ser gerada.
    mkdir -p "$(dirname "${REIMS_QEMU_SHORTCUT}")"
    cat > "${REIMS_QEMU_SHORTCUT}" <<'VHACK_QEMU_SHORTCUT'
#!/bin/sh
# Atalho para o QEMU pré-compilado da release, instalado por build-iso.sh em
# /usr/local/lib/reims-vgpu-qemu/{bin,share}.
#
# É um SCRIPT e não um link simbólico de propósito: o mkarchiso resolve symlinks
# ao aplicar `file_permissions`, e um link absoluto para um caminho que só existe
# dentro da ISO é "quebrado" no host de build — ele aborta com
# "Outside of valid path" e a ISO não sai.
#
# O exec é o que preserva o prefixo: o QEMU localiza `share/qemu/` a partir do
# seu próprio binário (/proc/self/exe), então ele precisa SER o binário real.
exec /usr/local/lib/reims-vgpu-qemu/bin/qemu-system-x86_64 "$@"
VHACK_QEMU_SHORTCUT
    chmod 0755 "${REIMS_QEMU_SHORTCUT}"

    if [ -n "${REIMS_ROM}" ]; then
        install -m 0644 "${REIMS_ROM}" "${REIMS_QEMU_PREFIX}/share/qemu/ReimsVgpu.rom"
        echo -e "${GREEN}  ✓ ROM do vGPU: $(basename "${REIMS_ROM}")${NC}"
    fi
    # As demais ROMs (pxe, vgabios) acompanham, se vierem no pacote.
    for origem in \
        "${REIMS_DEST}/vendor/qemu/build/share/qemu" \
        "${REIMS_DEST}/vendor/qemu/pc-bios"
    do
        [ -d "${origem}" ] || continue
        cp -a "${origem}/." "${REIMS_QEMU_PREFIX}/share/qemu/" 2>/dev/null || true
    done
    # Sem o pc-bios o QEMU nem inicia ("could not load PC BIOS"): isto é erro.
    if [ ! -f "${REIMS_QEMU_PREFIX}/share/qemu/bios-256k.bin" ]; then
        echo -e "${RED}Erro: a release ${REIMS_RELEASE} não trouxe share/qemu/bios-256k.bin.${NC}"
        echo -e "${RED}  Sem ele o QEMU não inicia. Publicar de novo com scripts/release.sh.${NC}"
        exit 1
    fi
    # O firmware UEFI é o outro artefato que a release pode não trazer: sem ele
    # a VM não dá boot, e o sintoma (tela preta) não aponta para a causa. A ISO
    # ainda consegue usar o OVMF do próprio sistema, então isto é aviso.
    if [ -z "$(ls -A "${REIMS_DEST}/vm/ovmf" 2>/dev/null)" ]; then
        echo -e "${YELLOW}Aviso: a release ${REIMS_RELEASE} veio sem vm/ovmf (firmware UEFI).${NC}"
        echo -e "${YELLOW}  A ISO vai depender do OVMF do sistema em tempo de execução;${NC}"
        echo -e "${YELLOW}  se ele não existir no host de destino, a VM não dará boot.${NC}"
    else
        OVMF_BIN="$(ls "${REIMS_DEST}/vm/ovmf/"*.fd 2>/dev/null | head -n1 || true)"
        if [ -n "${OVMF_BIN}" ]; then
            echo -e "${GREEN}  ✓ OVMF: $(basename "${OVMF_BIN}")${NC}"
        else
            echo -e "${YELLOW}Aviso: vm/ovmf existe mas sem arquivo .fd — o firmware pode não carregar.${NC}"
        fi
    fi
    printf '%s\n' \
        "mode=prebuilt" \
        "release=${REIMS_RELEASE}" \
        "repo=${REIMS_REPO}" \
        "asset=${RELEASE_ASSET_NAME}" > "${REIMS_DEST}/SOURCE-PROVENANCE.txt"
    echo -e "${BLUE}▶ Modo pré-compilado: o appliance NÃO compila o reims-vgpu.${NC}"
fi

# 5b. Modo fontes (fallback ou --from-source explícito).
if [ "${REIMS_PREBUILT_OK}" -ne 1 ]; then
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

    # Cache git do Cargo. O `cargo vendor` cobre apenas o crates.io, mas o
    # `metal2vulkan` é dependência **git** (Cargo.toml do fork,
    # `rev = 29d3ebb…`) e NÃO existe em vendor/cargo. Sem este cache o Cargo do
    # appliance vai ao GitHub — e sob `--offline` morre com
    #   can't checkout from 'https://github.com/steelbrain/metal2vulkan':
    #   you are in the offline mode (--offline)
    #
    # Levamos apenas o repositório **bare** (`git/db`), que é o que o Cargo
    # precisa para materializar o checkout sozinho. Medido neste host: o `db` do
    # metal2vulkan tem 188 MB e o `checkouts/` tem 9,3 GB, porque guarda uma
    # árvore de trabalho inteira por revisão (8 revisões, ~1,5 GB cada) — copiar
    # o `checkouts/` inteiro inviabiliza a ISO. Testado: com só o `db` e
    # CARGO_NET_OFFLINE=true, o Cargo materializa `checkouts/.../29d3ebb`
    # offline, sem rede.
    _CARGO_HOME_HOST="${CARGO_HOME:-${HOME}/.cargo}"
    REIMS_GIT_CACHE_DEST="${ARCHISO_PROFILE}/airootfs/opt/reims-vgpu-cargo-git"
    _packed_git_cache=0
    for _repo in "${_CARGO_HOME_HOST}"/git/db/metal2vulkan*; do
        # Sem correspondência o glob fica literal; o teste abaixo descarta.
        [ -d "${_repo}" ] || continue
        _name="$(basename "${_repo}")"
        echo -e "${BLUE}▶ Empacotando cache git do Cargo p/ build offline: ${_name}...${NC}"
        mkdir -p "${REIMS_GIT_CACHE_DEST}/git/db/${_name}"
        rsync -a "${_repo}/" "${REIMS_GIT_CACHE_DEST}/git/db/${_name}/"
        _packed_git_cache=1
    done
    if [ "${_packed_git_cache}" -eq 1 ]; then
        # A appliance copia este cache para o CARGO_HOME dela no primeiro boot
        # (ver core/reims_build.py:seed_cargo_git_cache).
        printf '%s\n' "metal2vulkan" > "${REIMS_GIT_CACHE_DEST}/REQUIRED-GIT-DEPS.txt"
    else
        echo -e "${YELLOW}Aviso: sem cache git do Cargo (metal2vulkan) em ${_CARGO_HOME_HOST}/git/db;${NC}"
        echo -e "${YELLOW}  o build do reims-vgpu no appliance exigirá rede na primeira compilação.${NC}"
    fi

    # Cache do registry do crates.io. O fork NÃO usa `cargo vendor` (não tem
    # `.cargo/config.toml` com `replace-with`), então as ~88 crates do Cargo.lock
    # vêm do registry em tempo de build. Sem `registry/cache` + `registry/index`
    # o offline para em "no matching package named `bitflags` found".
    # Medido neste host: index 33 MB + cache 96 MB. Com `git/db` (188 MB) mais
    # isto, `cargo metadata --offline` sai rc=0 do zero — sem rede.
    REIMS_REGISTRY_CACHE_DEST="${ARCHISO_PROFILE}/airootfs/opt/reims-vgpu-cargo-registry"
    _packed_registry=0
    for _sub in cache index; do
        if [ -d "${_CARGO_HOME_HOST}/registry/${_sub}" ]; then
            mkdir -p "${REIMS_REGISTRY_CACHE_DEST}/registry/${_sub}"
            rsync -a "${_CARGO_HOME_HOST}/registry/${_sub}/" \
                "${REIMS_REGISTRY_CACHE_DEST}/registry/${_sub}/"
            _packed_registry=1
        fi
    done
    if [ "${_packed_registry}" -eq 1 ]; then
        echo -e "${GREEN}  cache do registry empacotado: $(du -sh "${REIMS_REGISTRY_CACHE_DEST}" | cut -f1)${NC}"
    else
        echo -e "${YELLOW}Aviso: sem cache do registry em ${_CARGO_HOME_HOST}/registry;${NC}"
        echo -e "${YELLOW}  o build do reims-vgpu no appliance exigirá rede para as crates do Cargo.lock.${NC}"
    fi

    chmod +x "${REIMS_DEST}/scripts/"* 2>/dev/null || true
else
    echo -e "${YELLOW}Aviso: fontes do reims-vgpu não encontradas em ${REIMS_SRC}; ISO será criada sem elas.${NC}"
fi
fi  # fim do modo fontes (REIMS_PREBUILT_OK -ne 1)

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
#
# Isto só se aplica ao modo fontes: no modo pré-compilado não há compilação no
# appliance, e exigir as dependências de build travaria a ISO sem motivo.
if [ "${REIMS_PREBUILT_OK}" -eq 1 ]; then
    echo -e "\n${GREEN}✓ Modo pré-compilado: dependências de build do reims-vgpu não são exigidas.${NC}"
elif ! python3 "${PROJECT_ROOT}/appliance/check_build_deps.py" \
    --reims-src "${REIMS_SRC}" \
    --profile "${ARCHISO_PROFILE}"; then
    echo -e "${RED}Erro: o perfil da ISO não cobre as dependências exigidas pelo reims-vgpu.${NC}"
    echo -e "${RED}Declare os pacotes listados acima em appliance/archiso/packages.x86_64.${NC}"
    exit 1
else
    echo -e "${GREEN}✓ Dependências de build do reims-vgpu declaradas no perfil.${NC}"
fi

# 6.6. Bibliotecas de RUNTIME do QEMU pré-compilado.
# O binário da release é um ELF x86-64 dinâmico: exige libgtk-3, libepoxy,
# libpixman, libslirp, libpng, libjpeg, libasound, libpulse, libzstd, libz,
# libncursesw e libbz2. Na ISO isso TEM de estar presente, senão o appliance
# sobe sem QEMU. A checagem real (dentro do squashfs) está em
# appliance/validate_iso.py; aqui conferimos o contrato do perfil.
if [ "${REIMS_PREBUILT_OK}" -eq 1 ]; then
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
        echo -e "${YELLOW}Aviso: o perfil não declara libs de runtime do QEMU pré-compilado:${NC}"
        echo -e "${YELLOW}  ${_missing_libs[*]}${NC}"
        echo -e "${YELLOW}  O QEMU pode não iniciar no live. Adicione em appliance/archiso/packages.x86_64.${NC}"
    else
        echo -e "${GREEN}✓ Libs de runtime do QEMU pré-compilado declaradas no perfil.${NC}"
    fi
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
