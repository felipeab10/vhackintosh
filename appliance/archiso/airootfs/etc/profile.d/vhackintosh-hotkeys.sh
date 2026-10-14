#!/usr/bin/env bash
# vHackintosh — listener de hotkeys XF86 (brilho/volume) na sessão do appliance
# (change appliance-i18n…, grupo 4/D4). Arquivo próprio, separado do
# vhackintosh.sh, para o profile da sessão continuar sendo do usuário.
#
# Só na tty1 (a sessão do appliance), uma vez por sessão, em background e com
# silêncio total: sem hardware de brilho (desktop) o módulo simplesmente não
# trata as teclas e sai — inofensivo por construção.
if [ "$(tty)" = "/dev/tty1" ] && [ -z "${VHACKINTOSH_HOTKEYS:-}" ]; then
    export VHACKINTOSH_HOTKEYS=1
    if [ -d /opt/vhackintosh ] && command -v /usr/bin/python3 >/dev/null 2>&1; then
        PYTHONPATH=/opt/vhackintosh setsid /usr/bin/python3 -m core.hotkeys \
            >/dev/null 2>&1 &
    fi
fi
