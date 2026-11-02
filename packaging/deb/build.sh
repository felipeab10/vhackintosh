#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="${1:-1.0.0}"
OUT="${2:-${ROOT}/dist}"
WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT

for tool in npm uv dpkg-deb rsync python3; do
  command -v "$tool" >/dev/null || { echo "missing build dependency: $tool" >&2; exit 1; }
done

PKG="${WORK}/vhackintosh-control"
APP="${PKG}/opt/vhackintosh/web/current"
FRONTEND="${WORK}/frontend"
mkdir -p "${APP}/core" "${APP}/web/api" "${APP}/web/frontend" "${APP}/vendor" "${PKG}/DEBIAN" "${FRONTEND}"

rsync -a --delete \
  --exclude=node_modules/ --exclude=dist/ --exclude=.git/ --exclude=.env \
  --exclude='.env.*' --exclude=.npmrc \
  "${ROOT}/web/frontend/" "${FRONTEND}/"
npm ci --prefix "${FRONTEND}" --ignore-scripts --no-audit --no-fund
npm run build --prefix "${FRONTEND}"

rsync -a --exclude=__pycache__/ --exclude='*.pyc' "${ROOT}/core/" "${APP}/core/"
rsync -a --exclude=__pycache__/ --exclude='*.pyc' "${ROOT}/web/api/" "${APP}/web/api/"
rsync -a "${FRONTEND}/dist/" "${APP}/web/frontend/dist/"
install -m 0644 "${ROOT}/web/frontend/THIRD_PARTY_NOTICES.md" "${APP}/web/frontend/THIRD_PARTY_NOTICES.md"
install -m 0644 "${ROOT}/web/requirements.lock" "${APP}/requirements.lock"
uv pip install --python "$(command -v python3)" --target "${APP}/vendor" \
  --require-hashes --link-mode=copy --python-platform x86_64-unknown-linux-gnu \
  -r "${ROOT}/web/requirements.lock"

install -m 0644 "${ROOT}/packaging/deb/control" "${PKG}/DEBIAN/control"
sed -i "s/^Version: .*/Version: ${VERSION}/" "${PKG}/DEBIAN/control"
install -m 0755 "${ROOT}/packaging/deb/postinst" "${PKG}/DEBIAN/postinst"

install -d "${PKG}/etc/systemd/system" "${PKG}/etc/vhackintosh" \
  "${PKG}/usr/local/bin" "${PKG}/usr/lib/sysusers.d" "${PKG}/usr/lib/tmpfiles.d"
for unit in vhackintosh-web.service vhackintosh-agent.service vhackintosh-display.service vhackintosh-vm@.service; do
  install -m 0644 "${ROOT}/appliance/archiso/airootfs/etc/systemd/system/${unit}" "${PKG}/etc/systemd/system/${unit}"
done
install -m 0644 "${ROOT}/appliance/archiso/airootfs/etc/vhackintosh/sway.conf" "${PKG}/etc/vhackintosh/sway.conf"
install -m 0644 "${ROOT}/appliance/archiso/airootfs/usr/lib/sysusers.d/vhackintosh-web.conf" "${PKG}/usr/lib/sysusers.d/vhackintosh-web.conf"
install -m 0644 "${ROOT}/appliance/archiso/airootfs/usr/lib/tmpfiles.d/vhackintosh-web.conf" "${PKG}/usr/lib/tmpfiles.d/vhackintosh-web.conf"
for helper in vhackintosh-web-bootstrap vhackintosh-waysession vhackintosh-wait-wayland; do
  install -m 0755 "${ROOT}/appliance/archiso/airootfs/usr/local/bin/${helper}" "${PKG}/usr/local/bin/${helper}"
done

if find "${PKG}" -type f \( -name '.env' -o -name '.npmrc' -o -name '*.key' -o -name '*.crt' \) -print -quit | grep -q .; then
  echo "refusing to package secrets or developer configuration" >&2
  exit 1
fi
if find "${PKG}" -type d -name node_modules -print -quit | grep -q .; then
  echo "refusing to package node_modules" >&2
  exit 1
fi

mkdir -p "${OUT}"
dpkg-deb --root-owner-group --build "${PKG}" "${OUT}/vhackintosh-control_${VERSION}_amd64.deb"
sha256sum "${OUT}/vhackintosh-control_${VERSION}_amd64.deb" > "${OUT}/SHA256SUMS"
echo "Built ${OUT}/vhackintosh-control_${VERSION}_amd64.deb"
