#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DESTDIR="${1:?usage: stage-app.sh DESTDIR}"
VERSION="${2:-${VHACK_CONTROL_VERSION:-1.0.0}}"

for tool in npm rsync sha256sum cut; do
  command -v "$tool" >/dev/null || { echo "missing build dependency: $tool" >&2; exit 1; }
done

WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT
FRONTEND="${WORK}/frontend"
APP="${WORK}/control-bundle"
mkdir -p "$FRONTEND" "${APP}/core" "${APP}/web/api" "${APP}/web/frontend" "${APP}/vendor" "${APP}/templates" "${APP}/resources"

rsync -a --delete \
  --exclude=node_modules/ --exclude=dist/ --exclude=.git/ --exclude=.env \
  --exclude='.env.*' --exclude=.npmrc \
  "${ROOT}/web/frontend/" "${FRONTEND}/"
npm ci --prefix "$FRONTEND" --ignore-scripts --no-audit --no-fund
npm run build --prefix "$FRONTEND"

rsync -a --exclude=__pycache__/ --exclude='*.pyc' "${ROOT}/core/" "${APP}/core/"
rsync -a --exclude=__pycache__/ --exclude='*.pyc' "${ROOT}/web/api/" "${APP}/web/api/"
# Os kexts de guest do reims-util novo (`--add-kext`) vivem ao lado do `core/`
# empacotado: `bundles/<id>/core/reims_util.py` sobe dois niveis e resolve
# `bundles/<id>/resources/reims-kexts`. Sem esta copia o pacote instala e o
# provisionamento falha em silencio ao gerar a EFI.
rsync -a --delete "${ROOT}/resources/reims-kexts/" "${APP}/resources/reims-kexts/"
install -m 0644 "${ROOT}/templates/OVMF_VARS.fd" "${APP}/templates/OVMF_VARS.fd"
install -m 0644 "${ROOT}/templates/OVMF_CODE_4M.fd" "${APP}/templates/OVMF_CODE_4M.fd"
rsync -a "${FRONTEND}/dist/" "${APP}/web/frontend/dist/"
install -m 0644 "${ROOT}/web/frontend/THIRD_PARTY_NOTICES.md" "${APP}/web/frontend/THIRD_PARTY_NOTICES.md"
install -m 0644 "${ROOT}/web/requirements.lock" "${APP}/requirements.lock"

# The appliance build may run under sudo while the source tree contains files
# with mode 0600 (for example, after copying files from a restrictive umask).
# makepkg later reads this payload as an unprivileged builder, so normalize the
# staged bundle before copying it into DESTDIR.
bash "${ROOT}/packaging/common/normalize-tree-permissions.sh" "${APP}"

WEB_ROOT="${DESTDIR}/opt/vhackintosh/web"
install -d "${WEB_ROOT}/bundles"
cp -a "${APP}" "${WEB_ROOT}/bundles/.control-staging"
ln -s "bundles/.control-staging" "${WEB_ROOT}/current"

MACSERIAL_DIR="${DESTDIR}/opt/vhackintosh/resources/macserial"
mkdir -p "$MACSERIAL_DIR"
install -m 0755 "${ROOT}/resources/macserial/macserial" "${MACSERIAL_DIR}/macserial"
install -m 0644 "${ROOT}/resources/macserial/LICENSE.txt" "${MACSERIAL_DIR}/LICENSE.txt"
install -m 0644 "${ROOT}/resources/macserial/README.md" "${MACSERIAL_DIR}/README.md"
MACSERIAL_SHA256="$(sha256sum "${MACSERIAL_DIR}/macserial" | cut -d ' ' -f 1)"
[[ "$MACSERIAL_SHA256" == "b0a4a3581c0f06b3ddce915165eb3a1726acfa876192fe9ee5ef9ea08a469ad8" ]] || {
  echo "macserial binary does not match the pinned OpenCorePkg 1.0.8 tool" >&2
  exit 1
}
MACSERIAL_VERSION="$("${MACSERIAL_DIR}/macserial" -v 2>&1)"
[[ "$MACSERIAL_VERSION" == *"2.1.8"* ]] || {
  echo "unexpected macserial version: ${MACSERIAL_VERSION}" >&2
  exit 1
}
install -d "${DESTDIR}/usr/lib/systemd/system" "${DESTDIR}/etc/vhackintosh" \
  "${DESTDIR}/usr/local/bin" "${DESTDIR}/usr/lib/sysusers.d" "${DESTDIR}/usr/lib/tmpfiles.d" \
  "${DESTDIR}/usr/lib/vhackintosh"
printf '%s\n' "$VERSION" > "${DESTDIR}/usr/lib/vhackintosh/control-version"
install -m 0644 "${ROOT}/packaging/systemd/vhackintosh-http-redirect.service" \
  "${DESTDIR}/usr/lib/systemd/system/vhackintosh-http-redirect.service"
for unit in vhackintosh-web.service vhackintosh-agent.service vhackintosh-display.service; do
  install -m 0644 "${ROOT}/appliance/archiso/airootfs/etc/systemd/system/${unit}" "${DESTDIR}/usr/lib/systemd/system/${unit}"
done
install -m 0644 "${ROOT}/packaging/systemd/vhackintosh-vm@.service" \
  "${DESTDIR}/usr/lib/systemd/system/vhackintosh-vm@.service"
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
