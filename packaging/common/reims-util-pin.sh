#!/usr/bin/env bash
# ==============================================================================
# Leitor único do pin do `reims-util` (packaging/reims-util.pin).
#
# Fonte de verdade compartilhada pelo pacote do Control
# (packaging/common/build-reims-util.sh) e pelo build da ISO do appliance
# (appliance/build-iso.sh). Sem este arquivo, os dois caminhos voltam a divergir
# de origem — foi exatamente o bug D1 do PLANO-INTEGRACAO-REIMS-UTIL.md.
#
# Uso (source, não exec):
#   . "packaging/common/reims-util-pin.sh"
#   reims_util_pin_load          # define REIMS_UTIL_REPO, REIMS_UTIL_REV,
#                                # REIMS_UTIL_TOOLCHAIN e REIMS_UTIL_GRAPHICS_PROFILE
#
# `reims_util_pin_verify_qemu_device <QEMU>` confirma que o QEMU que a ISO
# EMBARCA traz o device do perfil declarado; divergência aborta o build, para
# que os kexts gerados e o device do QEMU não se separem em silêncio.
#
# `reims_util_pin_verify_binary <BIN>` aborta (return 1) quando o binário do
# host não declara exatamente o SHA do pin. Sem o sidecar `<BIN>.commit` — ou
# com um SHA diferente — a procedência é desconhecida, e procedência
# desconhecida é ERRO de build, nunca o default `commit=unknown`.
# ==============================================================================

# Raiz do repositório (o arquivo vive em packaging/common/). Permite que o
# chamador leia o pin mesmo quando roda de outro cwd.
REIMS_UTIL_PIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REIMS_UTIL_PIN_FILE="${REIMS_UTIL_PIN_FILE:-${REIMS_UTIL_PIN_ROOT}/packaging/reims-util.pin}"

reims_util_pin_load() {
    local file="${REIMS_UTIL_PIN_FILE}"
    if [ ! -r "$file" ]; then
        echo "reims-util pin file not found or unreadable: $file" >&2
        return 1
    fi
    REIMS_UTIL_REPO=""
    REIMS_UTIL_REV=""
    REIMS_UTIL_TOOLCHAIN=""
    REIMS_UTIL_GRAPHICS_PROFILE=""
    REIMS_CTL_REPO=""
    REIMS_CTL_RELEASE=""
    REIMS_CTL_ASSET=""
    REIMS_CTL_BUNDLE_REL=""
    local line key value
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|\#*) continue ;;
        esac
        key="${line%%=*}"
        value="${line#*=}"
        case "$key" in
            REPO) REIMS_UTIL_REPO="$value" ;;
            REV) REIMS_UTIL_REV="$value" ;;
            TOOLCHAIN) REIMS_UTIL_TOOLCHAIN="$value" ;;
            GRAPHICS_PROFILE) REIMS_UTIL_GRAPHICS_PROFILE="$value" ;;
            CTL_REPO) REIMS_CTL_REPO="$value" ;;
            CTL_RELEASE) REIMS_CTL_RELEASE="$value" ;;
            CTL_ASSET) REIMS_CTL_ASSET="$value" ;;
            CTL_BUNDLE_REL) REIMS_CTL_BUNDLE_REL="$value" ;;
            *) echo "unknown key in reims-util pin: $key" >&2; return 1 ;;
        esac
    done < "$file"

    local missing=""
    [ -n "$REIMS_UTIL_REPO" ] || missing="${missing} REPO"
    [ -n "$REIMS_UTIL_REV" ] || missing="${missing} REV"
    [ -n "$REIMS_UTIL_TOOLCHAIN" ] || missing="${missing} TOOLCHAIN"
    [ -n "$REIMS_UTIL_GRAPHICS_PROFILE" ] || missing="${missing} GRAPHICS_PROFILE"
    if [ -n "$missing" ]; then
        echo "reims-util pin is missing required keys:${missing} (${file})" >&2
        return 1
    fi
    if ! printf '%s' "$REIMS_UTIL_REV" | grep -Eq '^[0-9a-f]{40}$'; then
        echo "reims-util pin REV is not a full 40-char commit SHA: $REIMS_UTIL_REV" >&2
        return 1
    fi
    # O motor: se UMA das chaves do par aparecer, todas têm de aparecer. Um pin
    # pela metade empacotaria o par descasado de novo em silêncio.
    local ctl_keys="${REIMS_CTL_REPO}${REIMS_CTL_RELEASE}${REIMS_CTL_ASSET}${REIMS_CTL_BUNDLE_REL}"
    if [ -n "$ctl_keys" ]; then
        local ctl_missing=""
        [ -n "$REIMS_CTL_REPO" ] || ctl_missing="${ctl_missing} CTL_REPO"
        [ -n "$REIMS_CTL_RELEASE" ] || ctl_missing="${ctl_missing} CTL_RELEASE"
        [ -n "$REIMS_CTL_ASSET" ] || ctl_missing="${ctl_missing} CTL_ASSET"
        [ -n "$REIMS_CTL_BUNDLE_REL" ] || ctl_missing="${ctl_missing} CTL_BUNDLE_REL"
        if [ -n "$ctl_missing" ]; then
            echo "reims-util pin declares the motor partially, missing:${ctl_missing} (${file})" >&2
            return 1
        fi
        if [ "$REIMS_CTL_BUNDLE_REL" != "bin/reims-ctl" ]; then
            echo "reims-util pin CTL_BUNDLE_REL must be bin/reims-ctl, got: $REIMS_CTL_BUNDLE_REL" >&2
            return 1
        fi
    fi
    return 0
}

# Confere que `<BIN>` é o binário do commit pinado. Exige o sidecar `<BIN>.commit`
# gravado pelo `appliance/update-reims-util.sh` (ou pelo `install-ctl.sh`).
# Falha ruidosamente quando o sidecar falta ou o SHA diverge.
reims_util_pin_verify_binary() {
    local bin="${1:?usage: reims_util_pin_verify_binary BINARY}"
    [ -n "${REIMS_UTIL_REV:-}" ] || { reims_util_pin_load || return 1; }
    if [ ! -x "$bin" ] || [ -d "$bin" ]; then
        echo "reims-util candidate is not an executable file: $bin" >&2
        return 1
    fi
    local sidecar="${bin}.commit"
    if [ ! -f "$sidecar" ]; then
        echo "reims-util provenance unknown: missing sidecar ${sidecar}" >&2
        echo "  O binário do host precisa declarar o commit pinado (${REIMS_UTIL_REV})." >&2
        echo "  Gere com: appliance/update-reims-util.sh  (grava ${sidecar})" >&2
        return 1
    fi
    local declared
    declared="$(head -n1 "$sidecar" | tr -d '[:space:]')"
    if [ "$declared" != "$REIMS_UTIL_REV" ]; then
        echo "reims-util provenance mismatch for $bin" >&2
        echo "  sidecar ${sidecar} declares: ${declared:-<empty>}" >&2
        echo "  pin (packaging/reims-util.pin) requires: ${REIMS_UTIL_REV}" >&2
        return 1
    fi
    return 0
}

# Confere que o QEMU que a ISO EMBARCA traz o device do perfil declarado no pin.
# Isto fecha o laço da declaração a priori: se o perfil pinado diz `apple-gfx-pci`
# e o tarball da release trouxer `reims-graphics-pci`, o build para em vez de
# gerar uma EFI cujos kexts não casam com o device do QEMU.
reims_util_pin_verify_qemu_device() {
    local qemu="${1:?usage: reims_util_pin_verify_qemu_device QEMU_BINARY}"
    [ -n "${REIMS_UTIL_GRAPHICS_PROFILE:-}" ] || { reims_util_pin_load || return 1; }
    if [ ! -f "$qemu" ]; then
        echo "qemu binary not found for profile check: $qemu" >&2
        return 1
    fi
    local has_profile=0 other=0
    if grep -qa -- "${REIMS_UTIL_GRAPHICS_PROFILE}" "$qemu"; then
        has_profile=1
    fi
    # O device novo, quando a branch development o soma, prevalece; se o perfil
    # pinado é o legado e este binário SÓ traz o novo, é divergência.
    case "${REIMS_UTIL_GRAPHICS_PROFILE}" in
        apple-gfx-pci)
            if grep -qa -- 'reims-graphics-pci' "$qemu"; then other=1; fi
            ;;
    esac
    if [ "$has_profile" -ne 1 ]; then
        echo "qemu device mismatch: $qemu does not advertise the pinned profile " >&2
        echo "  ${REIMS_UTIL_GRAPHICS_PROFILE} (packaging/reims-util.pin)" >&2
        return 1
    fi
    if [ "$other" -eq 1 ]; then
        echo "qemu device mismatch: $qemu advertises reims-graphics-pci, but the " >&2
        echo "  ISO is pinned to ${REIMS_UTIL_GRAPHICS_PROFILE} (packaging/reims-util.pin)" >&2
        return 1
    fi
    return 0
}
