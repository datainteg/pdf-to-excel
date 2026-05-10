#!/usr/bin/env bash
set -euo pipefail

echo "[1/4] Updating apt index..."
sudo apt update

echo "[2/4] Installing system dependencies..."
sudo apt install -y \
  python3 \
  python3-pip \
  python3-venv \
  tesseract-ocr \
  tesseract-ocr-eng \
  tesseract-ocr-mar \
  poppler-utils

echo "[3/4] Creating virtual environment (.venv)..."
python3 -m venv .venv
source .venv/bin/activate

echo "[4/4] Installing Python dependencies..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo ""
echo "Setup complete."
echo "Next:"
echo "  1) source .venv/bin/activate"
echo "  2) Start web app: uvicorn app.main:app --host 0.0.0.0 --port 8082"
echo "  3) Open: http://localhost:8082"
echo "  4) CLI mode (optional): python run_batch.py --input-dir pdfs --output-format both --accuracy-mode balanced"
