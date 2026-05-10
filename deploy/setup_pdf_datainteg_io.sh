#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: sudo bash deploy/setup_pdf_datainteg_io.sh <your-email>"
  exit 1
fi

EMAIL="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_DIR}"
APP_DOMAIN="pdf.datainteg.io" LETSENCRYPT_EMAIL="${EMAIL}" bash ./setup.sh
