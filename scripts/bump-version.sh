#!/usr/bin/env bash
# bump-version.sh — rewrite app/version.py's VERSION to today's date (CalVer:
# year.month.day). An optional suffix is appended with a dash, e.g.
#   bump-version.sh          -> VERSION = "2026.08.06"
#   bump-version.sh rc1      -> VERSION = "2026.08.06-rc1"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION_FILE="${GSC_VERSION_FILE:-$SCRIPT_DIR/../app/version.py}"

[ -f "$VERSION_FILE" ] || {
  echo "ERROR: version file not found: $VERSION_FILE" >&2
  exit 1
}

suffix="${1:-}"
new_version="$(date -u +%Y.%m.%d)"
[ -n "$suffix" ] && new_version="${new_version}-${suffix}"

sed -i -E "s/^VERSION = \"[^\"]*\"/VERSION = \"${new_version}\"/" "$VERSION_FILE"

grep -q "^VERSION = \"${new_version}\"$" "$VERSION_FILE" || {
  echo "ERROR: failed to rewrite VERSION in $VERSION_FILE" >&2
  exit 1
}
echo "VERSION = ${new_version} (${VERSION_FILE})"
