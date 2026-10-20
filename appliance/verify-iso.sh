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
BUILD_TEST=0
# Diretório temporário do kernel/initrd extraídos no modo --build-test.
# Declarado aqui (e não só dentro do if) para o trap de limpeza poder consultá-lo
# com `set -u` mesmo quando o modo não é usado.
KERNEL_TMP=""
BUILD_TEST_TIMEOUT="${BUILD_TEST_TIMEOUT:-21600}"   # 6 h de teto para o build

usage() {
    echo "uso: $(basename "$0") [ISO] [opções] [args extras do QEMU...]"
    echo
    echo "  ISO             imagem a testar (padrão: a mais recente em appliance/out)"
    echo "  --window, -w    inicia em janela em vez de tela cheia"
    echo "  --headless      sem display: '-display none' + serial para arquivo de log"
    echo "  --no-window     apelido de --headless"
    echo "  --build-test    boota a ISO, roda o build do reims-vgpu DENTRO do guest e"
    echo "                  imprime PASS/FAIL. Implica --headless."
    echo "                  (exige uma ISO que já contenha o hook; ver AVISO abaixo)"
    echo "  --timeout <s>   teto de segundos do --build-test (padrão: ${BUILD_TEST_TIMEOUT})"
    echo "  args extras     repassados literalmente ao qemu-system-x86_64"
    echo
    echo "Ambiente:"
    echo "  VHACK_QEMU      binário do QEMU (padrão: qemu-system-x86_64)"
    echo "  VHACK_TEST_SSD  disco de teste (padrão: /var/tmp/vhackintosh-test-ssd.qcow2)"
    echo "  VHACK_SERIAL_DIR diretório do log serial (padrão: /var/tmp)"
    echo "  VHACK_OVMF      caminho do firmware OVMF (sobrepõe a autodetecção)"
    echo
    echo "Nota sobre o --build-test: ele boota o KERNEL DO PRÓPRIO ISO via"
    echo "  '-kernel/-initrd' extraídos do arquivo, porque o QEMU só aceita"
    echo "  '-append' junto de '-kernel' (com ISO/firmware ele aborta com"
    echo "  \"-append only allowed with -kernel option\"). O '-cdrom' continua"
    echo "  presente para o archiso encontrar a própria mídia."
    echo
    echo "Exemplos:"
    echo "  $(basename "$0")                                  # kiosk em tela cheia"
    echo "  $(basename "$0") --window                         # janela"
    echo "  $(basename "$0") --headless                       # sem display, log em arquivo"
    echo "  $(basename "$0") --build-test                     # build automatizado PASS/FAIL"
}

# O parsing aceita `--timeout <s>` e `--timeout=<s>`; por isso não é um `for arg`.
while [ "$#" -gt 0 ]; do
    arg="$1"
    case "$arg" in
        --window|-w|--windowed)
            FULLSCREEN_FLAG=""
            ;;
        --headless|--no-window)
            HEADLESS=1
            ;;
        --build-test)
            BUILD_TEST=1
            HEADLESS=1
            ;;
        --timeout)
            if [ "$#" -lt 2 ]; then
                echo -e "${RED}Erro: --timeout exige um valor em segundos.${NC}" >&2
                exit 2
            fi
            BUILD_TEST_TIMEOUT="$2"
            shift
            ;;
        --timeout=*)
            BUILD_TEST_TIMEOUT="${arg#--timeout=}"
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

if ! [[ "${BUILD_TEST_TIMEOUT}" =~ ^[0-9]+$ ]] || [ "${BUILD_TEST_TIMEOUT}" -le 0 ]; then
    echo -e "${RED}Erro: --timeout deve ser um inteiro positivo de segundos (recebido: ${BUILD_TEST_TIMEOUT}).${NC}" >&2
    exit 2
fi

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
    -boot d
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

# ==============================================================================
# 5. --build-test: injeção do hook de cmdline + espera pelo veredito
# ==============================================================================
# COMO FUNCIONA (e por que assim):
#   Passando `-append` ao QEMU (que define o cmdline do kernel, SUBSTITUINDO o
#   do grub), injetamos `vhack_build_test=1` — lido dentro do guest por
#   /etc/profile.d/vhackintosh.sh, que então chama
#   /usr/local/bin/verify-build-in-guest.sh e desliga a máquina. O veredito sai
#   no serial (ver `console=` abaixo) e este script o extrai do log.
#
#   POR QUE `-append` E NÃO EDITAR O grub.cfg: o grub.cfg é compartilhado pelo
#   boot normal de kiosk. Adicionar `console=ttyS0` lá mudaria (e poluiria) a
#   experiência normal para beneficiar só o teste. Pelo `-append` o efeito fica
#   confinado à execução do --build-test.
#
#   TRÊS PEÇAS SÃO NECESSÁRIAS — as três já morderam:
#     1. `console=tty0 console=ttyS0,115200` no cmdline, senão kernel e init
#        escrevem só na VGA e o arquivo de serial fica com as 2 linhas do OVMF;
#     2. reenviar `archisosearchuuid`/`archisobasedir`, porque o `-append`
#        SUBSTITUI o cmdline inteiro e sem eles o archiso não acha a mídia;
#     3. o script do guest espelhar o log em /dev/ttyS0 (feito lá), pois o host
#        não enxerga o stdout do guest.
#
#   IMPORTANTE — LIMITE HONESTO DESTE RECURSO: o hook só existe em ISOs
#   construídas DEPOIS que ele foi adicionado ao perfil. Uma ISO antiga bootará
#   normalmente, ignorará a flag e ficará parada na TUI até o timeout. Por isso o
#   timeout é reportado como INDETERMINADO, com essa causa listada primeiro, em
#   vez de fingir um FAIL.
#
#   `install_dir` e o UUID são lidos da própria ISO (grub.cfg embutido) /
#   profiledef.sh, para não codificar valores que mudam a cada build.

if [ "${BUILD_TEST}" -eq 1 ]; then
    # Descobre o UUID do archiso a partir do grub.cfg da própria ISO.
    ARCHISO_UUID=""
    GRUB_TMP="$(mktemp)" || GRUB_TMP=""
    if [ -n "${GRUB_TMP}" ]; then
        if command -v bsdtar >/dev/null 2>&1; then
            bsdtar -xOf "${ISO_FILE}" boot/grub/grub.cfg > "${GRUB_TMP}" 2>/dev/null || true
        fi
        if [ ! -s "${GRUB_TMP}" ] && command -v isoinfo >/dev/null 2>&1; then
            isoinfo -i "${ISO_FILE}" -x '/BOOT/GRUB/GRUB.CFG;1' > "${GRUB_TMP}" 2>/dev/null || true
        fi
        if [ -s "${GRUB_TMP}" ]; then
            ARCHISO_UUID="$(sed -n 's/.*archisosearchuuid=\([^ ]*\).*/\1/p' "${GRUB_TMP}" | head -n 1)"
        fi
        rm -f "${GRUB_TMP}"
    fi

    INSTALL_DIR="arch"
    if [ -f "${SCRIPT_DIR}/archiso/profiledef.sh" ]; then
        id_line="$(sed -n 's/^install_dir="\(.*\)"/\1/p' "${SCRIPT_DIR}/archiso/profiledef.sh")"
        if [ -n "${id_line}" ]; then
            INSTALL_DIR="${id_line}"
        fi
    fi

    # `console=` é o que FAZ o veredito chegar: sem ele o kernel/init escrevem
    # só na tela VGA (o perfil usa `terminal_output gfxterm` no GRUB e não tem
    # `console=` nenhum), então o arquivo de serial do QEMU recebe apenas as 2
    # linhas do OVMF e o --build-test dá TIMEOUT para sempre.
    #
    # Escolha: `console=tty0 console=ttyS0,115200` — tty0 PRIMEIRO de propósito.
    # O kernel manda o console interativo (/dev/console) para o ÚLTIMO
    # `console=` da linha. Com tty0 por último, o kiosk normal continua na tela
    # e o serial vira só um espelho de log; é o getty em tty1 que serve a TUI,
    # então nada da experiência de kiosk depende disso. Invertendo a ordem, o
    # /dev/console iria para o serial e a ISO bootada "normalmente" ficaria sem
    # saída na tela — exatamente a regressão que queremos evitar.
    #
    # Vai na variável reenviada (não no grub.cfg) para não afetar o boot normal:
    # o grub.cfg é compartilhado por kiosk e build-test.
    APPEND_ARGS="archisobasedir=${INSTALL_DIR}"
    if [ -n "${ARCHISO_UUID}" ]; then
        APPEND_ARGS="${APPEND_ARGS} archisosearchuuid=${ARCHISO_UUID}"
    else
        echo -e "${YELLOW}Aviso: não consegui extrair archisosearchuuid do grub.cfg da ISO.${NC}" >&2
        echo -e "${YELLOW}       Bootando sem ele; se a ISO não montar a mídia, o veredito será FAIL.${NC}" >&2
    fi
    APPEND_ARGS="${APPEND_ARGS} cow_spacesize=10G console=tty0 console=ttyS0,115200 vhack_build_test=1"

    # --------------------------------------------------------------------------
    # SOBRE `-append`: ele NÃO funciona com boot por ISO/firmware.
    # O QEMU aborta com:
    #   qemu-system-x86_64: -append only allowed with -kernel option
    # e o processo morre ANTES de qualquer boot — o que aparecia como
    # "TIMEOUT imediato" e nos fez culpar o hook do guest por várias rodadas.
    # Medido nesta máquina (QEMU 11.1.2): o erro ocorre com `-bios OVMF` e
    # também sem ele, ou seja, `-append` exige mesmo `-kernel`.
    #
    # Por isso bootamos DIRETO o kernel do próprio ISO (`-kernel`/`-initrd`
    # extraídos do arquivo) + `-cdrom` para o archiso achar a mídia. Assim o
    # `-append` é aceito e o cmdline chega ao kernel. É a única via que entrega
    # `vhack_build_test=1` ao guest sem tocar no grub.cfg.
    #
    # Custo honesto: o menu do GRUB é pulado (bootamos o kernel do mesmo jeito
    # que a entrada "vHackintosh OS Appliance" faria). O que validamos continua
    # sendo o kernel real + initramfs real + airootfs real da ISO.
    # --------------------------------------------------------------------------
    KERNEL_IMG=""
    INITRD_IMG=""
    # Caminho interno do archiso: <install_dir>/boot/x86_64/{vmlinuz-linux,initramfs-linux.img}
    # Descobrimos a arch do próprio ISO em vez de assumir x86_64.
    BOOT_ARCH="$(bsdtar -tf "${ISO_FILE}" 2>/dev/null \
        | sed -n "s|^${INSTALL_DIR}/boot/\([^/]*\)/vmlinuz-linux$|\1|p" | head -n 1)"
    [ -z "${BOOT_ARCH}" ] && BOOT_ARCH="x86_64"

    KERNEL_TMP="$(mktemp -d)" || KERNEL_TMP=""
    if [ -n "${KERNEL_TMP}" ]; then
        if bsdtar -xf "${ISO_FILE}" -C "${KERNEL_TMP}" \
               "${INSTALL_DIR}/boot/${BOOT_ARCH}/vmlinuz-linux" \
               "${INSTALL_DIR}/boot/${BOOT_ARCH}/initramfs-linux.img" 2>/dev/null; then
            KERNEL_IMG="${KERNEL_TMP}/${INSTALL_DIR}/boot/${BOOT_ARCH}/vmlinuz-linux"
            INITRD_IMG="${KERNEL_TMP}/${INSTALL_DIR}/boot/${BOOT_ARCH}/initramfs-linux.img"
            [ -s "${KERNEL_IMG}" ] || KERNEL_IMG=""
            [ -s "${INITRD_IMG}" ] || INITRD_IMG=""
        fi
    fi

    if [ -n "${KERNEL_IMG}" ] && [ -n "${INITRD_IMG}" ]; then
        QEMU_ARGS+=(-kernel "${KERNEL_IMG}" -initrd "${INITRD_IMG}")
        QEMU_ARGS+=(-append "${APPEND_ARGS}")
        BUILD_TEST_BOOT_MODE="kernel-direto"
        echo -e "${BLUE}▶ Teste automatizado de build ativado (boot direto do kernel do ISO).${NC}"
        echo -e "  • kernel:  ${GREEN}${KERNEL_IMG}${NC}"
        echo -e "  • initrd:  ${GREEN}${INITRD_IMG}${NC}"
    else
        # Sem extrair o kernel não há como injetar o cmdline: `-append` com ISO
        # seria rejeitado pelo QEMU. Falhamos ALTO em vez de rodar um teste que
        # nunca poderia dar veredito (e reportar TIMEOUT como se fosse do build).
        KEEP_KERNEL_TMP="${KERNEL_TMP}"
        KERNEL_TMP=""
        echo -e "${RED}Erro: não consegui extrair vmlinuz/initramfs de ${ISO_FILE}.${NC}" >&2
        echo -e "${RED}       Sem eles o QEMU recusaria '-append' e o teste não teria como${NC}" >&2
        echo -e "${RED}       injetar 'vhack_build_test=1'. Verifique se 'bsdtar' está instalado.${NC}" >&2
        [ -n "${KEEP_KERNEL_TMP}" ] && rm -rf "${KEEP_KERNEL_TMP}"
        exit 2
    fi

    echo -e "  • cmdline injetado: ${GREEN}${APPEND_ARGS}${NC}"
    echo -e "  • timeout: ${GREEN}${BUILD_TEST_TIMEOUT}s${NC}"
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

if [ "${BUILD_TEST}" -eq 0 ]; then
    exec "${QEMU_BIN}" "${QEMU_ARGS[@]}" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
fi

# --- Execução em modo build-test ------------------------------------------
"${QEMU_BIN}" "${QEMU_ARGS[@]}" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}" &
QEMU_PID=$!

cleanup_qemu() {
    if kill -0 "${QEMU_PID}" 2>/dev/null; then
        kill "${QEMU_PID}" 2>/dev/null || true
        wait "${QEMU_PID}" 2>/dev/null || true
    fi
    # Remove o diretório do kernel extraído: são ~212 MB (initramfs de ~195 MB)
    # por execução. Sem isto o /var/tmp do usuário enche silenciosamente.
    if [ -n "${KERNEL_TMP:-}" ] && [ -d "${KERNEL_TMP}" ]; then
        rm -rf "${KERNEL_TMP}" 2>/dev/null || true
    fi
}
trap cleanup_qemu EXIT INT TERM

echo -e "${CYAN}▶ QEMU rodando (pid ${QEMU_PID}). Aguardando o veredito no serial...${NC}"

VERDICT=""
# Marca se o QEMU morreu quase instantaneamente: sinal de erro de linha de
# comando (ex.: '-accel tcg' mal formado), não de boot lento/falho.
QEMU_EARLY_EXIT=0
QEMU_START_TS=$(date +%s)
DEADLINE=$(( QEMU_START_TS + BUILD_TEST_TIMEOUT ))
while kill -0 "${QEMU_PID}" 2>/dev/null; do
    if [ -n "${SERIAL_LOG}" ] && [ -f "${SERIAL_LOG}" ]; then
        if grep -q 'VHACK_BUILD_TEST_RESULT=PASS' "${SERIAL_LOG}" 2>/dev/null; then
            VERDICT="PASS"; break
        fi
        if grep -q 'VHACK_BUILD_TEST_RESULT=FAIL' "${SERIAL_LOG}" 2>/dev/null; then
            VERDICT="FAIL"; break
        fi
    fi
    if [ "$(date +%s)" -ge "${DEADLINE}" ]; then
        VERDICT="TIMEOUT"; break
    fi
    sleep 5
done

# Se saiu em menos de 20s sem veredito, nenhuma VM real chegou a bootar.
if [ -z "${VERDICT}" ] && [ $(( $(date +%s) - QEMU_START_TS )) -lt 20 ]; then
    QEMU_EARLY_EXIT=1
fi

# Se o QEMU morreu sozinho antes do veredito, ainda vale reler o log.
if [ -z "${VERDICT}" ] && [ -n "${SERIAL_LOG}" ] && [ -f "${SERIAL_LOG}" ]; then
    if grep -q 'VHACK_BUILD_TEST_RESULT=PASS' "${SERIAL_LOG}" 2>/dev/null; then
        VERDICT="PASS"
    elif grep -q 'VHACK_BUILD_TEST_RESULT=FAIL' "${SERIAL_LOG}" 2>/dev/null; then
        VERDICT="FAIL"
    fi
fi

cleanup_qemu
trap - EXIT INT TERM

echo
echo -e "${CYAN}===================================================================${NC}"
case "${VERDICT}" in
    PASS)
        echo -e "${GREEN}  RESULTADO: PASS — o reims-vgpu compilou dentro da ISO.${NC}"
        ;;
    FAIL)
        echo -e "${RED}  RESULTADO: FAIL — o build falhou dentro do guest.${NC}"
        ;;
    *)
        echo -e "${YELLOW}  RESULTADO: TIMEOUT/INDETERMINADO (${BUILD_TEST_TIMEOUT}s sem veredito).${NC}"
        # Diagnóstico barato e decisivo: se o serial não tem NENHUMA saída do
        # kernel, o problema é o console, não o build. Isso separa "a ISO é
        # antiga/sem hook" de "o cmdline não chegou ao serial", que exigem
        # correções diferentes.
        SERIAL_IS_EMPTY=1
        if [ -n "${SERIAL_LOG}" ] && [ -f "${SERIAL_LOG}" ]; then
            if grep -qiE 'Linux version|systemd|archiso|Welcome|BdsDxe' "${SERIAL_LOG}" 2>/dev/null; then
                SERIAL_IS_EMPTY=0
            fi
        fi
        # Distinção que já custou rodadas inteiras: se o QEMU nem chegou a rodar
        # (argumento inválido, binário ausente), NÃO é problema de console nem de
        # build. Sem isto o usuário ia caçar 'console=ttyS0' enquanto o QEMU
        # abortava na partida — exatamente o que aconteceu com '-accel tcg'.
        if [ "${QEMU_EARLY_EXIT}" -eq 1 ]; then
            echo -e "${RED}  Diagnóstico: o QEMU MORREU IMEDIATAMENTE — a VM nunca iniciou.${NC}"
            echo -e "    Isto NÃO é falha de build nem de console: é erro de argumento/ambiente."
            echo -e "    Veja a mensagem do QEMU impressa logo acima."
        elif [ "${SERIAL_IS_EMPTY}" -eq 1 ]; then
            echo -e "${YELLOW}  Diagnóstico: o serial não tem saída de kernel/guest nenhuma.${NC}"
            echo -e "    Isso indica que o console NÃO chegou ao ttyS0 — não que o build falhou."
            echo -e "    Confirme que a ISO foi reconstruída e que 'console=ttyS0' está no"
            echo -e "    cmdline injetado (impresso acima na linha 'cmdline injetado')."
        else
            echo -e "${YELLOW}  Diagnóstico: o guest produziu saída, então o console funciona.${NC}"
            echo -e "    Isso indica que o hook não rodou até o fim — build longo ou hook ausente."
        fi
        echo -e "${YELLOW}  Causas prováveis:${NC}"
        echo -e "    • A ISO é ANTIGA e não contém o hook (rebuilde com build-iso.sh)."
        echo -e "      Nesse caso ela boota normal e fica parada na TUI até o timeout."
        echo -e "    • O build é longo demais em TCG — aumente com --timeout <s>."
        echo -e "    • A ISO não conseguiu montar a própria mídia (archisosearchuuid)."
        echo -e "    • O console não chegou ao serial (ver diagnóstico acima)."
        ;;
esac
if [ -n "${SERIAL_LOG}" ]; then
    echo -e "  Log serial: ${GREEN}${SERIAL_LOG}${NC}"
    echo -e "  Últimas linhas:"
    tail -n 20 "${SERIAL_LOG}" 2>/dev/null | sed 's/^/    /' || true
fi
echo -e "${CYAN}===================================================================${NC}"

case "${VERDICT}" in
    PASS) exit 0 ;;
    FAIL) exit 1 ;;
    *)    exit 2 ;;
esac
