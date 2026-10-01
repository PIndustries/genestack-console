#!/usr/bin/env bash
# vacuum-console.sh — SQLite VACUUM (or dry-run freelist/page stats) for the
# console database. Catalog op console.vacuum calls this script. Do not add
# a second in-process vacuum path.
#
# Usage:
#   vacuum-console.sh [--config <path>] [--dry-run]
#
# SQLite (default layout): dry-run prints page_count / freelist_count /
# estimated free bytes; live runs PRAGMA wal_checkpoint(TRUNCATE) then VACUUM.
# Always prefer a retention sweep before vacuum, and console.backup after.
#
# Postgres (database_url: postgresql+psycopg://...): not handled here.
# This script exits non-zero.
set -euo pipefail

CONFIG_FILE=""
DRY_RUN=0

usage() {
  cat <<USAGE
Usage: vacuum-console.sh [--config <path>] [--dry-run]

SQLite: report freelist/page stats (--dry-run) or wal_checkpoint+VACUUM (live).
Postgres: unsupported in this script.
USAGE
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG_FILE="$2"; shift 2 ;;
    --config=*) CONFIG_FILE="${1#*=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h | --help) usage; exit 0 ;;
    -*) die "unknown option: $1 (see --help)" ;;
    *) die "unexpected argument: $1 (see --help)" ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -n "$CONFIG_FILE" ] || CONFIG_FILE="$SCRIPT_DIR/../config.yaml"
[ -f "$CONFIG_FILE" ] || die "config file not found: $CONFIG_FILE (pass --config)"

CONSOLE_DIR="$(cd "$(dirname "$CONFIG_FILE")" && pwd)"

DB_URL="$(sed -n 's/^[[:space:]]*database_url:[[:space:]]*//p' "$CONFIG_FILE" | head -n1 | sed 's/[[:space:]]*#.*$//')"
DATA_DIR_REL="$(sed -n 's/^[[:space:]]*data_dir:[[:space:]]*//p' "$CONFIG_FILE" | head -n1 | sed 's/[[:space:]]*#.*$//')"
[ -n "$DATA_DIR_REL" ] || DATA_DIR_REL="./data"
case "$DATA_DIR_REL" in
  /*) DATA_DIR="$DATA_DIR_REL" ;;
  *) DATA_DIR="$CONSOLE_DIR/$DATA_DIR_REL" ;;
esac

info "config: $CONFIG_FILE"
info "dry_run: $DRY_RUN"

if [ -n "$DB_URL" ]; then
  case "$DB_URL" in
    postgresql*)
      die "Postgres vacuum is not supported by this script (database_url is Postgres)"
      ;;
    sqlite*)
      # sqlite:///absolute or sqlite:///./relative — strip scheme for the file path
      DB_FILE="$(python3 - "$DB_URL" "$CONSOLE_DIR" <<'PY'
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

url = sys.argv[1]
base = Path(sys.argv[2])
u = urlparse(url)
path = unquote(u.path or "")
if path.startswith("//"):
    # sqlite:////abs -> path //abs; normalize
    path = path[1:]
p = Path(path)
if not p.is_absolute():
    p = (base / p).resolve()
print(p)
PY
)"
      ;;
    *) die "unsupported database_url for vacuum: $DB_URL" ;;
  esac
else
  DB_FILE="$DATA_DIR/console.db"
fi

[ -f "$DB_FILE" ] || die "no console.db at $DB_FILE"

info "database: $DB_FILE"

python3 - "$DB_FILE" "$DRY_RUN" <<'PY'
import sqlite3
import sys

db_path = sys.argv[1]
dry_run = sys.argv[2] == "1"

conn = sqlite3.connect(db_path)
try:
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    free_bytes = freelist * page_size
    print(
        f"==> stats: page_count={page_count} freelist_count={freelist} "
        f"page_size={page_size} free_bytes≈{free_bytes}"
    )
    if dry_run:
        print("==> dry-run: skipping wal_checkpoint + VACUUM")
        raise SystemExit(0)
    print("==> PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print("==> VACUUM")
    conn.execute("VACUUM")
    page_count2 = conn.execute("PRAGMA page_count").fetchone()[0]
    freelist2 = conn.execute("PRAGMA freelist_count").fetchone()[0]
    print(
        f"==> after: page_count={page_count2} freelist_count={freelist2}"
    )
finally:
    conn.close()
PY

info "vacuum complete (dry_run=$DRY_RUN)"
