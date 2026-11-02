#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="${1:?usage: build-package.sh VERSION PAYLOAD_DIR OUTPUT_DIR}"
PAYLOAD="${2:?usage: build-package.sh VERSION PAYLOAD_DIR OUTPUT_DIR}"
OUT="${3:?usage: build-package.sh VERSION PAYLOAD_DIR OUTPUT_DIR}"

command -v rpmbuild >/dev/null || { echo "missing build dependency: rpmbuild" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT
mkdir -p "${WORK}/BUILD" "${WORK}/BUILDROOT" "${WORK}/RPMS" "${WORK}/SOURCES" "${WORK}/SPECS" "${WORK}/SRPMS"
cp "${ROOT}/packaging/fedora/vhackintosh-control.spec" "${WORK}/SPECS/vhackintosh-control.spec"
SAFE_VERSION="${VERSION//-/.}"

rpmbuild -bb "${WORK}/SPECS/vhackintosh-control.spec" \
  --define "_topdir ${WORK}" \
  --define "pkg_version ${SAFE_VERSION}" \
  --define "payload_dir ${PAYLOAD}"

mkdir -p "$OUT"
install -m 0644 "${WORK}/RPMS/x86_64/vhackintosh-control-${SAFE_VERSION}-1"*.rpm "$OUT/"
