#!/usr/bin/env bash
# Compile the Console to a native Linux binary (Nuitka). Operators get that
# file — not a git tree, not a docker save tarball of source layers.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${GSC_DIST:-$ROOT/dist}"
NAME="genestack-console-linux-$(uname -m)"
case "$(uname -m)" in
  x86_64|amd64) NAME="genestack-console-linux-amd64" ;;
  aarch64|arm64) NAME="genestack-console-linux-arm64" ;;
esac

mkdir -p "$OUT_DIR"
cd "$ROOT"

python3 -m pip install -q --upgrade pip
python3 -m pip install -q -r requirements.txt "nuitka>=2.4" ordered-set zstandard

echo "==> Nuitka onefile → $OUT_DIR/$NAME"
python3 -m nuitka \
  --onefile \
  --assume-yes-for-downloads \
  --lto=no \
  --output-dir="$OUT_DIR" \
  --output-filename="$NAME" \
  --include-package=app \
  --include-package=uvicorn \
  --include-package=fastapi \
  --include-package=starlette \
  --include-package=sqlalchemy \
  --include-package=pydantic \
  --include-package=cryptography \
  --include-package=yaml \
  --include-package=httpx \
  --include-package=jinja2 \
  --include-package=websockets \
  --include-package=anyio \
  --include-package=click \
  --include-package=h11 \
  --include-data-dir="$ROOT/app/static=app/static" \
  --include-data-dir="$ROOT/app/templates=app/templates" \
  --include-data-dir="$ROOT/ansible=ansible" \
  --include-data-dir="$ROOT/agent=agent" \
  --include-data-dir="$ROOT/terraform=terraform" \
  --nofollow-import-to=pytest \
  --nofollow-import-to=ruff \
  --nofollow-import-to=ansible \
  --nofollow-import-to=ansible_collections \
  --nofollow-import-to=IPython \
  app/bin_main.py

echo "OK: $OUT_DIR/$NAME"
VER="$(python3 -c 'from app.version import VERSION; print(VERSION)')"
cat > "$OUT_DIR/version.json" <<EOF
{"name":"genestack-console","version":"${VER}","binary":"${NAME}"}
EOF
echo "Built $OUT_DIR/$NAME"
echo "GitHub Actions attaches this file to the release. This script does not publish it."
