#!/bin/bash
# Seed the appliance data disk. A later boot leaves config.yaml and the
# console binary alone, so `genestack-console update` can replace the binary.
set -euo pipefail

PREFIX="${GSC_APPLIANCE_PREFIX:-/var/lib/genestack-console}"
IMAGE_BIN="${GSC_APPLIANCE_IMAGE_BIN:-/usr/lib/genestack-console/genestack-console}"
GENESTACK_ROOT="${GSC_APPLIANCE_GENESTACK:-/var/lib/genestack}"

fernet_key() {
  openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'
}

urlsafe() {
  openssl rand -base64 48 | tr '+/' '-_' | tr -d '=\n' | cut -c1-"$1"
}

link_opt() {
  local dest="$1" target="$2"
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    return 0
  fi
  ln -sfn "$target" "$dest" 2>/dev/null || true
}

mkdir -p "$PREFIX/bin" "$PREFIX/data" "$GENESTACK_ROOT"
if [ -w /etc ]; then
  mkdir -p /etc/genestack
fi
link_opt /opt/genestack-console "$PREFIX"
link_opt /opt/genestack "$GENESTACK_ROOT"

if [ ! -x "$PREFIX/bin/genestack-console" ]; then
  install -m 0755 "$IMAGE_BIN" "$PREFIX/bin/genestack-console"
fi

if ! id genestack >/dev/null 2>&1; then
  useradd --system --home-dir "$PREFIX" --no-create-home --shell /usr/sbin/nologin genestack \
    || useradd --system --home-dir "$PREFIX" --no-create-home --shell /sbin/nologin genestack \
    || true
fi

if [ ! -f "$PREFIX/config.yaml" ]; then
  admin_key="gsc-admin-$(urlsafe 32)"
  operator_key="gsc-operator-$(urlsafe 32)"
  viewer_key="gsc-viewer-$(urlsafe 32)"
  secret="$(fernet_key)"
  umask 077
  cat > "$PREFIX/config.yaml" <<EOF
# Generated on first boot of the Genestack Console appliance.
# A later boot does not overwrite this file.
dry_run: true
seed_demo: true
data_dir: ${PREFIX}/data
update:
  url: https://github.com/PIndustries/genestack-console/releases/latest/download/version.json
  auto: false
  watch: true
secret_key: ${secret}
auth:
  api_keys:
    ${admin_key}: admin
    ${operator_key}: operator
    ${viewer_key}: viewer
  session_ttl_hours: 12
  refresh_ttl_hours: 168
  dev_auto_login: false
genestack:
  root: /opt/genestack
server:
  host: 127.0.0.1
  port: 8080
EOF
  chmod 600 "$PREFIX/config.yaml"
fi

if id genestack >/dev/null 2>&1; then
  chown -R genestack:genestack "$PREFIX" "$GENESTACK_ROOT" || true
fi

if [ ! -f "$PREFIX/ADMIN_CREDENTIALS.txt" ] && id genestack >/dev/null 2>&1 && [ -x "$PREFIX/bin/genestack-console" ]; then
  password="$(urlsafe 24)"
  if runuser -u genestack -- env \
    GSC_PREFIX="$PREFIX" \
    CONSOLE_CONFIG="$PREFIX/config.yaml" \
    "$PREFIX/bin/genestack-console" create-user \
      --username admin --password "$password" --platform-admin; then
    admin_key="$(awk '/^[[:space:]]*gsc-admin-/{gsub(/:/,"",$1); print $1; exit}' "$PREFIX/config.yaml")"
    umask 077
    cat > "$PREFIX/ADMIN_CREDENTIALS.txt" <<EOF
username: admin
password: ${password}
api_key: ${admin_key}
EOF
    chmod 600 "$PREFIX/ADMIN_CREDENTIALS.txt"
    chown genestack:genestack "$PREFIX/ADMIN_CREDENTIALS.txt" || true
  fi
fi
