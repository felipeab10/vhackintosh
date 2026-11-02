#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:?usage: build-package.sh VERSION PAYLOAD_DIR OUTPUT_DIR}"
PAYLOAD="${2:?usage: build-package.sh VERSION PAYLOAD_DIR OUTPUT_DIR}"
OUT="${3:?usage: build-package.sh VERSION PAYLOAD_DIR OUTPUT_DIR}"

command -v makepkg >/dev/null || { echo "missing build dependency: makepkg" >&2; exit 1; }
command -v runuser >/dev/null || { echo "missing build dependency: runuser" >&2; exit 1; }
WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT
install -m 0644 "$(dirname "${BASH_SOURCE[0]}")/PKGBUILD" "${WORK}/PKGBUILD"
install -m 0644 "$(dirname "${BASH_SOURCE[0]}")/vhackintosh-control.install" "${WORK}/vhackintosh-control.install"
install -d -m 0755 "$OUT"

if ! getent passwd vhackpkg >/dev/null; then
  useradd --create-home vhackpkg
fi
chown -R vhackpkg:vhackpkg "$WORK"
SAFE_VERSION="${VERSION//-/.}"
runuser -u vhackpkg -- env \
  VHACK_CONTROL_STAGE="$PAYLOAD" \
  VHACK_CONTROL_PKGVER="$SAFE_VERSION" \
  makepkg --nodeps --noconfirm --force --clean --dir "$WORK"
PACKAGE="$(find "$WORK" -maxdepth 1 -type f -name 'vhackintosh-control-*-x86_64.pkg.tar.zst' -print -quit)"
test -n "$PACKAGE" || { echo "makepkg did not create the expected x86_64 package" >&2; exit 1; }
install -d "$OUT"
install -m 0644 "$PACKAGE" "$OUT/"
