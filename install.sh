#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${ROOT_DIR}/.venv"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "[ERROR] ${PYTHON_BIN} is not available."
  exit 1
fi

"${PYTHON_BIN}" -m venv "${VENV_DIR}"
source "${VENV_DIR}/bin/activate"

python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[dev]"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[INFO] ffmpeg not found on PATH. Trying an OS package manager."
  if [[ "${PARADOX_MEDIA_ENGINE_NO_SYSTEM_INSTALL:-0}" != "1" ]]; then
    if command -v brew >/dev/null 2>&1; then
      brew install ffmpeg || true
    elif command -v apt-get >/dev/null 2>&1; then
      sudo apt-get update && sudo apt-get install -y ffmpeg || true
    elif command -v dnf >/dev/null 2>&1; then
      sudo dnf install -y ffmpeg || true
    elif command -v yum >/dev/null 2>&1; then
      sudo yum install -y ffmpeg || true
    elif command -v pacman >/dev/null 2>&1; then
      sudo pacman -Sy --noconfirm ffmpeg || true
    fi
  fi
fi

python parad0x_media_engine.py --help >/dev/null
python media_benchmark.py --help >/dev/null
python scripts/public_surface_check.py

echo
echo "Parad0x Media Engine bootstrap complete."
echo "Activate the environment with:"
echo "  source .venv/bin/activate"
