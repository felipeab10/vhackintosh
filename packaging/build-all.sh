#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-1.0.0}"
OUT="${2:-${ROOT}/dist}"
ENGINE="${VHACK_CONTAINER_ENGINE:-}"

if [[ ! "$VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z.+~_-]*$ ]]; then
  echo "invalid version: $VERSION" >&2
  exit 2
fi
if [[ -z "$ENGINE" ]]; then
  if command -v podman >/dev/null 2>&1; then ENGINE=podman
  elif command -v docker >/dev/null 2>&1; then ENGINE=docker
  else echo "install Podman or Docker to build the distro packages" >&2; exit 1
  fi
fi
command -v "$ENGINE" >/dev/null || { echo "container engine not found: $ENGINE" >&2; exit 1; }

ROOT="$(realpath "$ROOT")"
OUT="$(mkdir -p "$OUT" && realpath "$OUT")"
WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT
PAYLOAD="${WORK}/payload"
mkdir -p "$PAYLOAD"

echo "[1/4] Building the shared Control payload (${VERSION})"
"${ROOT}/packaging/common/stage-app.sh" "$PAYLOAD"

container_build() {
  local image="$1"; shift
  "$ENGINE" run --rm \
    --volume "${ROOT}:/src:ro" \
    --volume "${PAYLOAD}:/payload:ro" \
    --volume "${OUT}:/out:rw" \
    --env "VHACK_CONTROL_VERSION=${VERSION}" \
    "$image" "$@"
}

echo "[2/4] Building Ubuntu 24.04 package"
container_build "${VHACK_UBUNTU_IMAGE:-ubuntu:24.04}" bash -euc '
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends dpkg-dev
  bash /src/packaging/deb/build-package.sh "$VHACK_CONTROL_VERSION" /payload /out
  dpkg-deb --info "/out/vhackintosh-control_${VHACK_CONTROL_VERSION}_amd64.deb" >/dev/null
'

echo "[3/4] Building Fedora 44 RPM"
container_build "${VHACK_FEDORA_IMAGE:-fedora:44}" bash -euc '
  dnf -y install rpm-build
  bash /src/packaging/fedora/build-package.sh "$VHACK_CONTROL_VERSION" /payload /out
  rpm -qp --info "/out/vhackintosh-control-${VHACK_CONTROL_VERSION//-/.}-1"*.rpm >/dev/null
'

echo "[4/4] Building Arch Linux package"
container_build "${VHACK_ARCH_IMAGE:-archlinux:latest}" bash -euc '
  pacman -Syu --noconfirm --needed base-devel
  bash /src/packaging/arch/build-package.sh "$VHACK_CONTROL_VERSION" /payload /out
  pacman -Qp "/out/vhackintosh-control-${VHACK_CONTROL_VERSION//-/.}-1-x86_64.pkg.tar.zst" >/dev/null
'

shopt -s nullglob
PACKAGES=(
  "${OUT}/vhackintosh-control_${VERSION}_amd64.deb"
  "${OUT}/vhackintosh-control-${VERSION//-/.}-1"*.rpm
  "${OUT}/vhackintosh-control-${VERSION//-/.}-1-x86_64.pkg.tar.zst"
)
for package in "${PACKAGES[@]}"; do
  [[ -f "$package" ]] || { echo "expected package was not created: $package" >&2; exit 1; }
done
sha256sum "${PACKAGES[@]}" > "${OUT}/SHA256SUMS"
printf '\nBuilt packages in %s:\n' "$OUT"
for package in "${PACKAGES[@]}"; do
  printf '  %s (%s bytes)\n' "$(basename "$package")" "$(stat -c '%s' "$package")"
done
