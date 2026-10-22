#!/usr/bin/env bash
# Atualiza reims-util para o HEAD exato da branch oficial escolhida.
set -euo pipefail

REPO_URL="https://github.com/steelbrain/reims-util.git"
REF="${REIMS_UTIL_REF:-master}"
DEST="${REIMS_UTIL_BIN:-}"
REFRESH_OPENCORE=0

usage() {
    echo "Uso: $0 [--ref BRANCH|TAG] [--refresh-opencore]"
    echo "Atualiza reims-util do repositório oficial, compila em diretório temporário"
    echo "e troca o binário instalado somente depois de validá-lo."
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref)
            [[ $# -ge 2 ]] || { usage >&2; exit 2; }
            REF="$2"
            shift 2
            ;;
        --refresh-opencore)
            REFRESH_OPENCORE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Opção inválida: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -z "$DEST" ]]; then
    if [[ -n "${CARGO_HOME:-}" ]]; then
        DEST="${CARGO_HOME}/bin/reims-util"
    elif [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
        SUDO_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
        DEST="${SUDO_HOME:-/home/${SUDO_USER}}/.cargo/bin/reims-util"
    else
        DEST="${HOME}/.cargo/bin/reims-util"
    fi
fi

if ! command -v cargo >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then
    echo "Erro: instale Rust/Cargo e git antes de atualizar reims-util." >&2
    exit 1
fi

REMOTE_REFS="$(git ls-remote --exit-code "$REPO_URL" \
    "refs/heads/${REF}" "refs/tags/${REF}" "refs/tags/${REF}^{}")"
COMMIT="$(awk -v branch="refs/heads/${REF}" -v tag="refs/tags/${REF}" -v peeled="refs/tags/${REF}^{}" '
    $2 == branch { branch_sha = $1 }
    $2 == tag { tag_sha = $1 }
    $2 == peeled { peeled_sha = $1 }
    END {
        if (branch_sha) print branch_sha
        else if (peeled_sha) print peeled_sha
        else if (tag_sha) print tag_sha
    }
' <<< "$REMOTE_REFS")"
if [[ ! "$COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Erro: não foi possível resolver '$REF' no repositório oficial." >&2
    exit 1
fi

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/vhackintosh-reims-util.XXXXXX")"
STAGED_BIN="${TMP_ROOT}/install/bin/reims-util"
DEST_DIR="$(dirname "$DEST")"
mkdir -p "$DEST_DIR"
cleanup() {
    rm -rf -- "$TMP_ROOT"
    [[ -z "${STAGED_DEST:-}" ]] || rm -f -- "$STAGED_DEST"
    [[ -z "${STAGED_REVISION:-}" ]] || rm -f -- "$STAGED_REVISION"
}
trap cleanup EXIT

echo "Compilando steelbrain/reims-util em ${COMMIT:0:12}..."
cargo install --locked --git "$REPO_URL" --rev "$COMMIT" --root "${TMP_ROOT}/install" --force

if [[ ! -x "$STAGED_BIN" ]] || ! "$STAGED_BIN" --version >/dev/null 2>&1 || ! "$STAGED_BIN" opencore --help | grep -q -- '--no-ovmf'; then
    echo "Erro: o binário compilado não passou na validação." >&2
    exit 1
fi

STAGED_DEST="$(mktemp "${DEST}.new.XXXXXX")"
STAGED_REVISION="$(mktemp "${DEST}.commit.new.XXXXXX")"
install -m 0755 "$STAGED_BIN" "$STAGED_DEST"
printf '%s\n' "$COMMIT" > "$STAGED_REVISION"

if [[ -e "$DEST" && "$(id -u)" -eq 0 ]]; then
    chown --reference="$DEST" "$STAGED_DEST"
elif [[ -n "${SUDO_USER:-}" && "$(id -u)" -eq 0 ]]; then
    SUDO_GROUP="$(id -gn "$SUDO_USER")"
    chown "${SUDO_USER}:${SUDO_GROUP}" "$STAGED_DEST" "$STAGED_REVISION"
fi

mv -f -- "$STAGED_DEST" "$DEST"
mv -f -- "$STAGED_REVISION" "${DEST}.commit"
echo "Atualizado: $DEST"
echo "Commit oficial: $COMMIT"

if [[ "$REFRESH_OPENCORE" -eq 1 ]]; then
    APP_ROOT="/opt/vhackintosh"
    BUILDER="${APP_ROOT}/appliance/build_opencore.py"
    if [[ ! -f "$BUILDER" ]]; then
        echo "Erro: não encontrei o gerador OpenCore instalado em $BUILDER." >&2
        exit 1
    fi
    python3 "$BUILDER" \
        --reims-util "$DEST" \
        --output "${APP_ROOT}/templates/OpenCore.qcow2" \
        --config-output "${APP_ROOT}/templates/config.plist"
fi
