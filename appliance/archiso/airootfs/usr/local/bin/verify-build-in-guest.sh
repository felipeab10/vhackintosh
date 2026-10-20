#!/usr/bin/env bash
# ==============================================================================
# vHackintosh OS — In-guest reims-vgpu build verification (headless)
# ==============================================================================
# Roda DENTRO da ISO vhackintosh para provar, sem olhar uma janela GTK, que a
# árvore do reims-vgpu que veio na imagem realmente compila.
#
# Por que este arquivo existe separado do TUI: a TUI exige um terminal
# interativo e faz perguntas (Confirm.ask) antes de compilar. Para automação
# headless precisamos de uma resposta binária PASS/FAIL, com o exit code
# carregando a resposta. Reescrever o TUI seria arriscado; aqui só *chamamos* o
# mesmo motor de build (`core.reims_build.build`), que é o único lugar onde a
# lógica de build mora. Se o motor mudar, este script o acompanha de graça.
#
# Saída: relatório legível em stdout E em /var/log/vhackintosh-build-test.log,
# mais um veredito em uma linha que o host pode grepar:
#     VHACK_BUILD_TEST_RESULT=PASS   (exit 0)
#     VHACK_BUILD_TEST_RESULT=FAIL   (exit 1)
#
# Uso: verify-build-in-guest.sh [-h|--help]
# Não é chamado automaticamente; o hook de cmdline e o usuário o invocam.
#
# PULAR O GATE DE VULKAN: não há flag própria aqui de propósito. O ÚNICO jeito
# de pular a checagem de Vulkan do appliance é `VHACK_SKIP_VULKAN_CHECK=1`
# (core/gpu.py:VULKAN_SKIP_ENV, lido pelo `vhackintosh check-vulkan`). Inventar
# uma segunda saída criaria dois caminhos de bypass para a mesma trava, o que é
# exatamente o tipo de divergência que causa bug de segurança/regressão.
# ==============================================================================

set -uo pipefail

REPORT_LOG="/var/log/vhackintosh-build-test.log"
VHACK_ROOT="/opt/vhackintosh"
REIMS_DIR="/opt/reims-vgpu"

for arg in "$@"; do
    case "$arg" in
        -h|--help)
            echo "uso: $(basename "$0")"
            echo "  Roda a verificação de build do reims-vgpu dentro do guest e imprime"
            echo "  VHACK_BUILD_TEST_RESULT=PASS|FAIL no stdout, no log e no serial (ttyS0)."
            echo
            echo "  Para pular o gate de Vulkan do appliance (guest sem GPU 1.2+):"
            echo "    VHACK_SKIP_VULKAN_CHECK=1 $(basename "$0")"
            exit 0
            ;;
    esac
done

# ------------------------------------------------------------------------------
# Log: tudo vai para stdout e para o arquivo ao mesmo tempo.
# ------------------------------------------------------------------------------
mkdir -p "$(dirname "$REPORT_LOG")" 2>/dev/null || true
# O redirecionamento é feito DENTRO de um subshell: se /var/log for somente
# leitura, o bash reportaria o erro de redirecionamento no stderr do script
# antes de o fallback rodar, sujando a saída que o host grepa. Aqui a falha é
# contida e só decidimos o caminho final pelo exit code.
if ! ( : > "$REPORT_LOG" ) 2>/dev/null; then
    REPORT_LOG="/dev/null"
fi

# Console serial do guest. O host (verify-iso.sh --build-test) NÃO consegue ler
# o stdout do guest: sem display, a única superfície observável de fora é o
# arquivo de serial do QEMU. Por isso o log espelha aqui.
#
# Detecção preguiçosa (na 1ª chamada) para não custar nada no caso comum:
#   • /dev/ttyS0 existe na ISO (kernel com 8250/earlyprintk) → espelha;
#   • não existe (ou não é gravável) → SERIAL_OK=0 e nunca mais tentamos.
# O teste `[ -w ]` evita escolher um device que existe mas rejeitaria a escrita.
SERIAL_OK=""
SERIAL_TTY="/dev/ttyS0"

log() {
    printf '%s\n' "$*"
    printf '%s\n' "$*" >> "$REPORT_LOG" 2>/dev/null || true

    if [ -z "$SERIAL_OK" ]; then
        if [ -w "$SERIAL_TTY" ]; then
            SERIAL_OK=1
        else
            SERIAL_OK=0
        fi
    fi
    # `|| true` é obrigatório: este script roda sem `set -e`, mas escrever num
    # tty que desapareceu no meio (console desconectado) não pode derrubar a
    # verificação nem sujar o stderr — o host grepa a saída.
    [ "$SERIAL_OK" = "1" ] && printf '%s\n' "$*" >> "$SERIAL_TTY" 2>/dev/null || true
}

section() {
    log ""
    log "─────────────────────────────────────────────────────────────────────"
    log "  $*"
    log "─────────────────────────────────────────────────────────────────────"
}

finish() {
    local verdict="$1" reason="$2"
    log ""
    log "═════════════════════════════════════════════════════════════════════"
    log "  VHACK_BUILD_TEST_RESULT=${verdict}"
    log "  ${reason}"
    log "  Log completo: ${REPORT_LOG}"
    log "═════════════════════════════════════════════════════════════════════"

    # Redundância deliberada: o veredito é a ÚNICA coisa que o host precisa ler.
    # Se SERIAL_OK ficou 0 por o device não ser gravável no momento em que o 1º
    # log rodou (ex.: o tty ainda não existia), reavaliamos AGORA — o console já
    # está de pé há muito tempo quando chegamos aqui. Assim uma detecção
    # pessimista no início não condena a medição inteira.
    if [ "$SERIAL_OK" != "1" ] && [ -w "$SERIAL_TTY" ]; then
        SERIAL_OK=1
    fi
    if [ "$SERIAL_OK" = "1" ]; then
        printf '%s\n' "VHACK_BUILD_TEST_RESULT=${verdict}" >> "$SERIAL_TTY" 2>/dev/null || true
    fi
    # O report file também recebe a linha isolada: se o host um dia montar a ISO
    # e ler esse arquivo em vez do serial, o formato é o mesmo.
    printf '%s\n' "VHACK_BUILD_TEST_RESULT=${verdict}" >> "$REPORT_LOG" 2>/dev/null || true
    sync 2>/dev/null || true

    # O host extrai o veredito do serial (espelhado por log()); o exit code é
    # para uso interativo.
    if [ "$verdict" = "PASS" ]; then
        exit 0
    fi
    exit 1
}

log "═══ vHackintosh in-guest build test ═══"
log "Data: $(date -Is 2>/dev/null || date)"
log "Kernel: $(uname -r 2>/dev/null || echo '?')"
log "cmdline: $(cat /proc/cmdline 2>/dev/null || echo '?')"

# ------------------------------------------------------------------------------
# 1. O ambiente do appliance existe? (PYTHONPATH + módulo de build)
# ------------------------------------------------------------------------------
section "1/5 — Ambiente do appliance"

if [ ! -d "$VHACK_ROOT" ]; then
    finish FAIL "Diretório $VHACK_ROOT ausente na ISO."
fi
log "  • $VHACK_ROOT presente."

PY_BIN=""
for cand in /usr/bin/python3 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        PY_BIN="$(command -v "$cand")"
        break
    fi
done
if [ -z "$PY_BIN" ]; then
    finish FAIL "python3 não encontrado no guest."
fi
log "  • python3: ${PY_BIN} ($("$PY_BIN" --version 2>&1))"

if [ ! -d "$REIMS_DIR" ]; then
    finish FAIL "Árvore de fontes do reims-vgpu ausente: $REIMS_DIR"
fi
log "  • Fontes do reims-vgpu: ${REIMS_DIR}"

# ------------------------------------------------------------------------------
# 2. O gate de Vulkan (informativo; não decide o build por padrão).
# ------------------------------------------------------------------------------
section "2/5 — Gate de Vulkan (informativo)"

VULKAN_RAW=""
VULKAN_OK=1
if VULKAN_RAW="$(PYTHONPATH="$VHACK_ROOT" "$PY_BIN" -m cli.main check-vulkan 2>&1)"; then
    VULKAN_OK=1
else
    VULKAN_OK=0
fi

if [ "$VULKAN_OK" -eq 1 ]; then
    log "  • Vulkan 1.2+: OK"
else
    log "  • Vulkan 1.2+: INDISPONÍVEL neste guest"
    log "$VULKAN_RAW" | sed 's/^/      /'
    # Diagnóstico da causa, não um bypass: o veredito do build NÃO depende disto.
    if [ "${VHACK_SKIP_VULKAN_CHECK:-0}" = "1" ]; then
        log "  • VHACK_SKIP_VULKAN_CHECK=1 estava definido, mas a checagem ainda"
        log "    reportou indisponível — o env var só vale para o gate do appliance."
    fi
    log "  • Aviso: compilar não exige GPU; o build continua e o veredito"
    log "    é sobre a compilação, não sobre aceleração gráfica."
fi

# ------------------------------------------------------------------------------
# 3. Ferramentas de build presentes na ISO?
# ------------------------------------------------------------------------------
section "3/5 — Toolchain"

TOOLCHAIN_FAIL=""
for tool in cargo rustc cc gcc make pkg-config; do
    if command -v "$tool" >/dev/null 2>&1; then
        log "  • ${tool}: $(command -v "$tool")"
    else
        log "  • ${tool}: AUSENTE"
        # cargo/cc são indispensáveis; make/pkg-config entram na lista porque o
        # configure do QEMU os usa — mas quem decide de verdade é o build, então
        # só marcamos cargo/cc como fatais aqui.
        case "$tool" in
            cargo|rustc|cc|gcc) TOOLCHAIN_FAIL="${TOOLCHAIN_FAIL} ${tool}" ;;
        esac
    fi
done

if [ -n "$TOOLCHAIN_FAIL" ]; then
    finish FAIL "Toolchain de compilação incompleta na ISO:${TOOLCHAIN_FAIL}"
fi

# ------------------------------------------------------------------------------
# 4. Avaliação do estado (precisa compilar? já está compilado?)
# ------------------------------------------------------------------------------
section "4/5 — Estado do build"

EVAL_OUT=""
if ! EVAL_OUT="$(PYTHONPATH="$VHACK_ROOT" "$PY_BIN" - <<'PYEOF' 2>&1
from core.reims_build import evaluate
state = evaluate(explicit="/opt/reims-vgpu")
print(f"reims_dir={state.reims_dir}")
print(f"sources_present={state.sources_present}")
print(f"needs_build={state.needs_build}")
print(f"reason={getattr(state, 'reason', '')}")
print(f"binary_path={getattr(state, 'binary_path', None)}")
PYEOF
)"; then
    log "$EVAL_OUT" | sed 's/^/      /'
    finish FAIL "core.reims_build.evaluate() falhou (veja o log acima)."
fi
log "$EVAL_OUT" | sed 's/^/  /'

# ------------------------------------------------------------------------------
# 5. O BUILD. Aqui é o teste de verdade.
# ------------------------------------------------------------------------------
section "5/5 — Compilando reims-vgpu (pode levar muitos minutos)"

START_TS="$(date +%s)"
BUILD_RC=0
PYTHONPATH="$VHACK_ROOT" "$PY_BIN" - <<'PYEOF' 2>&1 | tee -a "$REPORT_LOG"
import sys, time
from core.reims_build import build

started = time.time()

def on_start(name, expected):
    print(f"[build] >>> {name} (~{expected:.0f}s esperados)", flush=True)

def on_done(name, ok):
    print(f"[build] {'OK  ' if ok else 'FALHOU'} {name}", flush=True)

def on_progress(pct):
    print(f"[build] ... {pct:5.1f}%", flush=True)

try:
    ok = build("/opt/reims-vgpu", on_start=on_start, on_done=on_done, progress=on_progress)
except Exception as exc:
    print(f"[build] EXCEÇÃO: {exc!r}", flush=True)
    ok = False

print(f"[build] duração: {time.time() - started:.1f}s", flush=True)
sys.exit(0 if ok else 1)
PYEOF
BUILD_RC="${PIPESTATUS[0]}"

END_TS="$(date +%s)"
DURATION="$((END_TS - START_TS))"

if [ "$BUILD_RC" -ne 0 ]; then
    section "Falha no build — últimas linhas dos logs do motor"
    for log_name in build-qemu-reims.log build-rom.log; do
        snippet="$(PYTHONPATH="$VHACK_ROOT" "$PY_BIN" -c "
from core.reims_build import tail_log
print(tail_log('${log_name}', lines=25))
" 2>/dev/null || true)"
        if [ -n "$snippet" ]; then
            log "  ── ${log_name} ──"
            log "$snippet"
        fi
    done
    log "  (logs completos ficam em /var/log/vhackintosh/ dentro do guest)"
    finish FAIL "O build do reims-vgpu FALHOU (rc=${BUILD_RC}, ${DURATION}s)."
fi

# ------------------------------------------------------------------------------
# Verificação positiva: o artefato existe mesmo?
# ------------------------------------------------------------------------------
section "Verificação do artefato"

ARTIFACT_OUT="$(PYTHONPATH="$VHACK_ROOT" "$PY_BIN" - <<'PYEOF' 2>&1
from core.reims_build import evaluate
state = evaluate(explicit="/opt/reims-vgpu")
path = getattr(state, "binary_path", None)
print(f"binary_path={path}")
if path is not None:
    import os
    p = str(path)
    print(f"exists={os.path.exists(p)}")
    if os.path.exists(p):
        print(f"size={os.path.getsize(p)}")
        print(f"executable={os.access(p, os.X_OK)}")
PYEOF
)" || true
log "$ARTIFACT_OUT" | sed 's/^/  /'

if ! printf '%s' "$ARTIFACT_OUT" | grep -q '^exists=True'; then
    finish FAIL "build() retornou sucesso mas nenhum binário foi encontrado — resultado não confiável."
fi
if ! printf '%s' "$ARTIFACT_OUT" | grep -q '^executable=True'; then
    finish FAIL "Binário existe mas não é executável (bits de permissão perdidos?) — resultado não confiável."
fi

finish PASS "reims-vgpu compilou na ISO e o binário existe e é executável (${DURATION}s)."
