#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: sudo bash deploy/setup_ssl_certbot.sh <subdomain.example.com> <your-email>"
  exit 1
fi

DOMAIN="$1"
EMAIL="$2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_DIR}"
APP_DOMAIN="${DOMAIN}" LETSENCRYPT_EMAIL="${EMAIL}" bash ./setup.sh
