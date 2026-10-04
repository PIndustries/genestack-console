#!/usr/bin/env bash
# bump-version.sh — rewrite app/version.py's VERSION to year.month.day.build.
# An optional suffix is appended with a dash, e.g.
#   bump-version.sh          -> VERSION = "2026.10.04.1"
#   bump-version.sh 2        -> VERSION = "2026.10.04.2"
#   bump-version.sh rc1      -> VERSION = "2026.10.04.1-rc1"
#   bump-version.sh 2 rc1    -> VERSION = "2026.10.04.2-rc1"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION_FILE="${GSC_VERSION_FILE:-$SCRIPT_DIR/../app/version.py}"

[ -f "$VERSION_FILE" ] || {
  echo "ERROR: version file not found: $VERSION_FILE" >&2
  exit 1
}

build="${1:-1}"
suffix="${2:-}"
if ! [[ "$build" =~ ^[0-9]+$ ]]; then
  suffix="$build"
  build=1
fi
new_version="$(date -u +%Y.%m.%d).${build}"
[ -n "$suffix" ] && new_version="${new_version}-${suffix}"

sed -i -E "s/^VERSION = \"[^\"]*\"/VERSION = \"${new_version}\"/" "$VERSION_FILE"

grep -q "^VERSION = \"${new_version}\"$" "$VERSION_FILE" || {
  echo "ERROR: failed to rewrite VERSION in $VERSION_FILE" >&2
  exit 1
}
echo "VERSION = ${new_version} (${VERSION_FILE})"
