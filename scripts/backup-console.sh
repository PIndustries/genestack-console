#!/usr/bin/env bash
# backup-console.sh — online backup of the console database + config.yaml.
#
# Usage:
#   backup-console.sh <backup-dir> [--config <path>] [--keep N]
#
# SQLite (the default layout): an online backup via python3's sqlite3 backup
# API — a consistent snapshot even while the console is running (WAL-safe).
# Terraform state snapshots live in the database (encrypted), so this dump
# is enough to write terraform.tfstate back onto disk after a restore.
# Postgres (database_url: postgresql+psycopg://...): pg_dump (custom format).
#
# config.yaml is always copied alongside the database: it holds the Fernet
# secret_key and the break-glass API keys, and a database dump without them
# cannot be restored to a working console. Output files are chmod 0600 and
# the timestamped directory chmod 0700 — treat backups like secrets.
#
# Restore (SQLite): stop the console stack, then
#   cp <backup-dir>/<stamp>/console.db <prefix>/data/console.db
#   (and restore config.yaml from the same directory if yours was lost),
#   then start the stack. Postgres: pg_restore -d console < console.dump.
set -euo pipefail

BACKUP_DIR=""
CONFIG_FILE=""
KEEP=7

usage() {
  cat <<EOF
Usage: backup-console.sh <backup-dir> [--config <path>] [--keep N]

Backs up the console database (SQLite online backup or pg_dump) and
config.yaml into <backup-dir>/<UTC-stamp>/, keeping the newest N backups
(default 7).
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG_FILE="$2"; shift 2 ;;
    --config=*) CONFIG_FILE="${1#*=}"; shift ;;
    --keep) KEEP="$2"; shift 2 ;;
    --keep=*) KEEP="${1#*=}"; shift ;;
    -h | --help) usage; exit 0 ;;
    -*) die "unknown option: $1 (see --help)" ;;
    *) BACKUP_DIR="$1"; shift ;;
  esac
done
[ -n "$BACKUP_DIR" ] || { usage; exit 1; }
case "$KEEP" in (*[!0-9]*) die "--keep must be a positive integer (got: $KEEP)";; esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -n "$CONFIG_FILE" ] || CONFIG_FILE="$SCRIPT_DIR/../config.yaml"
[ -f "$CONFIG_FILE" ] || die "config file not found: $CONFIG_FILE (pass --config)"

CONSOLE_DIR="$(cd "$(dirname "$CONFIG_FILE")" && pwd)"

# Resolve the SQLite path the same way app/config.py does: data_dir is
# relative to the console directory, database_url (if set) wins.
DB_URL="$(sed -n 's/^[[:space:]]*database_url:[[:space:]]*//p' "$CONFIG_FILE" | head -n1 | sed 's/[[:space:]]*#.*$//')"
DATA_DIR_REL="$(sed -n 's/^[[:space:]]*data_dir:[[:space:]]*//p' "$CONFIG_FILE" | head -n1 | sed 's/[[:space:]]*#.*$//')"
[ -n "$DATA_DIR_REL" ] || DATA_DIR_REL="./data"
case "$DATA_DIR_REL" in
  /*) DATA_DIR="$DATA_DIR_REL" ;;
  *) DATA_DIR="$CONSOLE_DIR/$DATA_DIR_REL" ;;
esac

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="$(mkdir -p "$BACKUP_DIR" && printf '%s/%s' "$BACKUP_DIR" "$STAMP")"
mkdir -p "$OUT_DIR"
chmod 0700 "$OUT_DIR"

info "config: $CONFIG_FILE"
info "backup dir: $OUT_DIR"

if [ -n "$DB_URL" ]; then
  case "$DB_URL" in
    postgresql*)
      command -v pg_dump >/dev/null 2>&1 || die "database_url is Postgres but pg_dump is not installed"
      PG_ENV_LINES="$(python3 - "$DB_URL" <<'PY'
import sys
from urllib.parse import quote, urlparse

u = urlparse(sys.argv[1])
parts = []
if u.hostname:
    parts.append(f'PGHOST={quote(u.hostname, safe="")}')
if u.port:
    parts.append(f'PGPORT={u.port}')
if u.username:
    parts.append(f'PGUSER={quote(u.username, safe="")}')
if u.password:
    parts.append(f'PGPASSWORD={quote(u.password, safe="")}')
if u.path:
    parts.append(f'PGDATABASE={quote(u.path.lstrip("/"), safe="")}')
print(" ".join(parts))
PY
)"
      # shellcheck disable=SC2086
      env $PG_ENV_LINES pg_dump -Fc -f "$OUT_DIR/console.dump"
      info "pg_dump written: $OUT_DIR/console.dump"
      ;;
    *) die "unsupported database_url for backup: $DB_URL (SQLite or postgresql+psycopg://)" ;;
  esac
else
  DB_FILE="$DATA_DIR/console.db"
  [ -f "$DB_FILE" ] || die "no console.db at $DB_FILE (and no database_url in config)"
  python3 - "$DB_FILE" "$OUT_DIR/console.db" <<'PY'
import sqlite3
import sys

src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
src.close()
dst.close()
PY
  info "sqlite online backup written: $OUT_DIR/console.db"
fi

cp "$CONFIG_FILE" "$OUT_DIR/config.yaml"
chmod 0600 "$OUT_DIR/config.yaml"
info "config.yaml copied (Fernet key + API keys): $OUT_DIR/config.yaml"

# Retention: keep the newest N timestamped directories under BACKUP_DIR.
# Portable (no GNU find -printf): UTC stamp names sort lexicographically.
python3 - "$BACKUP_DIR" "$KEEP" <<'PY'
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1])
keep = int(sys.argv[2])
dirs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
for old in dirs[: max(0, len(dirs) - keep)]:
    print(f"==> removing old backup: {old.name}")
    shutil.rmtree(old)
PY

info "backup complete: $OUT_DIR"
