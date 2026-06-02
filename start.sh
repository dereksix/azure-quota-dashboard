#!/usr/bin/env bash
# Azure Quota Dashboard — quick-start launcher (macOS / Linux).
# Creates a venv on first run, installs deps, then starts the server.

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x "backend/.venv/bin/python" ]; then
  echo "[setup] Creating Python virtualenv..."
  python3 -m venv backend/.venv
  echo "[setup] Installing dependencies..."
  backend/.venv/bin/python -m pip install -q -r backend/requirements.txt
fi

PORT="${PORT:-8765}"
echo "[run] Starting Azure Quota Dashboard at http://127.0.0.1:${PORT}/"
exec backend/.venv/bin/python -m uvicorn server:app --app-dir backend --host 127.0.0.1 --port "${PORT}"
