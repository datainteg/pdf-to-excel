#!/usr/bin/env bash
set -euo pipefail

DOMAIN="pdf.datainteg.io"

if [[ $# -lt 1 ]]; then
  echo "Usage: sudo bash deploy/setup_pdf_datainteg_io.sh <your-email>"
  exit 1
fi

EMAIL="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_PATH="${SCRIPT_DIR}/nginx/${DOMAIN}.conf"
TARGET_PATH="/etc/nginx/sites-available/${DOMAIN}.conf"

if [[ ! -f "${TEMPLATE_PATH}" ]]; then
  echo "Template not found: ${TEMPLATE_PATH}"
  exit 1
fi

echo "[1/6] Installing Nginx + Certbot..."
apt-get update
apt-get install -y nginx certbot python3-certbot-nginx

echo "[2/6] Installing Nginx site config for ${DOMAIN}..."
cp "${TEMPLATE_PATH}" "${TARGET_PATH}"

echo "[3/6] Enabling site..."
ln -sf "${TARGET_PATH}" "/etc/nginx/sites-enabled/${DOMAIN}.conf"
rm -f /etc/nginx/sites-enabled/default

echo "[4/6] Validating and reloading Nginx..."
nginx -t
systemctl reload nginx

echo "[5/6] Provisioning SSL certificate..."
certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos -m "${EMAIL}" --redirect

echo "[6/6] Done."
echo "HTTPS is active for: https://${DOMAIN}"
echo "Make sure DNS A record for ${DOMAIN} points to this server IP."
