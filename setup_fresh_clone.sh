#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/datainteg/pdf-to-excel.git}"
TARGET_DIR="${TARGET_DIR:-$HOME/pdf-to-excel}"
BRANCH="${BRANCH:-main}"
FORCE_SYNC="${FORCE_SYNC:-0}"

if ! command -v git >/dev/null 2>&1; then
  echo "git is required."
  exit 1
fi

if [[ "${TARGET_DIR}" == "$(pwd)" ]] || [[ "${TARGET_DIR}" == "$(pwd -P)" ]]; then
  echo "TARGET_DIR cannot be the current working directory."
  echo "Use a separate path, e.g. TARGET_DIR=$HOME/pdf-to-excel-clean"
  exit 1
fi

if [[ -d "${TARGET_DIR}" ]]; then
  if [[ ! -d "${TARGET_DIR}/.git" ]]; then
    echo "Target exists and is not a git repo: ${TARGET_DIR}"
    exit 1
  fi

  if [[ "${FORCE_SYNC}" != "1" ]]; then
    echo "Repo already exists: ${TARGET_DIR}"
    echo "Use FORCE_SYNC=1 to hard-sync it to origin/${BRANCH}."
    exit 1
  fi

  echo "[1/4] Hard syncing existing repo..."
  git -C "${TARGET_DIR}" fetch --all --prune
  git -C "${TARGET_DIR}" checkout "${BRANCH}"
  git -C "${TARGET_DIR}" reset --hard "origin/${BRANCH}"
  git -C "${TARGET_DIR}" clean -fd
else
  echo "[1/4] Cloning repository..."
  git clone --branch "${BRANCH}" "${REPO_URL}" "${TARGET_DIR}"
fi

echo "[2/4] Entering repo..."
cd "${TARGET_DIR}"

echo "[3/4] Making scripts executable..."
chmod +x setup.sh restart.sh

echo "[4/4] Running Docker setup..."
bash ./setup.sh
