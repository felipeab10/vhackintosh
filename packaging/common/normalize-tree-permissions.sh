#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 || ! -d "$1" ]]; then
  echo "usage: normalize-tree-permissions.sh DIRECTORY" >&2
  exit 2
fi

# Keep existing executable bits while ensuring directories are traversable
# and regular payload files are readable by the unprivileged package builder.
chmod -R u=rwX,go=rX -- "$1"
