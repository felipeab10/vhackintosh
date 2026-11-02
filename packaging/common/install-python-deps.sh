#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${1:?usage: install-python-deps.sh APP_DIR REQUIREMENTS_LOCK}"
REQUIREMENTS="${2:?usage: install-python-deps.sh APP_DIR REQUIREMENTS_LOCK}"

command -v python3 >/dev/null || { echo "missing target distribution Python" >&2; exit 1; }
python3 -m venv /tmp/vhackintosh-control-build-venv
BUILD_PYTHON=/tmp/vhackintosh-control-build-venv/bin/python
"$BUILD_PYTHON" -m pip install \
  --disable-pip-version-check \
  --no-warn-script-location \
  --require-hashes \
  --only-binary=:all: \
  --target "${APP_DIR}/vendor" \
  -r "$REQUIREMENTS"

PYTHONPATH="${APP_DIR}/vendor" python3 - <<'PY'
import sysconfig

import fastapi
import pydantic
import pydantic_core
import uvicorn
from pydantic_core import ArgsKwargs

suffix = sysconfig.get_config_var("EXT_SUFFIX")
extension = pydantic_core._pydantic_core.__file__
if not suffix or not extension.endswith(suffix):
    raise SystemExit(
        f"pydantic-core ABI mismatch: Python expects {suffix!r}, got {extension!r}"
    )
print(f"Python {sysconfig.get_python_version()} dependency ABI verified: {extension}")
PY
