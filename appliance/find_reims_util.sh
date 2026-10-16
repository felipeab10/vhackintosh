#!/usr/bin/env bash
# ==============================================================================
# Resolve o binário `reims-util` que a ISO leva em /usr/local/bin.
#
# Por que isto é obrigatório: no primeiro boot o appliance compila o reims-vgpu
# a partir das fontes que estão DENTRO da ISO, mas o reims-util vem de fora —
# `reims-ctl __record-install` roda
# `cargo install --git https://github.com/steelbrain/reims-util`, que exige
# rede. O primeiro boot pode estar offline. Sem um reims-util pronto na imagem,
# `core/reims_util.py:is_available()` devolve False e o vHackintosh provisiona o
# OpenCore pelo caminho antigo (template + guestfish), injetando o SMBIOS na
# mão — exatamente o que o provisionamento nativo veio substituir.
#
# A ISO 1.1.0 (15/10 15:01) saiu SEM o binário por causa disto: o build roda
# como root (`sudo appliance/build-iso.sh`), então `$HOME` é `/root` e o
# `/home/<usuário>/.cargo/bin/reims-util` — onde o `scripts/install-ctl.sh`
# realmente instala — nunca era visto. Por isso a busca olha também o home de
# quem chamou o sudo (SUDO_USER).
#
# Ordem de busca (a mesma que o `reims_util::binary()` do reims-ctl prefere):
# o binário que o host já instalou ao lado do reims-ctl (CARGO_HOME/bin), depois
# o PATH. Um binário antigo em /usr/local/bin nunca deve vencer o recém-compilado.
#
# Uso: find_reims_util.sh
#   imprime o caminho do binário e sai 0; sai 1 (sem imprimir nada) se não houver.
# ==============================================================================
set -euo pipefail

if [ -n "${REIMS_UTIL_HOST_BIN:-}" ]; then
    if [ -x "${REIMS_UTIL_HOST_BIN}" ] && [ ! -d "${REIMS_UTIL_HOST_BIN}" ]; then
        printf '%s\n' "${REIMS_UTIL_HOST_BIN}"
        exit 0
    fi
    echo "REIMS_UTIL_HOST_BIN=${REIMS_UTIL_HOST_BIN} não é um arquivo executável" >&2
    exit 1
fi

CANDIDATES=()

if [ -n "${CARGO_HOME:-}" ]; then
    CANDIDATES+=("${CARGO_HOME}/bin/reims-util")
fi

if [ -n "${HOME:-}" ]; then
    CANDIDATES+=("${HOME}/.cargo/bin/reims-util")
fi

# Sob `sudo`, HOME aponta para /root; o reims-util costuma estar no home de quem
# invocou (é ali que o install-ctl.sh instala). Sem isto a ISO sai sem ele.
if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
    SUDO_HOME="$(getent passwd "${SUDO_USER}" 2>/dev/null | cut -d: -f6 || true)"
    if [ -z "${SUDO_HOME}" ]; then
        SUDO_HOME="/home/${SUDO_USER}"
    fi
    CANDIDATES+=("${SUDO_HOME}/.cargo/bin/reims-util")
fi

WHICH_REIMS_UTIL="$(command -v reims-util 2>/dev/null || true)"
if [ -n "${WHICH_REIMS_UTIL}" ]; then
    CANDIDATES+=("${WHICH_REIMS_UTIL}")
fi

for candidate in "${CANDIDATES[@]}"; do
    if [ -n "${candidate}" ] && [ -x "${candidate}" ] && [ ! -d "${candidate}" ]; then
        printf '%s\n' "${candidate}"
        exit 0
    fi
done

exit 1
