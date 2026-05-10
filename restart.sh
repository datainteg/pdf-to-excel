#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# Optional auto-pull before restart:
#   AUTO_PULL=1 bash restart.sh
if [[ "${AUTO_PULL:-0}" == "1" ]]; then
  if command -v git >/dev/null 2>&1; then
    echo "[restart] Pulling latest code..."
    git pull --rebase origin main
  fi
fi

echo "[restart] Restarting stack via setup.sh ..."
bash ./setup.sh

