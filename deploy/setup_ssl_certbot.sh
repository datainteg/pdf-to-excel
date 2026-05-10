#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: sudo bash deploy/setup_ssl_certbot.sh <subdomain.example.com> <your-email>"
  exit 1
fi

DOMAIN="$1"
EMAIL="$2"
NGINX_SITE="/etc/nginx/sites-available/${DOMAIN}.conf"

echo "[1/5] Installing Nginx + Certbot..."
apt-get update
apt-get install -y nginx certbot python3-certbot-nginx

echo "[2/5] Writing Nginx reverse proxy config..."
cat >"${NGINX_SITE}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    client_max_body_size 200M;

    location / {
        proxy_pass http://127.0.0.1:8082;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600;
        proxy_connect_timeout 3600;
    }
}
EOF

ln -sf "${NGINX_SITE}" "/etc/nginx/sites-enabled/${DOMAIN}.conf"
rm -f /etc/nginx/sites-enabled/default

echo "[3/5] Validating and reloading Nginx..."
nginx -t
systemctl reload nginx

echo "[4/5] Requesting SSL certificate with Certbot..."
certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos -m "${EMAIL}" --redirect

echo "[5/5] Done."
echo "HTTPS is active for: https://${DOMAIN}"
echo "Note: DNS A record for ${DOMAIN} must point to this server IP."
