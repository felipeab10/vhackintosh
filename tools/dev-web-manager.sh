#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
STATE_ROOT="${XDG_STATE_HOME:-${HOME}/.local/state}/vhackintosh-web-dev"
RUNTIME_ROOT="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/vhackintosh-web-dev"
INSTALL_ROOT="${HOME}/.local/share/vhackintosh-web-dev"
UNIT_ROOT="${HOME}/.config/systemd/user"
PYTHON_ENV="${INSTALL_ROOT}/venv"

if ! command -v uv >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "uv e npm precisam estar instalados para iniciar o painel local." >&2
  exit 1
fi
if ! systemctl --user is-system-running >/dev/null 2>&1; then
  echo "O gerenciador systemd do usuário não está disponível nesta sessão." >&2
  exit 1
fi

mkdir -p "${INSTALL_ROOT}" "${UNIT_ROOT}" "${STATE_ROOT}" "${RUNTIME_ROOT}"
chmod 700 "${INSTALL_ROOT}" "${STATE_ROOT}" "${RUNTIME_ROOT}"
if [[ ! -x "${PYTHON_ENV}/bin/python" ]]; then
  uv venv --python 3.12 "${PYTHON_ENV}"
fi
uv pip sync --python "${PYTHON_ENV}/bin/python" "${PROJECT_ROOT}/web/requirements.lock"
uv pip install --python "${PYTHON_ENV}/bin/python" 'rich>=13,<15'

for UNIT_SOURCE in "${SCRIPT_DIR}/systemd/user/"*.service; do
  UNIT_DEST="${UNIT_ROOT}/$(basename "${UNIT_SOURCE}")"
  if [[ -e "${UNIT_DEST}" ]] && ! cmp -s "${UNIT_SOURCE}" "${UNIT_DEST}"; then
    echo "Já existe um unit diferente em ${UNIT_DEST}; revise-o antes de substituir." >&2
    exit 1
  fi
  install -m 0644 "${UNIT_SOURCE}" "${UNIT_DEST}"
done

systemctl --user daemon-reload
systemctl --user enable --now vhackintosh-web-dev.service vhackintosh-vite-dev.service
systemctl --user --no-pager --full status vhackintosh-web-dev.service vhackintosh-vite-dev.service
echo "Painel local: http://127.0.0.1:5173"
echo "Catálogo real: ${HOME}/.config/vhackintosh/vms.json"
