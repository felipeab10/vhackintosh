#!/usr/bin/env bash
# ==============================================================================
# appliance/lib/bundle_checks.sh — conferências do pacote pré-compilado.
#
# Vive separado do build-iso.sh para poder ser exercitado sozinho: o build da
# ISO exige root, rede e ~20 minutos, então um aviso que só existe lá dentro
# nunca é medido. Aqui a função é chamada em teste com uma árvore de mentira,
# nos dois ramos (com e sem o motor) — um teste que só conferisse o TEXTO do
# build-iso.sh aceitaria `if [ -f ... ] || true;` e o aviso nunca apareceria
# (verificado por mutação: o teste de texto escapou).
#
# Espera as cores (GREEN/YELLOW/NC) definidas por quem faz o `source`.
# ==============================================================================

# Caminho do motor dentro do tarball da release: tem de ser o mesmo
# `PREBUILT_REIMS_CTL_RELS[0]` de core/reims_build.py, que é onde
# `install_prebuilt()` procura. Divergir daqui faz o appliance instalar o QEMU
# e não instalar o motor.
REIMS_CTL_BUNDLE_REL="bin/reims-ctl"

# checa_motor_do_pacote <destino> <release>
#
# 0 = o pacote trouxe o motor; 1 = não trouxe (e já imprimiu o aviso).
# NÃO exige o bit de execução: o tarball entrega 0644 e quem instala
# (`install_prebuilt`) é que faz o chmod 0755. Exigir `-x` aqui acusaria toda
# release legítima de estar quebrada.
checa_motor_do_pacote() {
    local destino="${1:?destino do pacote}"
    local release="${2:-desconhecida}"
    local motor="${destino}/${REIMS_CTL_BUNDLE_REL}"

    if [ -f "${motor}" ]; then
        printf '%b\n' "${GREEN}✓ motor reims-ctl presente no pacote.${NC}"
        return 0
    fi

    printf '%b\n' "${YELLOW}Aviso: o pacote ${release} não traz ${REIMS_CTL_BUNDLE_REL} (o motor da VM).${NC}"
    printf '%b\n' "${YELLOW}  Ele é instalado em tempo de execução em ~/.cargo/bin, mas esta"
    printf '%b\n' "  release é anterior ao empacotamento do motor: o appliance vai"
    printf '%b\n' "  instalar o QEMU e 'Iniciar VM' não terá com o que subir a VM.${NC}"
    printf '%b\n' "${YELLOW}  Publique/use uma tag que inclua ${REIMS_CTL_BUNDLE_REL} (ver scripts/release.sh).${NC}"
    return 1
}
