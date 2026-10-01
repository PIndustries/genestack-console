#!/usr/bin/env bash
# Deploy/update the genestack-console on this host:
# pull latest code, sync python deps, run the test suite, restart services.
#
# Usage:
#   scripts/deploy.sh              # pull, test, restart
#   scripts/deploy.sh --skip-tests # pull, restart (fast path)
#   scripts/deploy.sh --no-pull    # test + restart current checkout
#
# Run from anywhere; paths resolve from this script's location.
set -euo pipefail

CONSOLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${CONSOLE_DIR}/.." && pwd)"
VENV="${CONSOLE_DIR}/.venv"
SKIP_TESTS=false
NO_PULL=false

for arg in "$@"; do
  case "$arg" in
    --skip-tests) SKIP_TESTS=true ;;
    --no-pull)    NO_PULL=true ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

cd "$REPO_ROOT"

if [ "$NO_PULL" = false ]; then
  echo "==> git pull"
  git pull --ff-only
fi

echo "==> sync python deps"
# Editable install is idempotent; picks up any new/changed dependencies.
"${VENV}/bin/pip" install --quiet -e "${CONSOLE_DIR}[dev]"

if [ "$SKIP_TESTS" = false ]; then
  echo "==> pytest"
  (cd "$CONSOLE_DIR" && "${VENV}/bin/python" -m pytest -q)
fi

echo "==> restart services"
sudo systemctl restart genestack-console genestack-console-worker
sleep 3

echo "==> health check"
sudo systemctl is-active --quiet genestack-console || { echo "API service not active" >&2; exit 1; }
sudo systemctl is-active --quiet genestack-console-worker || { echo "worker service not active" >&2; exit 1; }
curl -fsS http://127.0.0.1:8080/health >/dev/null || { echo "health endpoint failed" >&2; exit 1; }

echo "==> deploy OK"
