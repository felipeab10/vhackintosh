#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUBLISH=0
VERSION=""
OUT="${ROOT}/dist"
ENGINE="${VHACK_CONTAINER_ENGINE:-}"

usage() {
  cat <<'EOF'
Usage:
  packaging/build-all.sh [VERSION] [OUTPUT_DIR]
  packaging/build-all.sh --publish [--version VERSION] [--output OUTPUT_DIR]

The default mode only builds packages. --publish builds all packages, chooses
the next control-v* beta tag (or uses --version), then creates and pushes an
annotated tag to origin. GitHub Actions builds and attaches the packages to a
release. Publishing requires a clean, up-to-date checkout of origin's default
branch.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ "${1:-}" == "--publish" || "${1:-}" == "--version" || "${1:-}" == "--output" ]]; then
  while (($#)); do
    case "$1" in
      --publish) PUBLISH=1; shift ;;
      --version)
        (($# >= 2)) || { echo "--version requires a value" >&2; exit 2; }
        VERSION="$2"; shift 2 ;;
      --output)
        (($# >= 2)) || { echo "--output requires a directory" >&2; exit 2; }
        OUT="$2"; shift 2 ;;
      *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
  done
else
  VERSION="${1:-1.0.0}"
  OUT="${2:-${ROOT}/dist}"
  (($# <= 2)) || { usage >&2; exit 2; }
fi

git_root() {
  git -C "$ROOT" rev-parse --show-toplevel 2>/dev/null
}

next_control_version() {
  local latest base beta
  latest="$(git -C "$ROOT" tag --list 'control-v*' --sort=-version:refname | head -n 1)"
  if [[ -z "$latest" ]]; then
    printf '1.0.0-beta.1\n'
  elif [[ "$latest" =~ ^control-v([0-9]+\.[0-9]+\.[0-9]+)-beta\.([0-9]+)$ ]]; then
    base="${BASH_REMATCH[1]}"
    beta="${BASH_REMATCH[2]}"
    printf '%s-beta.%s\n' "$base" "$((beta + 1))"
  elif [[ "$latest" =~ ^control-v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    printf '%s.%s.%s-beta.1\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "$((BASH_REMATCH[3] + 1))"
  else
    echo "cannot derive the next beta version from tag ${latest}; pass --version explicitly" >&2
    return 1
  fi
}

if ((PUBLISH)); then
  git_root >/dev/null || { echo "--publish must run from a Git checkout" >&2; exit 1; }
  [[ -z "$(git -C "$ROOT" status --porcelain)" ]] || {
    echo "--publish requires a clean working tree; commit or stash local changes first" >&2; exit 1;
  }
  git -C "$ROOT" fetch --quiet origin --tags
  default_ref="$(git -C "$ROOT" symbolic-ref --quiet --short refs/remotes/origin/HEAD || true)"
  if [[ -z "$default_ref" ]]; then
    default_branch="$(git -C "$ROOT" remote show origin | sed -n 's/^  HEAD branch: //p' | head -n 1)"
    [[ -n "$default_branch" ]] || { echo "could not determine origin's default branch" >&2; exit 1; }
    default_ref="origin/${default_branch}"
  fi
  current_branch="$(git -C "$ROOT" branch --show-current)"
  [[ "$current_branch" == "${default_ref#origin/}" ]] || {
    echo "--publish is restricted to origin's default branch (${default_ref#origin/}); current branch: ${current_branch:-detached HEAD}" >&2; exit 1;
  }
  [[ "$(git -C "$ROOT" rev-parse HEAD)" == "$(git -C "$ROOT" rev-parse "$default_ref")" ]] || {
    echo "checkout is not synchronized with ${default_ref}; update it before publishing" >&2; exit 1;
  }
  [[ -f "${ROOT}/.github/workflows/release-control.yml" ]] || {
    echo "the Control release workflow is missing from this checkout" >&2; exit 1;
  }
  if [[ -z "$VERSION" ]]; then
    VERSION="$(next_control_version)"
  fi
  [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-beta\.[0-9]+)?$ ]] || {
    echo "published versions must use MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH-beta.N" >&2; exit 2;
  }
  TAG="control-v${VERSION}"
  if git -C "$ROOT" show-ref --verify --quiet "refs/tags/${TAG}" || git -C "$ROOT" ls-remote --exit-code --tags origin "refs/tags/${TAG}" >/dev/null 2>&1; then
    echo "tag already exists: ${TAG}" >&2; exit 1;
  fi
  echo "Will publish ${TAG} after package builds pass."
fi

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
PAYLOAD_BASE="${WORK}/payload-base"
mkdir -p "$PAYLOAD_BASE"

echo "[1/4] Building shared Control sources and frontend (${VERSION})"
"${ROOT}/packaging/common/stage-app.sh" "$PAYLOAD_BASE"

container_build() {
  local image="$1" payload="$2"; shift 2
  "$ENGINE" run --rm \
    --volume "${ROOT}:/src:ro" \
    --volume "${payload}:/payload:ro" \
    --volume "${OUT}:/out:rw" \
    --env "VHACK_CONTROL_VERSION=${VERSION}" \
    "$image" "$@"
}

echo "[2/4] Building Ubuntu 24.04 package with Ubuntu Python dependencies"
PAYLOAD_UBUNTU="${WORK}/payload-ubuntu"
cp -a "$PAYLOAD_BASE" "$PAYLOAD_UBUNTU"
container_build "${VHACK_UBUNTU_IMAGE:-ubuntu:24.04}" "$PAYLOAD_UBUNTU" bash -euc '
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends dpkg-dev python3-venv
  cp -a /payload /tmp/vhackintosh-package-payload
  bash /src/packaging/common/install-python-deps.sh /tmp/vhackintosh-package-payload/opt/vhackintosh/web/current /tmp/vhackintosh-package-payload/opt/vhackintosh/web/current/requirements.lock
  bash /src/packaging/deb/build-package.sh "$VHACK_CONTROL_VERSION" /tmp/vhackintosh-package-payload /out
  dpkg-deb --info "/out/vhackintosh-control_${VHACK_CONTROL_VERSION}_amd64.deb" >/dev/null
'

echo "[3/4] Building Fedora 44 RPM with Fedora Python dependencies"
PAYLOAD_FEDORA="${WORK}/payload-fedora"
cp -a "$PAYLOAD_BASE" "$PAYLOAD_FEDORA"
container_build "${VHACK_FEDORA_IMAGE:-fedora:44}" "$PAYLOAD_FEDORA" bash -euc '
  dnf -y install rpm-build python3-pip
  cp -a /payload /tmp/vhackintosh-package-payload
  bash /src/packaging/common/install-python-deps.sh /tmp/vhackintosh-package-payload/opt/vhackintosh/web/current /tmp/vhackintosh-package-payload/opt/vhackintosh/web/current/requirements.lock
  bash /src/packaging/fedora/build-package.sh "$VHACK_CONTROL_VERSION" /tmp/vhackintosh-package-payload /out
  rpm -qp --info "/out/vhackintosh-control-${VHACK_CONTROL_VERSION//-/.}-1"*.rpm >/dev/null
'

echo "[4/4] Building Arch Linux package with Arch Python dependencies"
PAYLOAD_ARCH="${WORK}/payload-arch"
cp -a "$PAYLOAD_BASE" "$PAYLOAD_ARCH"
container_build "${VHACK_ARCH_IMAGE:-archlinux:latest}" "$PAYLOAD_ARCH" bash -euc '
  pacman -Syu --noconfirm --needed base-devel python-pip
  cp -a /payload /tmp/vhackintosh-package-payload
  bash /src/packaging/common/install-python-deps.sh /tmp/vhackintosh-package-payload/opt/vhackintosh/web/current /tmp/vhackintosh-package-payload/opt/vhackintosh/web/current/requirements.lock
  bash /src/packaging/arch/build-package.sh "$VHACK_CONTROL_VERSION" /tmp/vhackintosh-package-payload /out
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

if ((PUBLISH)); then
  git -C "$ROOT" tag -a "$TAG" -m "vHackintosh Control ${VERSION}"
  if ! git -C "$ROOT" push origin "$TAG"; then
    git -C "$ROOT" tag -d "$TAG" >/dev/null
    echo "failed to push ${TAG}; local tag removed" >&2
    exit 1
  fi
  printf '\nPushed %s. GitHub Actions will publish the release after its checks pass.\n' "$TAG"
  printf 'https://github.com/%s/releases/tag/%s\n' "$(git -C "$ROOT" remote get-url origin | sed -E 's#.*github.com[:/]([^/]+/[^/.]+)(\.git)?$#\1#')" "$TAG"
fi
