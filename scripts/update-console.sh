#!/usr/bin/env bash
# update-console.sh — push this checkout's console source to a console already
# installed on a remote host and rebuild/restart its containers. This is how
# long-lived installs (e.g. deployer) are kept current.
#
# Usage:
#   update-console.sh root@deployer.example.com [/opt/genestack-console]
#   update-console.sh --check root@deployer.example.com   # preview only
#
# Requires: ssh access with passwordless sudo on the target, and rsync on both
# ends. Idempotent: rsync only ships diffs and compose/podman rebuilds are
# no-ops when nothing changed.
#
# NOTE on --delete: the sync mirrors this checkout into $PREFIX/src, so files
# deleted locally are deleted remotely. Run with --check first to preview
# exactly what would change.
set -euo pipefail

PREFIX_DEFAULT="/opt/genestack-console"

usage() {
  cat <<EOF
Usage: update-console.sh [--check] <user@host> [prefix]

Pushes this checkout's genestack-console source to <prefix>/src on the target
(default prefix: $PREFIX_DEFAULT), rebuilds the containers (docker compose or
podman, matching the original install), restarts them, and prints the new
version from the remote /health endpoint.

Flags:
  --check   dry run: show what rsync would change (no --delete, no rebuild,
            no remote commands) and exit

Environment knobs:
  GSC_PORT    host port of the console (default: read from the remote
              compose file, fallback 8080)
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*"; }

CHECK_ONLY=0
TARGET="${1:-}"
PREFIX="${2:-$PREFIX_DEFAULT}"
if [ "$TARGET" = "--check" ]; then
  CHECK_ONLY=1
  TARGET="${2:-}"
  PREFIX="${3:-$PREFIX_DEFAULT}"
fi
case "$TARGET" in
  "" | -h | --help) usage; [ -n "$TARGET" ] && exit 0; exit 1 ;;
esac
PREFIX="${PREFIX%/}"

command -v ssh >/dev/null 2>&1 || die "ssh not found"
command -v rsync >/dev/null 2>&1 || die "rsync not found"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$SCRIPT_DIR/.." && pwd)"
[ -f "$SRC/Containerfile" ] || die "no Containerfile next to this script — run from the console repo"

# Git short hash baked into the image as GSC_BUILD (surfaces in /health + UI).
GSC_BUILD="$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)"

# Live secrets never leave the source checkout: config.yaml (generated API
# keys + Fernet secret_key) and .env files are gitignored by design and have
# no business being rsync'd to the remote.
RSYNC_EXCLUDES=(
  --exclude=.git --exclude=.venv --exclude=data --exclude=log
  --exclude=__pycache__ --exclude='*.egg-info' --exclude=.pytest_cache
  --exclude=config.yaml --exclude='.env' --exclude='.env.*' --exclude=HANDOFF.md
)

if [ "$CHECK_ONLY" -eq 1 ]; then
  info "dry-run sync $SRC -> $TARGET:$PREFIX/src (no deletions, no rebuild)"
  # shellcheck disable=SC2068
  rsync -azn --itemize-changes "${RSYNC_EXCLUDES[@]}" \
    --rsync-path="sudo rsync" \
    "$SRC/" "$TARGET:$PREFIX/src/"
  info "remote deletions (rsync --delete is NOT applied in --check mode):"
  # shellcheck disable=SC2068
  rsync -azn --delete --itemize-changes "${RSYNC_EXCLUDES[@]}" \
    --rsync-path="sudo rsync" \
    "$SRC/" "$TARGET:$PREFIX/src/" 2>&1 | grep '^\*deleting' || true
  exit 0
fi

info "syncing $SRC -> $TARGET:$PREFIX/src"
# shellcheck disable=SC2068
rsync -az --delete "${RSYNC_EXCLUDES[@]}" \
  --rsync-path="sudo rsync" \
  "$SRC/" "$TARGET:$PREFIX/src/"

info "rebuilding and restarting containers on $TARGET (build $GSC_BUILD)"
# Runs as root on the target. Compose installs rebuild via the compose file;
# podman installs rebuild the image and restart the two named containers.
# shellcheck disable=SC2029 # PREFIX/GSC_BUILD expand client-side on purpose.
ssh "$TARGET" "sudo PREFIX='$PREFIX' GSC_BUILD='$GSC_BUILD' bash -s" <<'REMOTE'
set -euo pipefail

wait_health() {
  local port="$1" try
  for try in $(seq 1 60); do
    curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

if [ -f "$PREFIX/docker-compose.yml" ] && docker compose version >/dev/null 2>&1; then
  # Published host port, e.g. "127.0.0.1:8080:8080" -> 8080 (default 8080).
  PORT="$(sed -n 's/.*127\.0\.0\.1:\([0-9]*\):8080.*/\1/p' "$PREFIX/docker-compose.yml" | head -n1)"
  PORT="${PORT:-8080}"
  (cd "$PREFIX" && GSC_BUILD="$GSC_BUILD" docker compose up -d --build)
elif command -v podman >/dev/null 2>&1; then
  PORT=8080
  podman build --build-arg "GSC_BUILD=$GSC_BUILD" \
    -t genestack-console:local -f "$PREFIX/src/Containerfile" "$PREFIX/src"
  podman restart genestack-console genestack-console-worker
else
  echo "ERROR: no docker compose stack or podman found on the target" >&2
  exit 1
fi

wait_health "$PORT" || {
  echo "ERROR: console did not become healthy on 127.0.0.1:$PORT within 120s" >&2
  exit 1
}
curl -fsS "http://127.0.0.1:${PORT}/health"
REMOTE

printf '\n'
info "update complete — remote /health reported above (version + build)"
