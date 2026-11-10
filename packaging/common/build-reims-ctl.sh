#!/usr/bin/env bash
set -euo pipefail

# Empacota o MOTOR (`reims-ctl`) do pacote do Control a partir da MESMA release
# do reims-vgpu que a ISO embarca. Não compilamos o motor aqui: o fork o publica
# pré-compilado em `bin/reims-ctl`, junto do `bin/reims-util` e do QEMU, com o
# `SOURCE-PROVENANCE.txt` da própria release. Compilar de novo criaria uma
# segunda origem para o mesmo par — exatamente o defeito que este arquivo fecha.
#
# A release/tag vem do pin único (packaging/reims-util.pin, chaves CTL_*),
# lido por `reims-util-pin.sh`. Sem o pin o script aborta; não há tag default.

OUT="${1:?usage: build-reims-ctl.sh OUTPUT_DIR}"
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reims-util-pin.sh"
reims_util_pin_load || exit 1

: "${REIMS_CTL_REPO:?pin has no CTL_REPO}"
: "${REIMS_CTL_RELEASE:?pin has no CTL_RELEASE}"
: "${REIMS_CTL_ASSET:?pin has no CTL_ASSET}"
: "${REIMS_CTL_BUNDLE_REL:?pin has no CTL_BUNDLE_REL}"

WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT

command -v curl >/dev/null || { echo "missing build dependency: curl" >&2; exit 1; }
command -v tar >/dev/null || { echo "missing build dependency: tar" >&2; exit 1; }

URL="https://github.com/${REIMS_CTL_REPO}/releases/download/${REIMS_CTL_RELEASE}/${REIMS_CTL_ASSET}"
TAR="${WORK}/${REIMS_CTL_ASSET}"
echo "reims-ctl: baixando ${REIMS_CTL_RELEASE} de ${REIMS_CTL_REPO}"
curl -fL --silent --show-error -o "${TAR}" "${URL}" \
  || { echo "não consegui baixar o motor de ${URL}" >&2; exit 1; }

# Só o motor e a proveniência: o resto do tarball (QEMU, ROMs) é do appliance.
if ! tar -xzf "${TAR}" -C "${WORK}" -- "./${REIMS_CTL_BUNDLE_REL}" ./SOURCE-PROVENANCE.txt; then
  echo "o tarball ${REIMS_CTL_ASSET} da release ${REIMS_CTL_RELEASE} não traz ${REIMS_CTL_BUNDLE_REL}" >&2
  exit 1
fi

BIN="${WORK}/${REIMS_CTL_BUNDLE_REL}"
test -f "${BIN}" || { echo "release ${REIMS_CTL_RELEASE} não trouxe ${REIMS_CTL_BUNDLE_REL}" >&2; exit 1; }
chmod 0755 "${BIN}"

# O motor tem de repassar `--add-kext` ao util. Um `reims-ctl` anterior ao
# suporte da flag não serve, mesmo que se chame `reims-ctl`: foi um motor desses
# que descasou o par e derrubou a tarefa reims.release.update.
if ! LC_ALL=C strings "${BIN}" | LC_ALL=C grep -a -- '--add-kext' >/dev/null; then
  echo "o reims-ctl da release ${REIMS_CTL_RELEASE} não conhece --add-kext; par recusado" >&2
  exit 1
fi

PROV="${WORK}/SOURCE-PROVENANCE.txt"
if [ ! -f "${PROV}" ]; then
  echo "a release ${REIMS_CTL_RELEASE} não trouxe SOURCE-PROVENANCE.txt; procedência desconhecida" >&2
  exit 1
fi
# O commit do reims-vgpu vai no cabeçalho `commit=`; exigir que exista evita
# gravar uma proveniência vazia em silêncio.
CTL_COMMIT="$(sed -n 's/^commit=//p' "${PROV}" | head -n1)"
[ -n "${CTL_COMMIT}" ] || { echo "SOURCE-PROVENANCE.txt sem commit=; procedência desconhecida" >&2; exit 1; }

install -d -m 0755 "$OUT"
install -m 0755 "${BIN}" "$OUT/reims-ctl"
cat > "$OUT/PROVENANCE.txt" <<EOF
Project: reims-ctl
Repository: https://github.com/${REIMS_CTL_REPO}
Release: ${REIMS_CTL_RELEASE}
Asset: ${REIMS_CTL_ASSET}
Commit: ${CTL_COMMIT}
Bundle path: ${REIMS_CTL_BUNDLE_REL}
The motor is the prebuilt binary published in the release above; it is the exact
counterpart of the reims-util and QEMU of the same release.
EOF
