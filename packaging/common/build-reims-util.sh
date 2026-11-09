#!/usr/bin/env bash
set -euo pipefail

OUT="${1:?usage: build-reims-util.sh OUTPUT_DIR}"
REPO="https://github.com/steelbrain/reims-util.git"
REV="0b9c3780a3ec2aa312a448b09b18320e5a2c9251"
TOOLCHAIN="1.98.1"
WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT

command -v git >/dev/null || { echo "missing build dependency: git" >&2; exit 1; }
command -v rustup >/dev/null || { echo "missing build dependency: rustup" >&2; exit 1; }
rustup toolchain install "$TOOLCHAIN" --profile minimal --no-self-update

git clone --quiet --filter=blob:none --no-checkout "$REPO" "$WORK/source"
git -C "$WORK/source" checkout --quiet --detach "$REV"
cargo "+${TOOLCHAIN}" build --locked --release --manifest-path "$WORK/source/Cargo.toml"

BIN="$WORK/source/target/release/reims-util"
test -x "$BIN" || { echo "pinned reims-util build did not produce an executable" >&2; exit 1; }
"$BIN" --version | grep -q '^reims-util '
# `--no-ovmf` é usado pelo build_opencore.py; `--add-kext` é a interface que
# leva os kexts de guest do reims-vgpu para a imagem. A antiga flag de
# "arm64-port" foi aposentada no upstream e não aparece mais no `--help`,
# então exigi-la aqui derrubava o pin novo sem motivo.
"$BIN" opencore --help | grep -q -- '--no-ovmf' || {
  echo "pinned reims-util does not expose --no-ovmf" >&2
  exit 1
}
"$BIN" opencore --help | grep -q -- '--add-kext' || {
  echo "pinned reims-util does not expose the required --add-kext option" >&2
  exit 1
}

install -d -m 0755 "$OUT"
install -m 0755 "$BIN" "$OUT/reims-util"
install -m 0644 "$WORK/source/LICENSE" "$OUT/LICENSE"
cat > "$OUT/PROVENANCE.txt" <<EOF
Project: reims-util
Repository: https://github.com/steelbrain/reims-util
Commit: $REV
Rust toolchain: $TOOLCHAIN
License: GPL-2.0-or-later OR LGPL-3.0-or-later
The upstream source for this exact binary is available at the repository commit above.
EOF
