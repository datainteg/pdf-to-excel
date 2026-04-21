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
  tesseract-ocr-mar \
  poppler-utils

echo "[3/4] Creating virtual environment (.venv)..."
python3 -m venv .venv
source .venv/bin/activate

echo "[4/4] Installing Python dependencies..."
python -m pip install --upgrade pip
python -m pip install \
  pytesseract \
  pdf2image \
  opencv-python \
  pymongo \
  pandas \
  openpyxl \
  pypdfium2

echo ""
echo "Setup complete."
echo "Next:"
echo "  1) Put PDFs in ./pdfs"
echo "  2) source .venv/bin/activate"
echo "  3) python run_batch.py --input-dir pdfs --excel output/all_voters.xlsx"
