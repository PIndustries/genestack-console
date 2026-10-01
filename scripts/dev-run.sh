#!/usr/bin/env bash
# Start Genestack Console from config.yaml
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VENV="${ROOT}/.venv"
if [[ ! -x "${VENV}/bin/uvicorn" ]]; then
  python3 -m venv "$VENV"
  "${VENV}/bin/pip" install -q -r requirements.txt
fi

mkdir -p data
# Optional: CONSOLE_CONFIG=/path/to.yaml
exec "${VENV}/bin/uvicorn" app.main:app --host 0.0.0.0 --port 8080 --reload
