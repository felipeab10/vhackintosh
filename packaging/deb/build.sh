#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="${1:-1.0.0}"
OUT="${2:-${ROOT}/dist}"
WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT

"${ROOT}/packaging/common/stage-app.sh" "${WORK}/payload"
"${ROOT}/packaging/deb/build-package.sh" "$VERSION" "${WORK}/payload" "$OUT"
sha256sum "${OUT}/vhackintosh-control_${VERSION}_amd64.deb" > "${OUT}/SHA256SUMS"
echo "Built ${OUT}/vhackintosh-control_${VERSION}_amd64.deb"
