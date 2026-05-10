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

echo "[1/7] Installing Nginx + Certbot..."
apt-get update
apt-get install -y nginx certbot python3-certbot-nginx

echo "[2/7] Installing Nginx site config for ${DOMAIN}..."
cp "${TEMPLATE_PATH}" "${TARGET_PATH}"

echo "[3/7] Enabling site..."
ln -sf "${TARGET_PATH}" "/etc/nginx/sites-enabled/${DOMAIN}.conf"
rm -f /etc/nginx/sites-enabled/default

echo "[4/7] Validating and reloading Nginx..."
nginx -t
systemctl reload nginx

echo "[5/7] Provisioning SSL certificate..."
certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos -m "${EMAIL}" --redirect

echo "[6/7] Verifying app and mapping..."
if command -v curl >/dev/null 2>&1; then
  curl -fsS "http://127.0.0.1:8082/health" >/dev/null
  curl -fsS "http://127.0.0.1:8082/login" >/dev/null
  curl -fsS -H "Host: ${DOMAIN}" "http://127.0.0.1/health" >/dev/null
  curl -kfsS --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/health" >/dev/null
  curl -kfsS --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/login" >/dev/null
fi

echo "[7/7] Done."
echo "HTTPS is active for: https://${DOMAIN}"
echo "Make sure DNS A record for ${DOMAIN} points to this server IP."
