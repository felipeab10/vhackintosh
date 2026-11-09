#!/usr/bin/env bash
# Build the native Arch Control package and install its payload into the ISO.
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: package_web_manager.sh PROJECT_ROOT ROOTFS BUILD_WORK" >&2
    exit 2
fi

PROJECT_ROOT="$(cd "$1" && pwd)"
ROOTFS="$2"
BUILD_WORK="$3"

for tool in npm uv python3 rsync makepkg bsdtar; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "missing Control package build dependency: ${tool}" >&2
        exit 1
    }
done

CONTROL_VERSION="${VHACKINTOSH_CONTROL_VERSION:-$(git -C "${PROJECT_ROOT}" describe --tags --match 'control-v*' --abbrev=0 2>/dev/null | sed 's/^control-v//' || true)}"
CONTROL_VERSION="${CONTROL_VERSION:-1.0.0}"
PAYLOAD="${BUILD_WORK}/control-payload"
PACKAGE_OUT="${BUILD_WORK}/control-package"
mkdir -p "${PAYLOAD}" "${PACKAGE_OUT}"

"${PROJECT_ROOT}/packaging/common/stage-app.sh" "${PAYLOAD}" "${CONTROL_VERSION}"
"${PROJECT_ROOT}/packaging/common/install-python-deps.sh" \
    "${PAYLOAD}/opt/vhackintosh/web/current" \
    "${PAYLOAD}/opt/vhackintosh/web/current/requirements.lock"
bash "${PROJECT_ROOT}/packaging/common/normalize-tree-permissions.sh" \
    "$(readlink -f "${PAYLOAD}/opt/vhackintosh/web/current")"
PYTHONPATH="${PROJECT_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" python3 \
    "${PROJECT_ROOT}/packaging/common/refresh-control-bundle.py" "${PAYLOAD}"
"${PROJECT_ROOT}/packaging/arch/build-package.sh" \
    "${CONTROL_VERSION}" "${PAYLOAD}" "${PACKAGE_OUT}"

PACKAGE="$(find "${PACKAGE_OUT}" -maxdepth 1 -type f -name 'vhackintosh-control-*-x86_64.pkg.tar.zst' -print -quit)"
if [ -z "${PACKAGE}" ]; then
    echo "Arch package builder did not produce the Control package" >&2
    exit 1
fi
bsdtar -xf "${PACKAGE}" -C "${ROOTFS}" \
    --exclude='.PKGINFO' --exclude='.BUILDINFO' --exclude='.MTREE' --exclude='.INSTALL'
PYTHONPATH="${PROJECT_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" python3 - "${ROOTFS}" <<'PY'
import sys
from pathlib import Path

from core.reims_build import prebuilt_release
from core.web_bundle import bind_current_bundle_runtime

rootfs = Path(sys.argv[1])
runtime = prebuilt_release(rootfs / "opt/reims-vgpu")
if not runtime:
    raise SystemExit("Control package cannot be staged without a tagged Reims runtime")
bind_current_bundle_runtime(rootfs / "opt/vhackintosh/web", runtime_release=runtime)
PY
echo "Installed the shared vhackintosh-control ${CONTROL_VERSION} Arch package payload into the appliance rootfs."
