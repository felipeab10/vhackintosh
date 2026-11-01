#!/usr/bin/env bash
# Build the web UI and install its hash-locked Python runtime into the ISO tree.
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: package_web_manager.sh PROJECT_ROOT ROOTFS BUILD_WORK" >&2
    exit 2
fi

PROJECT_ROOT="$(cd "$1" && pwd)"
ROOTFS="$2"
BUILD_WORK="$3"
FRONTEND_SOURCE="${PROJECT_ROOT}/web/frontend"
FRONTEND_WORK="${BUILD_WORK}/web-frontend"
WEB_ROOT="${ROOTFS}/opt/vhackintosh/web"
BUNDLE_WORK="${BUILD_WORK}/web-bundle"
BUNDLE_VERSIONS="${WEB_ROOT}/bundles"

for tool in npm uv python3 rsync; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "missing web-manager build dependency: ${tool}" >&2
        exit 1
    fi
done

if [ ! -f "${FRONTEND_SOURCE}/package-lock.json" ]; then
    echo "frontend package-lock.json is required for reproducible packaging" >&2
    exit 1
fi

mkdir -p "${FRONTEND_WORK}"
rm -rf -- "${BUNDLE_WORK}"
mkdir -p "${BUNDLE_WORK}"
# Never include developer-installed modules, build outputs, env files, or npm
# credentials/configuration in the appliance payload.
rsync -a --delete \
    --exclude=node_modules/ \
    --exclude=dist/ \
    --exclude=.git/ \
    --exclude=.env \
    --exclude='.env.*' \
    --exclude=.npmrc \
    "${FRONTEND_SOURCE}/" "${FRONTEND_WORK}/"

npm ci --prefix "${FRONTEND_WORK}" --ignore-scripts --no-audit --no-fund
npm run build --prefix "${FRONTEND_WORK}"

mkdir -p "${BUNDLE_WORK}/core" "${BUNDLE_WORK}/web/api" "${BUNDLE_WORK}/web/frontend/dist" "${BUNDLE_WORK}/vendor"
rsync -a --delete \
    --exclude=__pycache__/ \
    --exclude='*.pyc' \
    "${PROJECT_ROOT}/core/" "${BUNDLE_WORK}/core/"
rsync -a --delete \
    --exclude=__pycache__/ \
    --exclude='*.pyc' \
    "${PROJECT_ROOT}/web/api/" "${BUNDLE_WORK}/web/api/"
rsync -a --delete "${FRONTEND_WORK}/dist/" "${BUNDLE_WORK}/web/frontend/dist/"
install -m 0644 "${FRONTEND_SOURCE}/THIRD_PARTY_NOTICES.md" \
    "${BUNDLE_WORK}/web/frontend/THIRD_PARTY_NOTICES.md"
mkdir -p "${BUNDLE_WORK}/web/frontend/licenses"
install -m 0644 "${FRONTEND_WORK}/node_modules/@fontsource/inter/LICENSE" \
    "${BUNDLE_WORK}/web/frontend/licenses/inter-OFL-1.1.txt"
install -m 0644 "${FRONTEND_WORK}/node_modules/@fontsource/fira-code/LICENSE" \
    "${BUNDLE_WORK}/web/frontend/licenses/fira-code-OFL-1.1.txt"
install -m 0644 "${FRONTEND_WORK}/node_modules/lucide-react/LICENSE" \
    "${BUNDLE_WORK}/web/frontend/licenses/lucide-ISC.txt"

# Build against the same system interpreter used by the Arch appliance and
# require every pinned artifact hash. The isolated target avoids modifying the
# build host's Python environment and keeps the ISO independent of pip at boot.
uv pip install \
    --python "$(command -v python3)" \
    --target "${BUNDLE_WORK}/vendor" \
    --require-hashes \
    --link-mode=copy \
    --python-platform x86_64-unknown-linux-gnu \
    -r "${PROJECT_ROOT}/web/requirements.lock"

install -m 0644 "${PROJECT_ROOT}/web/requirements.lock" "${BUNDLE_WORK}/requirements.lock"
if find "${BUNDLE_WORK}" -type f \( -name '.env' -o -name '.npmrc' -o -name '*.key' -o -name '*.crt' \) -print -quit | grep -q .; then
    echo "refusing to package secrets or developer configuration in web payload" >&2
    exit 1
fi
if find "${BUNDLE_WORK}" -type d -name node_modules -print -quit | grep -q .; then
    echo "refusing to package node_modules in web payload" >&2
    exit 1
fi

# Publish the complete API/agent/schema/runtime contract as one immutable
# bundle, then switch `web/current` in one same-filesystem rename. Existing
# bundle data is retained so a failed boot can be rolled back by repointing it.
PYTHONPATH="${PROJECT_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" python3 - "${BUNDLE_WORK}" "${ROOTFS}" <<'PY'
import sys
from pathlib import Path

from core.reims_build import prebuilt_release
from core.web_bundle import promote_bundle, write_bundle_metadata

candidate = Path(sys.argv[1])
rootfs = Path(sys.argv[2])
runtime = prebuilt_release(rootfs / "opt/reims-vgpu")
if not runtime:
    raise SystemExit("web bundle cannot be packaged without a tagged Reims runtime")
write_bundle_metadata(candidate, runtime_release=runtime)
promote_bundle(
    candidate,
    bundles_dir=rootfs / "opt/vhackintosh/web/bundles",
    current_link=rootfs / "opt/vhackintosh/web/current",
    jobs_schema=0,
    runtime_release=runtime,
)
PY
