#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="${1:?usage: build-package.sh VERSION PAYLOAD_DIR OUTPUT_DIR}"
PAYLOAD="${2:?usage: build-package.sh VERSION PAYLOAD_DIR OUTPUT_DIR}"
OUT="${3:?usage: build-package.sh VERSION PAYLOAD_DIR OUTPUT_DIR}"

command -v dpkg-deb >/dev/null || { echo "missing build dependency: dpkg-deb" >&2; exit 1; }
PKG="$(mktemp -d)"
trap 'rm -rf -- "$PKG"' EXIT
cp -a "${PAYLOAD}/." "$PKG/"
install -d "${PKG}/DEBIAN"
install -m 0644 "${ROOT}/packaging/deb/control" "${PKG}/DEBIAN/control"
sed -i "s/^Version: .*/Version: ${VERSION}/" "${PKG}/DEBIAN/control"
install -m 0755 "${ROOT}/packaging/deb/postinst" "${PKG}/DEBIAN/postinst"

mkdir -p "$OUT"
dpkg-deb --root-owner-group --build "$PKG" "${OUT}/vhackintosh-control_${VERSION}_amd64.deb"
