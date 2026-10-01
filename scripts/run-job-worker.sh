#!/usr/bin/env bash
# Optional background job loop (sync runner already used by API for local).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# Config from config.yaml — no env laundry list.
exec "${ROOT}/.venv/bin/python" -m app.worker.runner
