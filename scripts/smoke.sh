#!/usr/bin/env bash
# Smoke checks against a running Genestack Console instance.
#   ./scripts/smoke.sh
#   CONSOLE_API_BASE=http://127.0.0.1:8000 CONSOLE_API_KEY=dev-admin-key ./scripts/smoke.sh
set -euo pipefail

API_BASE="${CONSOLE_API_BASE:-http://127.0.0.1:8080}"
API_KEY="${CONSOLE_API_KEY:-dev-admin-key}"

pass=0
fail=0

check() {
  local name="$1"
  local method="$2"
  local path="$3"
  local expect="${4:-200}"
  local url="${API_BASE}${path}"
  local code body
  body=$(mktemp)
  code=$(curl -sS -o "$body" -w "%{http_code}" -X "$method" "$url" \
    -H "X-API-Key: ${API_KEY}" \
    -H "Accept: application/json" \
    || echo "000")
  if [[ "$code" == "$expect" ]]; then
    echo "OK  ${name} (${method} ${path} -> ${code})"
    pass=$((pass + 1))
  else
    echo "FAIL ${name} (${method} ${path} -> ${code}, expected ${expect})"
    head -c 400 "$body" || true
    echo
    fail=$((fail + 1))
  fi
  rm -f "$body"
}

echo "[smoke] target=${API_BASE}"

# Health (unauthenticated variants)
check "health" "GET" "/health" "200" || true
if ! curl -sS -o /dev/null -w "%{http_code}" "${API_BASE}/health" | grep -q 200; then
  # try alternate paths
  for p in /healthz /api/health /api/v1/health; do
    code=$(curl -sS -o /dev/null -w "%{http_code}" "${API_BASE}${p}" || echo 000)
    if [[ "$code" == "200" ]]; then
      echo "OK  health via ${p}"
      pass=$((pass + 1))
      break
    fi
  done
fi

# Operations catalogue
check "operations list" "GET" "/api/v1/operations" "200"
if [[ $? -ne 0 ]] 2>/dev/null; then
  :
fi
# Accept alternate path used by some routers
code=$(curl -sS -o /dev/null -w "%{http_code}" \
  -H "X-API-Key: ${API_KEY}" "${API_BASE}/api/v1/operations/" || echo 000)
if [[ "$code" == "200" ]]; then
  echo "OK  operations list trailing-slash"
  pass=$((pass + 1))
fi

# MAAS mock machines (when mock mode enabled)
code=$(curl -sS -o /tmp/smoke-maas.json -w "%{http_code}" \
  -H "X-API-Key: ${API_KEY}" "${API_BASE}/api/v1/maas/machines" || echo 000)
if [[ "$code" == "200" ]]; then
  echo "OK  maas machines (${code})"
  pass=$((pass + 1))
elif [[ "$code" == "404" ]]; then
  echo "SKIP maas machines (route not mounted yet)"
else
  echo "FAIL maas machines (${code})"
  fail=$((fail + 1))
fi

echo "[smoke] pass=${pass} fail=${fail}"
if [[ "$fail" -gt 0 ]]; then
  exit 1
fi
exit 0
