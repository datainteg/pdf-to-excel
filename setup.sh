#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

APP_DOMAIN="${APP_DOMAIN:-pdf.datainteg.io}"
LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-}"
FORCE_HTTP_ONLY="${FORCE_HTTP_ONLY:-0}"

CERT_DIR="${SCRIPT_DIR}/certbot/conf/live/${APP_DOMAIN}"
CERT_FULLCHAIN="${CERT_DIR}/fullchain.pem"
CERT_PRIVKEY="${CERT_DIR}/privkey.pem"
RUNTIME_NGINX_DIR="${SCRIPT_DIR}/deploy/nginx/runtime"
RUNTIME_NGINX_CONF="${RUNTIME_NGINX_DIR}/pdf2excel.conf"
HTTP_TEMPLATE="${SCRIPT_DIR}/deploy/nginx/templates/http-only.conf"
HTTPS_TEMPLATE="${SCRIPT_DIR}/deploy/nginx/templates/https-enabled.conf"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker first: https://docs.docker.com/engine/install/"
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD=(docker-compose)
else
  echo "Docker Compose is not available. Install Docker Compose plugin."
  exit 1
fi

stop_host_nginx() {
  if ! command -v systemctl >/dev/null 2>&1; then
    return
  fi
  if ! systemctl list-unit-files 2>/dev/null | grep -q '^nginx\.service'; then
    return
  fi

  if systemctl is-active --quiet nginx; then
    echo "Stopping host nginx service (non-dockerized)..."
    systemctl stop nginx
  fi

  if systemctl is-enabled --quiet nginx; then
    echo "Disabling host nginx service..."
    systemctl disable nginx >/dev/null 2>&1 || true
  fi
}

stop_host_nginx_processes() {
  if command -v pgrep >/dev/null 2>&1 && pgrep -x nginx >/dev/null 2>&1; then
    echo "Stopping remaining host nginx processes..."
    pkill -x nginx || true
  fi
}

prepare_dirs() {
  mkdir -p \
    output/jobs \
    output/csv \
    output/raw_text \
    pdfs \
    certbot/www \
    certbot/conf \
    deploy/nginx/runtime
}

has_cert() {
  [[ -f "${CERT_FULLCHAIN}" && -f "${CERT_PRIVKEY}" ]]
}

write_runtime_nginx_conf() {
  local template_path
  if [[ "${FORCE_HTTP_ONLY}" == "1" ]]; then
    template_path="${HTTP_TEMPLATE}"
  elif has_cert; then
    template_path="${HTTPS_TEMPLATE}"
  else
    template_path="${HTTP_TEMPLATE}"
  fi

  sed "s#__APP_DOMAIN__#${APP_DOMAIN}#g" "${template_path}" > "${RUNTIME_NGINX_CONF}"
}

wait_for_http_health() {
  if ! command -v curl >/dev/null 2>&1; then
    return
  fi
  local attempts=45
  for _ in $(seq 1 "${attempts}"); do
    if curl -fsS "http://127.0.0.1/health" >/dev/null 2>&1; then
      return
    fi
    sleep 2
  done
  echo "ERROR: Service did not become healthy via nginx on :80."
  echo "Run: ${COMPOSE_CMD[*]} logs --tail=200 pdf2excel-web nginx"
  exit 1
}

wait_for_https_health() {
  if ! command -v curl >/dev/null 2>&1; then
    return
  fi
  local attempts=30
  for _ in $(seq 1 "${attempts}"); do
    if curl -kfsS --resolve "${APP_DOMAIN}:443:127.0.0.1" "https://${APP_DOMAIN}/health" >/dev/null 2>&1; then
      return
    fi
    sleep 2
  done
  echo "ERROR: HTTPS check failed for ${APP_DOMAIN}."
  echo "Run: ${COMPOSE_CMD[*]} logs --tail=200 nginx"
  exit 1
}

issue_letsencrypt_cert() {
  if [[ -z "${LETSENCRYPT_EMAIL}" ]]; then
    return
  fi
  if [[ "${FORCE_HTTP_ONLY}" == "1" ]]; then
    return
  fi
  if has_cert; then
    return
  fi

  echo "No certificate found for ${APP_DOMAIN}. Issuing Let's Encrypt certificate..."
  docker run --rm \
    -v "${SCRIPT_DIR}/certbot/conf:/etc/letsencrypt" \
    -v "${SCRIPT_DIR}/certbot/www:/var/www/certbot" \
    certbot/certbot certonly \
      --webroot \
      --webroot-path=/var/www/certbot \
      --non-interactive \
      --agree-tos \
      --no-eff-email \
      --email "${LETSENCRYPT_EMAIL}" \
      -d "${APP_DOMAIN}"

  if ! has_cert; then
    echo "ERROR: Certificate was not created for ${APP_DOMAIN}."
    exit 1
  fi

  write_runtime_nginx_conf
  "${COMPOSE_CMD[@]}" up -d --force-recreate --no-deps nginx
}

echo "[1/5] Preparing directories..."
prepare_dirs

echo "[2/5] Stopping host (non-dockerized) nginx if present..."
if [[ "${EUID}" -eq 0 ]]; then
  stop_host_nginx
  stop_host_nginx_processes
else
  echo "Not running as root, skipping host nginx stop/disable step."
fi

echo "[3/5] Rendering nginx runtime config..."
write_runtime_nginx_conf

echo "[4/5] Building and starting docker services..."
"${COMPOSE_CMD[@]}" up --build -d

echo "[5/5] Verifying health and optional SSL..."
wait_for_http_health
issue_letsencrypt_cert
if [[ "${FORCE_HTTP_ONLY}" != "1" ]] && has_cert; then
  wait_for_https_health
fi

echo "OK: Stack is running."
echo "HTTP:  http://localhost"
echo "Direct app: http://127.0.0.1:8082"
if has_cert; then
  echo "HTTPS: https://${APP_DOMAIN}"
elif [[ -n "${LETSENCRYPT_EMAIL}" && "${FORCE_HTTP_ONLY}" != "1" ]]; then
  echo "HTTPS requested but certificate is not present. Check: ${COMPOSE_CMD[*]} logs --tail=200 nginx"
else
  echo "HTTPS is not enabled yet."
  echo "To enable SSL later, re-run with:"
  echo "  APP_DOMAIN=${APP_DOMAIN} LETSENCRYPT_EMAIL=you@example.com bash setup.sh"
fi
echo "Login username: datainteg"
echo "Login password: Welcome@911"
echo "MongoDB: mongodb://localhost:27017"
echo "Logs: ${COMPOSE_CMD[*]} logs -f"
