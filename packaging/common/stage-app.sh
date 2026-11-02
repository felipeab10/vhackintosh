#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DESTDIR="${1:?usage: stage-app.sh DESTDIR}"

for tool in npm rsync; do
  command -v "$tool" >/dev/null || { echo "missing build dependency: $tool" >&2; exit 1; }
done

WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT
FRONTEND="${WORK}/frontend"
APP="${DESTDIR}/opt/vhackintosh/web/current"
mkdir -p "$FRONTEND" "${APP}/core" "${APP}/web/api" "${APP}/web/frontend" "${APP}/vendor"

rsync -a --delete \
  --exclude=node_modules/ --exclude=dist/ --exclude=.git/ --exclude=.env \
  --exclude='.env.*' --exclude=.npmrc \
  "${ROOT}/web/frontend/" "${FRONTEND}/"
npm ci --prefix "$FRONTEND" --ignore-scripts --no-audit --no-fund
npm run build --prefix "$FRONTEND"

rsync -a --exclude=__pycache__/ --exclude='*.pyc' "${ROOT}/core/" "${APP}/core/"
rsync -a --exclude=__pycache__/ --exclude='*.pyc' "${ROOT}/web/api/" "${APP}/web/api/"
rsync -a "${FRONTEND}/dist/" "${APP}/web/frontend/dist/"
install -m 0644 "${ROOT}/web/frontend/THIRD_PARTY_NOTICES.md" "${APP}/web/frontend/THIRD_PARTY_NOTICES.md"
install -m 0644 "${ROOT}/web/requirements.lock" "${APP}/requirements.lock"

install -d "${DESTDIR}/usr/lib/systemd/system" "${DESTDIR}/etc/vhackintosh" \
  "${DESTDIR}/usr/local/bin" "${DESTDIR}/usr/lib/sysusers.d" "${DESTDIR}/usr/lib/tmpfiles.d"
for unit in vhackintosh-web.service vhackintosh-agent.service vhackintosh-display.service vhackintosh-vm@.service; do
  install -m 0644 "${ROOT}/appliance/archiso/airootfs/etc/systemd/system/${unit}" "${DESTDIR}/usr/lib/systemd/system/${unit}"
done
install -m 0644 "${ROOT}/appliance/archiso/airootfs/etc/vhackintosh/sway.conf" "${DESTDIR}/etc/vhackintosh/sway.conf"
install -m 0644 "${ROOT}/appliance/archiso/airootfs/usr/lib/sysusers.d/vhackintosh-web.conf" "${DESTDIR}/usr/lib/sysusers.d/vhackintosh-web.conf"
install -m 0644 "${ROOT}/appliance/archiso/airootfs/usr/lib/tmpfiles.d/vhackintosh-web.conf" "${DESTDIR}/usr/lib/tmpfiles.d/vhackintosh-web.conf"
for helper in vhackintosh-web-bootstrap vhackintosh-waysession vhackintosh-wait-wayland; do
  install -m 0755 "${ROOT}/appliance/archiso/airootfs/usr/local/bin/${helper}" "${DESTDIR}/usr/local/bin/${helper}"
done

if find "$DESTDIR" -type f \( -name '.env' -o -name '.npmrc' -o -name '*.key' -o -name '*.crt' \) -print -quit | grep -q .; then
  echo "refusing to package secrets or developer configuration" >&2
  exit 1
fi
if find "$DESTDIR" -type d -name node_modules -print -quit | grep -q .; then
  echo "refusing to package node_modules" >&2
  exit 1
fi
