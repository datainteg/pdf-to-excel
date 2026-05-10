#!/usr/bin/env bash
set -euo pipefail

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

mkdir -p output/jobs output/csv output/raw_text pdfs

echo "[1/3] Building and starting containers..."
"${COMPOSE_CMD[@]}" up --build -d

echo "[2/3] Waiting for service health..."
if command -v curl >/dev/null 2>&1; then
  for _ in $(seq 1 30); do
    if curl -fsS http://localhost:8082/health >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
fi

echo "[3/3] Ready."
echo "OK: Website is running at http://localhost:8082"
echo "Login username: datainteg"
echo "Login password: Welcome@911"
echo "MongoDB is running at mongodb://localhost:27017"
echo "Use this command to see logs:"
echo "  ${COMPOSE_CMD[*]} logs -f"
echo "For domain + SSL on server:"
echo "  sudo bash deploy/setup_pdf_datainteg_io.sh your-email@domain.com"
